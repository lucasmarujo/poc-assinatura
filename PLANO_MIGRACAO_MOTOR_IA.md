# Plano de execução — substituir a `validar_assinatura` do motor-ia pela tool da PoC

Alvo: `C:\PROJETOS\greenlegis-hub\greenia\apis\motor-ia`
Origem: `poc-assinatura/tool_validar_assinatura/` (Nível 0 + Nível 1 + fallback de ladrilhos)

Objetivo: **deletar** a detecção baseada em AWS Textract `SIGNATURES` e colocar no lugar
a cascata local da PoC, religando a tool nas pipelines de **Fornecedores** e
**Trabalhadores**, com validação por smoke local antes de qualquer deploy.

---

## 0. Estado atual — o que existe hoje dos dois lados

### 0.1 motor-ia: a tool está morta, mas o encanamento está inteiro

A tool não foi removida: foi **desativada em 4 camadas**, com o wiring de consumo
preservado. Isso é a melhor notícia deste plano — a maior parte do trabalho é
descomentar e trocar o motor de detecção por baixo.

| Camada | Arquivo | Como está desativada |
|---|---|---|
| Detecção | `src/motor_ia/extractors/signatures.py` | viva, mas `detect_all_signatures` chama o Textract |
| Tool | `src/motor_ia/tools/assinatura.py` | 3× `raise Exception("Desativado.")` (linhas 86, 147, 178) |
| Registry | `src/motor_ia/tools/__init__.py` | import e instanciação comentados (linhas 17, 102) |
| Catálogo (front) | `src/motor_ia/tools/catalog.py` | entrada comentada (linhas 47–54) |
| Pipeline | `src/motor_ia/analyzers/fornecedores/pipeline.py` | `tool` fixado em `None` (linha ~1367); bloco de execução comentado (linhas ~1428–1461); memo comentado (linha ~837) |
| Runners | `.../fornecedores/runner.py`, `.../trabalhadores/runner.py` | `assinatura_tool=...` comentado (linhas ~368 e ~251) |
| Prompts | `.../fornecedores/prompts.py`, `.../trabalhadores/prompts.py` | seção `# Ferramentas` comentada (linhas 49–68 e 37–56) |

**O que continua vivo e não precisa ser reescrito** (é o que recebe o resultado da detecção):

- `pipeline.py:284` — `assinatura_por_doc: dict[str, dict[str, Any]]`
- `pipeline.py:566` / `:591` — aplicação dos campos por documento
- `pipeline.py:2029-2031` — `_apply_signature_fields` (`tem_assinatura`, `assinatura_signatarios`, `assinatura_fontes`)
- `pipeline.py:384` — `reconcile_assinatura(output, assinatura_result)`
- `reconciliation.py:947` — `reconcile_assinatura` (PDA-774: promove critério de assinatura a Atendido quando o sistema detectou e o LLM hesitou)
- `schemas.py:375-377` — os três campos no `DocumentoExtraido`
- `_run_analyser` / `_call_analyser_once` — já propagam `assinatura` de volta em todos os retornos

Ou seja: **o contrato de saída da detecção (`dict` com `tem_assinatura`/`fontes`/`signatarios`/
`visual`/`digital`/`embedded`/`visual_error`) já é consumido ponta a ponta.** A PoC foi
construída para devolver exatamente esse dicionário (`ResultadoDeteccao.para_dict()`).

### 0.2 PoC: o que foi construído e validado

| Nível | Arquivo | O que faz | Custo |
|---|---|---|---|
| **Nível 0** | `nivel0.py` | campos `/Sig` no AcroForm; carimbo digital na camada de texto; rótulo de campo de assinatura; densidade de tinta (descarta página em branco) | zero (PyMuPDF + numpy) |
| **Nível 1** | `nivel1.py` | `tech4humans/yolov8s-signature-detector` em ONNX Runtime CPU, 640×640, NMS numpy puro | zero por página |
| **Fallback** | `nivel1.py` (`_detectar_ladrilhos`) | documento que terminou sem **nenhuma** detecção é refeito em 3×3 ladrilhos sobrepostos a 300 DPI — mesma rede, 2,3× de resolução efetiva | só sobre os ❌ |
| Cascata | `deteccao.py` | N0 decide primeiro; N1 só nas páginas com tinta; converte para o dict legado | — |
| Tool | `assinatura.py` | mesmo `name`, mesmos `Args`, mesmo texto de retorno da tool de produção | — |

O Nível 0 reaproveita **verbatim** `find_digital_signatures` e `find_embedded_pdf_signatures`
de `motor_ia.extractors.signatures`, e `deteccao.py` formata com
`format_signature_context` do mesmo módulo. `poc-assinatura/compat/` é só um recorte
dessas funções para a PoC rodar fora do repositório — **dentro do motor-ia esse recorte
deixa de existir e os imports resolvem sozinhos, sem uma linha de mudança.**

Uma exceção, e ela vale para o motor-ia também: `find_embedded_pdf_signatures` conta a
**presença do widget** `/Sig`, então um campo "assine aqui" nunca preenchido entra como
assinatura. `nivel0.py` filtra o resultado dela com `campos_assinados()`, exigindo `/V`
com `/ByteRange` e `/Contents` — o critério do formato. O `/V /Name` **não** entra no
critério: é opcional e a maioria dos PDFs do ICP-Brasil não o preenche, então usá-lo
descartaria assinatura válida. O filtro fica no `nivel0.py` e viaja com ele na migração —
§4.2 explica por que ele não vai para dentro de `find_embedded_pdf_signatures` agora e
quando essa limpeza passa a ser segura.

### 0.3 O que a PoC validou, e o que este plano faz com isso

A PoC foi medida com rótulo humano cego sobre um lote real, e o resultado é o que
autorizou a substituição — **os números estão em `poc-assinatura/auditoria/CARD.md` e
`VALIDACAO.md` e não se repetem aqui.** Este plano é o *como implementar*; acurácia e
custo pertencem à decisão, que já foi tomada.

O que atravessa dessa validação para cá é só o que muda uma linha de código: as
constantes de calibração (`CONFIANCA_MINIMA=0.15`, LANCZOS, 3×3 ladrilhos a 300 DPI,
`max_paginas`) e as medições de **desempenho e memória** da §5, que são o que dimensiona
slots, threads e timeouts.

---

## 1. Inventário — o que sai e o que entra

### 1.1 Código REMOVIDO do motor-ia

| Arquivo | O que remover |
|---|---|
| `src/motor_ia/extractors/textract.py` | **linhas 402–783** (bloco inteiro `signature detection` + `fallback por região`): `_bounding_box`, `_nearest_lines_below`, `parse_signature_blocks`, `_signature_result`, `_pdf_page_count`, `analyze_signatures_sync`, `analyze_signatures_async`, `detect_signatures`, `_render_region_pngs`, `detect_signatures_in_regions`, e as constantes `_SIGNATURE_SIGNER_MAX_VERTICAL_GAP` (l. 57) / `_SIGNATURE_SIGNER_MAX_LINES` (l. 60) |
| `src/motor_ia/extractors/signatures.py` | `detect_all_signatures`, `_expects_signature`, `_SIGNATURE_FIELD_CUES`, `_pdf_text_layer`, `_consolidar_signatarios`, o import de `motor_ia.extractors.textract`, o import de `ExtractionError` e o `asyncio` |
| `src/motor_ia/tools/assinatura.py` | conteúdo inteiro (substituído — ver §4.6) |
| `src/motor_ia/analyzers/fornecedores/pipeline.py` | linhas ~455–462 (`assinatura_cost` / `UsageStep(step="assinatura", model="textract")`) — a detecção nova não tem `cost_usd` |
| `tests/extractors/test_textract.py` | os testes de assinatura: `test_parse_signature_blocks_*` (l. 381–435) e `test_analyze_signatures_sync_*` (l. 438–488) + os imports correspondentes (l. 13, 17) |
| `tests/extractors/test_signatures.py` | os testes de `detect_all_signatures` (l. 72–215) — substituídos pelos da PoC |

Verificação prévia obrigatória (todas devem voltar vazias, exceto as ocorrências dentro dos próprios arquivos acima):

```bash
motor-ia$ grep -rn "detect_signatures\|parse_signature_blocks\|analyze_signatures\|detect_all_signatures" --include="*.py" src tests
```

`TEXTRACT_SYNC_MAX_BYTES` e `fits_sync_api` **ficam** — são usados por `extractors/hybrid.py`
(OCR), que não tem relação com assinatura.

### 1.2 Código INSERIDO no motor-ia

| Origem (PoC) | Destino (motor-ia) | Mudanças |
|---|---|---|
| `tool_validar_assinatura/nivel0.py` | `src/motor_ia/extractors/assinatura/nivel0.py` | nenhuma (o import de `motor_ia.extractors.signatures` já é o real) |
| `tool_validar_assinatura/nivel1.py` | `src/motor_ia/extractors/assinatura/nivel1.py` | `from modelo import resolver_modelo` → `from motor_ia.extractors.assinatura.modelo import resolver_modelo`; threads via Settings |
| `tool_validar_assinatura/modelo.py` | `src/motor_ia/extractors/assinatura/modelo.py` | path via Settings; download desligado no caminho de runtime |
| `tool_validar_assinatura/deteccao.py` | `src/motor_ia/extractors/assinatura/deteccao.py` | `from nivel0/nivel1 import ...` → imports absolutos do pacote |
| — | `src/motor_ia/extractors/assinatura/__init__.py` | novo, reexporta `detectar_assinaturas_async` e `formatar_contexto` |
| — | `src/motor_ia/extractors/assinatura/gate.py` | **novo** — portão de slots por container (§4.4-bis). Único código sem origem na PoC |
| — | `examples/extractors/carga_assinatura.py` | **novo** — teste de carga com 20 processos (§5.8.4) |
| `tool_validar_assinatura/assinatura.py` | `src/motor_ia/tools/assinatura.py` | `from deteccao import ...` → import absoluto |
| `tool_validar_assinatura/tests/*` | `tests/extractors/assinatura/` e `tests/tools/test_assinatura.py` | ajuste de imports; remover o `_preparar_imports()` do `conftest.py` |
| `models/yolov8s.onnx` (~44 MB) | `src/motor_ia/extractors/assinatura/models/yolov8s.onnx` | **commitado no repositório** (decisão do time) |

**Por que um subpacote `extractors/assinatura/` e não arquivos soltos:** os quatro
módulos são coesos e já existe precedente de subpacote em `extractors/cache/`. Mantém a
estrutura da PoC 1:1, então o diff dos arquivos movidos é só a linha de import — nada de
reescrita. `extractors/signatures.py` **continua onde está** (enxugado), porque é dele que
o Nível 0 importa os dois sinais de custo zero e a formatação para o LLM.

---

## 2. Decisões que precisam de resposta antes da execução

| # | Decisão | Recomendação |
|---|---|---|
| D1 | ~~Como os pesos `.onnx` (44 MB) chegam na imagem?~~ | **DECIDIDO: commitados no repositório**, dentro do pacote (`src/motor_ia/extractors/assinatura/models/`). Simplifica a Etapa 2 a um `git add` + 3 verificações. |
| D2 | `incluir_visual` (arg da tool) hoje é descrito ao LLM como "economia de custo". Sem Textract não há custo. | Redescrever como "detecção visual de rubrica (CPU local, sem custo)" e manter `default=True`. O prompt deixa de instruir economia. |
| D3 | Adaptação de formato (`.docx`, `.pptx`, imagem, `.zip`) que a PoC tem em `processamento/documentos.py` | **Fora do escopo desta migração.** A tool antiga também só tratava PDF. Fica como follow-up. |
| D4 | `max_paginas` (teto de páginas no Nível 1) — a PoC usa 30 no lote | Expor como setting com default **30**. Sem teto, um PDF de 2.000 páginas monopoliza o worker. |
| D5 | `pricing.py` mantém o modo `analyze_signatures` (US$ 0,0035/página), que fica sem chamador | Manter (é tabela de preço, 2 linhas, com teste). Remover só se houver limpeza dedicada. |

---

## 3. Ordem de execução (resumo)

```
Etapa 1  Dependências (onnxruntime, numpy) + mypy overrides
Etapa 2  Pesos do modelo: commit no pacote + verificar empacotamento
Etapa 3  Settings (ExtractorSettings): path, threads, max_paginas
Etapa 4  Mover o núcleo: extractors/assinatura/ (N0, N1, modelo, cascata+fallback)
         + gate.py e worker.py — portão de slots e inferência em subprocesso (§4.4-bis
         e §4.4-ter). São os dois únicos arquivos sem origem na PoC, e é o que impede
         20 jobs × 1 sessão ONNX de estourar a memória do container.
Etapa 5  Enxugar extractors/signatures.py
Etapa 6  Deletar o Textract SIGNATURES
Etapa 7  Reescrever tools/assinatura.py
Etapa 8  Religar registry + catálogo público
Etapa 9  Religar a pipeline (o coração — vale para os dois agentes)
Etapa 10 Religar os runners (Fornecedores + Trabalhadores)
Etapa 11 Religar os prompts
Etapa 12 Testes (migrar os 52 da PoC + corrigir os de regressão)
Etapa 13 Smoke local (validação de ponta a ponta)
Etapa 14 Deploy
```

