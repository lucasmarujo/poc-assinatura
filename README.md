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

## Quanto disso está certo? — `validar.py`

O `resultados.jsonl` diz o que a tool **decidiu**; não diz se ela **acertou**.
Acurácia não sai de dentro da tool: qualquer número que ela calcule sobre si
mesma é a mesma opinião que se quer auditar. Precisa de rótulo humano — e
[`validar.py`](validar.py) existe para que ele custe o mínimo possível.

```bash
# 1) sorteia a amostra e gera as imagens a rotular
poc-assinatura$ .venv/Scripts/python validar.py amostrar

# 2) rotule no navegador: S = tem assinatura, N = não tem, D = dúvida
poc-assinatura$ .venv/Scripts/python validar.py rotular

# 3) cruza rótulo × veredito e escreve o relatório
poc-assinatura$ .venv/Scripts/python validar.py avaliar

# 4) abre a triagem: cada erro com a imagem ao lado da evidência da tool
poc-assinatura$ .venv/Scripts/python validar.py erros
```

Aponte `--resultados` para a pasta do lote se ela não for `resultados/`.

**`files/` nunca é escrito.** A imagem que aparece no rotulador é uma folha de
contato **renderizada** em `auditoria/amostra/pendentes/`; rotular apenas a
vincula em `com_assinatura/`, `sem_assinatura/` ou `duvida/` (`os.link`, ou cópia
quando o sistema de arquivos não permite vínculo) sem tirá-la de `pendentes/`.
Nada do seu dataset se move.

### O rotulador (`validar.py rotular`)

Um servidor `http.server` em `127.0.0.1` — só `stdlib`, nada exposto na rede — e
uma página com um documento por vez:

| Tecla | |
|---|---|
| **S** | tem assinatura |
| **N** | não tem |
| **D** | dúvida |
| **←** / **→** | volta / pula sem rotular |
| clique na imagem | alterna entre caber na tela e tamanho natural (para conferir rubrica fina) |

**Cada clique grava na hora**, na pasta e no `rotulos.jsonl`: não há botão de
salvar e não há trabalho para perder se o navegador fechar. Reabrir continua na
primeira imagem sem rótulo, e a barra de progresso diz quantas faltam.

A fila é **embaralhada** de propósito: em ordem de índice os ✅ e os ❌ sairiam em
blocos, e depois de dez seguidos o rotulador passa a adivinhar em vez de olhar. A
página recebe **só o id** da imagem — nem caminho, nem estrato, nem o veredito da
tool. Mudar de ideia é só voltar e apertar outra tecla; a última decisão vence.

Se preferir, mover os arquivos na mão entre as pastas continua funcionando: a
pasta é o rótulo, e é ela que o `avaliar` lê.

### A triagem dos erros (`validar.py erros`)

O `VALIDACAO.md` diz **quantos** erros existem e em que estrato; ele não diz
**qual** foi o erro. `validar.py erros` escreve `auditoria/ERROS.html` — um
arquivo estático, aberto com duplo clique — com um cartão por conteúdo rotulado:
a folha de contato que foi rotulada, o rótulo dado, o veredito da tool e a
evidência que sustentou esse veredito.

| No cartão | |
|---|---|
| Nível 0 — AcroForm `/Sig` | sim/não: assinatura criptográfica embutida no PDF |
| Nível 0 — carimbo digital | sim/não: "Assinado digitalmente por…" na camada de texto |
| Nível 1 | quantas detecções visuais, e quantas páginas foram ao fallback 3×3 |
| Páginas | quantas o documento tem, quantas chegaram ao Nível 1, quantas eram brancas |
| Truncado, erro do Nível 1, observação | o que impediu a tool de olhar o documento inteiro |

O filtro abre em **só erros** (falso ✅ e falso ❌) e dá para restringir por
estrato; "Tudo" traz os acertos para comparar. Cada cartão tem botões de **causa**
— `modelo`, `rotulagem`, `documento`, `rever` —, que é a classificação que separa
"o detector errou" de "eu rotulei errado" de "o documento estava quebrado". A
escolha fica no `localStorage` do navegador e sai pelo botão **Baixar triagem
(JSON)**; nada é gravado em disco, e o `rotulos.jsonl` não é tocado — mudar de
ideia sobre um rótulo é `validar.py rotular`.

### Voltar para o rotulador (`validar.py rotular --rever`)

O JSON da triagem alimenta o rotulador de volta:

```bash
poc-assinatura$ .venv/Scripts/python validar.py rotular --rever triagem-erros.json
poc-assinatura$ .venv/Scripts/python validar.py avaliar
```

