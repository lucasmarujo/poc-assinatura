# PoC — `validar_assinatura` sem AWS Textract

Refatoração da tool `validar_assinatura` (hoje em
[`src/motor_ia/tools/assinatura.py`](../../src/motor_ia/tools/assinatura.py)) para
detectar assinatura **sem chamar o Textract**, cujo custo por página
(US$ 3,50/1.000 páginas na feature `SIGNATURES`) é insustentável no volume de
produção da análise documental de fornecedores e trabalhadores.

Nada fora desta pasta foi alterado — a tool em produção segue intacta.

## Como funciona

Cascata de dois níveis; o barato decide primeiro.

**Nível 0 — Python puro, custo zero** ([`nivel0.py`](nivel0.py))

| Sinal | O que faz | Origem |
|---|---|---|
| Campos `/Sig` no AcroForm | assinatura criptográfica viva no PDF (PAdES/ICP-Brasil) | reusa `motor_ia.extractors.signatures.find_embedded_pdf_signatures` |
| Carimbo na camada de texto | "Assinado digitalmente por…", ICP-Brasil, `DN: C=` | reusa `motor_ia.extractors.signatures.find_digital_signatures` |
| Rótulo de assinatura | "Assinatura do empregado", "Recebi…" — indício de *campo*, não de assinatura; **não** entra no veredito | novo |
| Densidade de tinta | fração de pixels escuros por página (raster 72 DPI, grayscale); abaixo do limiar a página é **em branco** e não vai ao Nível 1 | novo |

**Nível 1 — CPU local, custo zero por página** ([`nivel1.py`](nivel1.py))

`tech4humans/yolov8s-signature-detector` exportado em ONNX, rodando em ONNX
Runtime (`CPUExecutionProvider`). Pré/pós-processamento conforme o `detector.py`
oficial do Space `tech4humans/signature-detection`: entrada 640×640 RGB
normalizada (NCHW), saída `(1, 4+classes, 8400)` com caixas `cx,cy,w,h`, filtro
por confiança + NMS (numpy puro, sem OpenCV).

A saída de cada rubrica usa **o mesmo contrato do Textract**
(`{page, confidence, bounding_box{left,top,width,height}, quem_assinou}`), então
o bloco `<assinaturas_detectadas>` entregue ao LLM não muda — a formatação segue
sendo a `format_signature_context` já existente. A única perda em relação ao
Textract é `quem_assinou`, que exigia OCR do texto ao lado da rubrica.

```
Nível 0 (sempre, ~20 ms/doc)
  ├─ achou /Sig ou carimbo digital → devolve (Nível 1 não roda)
  └─ não achou → Nível 1 nas páginas com tinta
```

`escalonar=False` desliga a cascata e roda os dois níveis sempre — é o modo do
script de teste, para poder comparar N0 × N1 no mesmo documento.

## Arquivos

| Arquivo | Papel |
|---|---|
| [`nivel0.py`](nivel0.py) | sinais de custo zero + triagem de páginas |
| [`nivel1.py`](nivel1.py) | detector YOLOv8s ONNX (sessão singleton, NMS, varredura do PDF) |
| [`modelo.py`](modelo.py) | resolução/download dos pesos `.onnx` |
| [`deteccao.py`](deteccao.py) | cascata, veredito consolidado e formatação para o LLM |
| [`assinatura.py`](assinatura.py) | a tool `validar_assinatura` refatorada (mesmo contrato) |
| [`testar_documentos.py`](testar_documentos.py) | roda o lote de PDFs e gera `resultados/RESULTADOS.md` + `.json` |
| [`tests/`](tests/) | 40 testes (sem rede, sem AWS, sem os pesos — sessão ONNX injetada) |

## Pré-requisitos

```bash
# 1. dependência extra desta PoC (não foi adicionada ao pyproject do motor-ia).
#    ATENÇÃO: `uv sync` / `make dev` remove esta dep, porque ela não está
#    declarada no pyproject — reinstale depois de rodar qualquer um dos dois.
motor-ia$ uv pip install onnxruntime

# 2. pesos do modelo (~44 MB). O repo no HF é gated: é preciso aceitar os termos
#    AGPL-3.0 em https://huggingface.co/tech4humans/yolov8s-signature-detector
#    e usar um token com leitura de repos gated públicos.
motor-ia$ HF_TOKEN=hf_... ./.venv/bin/python poc-assinatura/tool_validar_assinatura/modelo.py
# ou, se você já tem o arquivo local:
motor-ia$ export POC_ASSINATURA_MODELO_PATH=/caminho/para/yolov8s.onnx
```