**Antes do rollout, ler a §5** (estabilidade / disponibilidade / escalabilidade): é lá que
estão a conta de memória do worker, a configuração obrigatória de threads e a armadilha do
warmup. Não é apêndice — muda decisão de infraestrutura.

Etapas 1–8 são preparação e podem ser um PR só. Etapas 9–11 são a religação e não fazem
sentido separadas. Etapas 12–13 fecham.

---

## 4. Etapas detalhadas

### Etapa 1 — Dependências

**`pyproject.toml`**, bloco `dependencies` (após `"pillow>=10.4.0"`, l. 36):

```toml
  # Detector visual de assinatura (Nível 1): YOLOv8s em ONNX Runtime CPU.
  # Substitui a feature SIGNATURES do Textract, cobrada por página.
  "onnxruntime>=1.20.0",
  "numpy>=1.26.0",
```

`numpy` é usado diretamente por `nivel0.densidade_tinta` e por todo o pré/pós-processamento
do `nivel1` — declarar explicitamente, não confiar em transitividade.

**`pyproject.toml`**, override do mypy (l. 139–162), acrescentar à lista:

```toml
  "onnxruntime",
  "onnxruntime.*",
```

Depois: `uv lock && uv sync --extra dev`.

> A wheel CPU-only do `onnxruntime` tem ~18 MB. Confirmar que o build multi-stage
> (`python:3.13-slim-bookworm`) resolve — é o alvo oficial da wheel `manylinux`.

---

### Etapa 2 — Pesos do modelo (D1: commitados no repositório)

Com o `.onnx` versionado dentro do pacote, **não há passo de build, de CI, nem de
infraestrutura**. O arquivo viaja junto do código, e a resolução em runtime é um
`Path(__file__).parent / "models" / "yolov8s.onnx"` — sem env obrigatória, sem rede,
igual em dev, CI, imagem e worker.

**2.1 Commitar o arquivo:**

```bash
motor-ia$ mkdir -p src/motor_ia/extractors/assinatura/models
motor-ia$ cp <poc>/tool_validar_assinatura/models/yolov8s.onnx \
             src/motor_ia/extractors/assinatura/models/
motor-ia$ git add -f src/motor_ia/extractors/assinatura/models/yolov8s.onnx
```

**2.2 Verificações — as três já passam hoje, mas confirmar antes de fechar o PR:**

| O quê | Estado | Como confirmar |
|---|---|---|
| `.gitignore` do motor-ia não exclui `*.onnx` | ✅ não exclui | `git check-ignore -v src/motor_ia/extractors/assinatura/models/yolov8s.onnx` deve sair vazio |
| `.dockerignore` não exclui o arquivo do contexto | ✅ não exclui | conferir também o `.dockerignore` da **raiz do contexto de build** (`greenlegis-hub/`), não só o do motor-ia — o `Dockerfile` copia de `greenia/apis/motor-ia/src` |
| hatchling empacota o `.onnx` no wheel | ✅ inclui por padrão tudo sob `packages = ["src/motor_ia"]` | após `uv build`, `unzip -l dist/*.whl \| grep onnx` |

A terceira é a que importa: se o wheel não levar o arquivo, o container roda a partir de
`/app/.venv/.../site-packages/motor_ia/` e não acha os pesos. Vale rodar uma vez.

**2.3 `Dockerfile`: nenhuma mudança.** O `COPY greenia/apis/motor-ia/src ./src` e o
`uv pip install --no-deps .` já levam o arquivo — nas duas cópias (source e wheel).

**2.4 Desenvolvimento local: nenhuma configuração.** Clonou, tem os pesos.

> **Uma consequência operacional a registrar:** git guarda blob binário para sempre.
> Trocar os pesos no futuro (recalibração, modelo novo) adiciona outros ~44 MB
> permanentes ao histórico — não substitui. Se isso virar rotina, aí sim vale mover para
> um bucket. Para uma troca eventual, o custo é aceitável e a simplicidade compensa.

---

### Etapa 3 — Settings

**`src/motor_ia/config/settings.py`**, classe `ExtractorSettings` (l. 389), ao final dos campos
(após `docx_streaming_threshold_mb`):

```python
    # ---------- Detecção de assinatura (Nível 1: YOLOv8s ONNX, CPU local) ----
    #
    # Os defaults abaixo são calibrados para o alvo real: Fargate 2 vCPU / 4 GB
    # com até 20 processos de job simultâneos, todos de análise documental. Ver
    # §5 do plano — não são valores de conveniência, são o teto de recursos.
    #
    # Override do caminho dos pesos `.onnx`. Vazio (o normal) = usa o arquivo
    # versionado dentro do pacote (`extractors/assinatura/models/`).
    signature_model_path: str = Field(
        default="", validation_alias="ExtractorConfiguration__SignatureModelPath"
    )
    # Quantos subprocessos de inferência este container permite ao mesmo tempo.
    # É o teto de CPU e de memória da feature inteira: o consumo é
    # O(max_concurrent), NÃO O(worker_count). Com 20 jobs no container e este
    # valor em 2, o pico é ~300 MB transitórios, não ~3 GB residentes.
    # 2 casa com 2 vCPU: mais subprocessos não aumentam vazão, só disputa.
    signature_max_concurrent: int = Field(
        default=2, ge=1, le=16, validation_alias="ExtractorConfiguration__SignatureMaxConcurrent"
    )
    # Threads da inferência ONNX por subprocesso. 1 é o certo: com 2 vCPU e
    # `max_concurrent=2`, dois inferindo com 1 thread já saturam a caixa — e o
    # container ainda roda extração, OCR e os outros jobs.
    signature_onnx_threads: int = Field(
        default=1, ge=0, le=64, validation_alias="ExtractorConfiguration__SignatureOnnxThreads"
    )
    # Prazo para conseguir um slot. Generoso de propósito: o job tem 30 min de
    # timeout (`workers/queues.py`), então ESPERAR é quase sempre melhor que
    # degradar para "assinatura não verificada". Estourou = degrada e segue.
    signature_slot_timeout_s: float = Field(
        default=600.0, ge=5.0, le=1500.0,
        validation_alias="ExtractorConfiguration__SignatureSlotTimeoutSeconds",
    )
    # Orçamento de relógio da inferência num documento, contado depois de obter
    # o slot. Medido no acervo de 72k com o fallback em TODAS as páginas: p99 =
    # 30 s, p99.9 = 181 s, máximo = 5 min. 600 s cobre o máximo com folga em
    # hardware mais lento e ainda cabe no timeout de 30 min do job.
    signature_budget_s: float = Field(
        default=600.0, ge=10.0, le=1500.0,
        validation_alias="ExtractorConfiguration__SignatureBudgetSeconds",
    )
    # Teto de páginas por documento no Nível 1. 100 e não 30: medido no acervo,
    # subir de 30 para 100 custa +4,5% de inferência no lote inteiro e leva os
    # documentos truncados de 51 para 4 (em 18.082 que caem no fallback). É o
    # teto que quase nunca corta — e cortar é o que a área não quer.
    signature_max_paginas: int = Field(
        default=100, ge=0, validation_alias="ExtractorConfiguration__SignatureMaxPages"
    )
```

**`.env.example`**: acrescentar as três chaves, vazias/comentadas, junto do bloco `ExtractorConfiguration__*`.

---

### Etapa 4 — Mover o núcleo de detecção

Criar `src/motor_ia/extractors/assinatura/` com 5 arquivos.

#### 4.1 `__init__.py` (novo)

```python
"""Detecção de assinatura local — cascata Nível 0 → Nível 1 → fallback.

Substitui a feature `SIGNATURES` do AWS Textract (US$ 3,50/1.000 páginas),
insustentável no volume da análise documental. O veredito sai em CPU no
próprio worker, sem custo por página.

Ver `deteccao.py` para a cascata e o contrato de saída.
"""

from __future__ import annotations

from motor_ia.extractors.assinatura.deteccao import (
    ResultadoDeteccao,
    detectar_assinaturas,
    detectar_assinaturas_async,
    formatar_contexto,
)

__all__ = [
    "ResultadoDeteccao",
    "detectar_assinaturas",
    "detectar_assinaturas_async",
    "formatar_contexto",
]
```

#### 4.2 `nivel0.py` — cópia **sem alteração**

`poc-assinatura/tool_validar_assinatura/nivel0.py` → `src/motor_ia/extractors/assinatura/nivel0.py`.

Já importa `from motor_ia.extractors.signatures import find_digital_signatures,
find_embedded_pdf_signatures` — dentro do motor-ia isso resolve para o módulo real.
Zero edições.

Entrega: `/Sig` no AcroForm **assinado de verdade**, carimbo digital no texto, rótulo de
campo de assinatura por página, e a triagem de tinta (72 DPI, limiar 0,0005) que tira
página em branco do caminho do Nível 1.

O "de verdade" é o filtro de §0.2, e ele viaja dentro deste arquivo: `campos_assinados()`
lê `/V` → `/ByteRange` → `/Contents` de cada widget e `somente_assinados()` descarta os
que não têm assinatura viva. Duas coisas que a revisão do PR vai perguntar:

- **Por que não corrigir direto em `find_embedded_pdf_signatures`?** Porque ela é do
  módulo compartilhado e a correção lá muda o comportamento de qualquer outro chamador
  sem aviso. Depois da Etapa 6 o Nível 0 é o **único** chamador que sobra no motor-ia
  (`detect_all_signatures` sai) — então mover o filtro para dentro dela vira uma limpeza
  segura e opcional, e `somente_assinados()` continua correto (vira no-op: filtrar duas
  vezes o mesmo conjunto não tira nada).
- **Por que o nome do signatário não entra no critério?** `/V /Name` é opcional e a
  maioria dos PDFs do ICP-Brasil não o preenche; o `widget.field_value` do PyMuPDF
  devolve `""` mesmo em assinatura válida. Usar o nome como prova inverte o diagnóstico e
  descarta assinatura em massa — foi assim que a leitura anterior concluiu, errado, que os
  campos estavam vazios. Auditado em `poc-assinatura/auditoria/ACROFORM.md`.

#### 4.3 `nivel1.py` — cópia com 2 ajustes

Copiar e alterar apenas:

```python
# ANTES (l. 39)
from modelo import resolver_modelo

# DEPOIS
from motor_ia.extractors.assinatura.modelo import resolver_modelo
```

E trocar a leitura de threads por env crua pela Settings (l. 76 e `_threads_configuradas`):

```python
# ANTES
ENV_THREADS = "POC_ASSINATURA_ONNX_THREADS"

def _threads_configuradas() -> int | None:
    bruto = os.getenv(ENV_THREADS)
    ...

# DEPOIS
def _threads_configuradas() -> int | None:
    """Threads da inferência via configuração (0 = ONNX Runtime decide)."""
    from motor_ia.config import get_settings

    return get_settings().extractor.signature_onnx_threads or None
```

**Não tocar em nada mais.** As duas calibrações que saíram da PoC ficam como estão, com os
comentários que as justificam:

- `CONFIANCA_MINIMA = 0.15` (com 0,25 o lote de calibração fecha em 15/17; com 0,15, em 17/17, sem falso-positivo nas conferências)
- reamostragem **LANCZOS** em `_preprocessar` (o BILINEAR do Space oficial apaga rubrica de caneta fina)
- `enable_cpu_mem_arena = False` (a arena aloca em blocos grandes e não devolve ao SO)
- ⚠️ **exceção**: o `@lru_cache(maxsize=1)` em `_detector_cacheado` **não** migra — é o único
  ponto da PoC que não sobrevive ao alvo de produção. Ver (a) logo abaixo e §5.3

O **fallback de ladrilhos** vive aqui: `_detectar_ladrilhos`, `retangulos_ladrilhos`
(3×3 com 15% de sobreposição), `caixa_para_pagina`, `_deduplicar`, e o gatilho em
`detectar_pdf` (`if fallback_ladrilhos and not assinaturas`). Migra inteiro, sem mudança.

**Duas correções obrigatórias.**

**(a) A inferência sai do processo do job e vai para um subprocesso descartável.**

Remover `_detector_cacheado` / `obter_detector` (l. 448–455). A classe
`DetectorAssinaturaOnnx` continua idêntica — o que muda é **quem a instancia**: não mais o
processo do job, e sim um filho de vida curta (`assinatura/worker.py`, §4.4-ter) que o
sistema operacional reap por completo ao terminar.

