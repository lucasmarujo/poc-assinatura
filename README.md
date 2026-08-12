# Processamento em lote — detecção de assinatura

Plataforma para rodar a tool [`tool_validar_assinatura/`](tool_validar_assinatura/)
(cascata Nível 0 → Nível 1, sem AWS Textract) sobre a árvore inteira de
[`files/`](files/) e produzir os relatórios de auditoria.

```bash
# 1. dependências (uma vez)
poc-assinatura$ python -m venv .venv
poc-assinatura$ .venv/Scripts/python -m pip install -r requirements.txt

# 2. jogue os documentos em files/ (subpastas à vontade, qualquer profundidade)

# 3. rode
poc-assinatura$ .venv/Scripts/python processar.py
```

No Linux/macOS troque `.venv/Scripts/python` por `./.venv/bin/python`.

## O que sai em `resultados/`

| Arquivo | Conteúdo |
|---|---|
| `RESUMO.md` | contagens, ganho do fallback, desempenho, uso de hardware, erros por tipo, mais lentos |
| `RESUMO.json` | o mesmo, para consumo por outro sistema |
| `COMPLETO.md` | **uma linha por documento**: ✅/❌/⚠️, assinaturas no Nível 0, no Nível 1, páginas, tempo e observação |
| `resultados.jsonl` | uma linha JSON por documento — checkpoint e fonte dos relatórios |
| `logs/lote.jsonl` | eventos em JSONL: retries, falhas de documento, início e fim |

Legenda do `COMPLETO.md`:

| | Significado |
|---|---|
| ✅ | assinatura encontrada (Nível 0, Nível 1 ou os dois) |
| ❌ | processou até o fim e não encontrou assinatura |
| ⚠️ | não deu para concluir: erro no processamento, formato não suportado, ou Nível 1 indisponível (que **não** é o mesmo que "não tem assinatura") |
| 📦 | `.zip`: caixa, não documento. Foi extraído — o conteúdo tem linha própria. Fica fora das contagens de ✅/❌/⚠️ |

## Fallback: quando não acha nada, olha mais de perto

Documento que termina sem **nenhuma** assinatura (Nível 0 e Nível 1) é refeito no
mesmo Nível 1, com cada página analisada dividida em 9 ladrilhos sobrepostos e
rasterizada em 300 DPI.

Não é um nível novo, é escala. A A4 inteira comprimida para a entrada 640×640 do
modelo dá ~77 px por polegada do documento; um ladrilho de 1/3 do lado ocupa a
mesma entrada com ~179 px/pol — 2,3× mais resolução, que é o que resgata rubrica
de traço fino. Os ladrilhos se sobrepõem em 15% para que assinatura em cima de
uma divisa apareça inteira em pelo menos um deles; a duplicata sai no mesmo NMS
do pós-processamento.

Documento que já achou não paga nada: o custo cai só sobre os ❌. O `RESUMO.md`
traz quantos entraram no fallback, quantos foram resgatados e em quanto o lote
teria fechado sem ele. Para desligar, `--sem-fallback`.

## Formatos aceitos

| Entrada | Como é processada |
|---|---|
| `.pdf` | caminho nativo dos dois níveis |
| `.jpg .jpeg .png .tif .tiff .bmp .gif .webp` | aberta como documento de uma página; só o Nível 1 tem o que fazer |
| `.docx .docm` | texto do `word/document.xml` vai para o Nível 0; as imagens de `word/media/` viram páginas que o Nível 1 varre |
| `.pptx .pptm` | igual, com o texto de `ppt/slides/slide*.xml` e as imagens de `ppt/media/` |
| `.zip` | **extraído antes da varredura**, numa subpasta ao lado com o nome do arquivo (`lote.zip` → `lote/`). O que sai é processado como documento comum. Zip dentro de zip é expandido até 3 níveis |

Qualquer outra extensão entra no relatório como ⚠️ *formato não suportado* — de
propósito: nada some do lote em silêncio.

A extração de `.zip` é idempotente: pasta de destino já existente é respeitada,
então retomar o lote não re-extrai nem sobrescreve nada. Zip corrompido ou
absurdo (bomba) vira ⚠️ com o motivo, sem derrubar os outros.

**Limite conhecido do OOXML:** assinatura desenhada como vetor no próprio Word
(`w:ink`, EMF/WMF) não é imagem raster e não chega ao Nível 1. Assinatura
escaneada e colada — o caso real — chega. `.doc`, `.xls`, `.ppt` legados, `.odt`
e `.rtf` ficam como não suportados: o motor-ia os converte com LibreOffice
(`extractors/libreoffice.py`), que não está instalado nesta máquina. `.xlsx` é o
mesmo caminho do OOXML e é uma linha em `OOXML` (em `documentos.py`) se
aparecer necessidade.

Se um formato passar a ser suportado depois de uma execução, a retomada
reavalia sozinha os documentos que estavam como ⚠️ *formato não suportado* —
não é preciso apagar o checkpoint.