Em produção o download em runtime **não** deve existir: o `.onnx` entra na
imagem (ou vem de bucket interno) e `POC_ASSINATURA_MODELO_PATH` aponta pra ele.

## Rodando

```bash
# lote completo (os 17 PDFs de ../doc trabalhadores), N0 + N1
motor-ia$ ./.venv/bin/python poc-assinatura/tool_validar_assinatura/testar_documentos.py

# só o Nível 0 (não precisa dos pesos)
motor-ia$ ./.venv/bin/python poc-assinatura/tool_validar_assinatura/testar_documentos.py --sem-nivel1

# calibração
motor-ia$ ./.venv/bin/python poc-assinatura/tool_validar_assinatura/testar_documentos.py \
    --conf 0.35 --iou 0.5 --dpi 200 --densidade-minima 0.001

# testes
motor-ia$ ./.venv/bin/python -m pytest poc-assinatura/tool_validar_assinatura/tests -q --no-cov
```

Saída: [`resultados/RESULTADOS.md`](resultados/) (tabela com o total de
assinaturas por nível em cada documento, resumo e detalhe por página) e
`resultados/RESULTADOS.json` (todas as detecções, para diffar entre calibrações).

## Resultado nos 17 documentos de `../doc trabalhadores`

| | |
|---|---|
| Documentos com assinatura detectada | **17/17** (Nível 0: 3 — Nível 1: 17) |
| Assinaturas detectadas | 3 no Nível 0 (carimbo digital) + 36 no Nível 1 (rubricas) |
| Tempo | 5,2 s no lote — **181 ms/página**, dos quais 2,0 s de inferência ONNX |
| Custo | **US$ 0,00**. O mesmo lote no Textract `SIGNATURES`: US$ 0,1015 (29 págs) |

14 dos 17 documentos são digitalizações sem camada de texto — o Nível 0 não tem
como resolvê-los, e são exatamente os que o Nível 1 pega. Os 3 que o Nível 0
resolve sozinho (`Daniel de Melo`, `Raimundo de Oliveira`, `Vagner de Assunção`)
nem precisariam de inferência na cascata normal.

Duas decisões saíram da calibração e estão comentadas no código:

* **Reamostragem LANCZOS** em vez do BILINEAR do Space oficial. A página A4 é
  rasterizada por nós em ~1240×1754 e comprimida para 640×640; o bilinear apaga
  rubrica de caneta fina (`Douglas Martins` ia de 2 → 0 detecções).
* **Confiança mínima 0,15** em vez de 0,25. Com 0,25 o lote fecha em 15/17
  documentos; com 0,15, em 17/17 — e nenhuma das detecções conferidas
  visualmente é falso-positivo: campos de assinatura vazios ("Assinatura do
  responsável quando menor", "Testemunha") seguem sem detecção.

Limite conhecido: a recall **por rubrica** não é 100% — em página muito branca
com traço fino, uma das duas assinaturas pode escapar (visto em
`Douglas Martins.pdf` p5). Para o veredito da tool ("tem assinatura?") isso não
muda a resposta, mas se algum requisito passar a exigir *contagem* de
signatários, vale avaliar inferência em recortes da página (tiling).

## Variáveis de ambiente

| Variável | Efeito |
|---|---|
| `POC_ASSINATURA_MODELO_PATH` | caminho dos pesos `.onnx` (evita o download) |
| `POC_ASSINATURA_ONNX_THREADS` | limita as threads da inferência (worker compartilhado) |
| `HF_TOKEN` / `HUGGINGFACE_HUB_TOKEN` | token usado no download dos pesos |

## Se a PoC for promovida

1. `onnxruntime` entra em `pyproject.toml` (imagem CPU-only, ~18 MB de wheel).
2. `nivel0.py`/`nivel1.py` viram `src/motor_ia/extractors/` (ao lado de
   `signatures.py`), `deteccao.py` substitui o `detect_all_signatures` e a tool
   volta para `src/motor_ia/tools/assinatura.py` — o contrato já é o mesmo.
3. Os pesos vão para a imagem/bucket; `modelo.py` fica só com a resolução local.
4. O parâmetro `incluir_visual` da tool passa a significar "rodar o Nível 1",
   sem custo — vale revisar o prompt que hoje instrui o LLM a economizá-lo.