Por que não simplesmente carregar e liberar a sessão dentro do próprio job:

| | Sessão no processo do job | Sessão em subprocesso |
|---|---|---|
| Memória residente adicionada ao job | ~150 MB | **zero** |
| Devolução da memória | depende de `gc` + `glibc` devolverem | **garantida pelo SO** |
| Se o detector for morto por OOM | mata o job inteiro (perde 3 min de trabalho + LLM já pago) | mata um filho de 150 MB; **o job segue e degrada** |
| Custo | 0 | ~1–1,5 s de startup por documento |

Num container que **já dá OOM hoje** (ver §5.1), "confio que o alocador devolve" não é
garantia — e o pior modo de falha do desenho in-process é justamente o OOM killer escolher
um processo de job no meio de uma análise. O subprocesso troca ~1–1,5 s por documento
(≈3% de uma análise de 30–60 s) por um isolamento que não depende de nada.

> A sessão continua sendo carregada **uma vez por documento** e reusada por todas as
> páginas e por todos os 9 ladrilhos de cada página — é lá que estão as centenas de
> inferências. O que se paga a mais é o startup do interpretador do filho.
>
> Se a medição de §5.8 mostrar que esse startup pesa, o passo seguinte já está desenhado:
> um processo detector de vida longa por container, servindo por socket unix (amortiza o
> startup a zero e mantém o isolamento). Não entra agora porque adiciona um daemon a
> supervisionar por um ganho ainda não medido.

**(b) Orçamento de relógio, em duas camadas.** O `subprocess.run(timeout=...)` do pai é o
muro rígido — mata o filho mesmo travado dentro de uma chamada C++ do ONNX Runtime, coisa
que nenhum `deadline` cooperativo consegue. Mas matar perde o trabalho parcial, então o
filho também checa um prazo um pouco menor e devolve o que já achou:

```python
            for numero in selecionadas:
                if deadline is not None and time.monotonic() > deadline:
                    truncado = True
                    _logger.warning(
                        "assinatura.orcamento_estourado",
                        path=str(path), fase="pagina", analisadas=len(paginas_analisadas),
                    )
                    break
```

O mesmo antes do laço de ladrilhos. `ResultadoNivel1` ganha `truncado: bool = False`, que
sobe até o dict de saída — **assinatura não encontrada por falta de tempo não é o mesmo
que assinatura ausente**, e o LLM precisa dessa distinção (§5.6).

#### 4.4 `modelo.py` — encolhe de 156 para ~45 linhas

Com os pesos no repositório, **todo o download morre**. Não é simplificação opcional: é
código que não tem mais como ser alcançado.

**Remover** (l. 24–121 da versão da PoC): `import httpx`, `HF_REPO_ID`, `HF_FILENAME`,
`HF_REVISION`, `ENV_TOKENS_HF`, `_TIMEOUT_DOWNLOAD_SEGUNDOS`, `_CHUNK_BYTES`, `_token_hf`,
`_url_hf`, `_mensagem_acesso_negado`, `baixar_modelo`, o parâmetro `permitir_download` e o
bloco `__main__`.

**Manter**: `ModeloIndisponivelError`, `DIRETORIO_CACHE`, `CAMINHO_CACHE` e
`resolver_modelo`, que fica assim:

```python
"""Resolução do arquivo de pesos do detector visual local (Nível 1).

O `yolov8s.onnx` (~44 MB, `tech4humans/yolov8s-signature-detector` exportado
para ONNX) é versionado junto do pacote, em `models/`. A resolução é local e
sem rede: o worker não depende de nenhum serviço externo para subir.

O override por configuração existe só para apontar outros pesos numa
calibração, sem rebuild da imagem.
"""

from __future__ import annotations

from pathlib import Path

HF_MODELO = "tech4humans/yolov8s-signature-detector"  # origem, para rastreabilidade
NOME_ARQUIVO = "yolov8s.onnx"

DIRETORIO_MODELOS = Path(__file__).resolve().parent / "models"
CAMINHO_PADRAO = DIRETORIO_MODELOS / NOME_ARQUIVO


class ModeloIndisponivelError(RuntimeError):
    """Os pesos do detector visual não estão acessíveis."""


def resolver_modelo(caminho: str | Path | None = None) -> Path:
    """Caminho local dos pesos ONNX.

    Cascata: argumento explícito → `ExtractorConfiguration__SignatureModelPath`
    → arquivo versionado no pacote. Sem rede em nenhum dos três.
    """
    if caminho:
        explicito = Path(caminho).expanduser()
        if not explicito.is_file():
            raise ModeloIndisponivelError(f"Modelo ONNX não encontrado em `{explicito}`.")
        return explicito

    from motor_ia.config import get_settings

    override = get_settings().extractor.signature_model_path
    if override:
        do_config = Path(override).expanduser()
        if not do_config.is_file():
            raise ModeloIndisponivelError(
                f"`ExtractorConfiguration__SignatureModelPath` aponta para "
                f"`{do_config}`, que não existe."
            )
        return do_config

    if CAMINHO_PADRAO.is_file():
        return CAMINHO_PADRAO

    raise ModeloIndisponivelError(
        f"Pesos ausentes em `{CAMINHO_PADRAO}`. O arquivo é versionado no "
        "repositório — provavelmente o wheel foi construído sem ele (conferir "
        "se o `.onnx` entrou no pacote) ou o checkout está incompleto."
    )
```

A mensagem do último `raise` é deliberada: com os pesos no repo, "arquivo ausente" só
acontece por erro de empacotamento — e é isso que a mensagem tem de dizer a quem for
depurar às 3h da manhã, não "baixe do Hugging Face".

#### 4.4-bis `gate.py` — **arquivo novo**, o teto de recursos do container

Não existe na PoC (lá é um processo só). É o componente que torna a feature segura sob 20
workers, e o único código genuinamente novo desta migração.

```python
"""Portão de concorrência da inferência de assinatura — teto por container.

O `background_jobs` cria um `multiprocessing.Process` por job (até
`WorkerConfiguration__Count`, hoje 20). Sem portão, 20 jobs que precisem de
assinatura carregam 20 sessões ONNX no mesmo container de 4 GB. Com portão, o
consumo da feature é O(`signature_max_concurrent`) e independe de quantos
workers existem — é essa a garantia que permite subir a concorrência sem
mexer na máquina.

`fcntl.flock` e não um semáforo em Mongo, arquivo-contador ou
`multiprocessing.Semaphore`:

  * o kernel libera o lock quando o processo morre — worker morto por OOM,
    SIGKILL ou deploy não deixa slot preso, que é o modo de falha clássico de
    um contador persistido (e o que transformaria um incidente pequeno numa
    parada total da feature);
  * funciona entre processos sem parentesco, que é o caso: os filhos são
    criados pelo `background_jobs`, sem passar primitivas de sincronização;
  * é stdlib e não adiciona dependência nem serviço.
"""

from __future__ import annotations

import errno
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import structlog

_logger = structlog.get_logger(__name__)

# Em /tmp de propósito: o escopo do teto é o CONTAINER (é dele a CPU e a RAM
# que se está protegendo), não o cluster. Duas tasks Fargate têm tetos
# independentes, que é exatamente o desejado.
DIRETORIO_SLOTS = Path(os.getenv("TMPDIR", "/tmp")) / "motor-ia-assinatura-slots"


class SlotIndisponivelError(RuntimeError):
    """Nenhum slot de inferência ficou livre dentro do prazo."""


def _tentar_adquirir(slots: int) -> int | None:
    """Tenta cada slot uma vez, sem bloquear. Devolve o fd ou `None`."""
    import fcntl

    for indice in range(slots):
        fd = os.open(str(DIRETORIO_SLOTS / f"{indice}.lock"), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                raise
            continue
        return fd
    return None


@contextmanager
def slot_inferencia(
    *, slots: int, timeout_s: float, intervalo_s: float = 0.25
) -> Iterator[None]:
    """Reserva um dos `slots` de inferência do container.

    Levanta `SlotIndisponivelError` se não conseguir dentro de `timeout_s` — o
    chamador degrada para o Nível 0, nunca espera indefinidamente.
    """
    try:
        import fcntl
    except ImportError:
        # Windows (desenvolvimento local): um processo só, sem contenção real.
        yield
        return

    DIRETORIO_SLOTS.mkdir(parents=True, exist_ok=True)
    inicio = time.monotonic()
    limite = inicio + timeout_s
    fd: int | None = None
    while True:
        fd = _tentar_adquirir(slots)
        if fd is not None:
            break
        if time.monotonic() >= limite:
            raise SlotIndisponivelError(
                f"nenhum dos {slots} slots de inferência ficou livre em {timeout_s:.0f}s"
            )
        time.sleep(intervalo_s)

    espera_ms = (time.monotonic() - inicio) * 1000
    if espera_ms > 1000:
        _logger.info("assinatura.slot_apos_espera", espera_ms=round(espera_ms))
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
```

**Por que isso quase nunca vai bloquear na prática:** uma análise documental leva dezenas
de segundos (LLM + extração + OCR); a detecção leva ~1–15 s por documento. A fração do
tempo de um job gasta dentro da detecção é da ordem de 5%. Com 20 jobs, o número esperado
de detecções simultâneas é ~1. Os 2 slots absorvem a variância; o timeout existe para o
pico patológico, não para o regime normal.

**Ponto de atenção do `ge=1` no setting:** `signature_max_concurrent` nunca pode ser 0 —
seria desligar a feature por configuração, e o efeito colateral (todo documento vira
"assinatura não verificada") é pior do que uma fila. Para desligar de verdade existe o
kill-switch de negócio, `detectar_assinaturas=False` no `PipelineConfig`.

#### 4.4-ter `worker.py` — **arquivo novo**, o subprocesso de inferência

Entrada do filho. Deliberadamente enxuto: importa `nivel1` e nada do resto do `motor_ia`
(sem boto3, anthropic, fastapi, mongo) — é o que mantém o startup em ~1 s em vez de ~4 s.

```python
"""Subprocesso de inferência do Nível 1 — isolamento de memória.

Roda uma varredura e morre. Toda a memória da sessão ONNX (~150 MB) volta ao SO
por término de processo, sem depender de `gc` nem do alocador da libc. Se o OOM
killer escolher alguém, escolhe este processo de 150 MB e não o job que já
gastou minutos de LLM — o pai trata a morte como "detecção indisponível" e a
análise segue.

Protocolo: parâmetros em JSON no argv[1], resultado em JSON no stdout. Erro vai
para o stderr e sai com código != 0 — o pai degrada para o Nível 0.

NÃO importar nada do `motor_ia` além de `extractors.assinatura`: o custo de
startup deste processo é pago uma vez por documento.
"""

from __future__ import annotations

import json
import sys
import time


def main(argv: list[str]) -> int:
    from motor_ia.extractors.assinatura.modelo import resolver_modelo
    from motor_ia.extractors.assinatura.nivel1 import DetectorAssinaturaOnnx

    pedido = json.loads(argv[1])
    detector = DetectorAssinaturaOnnx(
        resolver_modelo(pedido.get("modelo_path")),
        threads=pedido.get("threads"),
    )
    resultado = detector.detectar_pdf(
        pedido["path"],
        paginas=pedido.get("paginas"),
        confianca_minima=pedido["confianca_minima"],
        iou_maximo=pedido["iou_maximo"],
        dpi=pedido["dpi"],
        fallback_ladrilhos=pedido["fallback_ladrilhos"],
        dpi_ladrilho=pedido["dpi_ladrilho"],
        deadline=time.monotonic() + pedido["orcamento_s"],
    )
    json.dump(
        {
            "assinaturas": resultado.assinaturas,
            "paginas_analisadas": resultado.paginas_analisadas,
            "paginas_ignoradas": resultado.paginas_ignoradas,
            "paginas_ladrilhadas": resultado.paginas_ladrilhadas,
            "tempo_inferencia_ms": resultado.tempo_inferencia_ms,
            "truncado": resultado.truncado,
            "modelo": resultado.modelo,
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
```

`DetectorAssinaturaOnnx.__init__` já aceita `threads` — passar
`settings.signature_onnx_threads` por aqui em vez de reler a configuração no filho (que
custaria importar `motor_ia.config` e suas dependências).

#### 4.5 `deteccao.py` — cópia com 2 imports ajustados