A fila passa a ser **só** os documentos com causa `rever` ou `rotulagem` — os que
pedem rótulo novo. `modelo` e `documento` ficam de fora de propósito: neles o
rótulo já foi dado por bom, e trazê-los de volta seria reetiquetar até a tool
concordar. O rótulo novo substitui o antigo (a imagem muda de pasta e a última
linha do `rotulos.jsonl` vence), e `avaliar` recalcula tudo.

> Corrigir rótulo só onde a tool errou **empurra a acurácia para cima sozinho**:
> os rótulos errados que por acaso concordaram com a tool continuam lá, e nunca
> passam por revisão. O número que sai depois disso é um teto, não a mesma
> medida de antes. Para uma correção sem esse viés, o caminho é rever uma fatia
> aleatória da amostra inteira — inclusive acertos — e não só as divergências.

### O sinal do AcroForm (`validar.py acroform`)

O Nível 0 conta a **presença do widget** `/Sig`. Um formulário com "assine aqui"
nunca preenchido tem widget e não tem assinatura — a hipótese de falso positivo
que este comando mede:

```bash
poc-assinatura$ .venv/Scripts/python validar.py acroform --n 100
```

Sorteia `--n` documentos por grupo (AcroForm como **única** evidência × AcroForm
acompanhado de carimbo/Nível 1), um por conteúdo distinto, reabre cada PDF e lê
o que decide:

| No PDF | Significa |
|---|---|
| campo `/FT /Sig` **sem** `/V` | placeholder — campo em branco, falso positivo |
| `/V` com `/ByteRange` e `/Contents` preenchido | assinado: há PKCS#7 no arquivo |
| `/V` sem `/ByteRange` ou `/Contents` vazio | assinatura preparada e não concluída |

