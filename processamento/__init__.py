"""Plataforma de processamento em lote da tool `validar_assinatura`.

Varre `files/` (subpastas em qualquer profundidade), roda a cascata
Nível 0 → Nível 1 em cada documento num pool de processos e grava os resultados
em JSONL, de onde saem os relatórios.

Módulos:

| Módulo | Papel |
|---|---|
| `documentos.py` | descoberta dos arquivos e adaptação por formato (PDF / imagem / DOCX) |
| `lote.py` | pool de processos, retry, watchdog de travamento e checkpoint |
| `recursos.py` | amostragem de CPU/RAM durante a execução |
| `relatorio.py` | `RESUMO.md`, `RESUMO.json` e `COMPLETO.md` |
"""

from __future__ import annotations

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
PASTA_TOOL = RAIZ / "tool_validar_assinatura"
PASTA_COMPAT = RAIZ / "compat"


def preparar_imports() -> None:
    """Torna a tool importável rodando fora do repositório do motor-ia.

    `tool_validar_assinatura/` entra no início do `sys.path` (os módulos da tool
    se importam por nome: `from deteccao import ...`). `compat/` entra no **fim**,
    de propósito: onde o `motor_ia` de verdade estiver instalado, é ele que vence.

    Roda no import do pacote porque cada worker do pool (spawn, no Windows)
    reimporta tudo do zero.
    """
    if str(PASTA_TOOL) not in sys.path:
        sys.path.insert(0, str(PASTA_TOOL))
    if str(PASTA_COMPAT) not in sys.path:
        sys.path.append(str(PASTA_COMPAT))


preparar_imports()