```python
# ANTES (l. 31–46)
from nivel0 import (DENSIDADE_TINTA_MINIMA, DPI_TRIAGEM, ResultadoNivel0, detectar_nivel0)
from nivel1 import (CONFIANCA_MINIMA, DPI_LADRILHO, DPI_RENDER, IOU_MAXIMO, ResultadoNivel1, obter_detector)
from motor_ia.extractors.signatures import format_signature_context

# DEPOIS
from motor_ia.extractors.assinatura.nivel0 import (
    DENSIDADE_TINTA_MINIMA, DPI_TRIAGEM, ResultadoNivel0, detectar_nivel0,
)
from motor_ia.extractors.assinatura.nivel1 import (
    CONFIANCA_MINIMA, DPI_LADRILHO, DPI_RENDER, IOU_MAXIMO, ResultadoNivel1,
)
from motor_ia.extractors.assinatura.gate import SlotIndisponivelError, slot_inferencia
```

`obter_detector` sai do import: quem instancia o detector agora é o subprocesso
(`worker.py`), não este módulo.
from motor_ia.extractors.signatures import format_signature_context
```

E o default de `max_paginas` (l. 152) passa a vir da Settings:

```python
# ANTES
    max_paginas: int | None = None,

# DEPOIS — dentro do corpo, antes de fatiar `paginas_alvo`:
    if max_paginas is None:
        from motor_ia.config import get_settings

        max_paginas = get_settings().extractor.signature_max_paginas or None
```

O resto (cascata, `escalonar`, `para_dict()` no formato legado, `nivel1_erro` no lugar de
`visual_error`, `detectar_assinaturas_async` rodando em `asyncio.to_thread`) migra intacto.
É esse `para_dict()` que garante que o consumo já existente na pipeline funcione sem
alteração.

**O Nível 1 passa a rodar sob o portão, num subprocesso.** Substituir o `try/except` de
`obter_detector` + `detectar_pdf` (l. 176–196) por:

```python
    settings = get_settings().extractor
    # Fallback varre o documento INTEIRO, inclusive as páginas que o Nível 0
    # marcou como em branco. Regra de negócio, não descuido: a tool só é chamada
    # quando o requisito tem critério de assinatura, e o fallback só dispara
    # quando NADA foi encontrado nos dois níveis. Nesse ponto a resposta que
    # interessa é "olhei tudo e não tem", e página classificada como branca por
    # densidade de tinta pode conter uma rubrica de traço fino — que é
    # exatamente o caso que o ladrilho existe para pegar.
    paginas_fallback = list(range(1, nivel0.total_paginas + 1))
    if max_paginas is not None:
        paginas_fallback = paginas_fallback[:max_paginas]

    try:
        with slot_inferencia(
            slots=settings.signature_max_concurrent,
            timeout_s=settings.signature_slot_timeout_s,
        ):
            nivel1 = _inferir_em_subprocesso(
                p,
                paginas=paginas_alvo,
                paginas_fallback=paginas_fallback,
                confianca_minima=confianca_minima,
                iou_maximo=iou_maximo,
                dpi=dpi_render,
                fallback_ladrilhos=fallback_ladrilhos and not nivel0.tem_assinatura,
                dpi_ladrilho=dpi_ladrilho,
                orcamento_s=settings.signature_budget_s,
                threads=settings.signature_onnx_threads or None,
                modelo_path=modelo_path,
            )
    except SlotIndisponivelError as exc:
        # Container saturado por tempo demais. Degrada para o veredito do Nível
        # 0 — o mesmo caminho de "detecção visual indisponível" que já existe.
        # NUNCA derruba a análise e NUNCA vira "não tem assinatura".
        _logger.warning("assinatura.slot_indisponivel", document=str(p), error=str(exc))
        return ResultadoDeteccao(nivel0=nivel0, nivel1_erro=str(exc))
    except Exception as exc:
        # Filho morto (OOM killer, timeout duro), stdout ilegível, pesos
        # ausentes — tudo cai aqui e degrada igual.
        _logger.warning(
            "assinatura.nivel1_indisponivel",
            document=str(p), error=str(exc), error_type=type(exc).__name__,
        )
        return ResultadoDeteccao(nivel0=nivel0, nivel1_erro=str(exc))

    return ResultadoDeteccao(nivel0=nivel0, nivel1=nivel1)
```

E a chamada do subprocesso (função nova, ~30 linhas):

```python
def _inferir_em_subprocesso(path: Path, **pedido: Any) -> ResultadoNivel1:
    """Roda o Nível 1 num filho descartável e devolve o resultado.

    `timeout` um pouco acima do `orcamento_s` do filho: o filho tenta terminar
    graciosamente e devolver o parcial; este é o muro rígido para quando ele
    trava dentro do C++ do ONNX Runtime e não chega a checar prazo nenhum.
    """
    import subprocess

    argumentos = {"path": str(path), **pedido}
    concluido = subprocess.run(
        [sys.executable, "-m", "motor_ia.extractors.assinatura.worker", json.dumps(argumentos)],
        capture_output=True,
        timeout=pedido["orcamento_s"] + _MARGEM_KILL_S,
        check=True,
    )
    dados = json.loads(concluido.stdout)
    return ResultadoNivel1(**dados)
```

`ResultadoNivel0` ganha a propriedade `total_paginas` (`len(self.paginas)`) — o Nível 0 já
tria todas as páginas, então o dado existe.

> **Diferença em relação ao comportamento da PoC:** lá o fallback rodava só sobre
> `paginas_uteis` (as não-brancas). Aqui roda sobre todas. Custo medido no acervo de 72k:
> as páginas em branco são 714 de 181.557 (0,4%) — o acréscimo é irrelevante e a cobertura
> passa a ser total, que é o requisito.

---

### Etapa 5 — Enxugar `extractors/signatures.py`

O módulo **fica**, mas perde tudo que dependia do Textract.

**Remover:**

| Símbolo | Linhas | Por quê |
|---|---|---|
| `import asyncio` | 24 | só `detect_all_signatures` usava |
| `from ...textract import detect_signatures as detect_visual_signatures` | 31 | Textract sai |
| `from ...textract import detect_signatures_in_regions` | 32 | idem |
| `from ...types import ExtractionError` | 33 | só o `except` do visual usava |
| `_SIGNATURE_FIELD_CUES` + `_expects_signature` | 40–47 | gatilho do fallback por região do Textract — substituído pelo fallback de ladrilhos do Nível 1 |
| `_pdf_text_layer` | 133–142 | duplicado em `nivel0._texto_camada_digital` |
| `_consolidar_signatarios` | 177–195 | só `detect_all_signatures` usava; `ResultadoDeteccao.signatarios` faz o papel |
| `detect_all_signatures` | 198–260 | **é a função que está sendo substituída** |

**Manter (é o que o Nível 0 e a formatação usam):** `_DIGITAL_PRESENCE_PATTERNS`,
`_DIGITAL_SIGNER_PATTERN`, `_PDF_SIGNATURE_WIDGET_TYPE`, `_clean_signer`, `_snippet`,
`find_digital_signatures`, `find_embedded_pdf_signatures`, `format_signature_context`.

`find_embedded_pdf_signatures` fica **como está**, contando a presença do widget: quem
descarta o campo em branco é o `nivel0.py` (§4.2). Com `detect_all_signatures` removido
aqui, ela passa a ter um único chamador no motor-ia — se a limpeza de mover o filtro para
dentro dela for feita algum dia, será nesse ponto, e sem quebrar o Nível 0.

**Atualizar o docstring do módulo** (l. 1–20): hoje descreve três sinais sendo que o
visual é "feature nativa `SIGNATURES` do Textract". Passa a descrever os dois sinais de
custo zero, apontando que o visual virou `extractors/assinatura/nivel1.py`.

---

### Etapa 6 — Deletar o Textract SIGNATURES

**`src/motor_ia/extractors/textract.py`**

1. Deletar **linhas 402–783** (do comentário `# ---------- signature detection
   (AnalyzeDocument SIGNATURES) ---------------` até o fim do arquivo).
2. Deletar as constantes `_SIGNATURE_SIGNER_MAX_VERTICAL_GAP` (l. 57) e
   `_SIGNATURE_SIGNER_MAX_LINES` (l. 60).
3. Rodar `ruff check --fix` para limpar imports que ficarem órfãos.

O arquivo continua com todo o OCR (`extract_pdf_ocr`, `extract_pdf_ocr_async`,
`_parse_textract_blocks`, `fits_sync_api`, `TEXTRACT_SYNC_MAX_BYTES`) — nada disso é
assinatura, e `extractors/hybrid.py` depende.

**`src/motor_ia/analyzers/fornecedores/pipeline.py`** — remover o `UsageStep` de custo:

```python
# REMOVER (l. ~455–462)
        assinatura_cost = sum(
            (Decimal(str(sig.get("cost_usd", 0) or 0)) for sig in assinatura_por_doc.values()),
            Decimal(0),
        )
        if assinatura_cost > 0:
            usage_steps.append(
                UsageStep(step="assinatura", model="textract", cost_usd=assinatura_cost)
            )
```

A detecção nova não emite `cost_usd` (é zero) e o `model="textract"` passa a ser mentira.

---

### Etapa 7 — Reescrever `tools/assinatura.py`

**Substituir o arquivo inteiro** por `poc-assinatura/tool_validar_assinatura/assinatura.py`,
com um ajuste de import e o texto do arg (D2):

```python
# ANTES (PoC, l. 23)
from deteccao import detectar_assinaturas_async, formatar_contexto

# DEPOIS
from motor_ia.extractors.assinatura import detectar_assinaturas_async, formatar_contexto
```

E na `ValidarAssinaturaArgs.incluir_visual` (D2), o texto já vem correto da PoC:

```python
    incluir_visual: bool = Field(
        default=True,
        description=(
            "Inclui a detecção visual de rubrica (modelo local em CPU, sem custo "
            "por página). Desligue para checar apenas assinatura digital/embedded."
        ),
    )
```

O que **desaparece** em relação ao arquivo atual do motor-ia:

- os três `raise Exception("Desativado.")` (l. 86, 147, 178)
- o import de `detect_all_signatures` / `format_signature_context` (l. 32–35)

O que **permanece idêntico** e é o motivo de a religação ser barata: `name =
"validar_assinatura"`, a classe `ValidarAssinaturaArgs` (`s3_key` + `incluir_visual`), o
`SignatureToolResult(deteccao, texto)`, `_baixar_anexo` via `Storage`, e as duas mensagens
de degradação (`_TEXTO_SEM_DOCUMENTO`, `_TEXTO_DETECCAO_FALHOU`).

Ajustar a `description` da tool (l. 131–139 do arquivo atual): trocar
`"visual OCR / digital text markers / embedded PDF fields"` por
`"visual detection / digital text markers / embedded PDF fields"` — já é o texto da PoC.

---

### Etapa 8 — Religar registry e catálogo

**`src/motor_ia/tools/__init__.py`**

```python
# l. 15–17 — REMOVER o comentário de desativação e DESCOMENTAR o import
from motor_ia.tools.assinatura import ValidarAssinaturaTool

# l. 58 — no __all__
    "ValidarAssinaturaTool",

# l. 98–102 — dentro de default_tools(), na lista `tools`
        # `validar_assinatura`: detecta assinaturas no documento ativo (ctx) ou
        # no anexo do S3 (por `s3_key`). Detecção local (Nível 0 → Nível 1),
        # sem custo por página.
        ValidarAssinaturaTool(storage=storage),
```

E no docstring de `default_tools` (l. 83–84), remover a frase `"storage is currently
unused: it only fed the disabled ValidarAssinaturaTool."` — o `storage` volta a ser usado.

**`src/motor_ia/tools/catalog.py`** — descomentar a entrada (l. 47–54):

```python
    CatalogTool(
        id="validar_assinatura",
        nome="Validação de Assinatura",
        descricao="Detecta assinaturas (visual, digital ou embutida no PDF) no documento.",
    ),
```

**`src/motor_ia/api/dependencies.py`** (l. 59–62) — atualizar o comentário do `storage`
(deixa de dizer "a tool está DESABILITADA"). Nenhuma mudança de código.

Com isso a tool volta ao chat (Vera) e ao seletor de Ferramentas do modal de agent.

---

### Etapa 9 — Religar a pipeline ⭐

`src/motor_ia/analyzers/fornecedores/pipeline.py`.

**Esta etapa vale para os dois agentes**: Trabalhadores reusa a mesma classe `Pipeline`,
parametrizada por `PipelineConfig` (`agent_name`, `system_prompt_builder`, etc.).

#### 9.1 Imports (topo do arquivo)

```python
# l. 70 — acrescentar local_source_path
from motor_ia.analyzers.shared.source_fetch import extract_source, local_source_path

# l. 86 — ampliar o import da tool
from motor_ia.tools.assinatura import (
    SignatureToolResult,
    ValidarAssinaturaArgs,
    ValidarAssinaturaTool,
    executar_deteccao_assinatura,
)
```

#### 9.2 `PipelineConfig` (l. ~213–223) — limpar os comentários de desativação