**O nome do signatário não decide nada.** `/V /Name` é opcional e vem vazio na
maioria dos PDFs do ICP-Brasil, e `widget.field_value` devolve `""` mesmo em
assinatura válida — concluir "campo vazio" a partir do nome vazio inverte o
diagnóstico. O nome real está dentro do certificado, e o relatório o extrai por
heurística (cadeias em caixa alta do PKCS#7).

Saem `auditoria/ACROFORM.md` (veredito por grupo, projeção do falso positivo
para o lote e o detalhe campo a campo) e `auditoria/ACROFORM.jsonl`. A auditoria
verifica que **existe** assinatura criptográfica, não que ela é **válida** —
cadeia ICP e integridade exigiriam um verificador de certificado.

Os cartões são por **conteúdo** (sha256), não por documento: o mesmo conteúdo
duplicado 96 vezes é uma imagem e uma decisão só, com a contagem de cópias no
cartão. Por isso a soma dos cartões de erro pode ficar abaixo dos erros da matriz
de confusão do `VALIDACAO.md`, que conta documentos.

### A régua, que precisa estar combinada antes de começar

Ambiguidade aqui estraga a métrica mais do que qualquer tamanho de amostra.

| Tecla / pasta | |
|---|---|
| **S** · `com_assinatura` | rubrica manuscrita (caneta, escaneada ou desenhada) em qualquer página, **ou** carimbo de assinatura digital. Rubrica ilegível ou parcial conta |
| **N** · `sem_assinatura` | campo vazio, linha pontilhada, "Assinatura: ______", nome apenas digitado |
| **D** · `duvida` | não deu para decidir — sai do denominador e é reportado à parte |

Se a fração em `duvida/` passar de 5%, o relatório sai com ressalva: nesse
volume, o problema costuma ser a régua ou a resolução da imagem, não a tool.

### O que sai em `auditoria/`

| Arquivo | Conteúdo |
|---|---|
| `VALIDACAO.md` | acurácia, precisão, recall, especificidade, **VPN**, F1 — cada uma com IC 95% de Wilson — matriz de confusão e a tabela de **onde o erro está** |
| `VALIDACAO.json` | o mesmo, para comparar duas execuções |
| `rotulos.jsonl` | o ativo: `sha256` + veredito humano, uma linha por decisão (a última de cada `sha256` vence). **Sem caminho de arquivo**, então é versionável e sobrevive a reorganização de `files/` |
| `indice.jsonl` | a amostra sorteada, documento a documento |
| `hashes.jsonl` | cache de sha256 (incremental — a primeira execução lê o lote inteiro) |
| `amostragem.json` | o desenho da amostra: semente, orçamentos, piso por estrato |
| `amostra/` | as folhas de contato, distribuídas nas quatro pastas de rotulagem |

### Por que a conta fecha com pouco trabalho humano

**Rótulo por conteúdo, não por arquivo.** Medido no lote de 72 mil: os 15.913
documentos ❌ são apenas **3.423 sha256 distintos** — 4,7× de duplicação (o mesmo
PDF repetido em dezenas de pastas de trabalhador, mais os `_1`/`_2`). 476
decisões humanas cobrem metade daquele universo; 3.423 o cobrem inteiro. O
rótulo é gravado por hash e herdado por todas as cópias.

O sha256 é calculado de verdade, não aproximado: o proxy barato
`(tamanho, formato, páginas)` daria 2.891 grupos contra 3.423 hashes reais —
ele **funde arquivos diferentes**, e isso propagaria rótulo errado.

**Contagem por documento, amostra por estrato.** O sorteio é de documentos
(conteúdo repetido 96 vezes tem 96 vezes mais chance de entrar, que é o peso
dele no lote), dentro de estratos tirados de campos que o checkpoint já tem:

| Estrato | Por que existe |
|---|---|
| ❌ truncado em `--max-paginas` | o Nível 1 **não olhou** as páginas restantes — cegueira garantida |
| ❌ todas as páginas em branco | verdadeiro negativo quase certo |
| ❌ `.docx`/`.pptx` | limite conhecido do `w:ink`/EMF |
| ❌ imagem · ❌ PDF de 1, 2, 3+ páginas | volume |
| ✅ resgatado pelo fallback 3×3 | 9 inferências por página, 9× mais chances de disparar → maior risco de falso positivo |
| ✅ só Nível 1, uma única detecção | evidência mais fraca do lote |
| ✅ só Nível 1, duas ou mais · ✅ com Nível 0 | verdadeiro positivo provável |

Estrato pequeno e perigoso recebe um piso de 20 documentos
(`--minimo-por-estrato`), senão receberia 2 na proporcional e não diria nada
sobre si mesmo. Esse oversampling **não** enviesa o total: na hora de somar, as
células de cada estrato são multiplicadas por `N_estrato / rotulados_no_estrato`,
o que reconstrói a matriz do lote inteiro.

**A rotulagem é cega.** A imagem se chama pelo hash, positivos e negativos caem
na mesma pilha e nada indica o que a tool decidiu — rotulador influenciado pelo
veredito não mede nada. Por isso `validar.py` nunca carrega os pesos ONNX: não
há detecção para rodar aqui.

### Quanto amostrar

| n por lado | IC 95% (pior caso, p=0,5) |
|---|---|
| 200 | ±6,9% |
| 400 | ±4,9% |
| 600 | ±3,9% |
| 1.000 | ±3,1% |

O default é 200+200 (~1h de rotulagem) — o suficiente para saber se a PoC está
em 95% ou em 70%. Suba para 600+600 quando o número for para apresentação.
Aumentar o orçamento **preserva** a amostra anterior: a mesma semente embaralha
e corta, então a amostra maior contém a menor e nenhum rótulo já feito se perde.

### O ganho que fica

`rotulos.jsonl` é um golden set permanente, chaveado por conteúdo. Mexeu em
`--conf`, trocou o modelo, desligou o fallback? Rode o lote e
`validar.py avaliar` de novo: precisão e recall se movem contra os **mesmos**
rótulos. É o que responde "essa mudança melhorou?" — hoje não há como saber se
`--conf 0.15` é melhor que `0.25` neste corpus, só que foi melhor em 17
documentos.

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
| [`processar.py`](processar.py) | entrada da linha de comando do lote |
| [`processamento/`](processamento/) | descoberta e adaptação de formato, execução do lote, amostragem de hardware, relatórios |
| [`validar.py`](validar.py) | entrada da linha de comando da auditoria de acurácia |
| [`validacao/`](validacao/) | sha256, estratos, sorteio e folhas de contato (`amostra.py`); rotulador local (`rotulagem.py`); matriz de confusão, Wilson e `VALIDACAO.md` (`metricas.py`); triagem dos erros em `ERROS.html` (`erros.py`); auditoria do campo `/Sig` (`acroform.py`) |
| [`tool_validar_assinatura/`](tool_validar_assinatura/) | a tool de detecção (Nível 0 + Nível 1) |
| [`compat/`](compat/) | recorte das funções do `motor_ia` que a tool importa — existe só porque esta cópia roda fora do repositório do motor-ia. Com o pacote real instalado, é ele que vence |
| [`tests/`](tests/) | testes da plataforma (`.venv/Scripts/python -m pytest tests -q`) |
