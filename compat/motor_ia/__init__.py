"""Stand-in mínimo do pacote `motor_ia` para esta cópia isolada da PoC.

A tool (`tool_validar_assinatura/`) reaproveita funções do motor-ia por import
absoluto (`motor_ia.extractors.signatures`). Esta pasta existe só porque a cópia
em `poc-assinatura/` roda **fora** do repositório do motor-ia, onde esse pacote
não está instalado.

`compat/` entra no `sys.path` por **append** (ver `processamento/__init__.py`):
com o motor-ia instalado, o pacote real vence e este aqui nunca é importado.
"""