```python
    # Kill-switch global da detecção de assinatura.
    detectar_assinaturas: bool = True
    # Tool de detecção de assinatura exposta ao analisador via tool-calling.
    # Quando setada (e `detectar_assinaturas=True`), o analisador recebe a tool
    # e DECIDE, por conta própria, se a chama para um requisito. None =
    # analisador sem a tool.
    assinatura_tool: ValidarAssinaturaTool | None = None
```

Nenhuma mudança de tipo ou default — só o texto deixa de mentir.

#### 9.3 `_analyse_with_critic` (l. ~827–840) — reativar o memo

```python
# DESCOMENTAR (l. ~837), substituindo todo o bloco de comentário acima dele:

        # Memo da detecção por documento: a detecção é determinística pro mesmo
        # arquivo, mas o analisador chama a tool em CADA volta de tool-calling,
        # em CADA chunk e em CADA tentativa — até 6× o mesmo documento. Escopo
        # local (não de instância) de propósito: `Pipeline` é stateless e
        # reusado entre jobs, então nada vaza entre documentos ou análises.
        assinatura_cache: dict[tuple[str, bool], SignatureToolResult] = {}
```

E passar o cache nas **duas** chamadas de `_run_analyser` (l. ~839 e ~866):

```python
        output, analyser_usages, assinatura_result = await self._run_analyser(
            payload=payload,
            chunks=chunks,
            doc_name=doc_name,
            documento_path=documento_path,
            data_atual=data_atual,
            assinatura_cache=assinatura_cache,          # <-- novo
        )
```

```python
            output, retry_usages, retry_assinatura = await self._run_analyser(
                ...
                instrucao_reavaliacao=instrucao_retry,
                assinatura_cache=assinatura_cache,      # <-- novo
            )
```

> O memo era caro no Textract (até 6 jobs async no mesmo PDF, ~100 s cada). No ONNX local
> ainda vale muito: cada repetição custa raster + inferência de todas as páginas do
> documento. Mantém.

#### 9.4 `_run_analyser` (l. ~1019) — repassar o cache

Novo parâmetro na assinatura e repasse a `_call_analyser_once`:

```python
    async def _run_analyser(
        self,
        *,
        payload: FornecedoresPayload,
        chunks: list[str],
        doc_name: str,
        documento_path: str,
        data_atual: str,
        instrucao_reavaliacao: str | None = None,
        assinatura_cache: dict[tuple[str, bool], SignatureToolResult],   # <-- novo
    ) -> tuple[AnalyserOutput, list[_LLMCallStats], dict[str, Any] | None]:
```

```python
            output, stats, chunk_assinatura = await self._call_analyser_once(
                ...
                instrucao_reavaliacao=instrucao_reavaliacao,
                assinatura_cache=assinatura_cache,      # <-- novo
            )
```

O resto do método (`if chunk_assinatura is not None: assinatura_result = chunk_assinatura`)
**já existe** — não mexer.

#### 9.5 `_call_analyser_once` (l. ~1081) — repassar o cache

Mesmo padrão: novo parâmetro `assinatura_cache` na assinatura, repassado na chamada a
`_collect_with_tools` (l. ~1128).

#### 9.6 `_collect_with_tools` (l. ~1335) — **o coração**

Novo parâmetro:

```python
        assinatura_cache: dict[tuple[str, bool], SignatureToolResult],
```

Reativar a tool (l. ~1366–1367):

```python
# ANTES
        # tool = self.config.assinatura_tool if self.config.detectar_assinaturas else None
        tool: ValidarAssinaturaTool | None = None

# DEPOIS
        tool = self.config.assinatura_tool if self.config.detectar_assinaturas else None
```

Descomentar o bloco de execução (l. ~1428–1461), substituindo os comentários por código:

```python
                try:
                    incluir_visual = ValidarAssinaturaArgs.model_validate(tu.input).incluir_visual
                except Exception:
                    incluir_visual = True
                # O analisador já decidiu que o requisito depende de assinatura ao
                # chamar a tool — aqui só detectamos no documento do payload.
                # `documento_path` é a chave S3 crua; os sinais do Nível 0 e o
                # raster do Nível 1 precisam do arquivo em disco, então baixamos
                # pro temp (só quando a tool é de fato chamada).
                cache_key = (documento_path, incluir_visual)
                sig = assinatura_cache.get(cache_key)
                if sig is None:
                    async with local_source_path(documento_path) as _doc_local:
                        sig = await executar_deteccao_assinatura(
                            documento_path=str(_doc_local),
                            documento_texto=documento_texto,
                            incluir_visual=incluir_visual,
                        )
                    assinatura_cache[cache_key] = sig
                else:
                    _logger.info(
                        f"{self.config.agent_name}.assinatura_cache_hit",
                        analise_id=payload.analise_id,
                        document=_basename(documento_path),
                        incluir_visual=incluir_visual,
                    )
                if sig.deteccao is not None:
                    assinatura = sig.deteccao
                result_blocks.append(ToolResultBlock(tool_use_id=tu.id, content=sig.texto))
```

E reescrever o docstring do método (l. ~1346–1364), removendo os dois parágrafos
`**Tool validar_assinatura DESABILITADA**`.

> **Ponto de atenção — `documento_texto`.** É o texto já extraído pela pipeline
> (camada digital **ou OCR**). Passá-lo ao Nível 0 é o que faz o carimbo digital ser
> detectado em documento escaneado, onde ele só existe depois do OCR. Não trocar por
> releitura do PDF.

#### 9.7 `run()` (l. ~280–284) — limpar o comentário

```python
# ANTES
        # Validação de assinatura DESABILITADA (custo do Textract SIGNATURES) —
        # ver `_collect_with_tools`. O mapa fica sempre vazio: [...]
        assinatura_por_doc: dict[str, dict[str, Any]] = {}

# DEPOIS
        # Detecção de assinatura por documento (preenchido quando o analisador
        # chamou a tool). Alimenta `_apply_signature_fields` e
        # `reconcile_assinatura`.
        assinatura_por_doc: dict[str, dict[str, Any]] = {}
```

**Nada mais na pipeline muda.** As linhas 310–311, 384, 566, 591 e 2029–2031 já consomem
o resultado corretamente.

---

### Etapa 10 — Religar os runners

**`src/motor_ia/analyzers/fornecedores/runner.py`**

```python
# l. 37–39 — REMOVER o comentário de desativação, DESCOMENTAR o import
from motor_ia.tools.assinatura import ValidarAssinaturaTool

# l. 362–368 — dentro de _ensure_pipeline(), no PipelineConfig
        config = PipelineConfig(
            provider=provider,
            critic=critic,
            analyser_model=analyser_model,
            context_window_tokens=context_window,
            # O analisador de Fornecedores decide, via tool-calling, quando o
            # requisito depende de assinatura. Detecção local (Nível 0 → Nível
            # 1), sem custo por página.
            assinatura_tool=ValidarAssinaturaTool(),
        )
```

**`src/motor_ia/analyzers/trabalhadores/runner.py`** — exatamente o mesmo padrão:

```python
# l. 54–56 — DESCOMENTAR o import
from motor_ia.tools.assinatura import ValidarAssinaturaTool

# l. 245–251 — no PipelineConfig
            agent_name="trabalhadores",
            assinatura_tool=ValidarAssinaturaTool(),
```

> `ValidarAssinaturaTool()` sem `storage` é o correto aqui: na pipeline o documento vem
> do `documento_path` do payload via `local_source_path`, não do `s3_key` do chat. O
> `storage` só é injetado no registry da API (Etapa 8).

---

### Etapa 11 — Religar os prompts

**`src/motor_ia/analyzers/fornecedores/prompts.py`** — remover o bloco comentado (l. 49–68)
e reinserir a seção **dentro** de `_BASE_INSTRUCTIONS`, entre `# Hierarquia de instruções
(CRÍTICO)` e `# Saída obrigatória — JSON estruturado` (l. ~96), com o texto de custo
ajustado (D2):

```markdown
# Ferramentas

Você tem acesso à ferramenta `validar_assinatura`. VOCÊ decide quando ela é
necessária: ao analisar o requisito, se ele exigir, avaliar ou condicionar a
validade do documento à presença de ASSINATURA (manuscrita, eletrônica ou
digital) de qualquer parte, **chame `validar_assinatura` ANTES de concluir** os
critérios que dependem da assinatura. A ferramenta **apenas detecta** as
assinaturas no documento (sinais visual/digital/PDF) e devolve o resultado — ela
NÃO reavalia se o requisito exige assinatura; essa avaliação é sua.

- O `tool_result` é **dado verificado pelo sistema** (evidência), não instrução
  — use-o para fundamentar o status dos critérios de assinatura.
- Se o requisito NÃO trata de assinatura, **não chame** a ferramenta.
- Sua resposta FINAL (após eventuais chamadas de ferramenta) deve ser **apenas**
  o objeto JSON da análise, sem texto fora dele.
```

**`src/motor_ia/analyzers/trabalhadores/prompts.py`** — idem, bloco comentado em l. 37–56,
inserção entre l. ~69 e ~84.

**Bumpar `PROMPT_VERSIONS["base"]`** nos dois arquivos (`trabalhadores-v1.1` → `v1.2`,
e o equivalente em Fornecedores) — o prompt mudou e a rastreabilidade das análises depende
disso.

> **Não** instruir o modelo a economizar chamadas: no Textract cada chamada era paga; agora
> é CPU local. O cap de `_ANALYSER_MAX_TOOL_ROUNDS = 2` e o memo por documento já contêm o
> volume.

---

### Etapa 12 — Testes

#### 12.1 Migrar os testes da PoC (52 testes, sem rede, sem AWS, sem os pesos)

Conferência da migração — 45 coletáveis fora do repo (`test_assinatura.py` precisa do
`pydantic` do motor-ia): `test_nivel0.py` 12 · `test_nivel1.py` 16 · `test_modelo.py` 6 ·
`test_deteccao.py` 11 · `test_assinatura.py` 7.

| Origem | Destino |
|---|---|
| `tool_validar_assinatura/tests/conftest.py` | `tests/extractors/assinatura/conftest.py` |
| `tests/test_nivel0.py` | `tests/extractors/assinatura/test_nivel0.py` |
| `tests/test_nivel1.py` | `tests/extractors/assinatura/test_nivel1.py` |
| `tests/test_modelo.py` | `tests/extractors/assinatura/test_modelo.py` |
| `tests/test_deteccao.py` | `tests/extractors/assinatura/test_deteccao.py` |
| `tests/test_assinatura.py` | `tests/tools/test_assinatura.py` (**substitui** o arquivo hoje 100% comentado) |

Ajustes na migração:

1. **Remover `_preparar_imports()`** do `conftest.py` (l. 17–28) — era o hack de `sys.path`
   para a PoC rodar fora do repo. Dentro do motor-ia o pacote está instalado.
2. Trocar `from nivel1 import ...` por `from motor_ia.extractors.assinatura.nivel1 import ...`
   (e análogos) em todos os arquivos.
3. Criar `tests/extractors/assinatura/__init__.py` (o projeto usa pacotes de teste).
4. **`test_modelo.py` encolhe junto com o módulo**: os testes de download (token, HTTP
   401/403, `.part`, tamanho mínimo) **saem** — o código que eles cobriam não existe mais.
   Sobram três, que são os que valem: caminho explícito inexistente levanta
   `ModeloIndisponivelError`; override por Settings tem precedência sobre o padrão; e o
   `CAMINHO_PADRAO` **existe de verdade** no repositório (`assert CAMINHO_PADRAO.is_file()`)
   — esse último é o teste que pega empacotamento quebrado antes do deploy.

As fixtures de PDF sintético (`pdf_carimbo_digital`, `pdf_formulario`,
`pdf_com_pagina_em_branco`, `pdf_campo_sig_em_branco`, `pdf_campo_sig_assinado`) e a
**injeção de sessão ONNX falsa** (`DetectorAssinaturaOnnx(..., sessao=...)`) migram como
estão — é o que permite testar o Nível 1 e o fallback de ladrilhos sem os 44 MB de pesos.

As duas últimas montam o campo `/Sig` **pelo xref** (`update_object` + `xref_set_key`), e
não por `page.add_widget`: o PyMuPDF 1.24 levanta `AttributeError` ao criar widget de
assinatura. Se a versão do motor-ia for outra, o helper `_novo_pdf_com_campo_sig` do
`conftest.py` continua valendo — ele não depende do `add_widget`. São elas que cobrem o
filtro de §4.2 nos dois sentidos: campo em branco não conta, campo assinado com `/Name`
vazio conta.

#### 12.2 Corrigir os testes de regressão (que hoje afirmam o contrário)