## Interrompeu? Rode de novo

O `resultados.jsonl` é gravado documento a documento, com `flush`. Ctrl+C, queda
de energia ou reinício: basta rodar `processar.py` de novo — o que já está no
checkpoint é pulado. Para recomeçar do zero, `--reprocessar`.

Para só regerar os relatórios a partir do checkpoint (mudou a formatação, quer
conferir no meio do lote): `--apenas-relatorio`.

## Opções

| Opção | Default | Para quê |
|---|---|---|
| `--files` / `--saida` | `files/` / `resultados/` | outras pastas |
| `--max-paginas` | 30 | teto de páginas por documento no Nível 1 (`0` = sem teto) |
| `--escalonar` | desligado | para no Nível 0 quando ele já achou. Mais rápido, mas o relatório deixa de comparar N0 × N1 |
| `--sem-nivel1` | desligado | só o Nível 0 (não precisa dos pesos ONNX) |
| `--sem-fallback` / `--dpi-fallback` | ligado / 300 | o segundo passe em ladrilhos (ver abaixo) |
| `--conf` `--iou` `--dpi` `--densidade-minima` | 0.15 / 0.5 / 150 / 0.0005 | calibração do detector (ver o README da tool) |
| `--tentativas` | 2 | retentativas por documento antes de virar ⚠️ |
| `--reprocessar` / `--apenas-relatorio` | — | ver acima |

## Um documento por vez, de propósito

Não há pool de processos, e não há `--workers`. Houve, e foi removido: cada
worker carrega a própria sessão ONNX (~0,6 GB de *commit*), e o lote morria com
`paging file is too small` muito antes de saturar a CPU — trazendo junto pool
quebrado, watchdog, worker órfão e diagnóstico de memória, tudo para acelerar
algo que já roda numa janela aceitável.

Medido nesta máquina, sequencial: **~250 ms/documento**, memória plana em
**393 MB de média e 478 MB de pico** ao longo de 424 documentos reais. O ONNX
Runtime usa todos os núcleos dentro do único processo, então a CPU continua
sendo aproveitada (~60% durante a medição).

Se algum dia precisar dividir: rode duas vezes apontando para subpastas
diferentes, com `--files` e `--saida` próprios. Sem estado compartilhado, sem
risco.

## Como o lote se defende

* **Erro é por documento, não por lote.** Documento corrompido, protegido por
  senha ou vazio vira ⚠️ com o motivo na linha dele; o lote continua.
* **Retry com política.** Falha transitória volta para o fim da fila até
  `--tentativas`. Formato não suportado e arquivo corrompido não são retentados
  — são determinísticos.
* **Memória constante.** Uma sessão ONNX só, e a arena de memória do ONNX
  Runtime desligada (`enable_cpu_mem_arena = False`): ela reserva em blocos
  grandes e não devolve ao SO.
* **Zip bomb.** Pacote OOXML e `.zip` são lidos com teto de tamanho
  descomprimido e de número de arquivos antes de extrair.
* **Uma execução por vez.** `resultados/.lock` recusa um segundo lote sobre o
  mesmo checkpoint — dois lotes simultâneos duplicam registros. Trava deixada
  por um processo que morreu é assumida sozinha (o critério é o processo estar
  vivo, não o arquivo existir).
* **`resultados/.atual`** guarda o documento em processamento. Se o lote parecer
  parado, é esse arquivo que diz em qual documento olhar.

## Observabilidade

`RESUMO.md` traz tempo médio/mediana/p95 por documento e por página, fração do
tempo em inferência ONNX, e o uso médio e de pico de CPU e RAM durante o lote
(amostrado a cada 2 s). O `logs/lote.jsonl` guarda o evento a evento: retries e
falhas de documento.

## Pesos do modelo

O Nível 1 usa `tool_validar_assinatura/models/yolov8s.onnx` (~44 MB), já
presente. Para apontar outro arquivo: `--modelo /caminho.onnx` ou a variável
`POC_ASSINATURA_MODELO_PATH`. Sem os pesos, `processar.py` falha na largada com
a instrução — em vez de rodar o lote inteiro e devolver "0 assinaturas no Nível
1" em todo documento.

## Estrutura

| Pasta | Papel |
|---|---|
| [`processar.py`](processar.py) | entrada da linha de comando |
| [`processamento/`](processamento/) | descoberta e adaptação de formato, execução do lote, amostragem de hardware, relatórios |
| [`tool_validar_assinatura/`](tool_validar_assinatura/) | a tool de detecção (Nível 0 + Nível 1) |
| [`compat/`](compat/) | recorte das funções do `motor_ia` que a tool importa — existe só porque esta cópia roda fora do repositório do motor-ia. Com o pacote real instalado, é ele que vence |
| [`tests/`](tests/) | testes da plataforma (`.venv/Scripts/python -m pytest tests -q`) |
