"""Auditoria de acerto do lote: amostra estratificada, rótulo humano, métricas.

O checkpoint diz o que a tool **decidiu**; não diz se ela **acertou**. Sem rótulo
humano não existe acurácia — qualquer número que a própria tool calcule é a mesma
opinião que se quer auditar. Este pacote cria o rótulo do jeito mais barato que
continua sendo estatística honesta:

| Módulo | Papel |
|---|---|
| `amostra.py` | sha256, estratos de risco, sorteio com semente e folha de contato para rotular |
| `rotulagem.py` | rotulador local (`http.server` + página com botões), gravando a cada clique |
| `metricas.py` | matriz de confusão ponderada, intervalo de Wilson, `VALIDACAO.md` |

Duas ideias sustentam o custo.

**A unidade de rotulagem é o conteúdo, não o arquivo.** Medido no lote real: os
15.913 documentos ❌ são apenas 3.423 sha256 distintos (4,7× de duplicação — o
mesmo PDF repetido em dezenas de pastas de trabalhador, mais os `_1`/`_2`). 476
decisões humanas cobrem metade daquele universo; 3.423 o cobrem inteiro. O
rótulo é gravado por hash e reaproveitado por todas as cópias.

**A unidade de contagem continua sendo o documento.** O sorteio é feito sobre
documentos, dentro de estratos, e depois reponderado pelo tamanho de cada
estrato. Assim a métrica responde "quanto do lote de 72 mil está certo" sem que
um arquivo repetido 96 vezes domine o número, e sem que oversampling dos
estratos de risco enviese o total.

O par ✅/❌ é o que se mede. Os ⚠️ (erro, formato não suportado, Nível 1
indisponível) ficam fora do denominador e entram no relatório como **cobertura**:
acurácia sem cobertura ao lado é fácil de inflar jogando tudo para ⚠️.

Nada aqui carrega os pesos ONNX — a rotulagem é cega ao que a tool achou, de
propósito, então não há detecção para rodar.
"""

from __future__ import annotations

from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