| Arquivo | Teste | Ação |
|---|---|---|
| `tests/analyzers/fornecedores/test_pipeline.py` | `test_assinatura_tool_nunca_exposta_ao_analisador` (l. ~1558) | **Inverter**: a tool passa a ser exposta; `tools_recebidas` tem `tool_defs` nas voltas < cap e `None` na última; `called is True`; `doc.tem_assinatura is True` |
| idem | `test_assinatura_sem_custo_textract_no_usage` (l. ~1596) | **Reescrever**: continua valendo que não há `UsageStep(step="assinatura")` — agora porque **não há custo**, não porque não há detecção. Renomear para `test_assinatura_local_nao_gera_custo_no_usage` |
| idem | `_fake_detect` (l. 1580, 1605) | trocar o alvo do monkeypatch: `motor_ia.tools.assinatura.detect_all_signatures` → `motor_ia.tools.assinatura.detectar_assinaturas_async` |
| `tests/analyzers/fornecedores/test_prompts.py:29` | `assert "validar_assinatura" not in prompt` | **inverter** para `in` |
| `tests/analyzers/trabalhadores/test_prompts.py:35` | `assert "validar_assinatura" not in system` | **inverter** para `in` |
| `tests/analyzers/fornecedores/test_runner_persistence.py:342` | comentário sobre tool desabilitada | atualizar |
| `tests/extractors/test_signatures.py` | `test_detect_all_signatures_*` (l. 72–215) | **deletar** — a função sumiu; cobertura equivalente vem de `test_deteccao.py` |
| `tests/extractors/test_textract.py` | `test_parse_signature_blocks_*`, `test_analyze_signatures_sync_*` (l. 381–488) | **deletar** + imports (l. 13, 17) |
| `tests/tools/test_assinatura.py` | arquivo inteiro comentado | substituído pelo da PoC |

#### 12.3 Cobertura nova a acrescentar

Cenários que hoje não têm teste e que são os que **quebram em produção**:

1. **Pesos ausentes não derrubam a análise** — `resolver_modelo` lança, `detectar_assinaturas`
   captura em `nivel1_erro`, o veredito do Nível 0 sobrevive, e a tool devolve texto com o
   aviso de detecção visual indisponível. (Já coberto por `test_deteccao.py` da PoC —
   confirmar que migrou.)
2. **Memo por documento** — analisador chama a tool 2×, `detectar_assinaturas_async` roda 1×.
3. **`reconcile_assinatura` de ponta a ponta** — pipeline com detecção positiva + LLM
   devolvendo critério de assinatura Inconclusivo ⇒ status final Atendido com o item de
   verificação humana. (`test_reconciliation.py` já testa a função isolada; falta o
   caminho completo.)
4. **`incluir_visual=False`** — só Nível 0, nenhuma sessão ONNX criada.

**E os do desenho de concorrência (§5), que são os que protegem o pior caso:**

5. **`gate.py` — teto respeitado**: com `slots=2`, três `slot_inferencia` concorrentes ⇒ o
   terceiro espera; quando um libera, o terceiro entra.
6. **`gate.py` — timeout degrada, não falha**: `slots=1` já tomado + `timeout_s=0.1` ⇒
   `SlotIndisponivelError`; e em `detectar_assinaturas`, esse erro vira
   `ResultadoDeteccao(nivel0=..., nivel1_erro=...)` com o Nível 0 preservado.
7. **`gate.py` — slot de processo morto é reaproveitado**: subprocesso que toma o slot e é
   morto com SIGKILL ⇒ o próximo `slot_inferencia` adquire. (Prova a escolha do `flock`.)
8. **Subprocesso morto degrada, não propaga**: `subprocess.run` levantando
   `CalledProcessError` (exit != 0, simulando OOM kill) ou `TimeoutExpired` ⇒
   `ResultadoDeteccao` com `nivel0` preservado e `nivel1_erro` preenchido. **É o teste do
   modo de falha mais provável em produção.**
9. **Orçamento cooperativo**: `deadline` já vencido ⇒ `detectar_pdf` devolve
   `truncado=True` sem percorrer página; `truncado` chega ao dict e ao texto do LLM.
10. **Fallback cobre tudo**: documento com página em branco no meio ⇒ `paginas_fallback`
    inclui a branca (contraste com `paginas_alvo`, que a exclui do passe normal); e
    respeita `max_paginas`.
11. **Contrato pai↔filho**: `worker.main` com um pedido válido escreve JSON no stdout que
    reconstrói um `ResultadoNivel1` — o teste que pega quebra de protocolo quando alguém
    mexer num dos dois lados. Sessão ONNX injetada, sem os pesos.

#### 12.4 Rodar

```bash
motor-ia$ make test     # pytest + mypy + ruff check + ruff format --check
```

Ponto de atenção do **mypy strict**: `nivel1.py` usa `cast(Any, _pymupdf)` e sessão ONNX
tipada como `Any` — é o padrão que `extractors/signatures.py` já usa e passa. `numpy` traz
stubs próprios; `onnxruntime` precisa do override da Etapa 1.

---

### Etapa 13 — Smoke local (a validação de ponta a ponta)

O motor-ia já tem os dois scripts — não criar nada novo:

- `examples/fornecedores/smoke_local.py`
- `examples/trabalhadores/smoke_local.py`

Ambos rodam com LLM real, arquivo **local**, `skip_persistence=True` (não grava no Mongo)
e imprimem o `analise_final` + salvam `_last_result.json`.

**13.0 Pré-condições**

```bash
motor-ia$ export ANTHROPIC_API_KEY=...
```

Só isso — os pesos vêm do checkout, nenhuma env de modelo é necessária.

Os PDFs de `examples/pdf/` são colocados lá manualmente por quem for rodar. O que o smoke
prova é o **encanamento** — o analisador chama a tool, o resultado entra no contexto do
LLM, a reconciliação roda, não há custo de Textract, o cache funciona. A qualidade da
detecção já foi validada na PoC e não é o que se mede aqui.

**13.1 Smoke seco do detector (sem LLM, sem custo)** — provar a cascata antes de gastar token:

```bash
motor-ia$ ./.venv/bin/python -c "
import asyncio, json
from motor_ia.extractors.assinatura import detectar_assinaturas_async
r = asyncio.run(detectar_assinaturas_async('examples/pdf/normal/<doc-assinado>.pdf'))
print(json.dumps(r.para_dict(), indent=2, ensure_ascii=False)[:2000])
"
```

Esperado: `tem_assinatura: true`, `fontes` preenchido, e o bloco `nivel1` com
`paginas_analisadas` / `paginas_ladrilhadas` / `tempo_inferencia_ms`.

**13.2 Smoke do agente de Trabalhadores** (é onde a assinatura mais aparece — ficha de EPI,
ASO, ordem de serviço):

1. Editar a seção `EDITAR AQUI` de `examples/trabalhadores/smoke_local.py`:
   - `DOCUMENT_PATHS` → um PDF **local** com rubrica manuscrita escaneada
   - `payload_dict.data.requisito.prompt` → um requisito que exija assinatura
     (ex.: *"A ficha de entrega de EPI deve estar assinada pelo trabalhador"*)
2. Rodar:

```bash
motor-ia$ ./.venv/bin/python examples/trabalhadores/smoke_local.py
```

3. **Critérios de aceite** (o que olhar na saída):

| # | Evidência | Onde |
|---|---|---|
| 1 | O analisador **chamou** a tool | log `trabalhadores.*` + o critério cita a detecção |
| 2 | O bloco `<assinaturas_detectadas>` entrou como `tool_result` | rodar com log `DEBUG` |
| 3 | `documentos_identificados[].tem_assinatura == true` | `_last_result.json` |
| 4 | `assinatura_fontes` e `assinatura_signatarios` preenchidos | idem |
| 5 | Critério de assinatura **Atendido** | `analise_detalhada_criterio` |
| 6 | Se o LLM hesitou: justificativa com `[Assinatura reconciliada por detecção determinística do sistema]` | idem (prova que `reconcile_assinatura` rodou) |
| 7 | **Nenhum** `UsageStep` com `model="textract"` para assinatura | `usage` |
| 8 | Cache: a tool chamada N vezes ⇒ 1 detecção | log `trabalhadores.assinatura_cache_hit` |

**13.3 Smoke do agente de Fornecedores** — mesmo roteiro com
`examples/fornecedores/smoke_local.py` (documento típico: contrato, PGR, PCMSO assinado).

**13.4 Caso negativo (obrigatório)** — rodar com um documento **sem** assinatura e um
requisito que a exija. Esperado: `tem_assinatura: false`, critério **Não Atendido**, e
`reconcile_assinatura` **não** dispara. Sem esse teste não dá para afirmar que a
reconciliação não promove tudo.

Se houver um à mão, o documento que cobre duas coisas de uma vez é um **formulário com
campo `/Sig` em branco** (o "assine aqui" de um envelope não concluído): antes do filtro
de §4.2 ele voltava `tem_assinatura: true` sem ninguém ter assinado. Um PDF sem nenhum
campo de assinatura serve igualmente para o caso negativo.

**13.5 Caso de degradação (obrigatório)** — apontar o override para um caminho inexistente
e rodar de novo:

```bash
motor-ia$ ExtractorConfiguration__SignatureModelPath=/nao/existe.onnx \
    ./.venv/bin/python examples/trabalhadores/smoke_local.py
```

Esperado: a análise **conclui**, o Nível 0 decide sozinho, e o bloco para o LLM traz
`- Aviso: detecção visual indisponível (...)`. Detecção indisponível ≠ "não tem assinatura".

Com os pesos no repositório esse cenário deixa de ser provável em produção, mas o caminho
de degradação continua tendo de funcionar — ele também cobre falha de carga da sessão ONNX
(memória, wheel incompatível com a arquitetura do pod).

**13.6 As constantes de calibração — conferir no diff, não no lote**

O lote da PoC **não** é reprocessado: a validação da migração é a execução real da
pipeline mais os smokes acima. Isso deixa um ponto cego pequeno e conhecido — uma
constante de calibração que se perca na cópia não quebra teste unitário e não aparece no
smoke de um documento só. Conferir no diff dos arquivos movidos, item a item:

| Constante | Valor | Onde |
|---|---|---|
| `CONFIANCA_MINIMA` | `0.15` | `nivel1.py` |
| Reamostragem do render | `Image.Resampling.LANCZOS` | `nivel1.py` |
| Fallback de ladrilhos | 3×3, 15% de sobreposição, 300 DPI | `nivel1.py` |
| Triagem de tinta | 72 DPI, limiar de pixel 200, densidade 0,0005 | `nivel0.py` |
| IoU do NMS | `IOU_MAXIMO` | `nivel1.py` |

São os cinco lugares onde um "arredondamento" silencioso muda o veredito sem quebrar
nada. O `git diff` dos arquivos copiados tem de mostrar **só linha de import**.

**13.7 Teste de carga do portão (§5.8.4) — o teste que reproduz o cenário real**

```bash
motor-ia$ ./.venv/bin/python examples/extractors/carga_assinatura.py \
    --processos 20 --documento examples/pdf/<doc-escaneado-30-paginas>.pdf
```

Critérios de aceite:

| # | O quê |
|---|---|
| 1 | RSS agregado no patamar de **2 sessões**, não 20 (é a prova do desenho) |
| 2 | Nenhum processo morto por OOM |
| 3 | Processos que não pegaram slot **degradam com aviso**, não falham |
| 4 | Nenhum slot preso ao fim (matar um processo no meio e conferir que o slot volta) |
| 5 | RSS volta ao baseline depois do lote, e **nenhum processo de job cresce** (a memória da detecção vive e morre nos filhos) |

Rodar preferencialmente num container com os mesmos limites do alvo
(`docker run --memory=4g --cpus=2`), não na máquina do dev — o resultado numa workstation
de 16 núcleos e 32 GB não diz nada sobre Fargate.

---

### Etapa 14 — Deploy

1. **Build**: nada a fazer. Sem `build-arg`, sem secret, sem mudança no `.gitlab-ci.yml` —
   os pesos entram pelo `COPY .../src` e pelo wheel.
2. **Sanidade da imagem** (o único passo novo de deploy, e vale para os dois targets):
   ```bash
   docker run --rm <imagem> python -c "
   from motor_ia.extractors.assinatura.modelo import resolver_modelo
   p = resolver_modelo(); print(p, p.stat().st_size)"
   ```
   Tem de imprimir um caminho dentro do `site-packages` e ~44.000.000 bytes. É a
   verificação que pega wheel construído sem o `.onnx` — o único modo de falha que a
   decisão de commitar os pesos deixa em aberto.
3. **Recursos do pod (worker)** — ver **§5**. Os defaults do `ExtractorSettings` já são os
   do alvo (2 vCPU / 4 GB / 20 jobs por container): `max_concurrent=2`, `onnx_threads=1`,
   `budget_s=600`, `slot_timeout_s=600`, `max_paginas=100`. **Não é preciso configurar
   nada no deploy** — as envs existem para ajustar depois da medição, não para fazer
   funcionar. Isso é deliberado: default seguro vale mais que default rápido quando o
   modo de falha é OOM.
4. **Não subir `WorkerConfiguration__Count` nem memória da task** por causa desta feature.
   O consumo dela é O(`max_concurrent`), não O(`worker_count`) — §5.2. Se a fila de
   assinatura virar gargalo medido, o caminho é §5.9, nesta ordem.
5. **Observabilidade**: os logs estruturados já saem — `poc_assinatura.nivel1_sessao_carregada`,
   `poc_assinatura.nivel1_indisponivel`, `<agent>.assinatura_cache_hit`,
   `assinatura.detect_failed`. **Renomear o prefixo `poc_assinatura.` para `assinatura.`**
   nos dois módulos migrados (`nivel1.py`, `deteccao.py`) — "poc" não faz sentido em produção.
6. **Rollback**: `ExtractorConfiguration__...` não desliga a tool. O kill-switch é
   `detectar_assinaturas=False` no `PipelineConfig` (por runner) ou tirar
   `assinatura_tool=` do config — volta ao comportamento de hoje sem redeploy de imagem.

---

## 5. Estabilidade, disponibilidade e escalabilidade

**Alvo dimensionante, fixo e inegociável:** 2 tasks Fargate de **2 vCPU / 4 GB**, com
**20–40 jobs simultâneos no total** — o plano dimensiona pelo limite superior, **20 por
container**. Todos de análise documental de Fornecedores ou Trabalhadores, todos com
requisito que exige assinatura, acervo variado com documentos grandes. A máquina não muda.

> **Uma ressalva que precisa estar escrita:** o container **já apresenta OOM hoje**, com
> vários processos em documentos grandes, sem esta feature. Isso muda a régua de duas
> formas. Primeira: qualquer memória residente que a detecção adicione ao processo do job
> é inaceitável — por isso a inferência vai para subprocesso (§4.3a), e não porque seja
> elegante. Segunda: rolar isto sobre um pod já instável significa que o próximo incidente
> vai ser atribuído aqui, com ou sem razão. Recomendo uma **Etapa 0**: capturar o RSS por
> processo sob carga antes de mexer, para haver linha de base. Não é bloqueante para
> implementar — é bloqueante para diagnosticar depois.

### 5.1 A conta que não fecha no desenho ingênuo

`workers/lifespan.py` chama `GreenlegisJobs.start(number_of_workers=...)`. Em
`background_jobs/main.py:495`, `_assign_jobs_to_workers` cria **um
`multiprocessing.Process` novo por job**, até o teto, e o processo morre no fim do job.

Com o desenho da PoC (`@lru_cache` na sessão ONNX, sem teto de concorrência):

| | Por processo | × 20 no container |
|---|---|---|
| Sessão ONNX (44 MB de pesos + estruturas do ORT) | ~120 MB | **~2,4 GB** |
| Raster/tensor em voo | ~30 MB | ~600 MB |
| **Só da detecção** | ~150 MB | **~3,0 GB** |

Em 4 GB que já estouram hoje. Não é risco a monitorar, é aritmética. Precedente direto: a
PoC **removeu** o pool de processos exatamente por isso (`README.md`, "Um documento por
vez, de propósito").

O erro do desenho ingênuo é tratar a sessão como recurso de processo, quando o processo é
efêmero e numeroso. **A sessão tem de ser recurso do container, e a memória tem de voltar
por término de processo — não por boa vontade do alocador.**

### 5.2 O desenho: consumo O(slots), memória devolvida pelo SO

| Mecanismo | Onde | Garante |
|---|---|---|
| **Portão de slots** (`fcntl.flock`, K=2) | `assinatura/gate.py` (§4.4-bis) | no máximo K inferências **no container**, tenha ele 20 ou 200 jobs |
| **Inferência em subprocesso** | `assinatura/worker.py` (§4.4-ter) | zero memória residente no processo do job; devolução garantida por término |
| **Timeout duro do `subprocess.run`** | `deteccao._inferir_em_subprocesso` | nenhum documento segura slot além do orçamento, nem travado dentro do C++ |
| **Prazo cooperativo no filho** | `nivel1.detectar_pdf(deadline=...)` | devolve o parcial e marca `truncado` antes do muro rígido |

| | Desenho ingênuo | Este desenho |
|---|---|---|
| Sessões vivas no pico | 20 | **2** |
| Memória da feature no pico | ~3,0 GB residentes | **~300 MB transitórios** |
| Cresce com o número de jobs? | linearmente | **não** |
| Memória volta? | se `gc` + `glibc` colaborarem | **sempre** (término de processo) |
| OOM killer escolhe a detecção | mata o job inteiro | mata um filho de 150 MB; **o job degrada e segue** |
| Worker morto deixa slot preso? | — | não (kernel libera o `flock`) |

O último item é o que mais vale num pod que já estoura: o OOM deixa de ser perda de
análise e vira degradação de um critério.

### 5.3 "Mas você não disse para carregar uma vez só?"

Sim — a questão é *uma vez por quê*, e a resposta é ditada pelo ciclo de vida do processo.

| Escopo | Carrega | Por quê |
|---|---|---|
| Dentro de **um documento** (todas as páginas + os 9 ladrilhos de cada) | **1×** | é onde estão as centenas de inferências — recarregar por página seria absurdo |
| Entre **chunks, voltas de tool-calling e retries** do mesmo documento | **1×** | o memo `assinatura_cache` (Etapa 9.3) impede redetectar o mesmo arquivo — eram até 6× no desenho antigo |
| Entre documentos do mesmo job | 1× por documento (~1–1,5 s) | preço do isolamento de memória |
| Entre jobs | não se aplica | o processo do job morre no fim de qualquer forma |

Paga-se ~1–1,5 s por documento (≈3% de uma análise de 30–60 s). Compra-se um teto de
memória que independe de quantos jobs a liderança resolver subir, num box que já estoura.

### 5.4 O custo real do fallback em todas as páginas — medido, não estimado

A área definiu: **o fallback varre o documento inteiro, inclusive páginas em branco**. A
justificativa é sólida — a tool só roda quando o requisito exige assinatura, e o fallback
só dispara quando nada foi achado nos dois níveis; nesse ponto a pergunta é "olhei tudo?".

Medi o custo sobre os **18.082 documentos que realmente caíram no fallback** no acervo de
72k, assumindo ~300 ms por inferência (1 thread, hardware alvo):

| Percentil | Páginas | Inferências (×9) | Tempo segurando o slot |
|---|---|---|---|
| p50 | 2 | 18 | **5 s** |
| p90 | 3 | 27 | 8 s |
| p95 | 4 | 36 | 11 s |
| p99 | 11 | 99 | 30 s |
| p99,9 | 67 | 603 | 181 s |
| máximo | 111 | 999 | **5 min** |

**A varredura completa é barata**, porque o acervo é curto: p50 de 2 páginas, p95 de 6,
apenas 0,3% acima de 30 páginas. O medo de "270 inferências por documento" era um p99,9,
não o caso comum.

E o teto de páginas quase não corta:

| `max_paginas` | Inferências no lote | Documentos truncados (de 18.082) |
|---|---|---|
| 30 | 346.743 | 51 |
| 100 | 362.196 | **4** |
| sem teto | 362.592 | 0 |

Subir de 30 para **100** custa **+4,5%** e leva os truncados de 51 para 4. É o default
escolhido: um teto que existe para conter o patológico, não para cortar o normal.

> **Ressalva honesta:** você disse que produção tem mais documento grande que este acervo.
> Se o p95 real for de 20 páginas em vez de 6, o tempo de slot no p95 vai de 11 s para
> ~54 s. O desenho aguenta (o portão bounda o total, o orçamento bounda o individual), mas
> a fila de slots fica mais quente. É o primeiro número a olhar depois do rollout — §5.8.

### 5.5 O envelope completo, no pior caso

| Recurso | Pior caso | Teto |
|---|---|---|
| Memória da detecção | 2 subprocessos × ~150 MB | **~300 MB transitórios**, constante no nº de jobs |
| Memória residente no processo do job | — | **zero** |
| CPU da detecção | 2 threads de inferência | **2**, constante no nº de jobs |
| Relógio por documento | `signature_budget_s` | **600 s**, depois trunca (p99,9 real: 181 s) |
| Espera por slot | `signature_slot_timeout_s` | **600 s**, depois degrada para Nível 0 |
| Acréscimo ao job, pior caso | espera + orçamento | ≤ 20 min por documento, contra timeout de **30 min** |
| Disco temporário | 1 PDF por processo em `/tmp` | limpo no `finally` de `local_source_path` |

Nada nessa tabela cresce com o número de jobs. **É a propriedade que responde à pergunta.**

**Sobre os prazos generosos:** com job timeout de 30 min (`workers/queues.py:22`) e
`repeat_limite=3`, esperar por slot é quase sempre melhor que degradar — degradar manda o
critério para revisão humana, esperar não custa nada além de relógio. Por isso
`slot_timeout_s=600` e não 30: o timeout existe para o starvation patológico, não para o
regime normal.

### 5.6 Disponibilidade — os cinco modos de degradação

Princípio, que já é o do código existente: **a detecção nunca derruba a análise, e "não
consegui verificar" nunca vira "não tem assinatura".**

| Situação | O que acontece | O LLM recebe |
|---|---|---|
| Container saturado (sem slot em 10 min) | veredito do Nível 0 | `- Aviso: detecção visual indisponível (...)` |
| Documento estourou o orçamento | parcial + `truncado=True` | achados parciais + aviso de varredura incompleta |
| **Subprocesso morto pelo OOM killer** | exit code != 0 → Nível 0 | aviso de indisponibilidade; **o job sobrevive** |
| Pesos ausentes / sessão não carrega | `nivel1_erro` → Nível 0 | aviso de indisponibilidade |
| Anexo inacessível no S3 | mensagem clara da tool | "evidência indisponível" |

Em nenhum deles o job falha, e em nenhum `reconcile_assinatura` promove critério (ele só
age com `tem_assinatura is True`). O pior resultado é um critério de assinatura indo para
verificação humana — o comportamento correto.

**Uma linha de código a mais:** `truncado` precisa aparecer em `format_signature_context`,
senão documento não varrido inteiro parece documento sem assinatura:

```python
    if result.get("truncado"):
        linhas.append(
            "- Aviso: a varredura visual não cobriu o documento inteiro (limite de "
            "tempo); ausência de detecção NÃO é evidência de ausência de assinatura."
        )
```

### 5.7 Armadilhas — o que não fazer

| Tentação | Por que quebra |
|---|---|
| Warmup da sessão no `worker_lifespan` | O lifespan roda no **pai**; os jobs são filhos por `fork`. O thread pool do ONNX Runtime **não sobrevive ao `fork`** — falha por deadlock silencioso no primeiro `run()`, não por exceção. |
| Trazer a sessão de volta para dentro do job "porque subprocesso é lento" | §5.1 = OOM, num pod que já estoura. O ganho é ~3% do tempo do job; a perda é a análise inteira. |
| Semáforo em Mongo / arquivo-contador | Worker morto por OOM ou SIGKILL deixa o slot preso para sempre. `flock` é liberado pelo kernel. |
| Importar `motor_ia.config`, boto3 ou o pipeline dentro de `worker.py` | Quadruplica o startup do filho, que é pago por documento. Os parâmetros chegam prontos no argv. |
| Paralelizar páginas dentro do filho | O portão limita processos, não threads internas. Fura o teto de CPU e não acelera em 2 vCPU. |
| Subir `signature_max_concurrent` acima de 2 em 2 vCPU | Não aumenta vazão (a caixa já satura com 2 threads), só multiplica memória e disputa. |

### 5.8 O que medir — antes e depois do rollout

**Antes (gate de merge):**

1. **RSS de um subprocesso de inferência** em pico. Valida os ~150 MB e, portanto, K=2.
2. **Startup do filho** (`python -m ...worker` num PDF de 1 página). Valida o ~1–1,5 s. Se
   passar de ~3 s, o processo detector de vida longa (§4.3a) deixa de ser opcional.
3. **Latência do pior caso no hardware alvo** — documento de 30+ páginas sem assinatura,
   `onnx_threads=1`, em `docker run --memory=4g --cpus=2`. Valida `budget_s=600`.
4. **Teste de carga com 20 processos** (§13.7). O único que prova o desenho.

**Depois (primeiras semanas):**

5. **Distribuição real de páginas** dos documentos que caem no fallback em produção — é a
   ressalva de §5.4. Se o p95 for muito acima de 6 páginas, revisitar `max_paginas`.
6. **Taxa de degradação por slot** (`assinatura.slot_indisponivel`). Se for além de ~1%,
   o portão está apertado: nesse caso, fila dedicada antes de subir K.
7. **RSS do container** contra a linha de base da Etapa 0 — para saber o que esta feature
   custou de verdade, e não por dedução.

### 5.9 Escalabilidade — como isso cresce daqui

| Dimensão | Comportamento |
|---|---|
| **Mais jobs** (20 → 40 por container) | Memória e CPU da detecção **não mudam** — muda a fila de slots. Fica mais lento, nunca instável. Degradação graciosa é o comportamento certo sob pressão. |
| **Mais tasks Fargate** | Linear e perfeita: teto por container, sem estado compartilhado, sem serviço central. |
| **Volume de documentos** | Custo constante em US$ 0,00. Sem quota, sem throttling — a diferença estrutural para o Textract. |
| **Documentos maiores** | Achatado por `max_paginas=100` e `budget_s=600`. Um PDF de 2.000 páginas custa o mesmo que um de 100. |
| **Se a detecção virar gargalo medido** | Nesta ordem: (1) processo detector de vida longa por container, que zera o startup e mantém o isolamento; (2) task dedicada à fila de fortra via `WorkerConfiguration__ActiveQueues`; (3) subir K **só** se as vCPUs subirem junto. Nunca (3) sozinho em 2 vCPU. |
## 6. Checklist final

**Preparação**
- [ ] `onnxruntime` + `numpy` no `pyproject.toml`, `uv.lock` atualizado
- [ ] override de mypy para `onnxruntime`
- [ ] `.onnx` commitado em `src/motor_ia/extractors/assinatura/models/`
- [ ] `uv build` + `unzip -l dist/*.whl | grep onnx` confirma o arquivo no wheel
- [ ] `.dockerignore` da **raiz do contexto de build** não exclui o `.onnx`
- [ ] 3 campos novos em `ExtractorSettings` + `.env.example`

**Código novo**
- [ ] `src/motor_ia/extractors/assinatura/{__init__,nivel0,nivel1,modelo,deteccao}.py`
- [ ] `nivel1.py`: LANCZOS, `CONFIANCA_MINIMA=0.15`, arena desligada — **preservados**
- [ ] fallback de ladrilhos (3×3, 15% de sobreposição, 300 DPI) — **preservado**
- [ ] `tools/assinatura.py` substituído, sem nenhum `raise Exception("Desativado.")`

**Desenho de concorrência (§5) — é o que faz caber em 2 vCPU / 4 GB com 20 jobs**
- [ ] `assinatura/gate.py` — portão de slots por `fcntl.flock`, com fallback no Windows
- [ ] `assinatura/worker.py` — subprocesso de inferência, sem importar `motor_ia.config`
- [ ] `_inferir_em_subprocesso` com timeout duro = orçamento + margem
- [ ] `@lru_cache`/`obter_detector` **removidos** (não migram — §4.3a)
- [ ] `deadline` em `detectar_pdf` + `truncado` em `ResultadoNivel1` → dict → texto do LLM
- [ ] fallback varrendo **todas** as páginas, inclusive em branco (§4.5)
- [ ] `ResultadoNivel0.total_paginas`
- [ ] aviso de truncamento em `format_signature_context` (§5.6)
- [ ] **nenhum** warmup no `worker_lifespan` (§5.7 — deadlock por `fork`)
- [ ] `examples/extractors/carga_assinatura.py`

**Código removido**
- [ ] `extractors/textract.py` linhas 402–783 + 2 constantes
- [ ] `extractors/signatures.py` enxugado (8 símbolos fora)
- [ ] `UsageStep(step="assinatura", model="textract")` fora da pipeline
- [ ] `grep -rn "detect_all_signatures\|detect_signatures\|parse_signature_blocks" src` volta vazio

**Religação**
- [ ] `tools/__init__.py` — import, `__all__`, `default_tools`
- [ ] `tools/catalog.py` — entrada descomentada
- [ ] `pipeline.py` — imports, `_collect_with_tools` (tool + bloco de execução), `assinatura_cache` em 4 métodos
- [ ] `fornecedores/runner.py` — `assinatura_tool=ValidarAssinaturaTool()`
- [ ] `trabalhadores/runner.py` — `assinatura_tool=ValidarAssinaturaTool()`
- [ ] `fornecedores/prompts.py` + `trabalhadores/prompts.py` — seção `# Ferramentas` + bump de versão

**Verificação**
- [ ] 52 testes da PoC migrados e verdes (menos os de download em `test_modelo.py`)
- [ ] `test_nivel0.py`: os 3 do campo `/Sig` (em branco não conta; assinado com `/Name`
      vazio conta; `somente_assinados` puro) — são o filtro de §4.2
- [ ] teste novo: `assert CAMINHO_PADRAO.is_file()` (pega empacotamento quebrado)
- [ ] `docker run ... resolver_modelo()` imprime caminho + ~44 MB
- [ ] 8 testes de regressão corrigidos/deletados
- [ ] `make test` limpo (pytest + mypy + ruff)
- [ ] smoke Trabalhadores: 8 critérios de aceite
- [ ] smoke Fornecedores: 8 critérios de aceite
- [ ] caso negativo (documento sem assinatura)
- [ ] caso de degradação (sem os pesos)
- [ ] logs renomeados de `poc_assinatura.` para `assinatura.`

**Etapa 0 — antes de escrever código**
- [ ] linha de base: RSS por processo de job e do container sob carga alta, hoje
      (§5 — o pod já dá OOM; sem baseline não há como atribuir o próximo incidente)

**Medições obrigatórias antes do rollout (§5.8) — sem elas os defaults são estimativa**
- [ ] RSS de pico de um subprocesso de inferência (valida ~150 MB/slot e, portanto, K=2)
- [ ] startup do filho (`python -m ...worker`) — **acima de ~3 s, ativar o detector de
      vida longa** (§4.3a)
- [ ] latência do pior caso (30+ páginas, sem assinatura, 1 thread) **no hardware alvo**
- [ ] **teste de carga com 20 processos** (§13.7), em `docker run --memory=4g --cpus=2`,
      com os 5 critérios de aceite
- [ ] constantes de calibração conferidas no diff (§13.6) — o `git diff` dos arquivos
      copiados mostra só linha de import

---

## 7. Riscos

| Risco | Impacto | Mitigação |
|---|---|---|
| **Falso ✅** — a detecção erra para o lado de aprovar (medido na PoC; ver `auditoria/VALIDACAO.md`) | documento sem assinatura aprovado | `reconcile_assinatura` só **eleva** status e sempre registra `VerificacaoHumanaItem`; o critério vai para revisão humana. Monitorar após o rollout. |
| Alguém "corrigir" o filtro do `/Sig` usando o nome do signatário | falso ❌ em massa — a maioria dos PDFs do ICP-Brasil não preenche `/V /Name` | O critério está no docstring de `campos_assinados()` e coberto por teste (`test_campo_assinado_sem_nome_continua_valendo`). Quem mexer sem ler quebra o teste. |
| Wheel construído sem o `.onnx` | Nível 1 nunca roda: só o Nível 0 decide, e todo documento escaneado com rubrica de caneta vira ❌ **sem erro visível** | Único modo de falha que sobrou dos pesos. Três redes: teste `CAMINHO_PADRAO.is_file()` (Etapa 12.3), `docker run resolver_modelo()` (Etapa 14.2), e alarme no log `assinatura.nivel1_indisponivel` |
| Repositório +44 MB de blob binário permanente | clone/CI um pouco mais pesados; cada troca de pesos soma outros 44 MB ao histórico para sempre | Aceito. Se a troca de pesos virar rotina, migrar para bucket interno — a seam já existe (`resolver_modelo` + o override por Settings) |
| **O container já dá OOM hoje**, sem esta feature | qualquer incidente pós-rollout será atribuído aqui, com ou sem razão | **Etapa 0**: capturar RSS por processo sob carga antes de mexer (§5). Não bloqueia implementar; bloqueia diagnosticar depois. O desenho de §5.2 é a resposta técnica: a feature adiciona **zero** memória residente ao job. |
| 20 sessões ONNX residentes em 4 GB (desenho ingênuo) | **OOM garantido**, ~3,0 GB só da detecção | Resolvido por desenho: portão de slots + inferência em subprocesso (§5.2). Consumo O(K), ~300 MB transitórios. **Validar com o teste de carga §13.7** — item que não pode ser pulado. |
| Memória não voltar ao SO | teto de §5.2 vira teoria | **Eliminado por construção**: o subprocesso morre, o SO reap. Não depende de `gc` nem do alocador da libc — que era a aposta do desenho anterior. |
| Startup do subprocesso pesar mais que o previsto | +3% vira +15% no tempo do job | §5.8.2 mede. Acima de ~3 s, o processo detector de vida longa (§4.3a) deixa de ser opcional — desenho já esboçado, é troca de mecanismo, não de arquitetura. |
| Acervo de produção com documentos bem maiores que o da PoC | slot segurado por mais tempo; fila de detecção esquenta | Ressalva explícita em §5.4. Portão bounda o total, orçamento bounda o individual — o sistema aguenta e fica mais lento, não instável. Primeiro número a olhar pós-rollout (§5.8.5). |
| ONNX pegando todos os núcleos × 20 processos | thrashing; degrada **todos** os jobs, não só os de assinatura | `signature_onnx_threads=1` como **default**, não como sugestão de deploy (§5.4) |
| Warmup da sessão no `worker_lifespan` (pai) | **deadlock silencioso** — thread pool do ORT não sobrevive ao `fork` | §5.7 — documentado como armadilha por ser o reflexo de quem for "otimizar" a carga depois |
| Slot preso por worker morto (OOM, SIGKILL, deploy) | feature morre no container até reiniciar | `flock` é liberado pelo kernel. Coberto por teste (§12.3.7) |
| Documento patológico segurando slot | fila de detecção trava | `budget_s=600` + timeout duro do `subprocess.run`. Medido: p99,9 = 181 s, máximo = 5 min (§5.4) |
| Latência: +974 ms/doc de média +1–1,5 s de startup por documento | análise mais lenta | ~3% de uma análise de 30–60 s, contra timeout de job de 30 min. Medir no hardware alvo (§5.8.3), não na workstation |
| Perda de `quem_assinou` (o Textract lia o texto ao lado da rubrica) | `signatarios` só vem do carimbo digital (Nível 0) | Já é assim na PoC e o campo é opcional em todo o caminho. Documentar. |
| LLM chamando a tool com frequência maior que o esperado | CPU do worker | `_ANALYSER_MAX_TOOL_ROUNDS = 2` (já existe) + memo por documento |
| Contagem por rubrica não é 100% (rubrica fina em página muito branca pode escapar) | irrelevante para "tem assinatura?"; relevante se algum requisito exigir **contar** signatários | Fora do escopo. O fallback de ladrilhos já é a mitigação. |

---

## 8. O que este plano NÃO faz

- **Não** trata `.docx`/`.pptx`/imagem/`.zip` na tool (D3) — a adaptação de formato existe em
  `poc-assinatura/processamento/documentos.py` e fica como follow-up. A tool antiga também só tratava PDF.
- **Não** migra o processamento em lote (`processar.py`) nem a auditoria (`validar.py`) —
  são ferramentas de PoC/calibração e continuam onde estão, disponíveis para qualquer
  recalibração futura.
- **Não reprocessa o lote da PoC** como gate da migração (§13.6). A validação pós-
  implementação é a execução real da pipeline e o smoke local.
- **Não repete os números da PoC** (acurácia, precisão, recall, custo evitado). Eles
  autorizaram a substituição e vivem em `poc-assinatura/auditoria/CARD.md` e
  `VALIDACAO.md`; aqui só entram medições que **dimensionam** algo (RSS por slot,
  latência, startup do subprocesso — §5).
- **Não leva nenhum documento do acervo da PoC para o motor-ia.** `files/` é dado real de
  trabalhador, existe só na máquina de quem rodou o lote (`.gitignore`) e serviu para
  medir a tool, não para alimentá-la. O que atravessa são **código, testes com PDF
  sintético e os pesos `.onnx`** (§1.2) — nada mais. Os `<doc-assinado>` /
  `<doc-escaneado-30-paginas>` dos smokes (§13) são placeholders para PDFs de
  `examples/pdf/` **do motor-ia**, colocados lá manualmente quando houver: se faltar um
  caso, gere um PDF sintético como o `conftest.py` faz — **não** copie de `files/`. Pela
  mesma razão, `auditoria/` fica na PoC.
- **Não** remove a entrada `analyze_signatures` de `pricing.py` (D5).
- **Não** mexe em `extractors/hybrid.py`, `tesseract.py` ou no restante do OCR.
