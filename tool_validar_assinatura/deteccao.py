"""Orquestração dos níveis de detecção de assinatura (PoC sem Textract).

Cascata deliberada — o nível barato decide primeiro:

    Nível 0 (Python puro, custo zero)
      ├─ achou assinatura digital (/Sig ou carimbo no texto) → devolve
      └─ não achou → Nível 1 (YOLOv8s ONNX em CPU) só nas páginas com tinta
                       └─ nada encontrado no documento inteiro → o mesmo
                          Nível 1 de novo, em ladrilhos 3×3 e DPI maior

Isso mantém o custo por documento em CPU local e concentra a inferência onde
ela é indispensável: PDF escaneado, com rubrica de caneta e sem camada de texto.
Com `escalonar=False` os dois níveis rodam sempre — é o modo que o script de
teste usa para comparar N0 × N1 no mesmo documento.

O resultado é convertido para o **mesmo dicionário** que
`motor_ia.extractors.signatures.detect_all_signatures` devolvia (chaves
`tem_assinatura`/`fontes`/`signatarios`/`visual`/`digital`/`embedded`/
`visual_error`), e formatado com a `format_signature_context` já existente —
o bloco `<assinaturas_detectadas>` que o LLM lê não muda.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
from nivel0 import (
    DENSIDADE_TINTA_MINIMA,
    DPI_TRIAGEM,
    ResultadoNivel0,
    detectar_nivel0,
)
from nivel1 import (
    CONFIANCA_MINIMA,
    DPI_LADRILHO,
    DPI_RENDER,
    IOU_MAXIMO,
    ResultadoNivel1,
    obter_detector,
)

from motor_ia.extractors.signatures import format_signature_context

_logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ResultadoDeteccao:
    """Veredito consolidado dos dois níveis num documento."""

    nivel0: ResultadoNivel0
    nivel1: ResultadoNivel1 | None = None
    nivel1_erro: str | None = None

    @property
    def fontes(self) -> list[str]:
        fontes = list(self.nivel0.fontes)
        if self.nivel1 and self.nivel1.tem_assinatura:
            fontes.append("visual")
        return fontes

    @property
    def tem_assinatura(self) -> bool:
        return bool(self.fontes)

    @property
    def total_assinaturas(self) -> int:
        return self.nivel0.total + (self.nivel1.total if self.nivel1 else 0)

    @property
    def signatarios(self) -> list[str]:
        """Nomes identificados. Só o Nível 0 nomeia signatário (carimbo textual);
        o detector visual localiza a rubrica, não lê quem assinou."""
        nomes: list[str] = []
        vistos: set[str] = set()
        for assinatura in self.nivel0.assinaturas:
            nome = assinatura.get("signatario")
            if not nome or nome.lower() in vistos:
                continue
            vistos.add(nome.lower())
            nomes.append(nome)
        return nomes

    def para_dict(self) -> dict[str, Any]:
        """Formato legado de `detect_all_signatures`, mais o detalhe da PoC."""
        nivel1 = self.nivel1
        return {
            "tem_assinatura": self.tem_assinatura,
            "fontes": self.fontes,
            "signatarios": self.signatarios,
            "visual": nivel1.assinaturas if nivel1 else [],
            "digital": self.nivel0.digital,
            "embedded": self.nivel0.embedded,
            "visual_error": self.nivel1_erro,
            "nivel0": {
                "total": self.nivel0.total,
                "fontes": self.nivel0.fontes,
                "paginas_uteis": self.nivel0.paginas_uteis,
                "paginas_em_branco": self.nivel0.paginas_em_branco,
                "paginas": [
                    {
                        "numero": p.numero,
                        "densidade_tinta": round(p.densidade_tinta, 6),
                        "em_branco": p.em_branco,
                        "tem_rotulo_assinatura": p.tem_rotulo_assinatura,
                    }
                    for p in self.nivel0.paginas
                ],
            },
            "nivel1": (
                {
                    "total": nivel1.total,
                    "modelo": nivel1.modelo,
                    "paginas_analisadas": nivel1.paginas_analisadas,
                    "paginas_ignoradas": nivel1.paginas_ignoradas,
                    "paginas_ladrilhadas": nivel1.paginas_ladrilhadas,
                    "tempo_inferencia_ms": nivel1.tempo_inferencia_ms,
                }
                if nivel1
                else None
            ),
        }


def detectar_assinaturas(
    path: str | Path,
    *,
    texto_extraido: str | None = None,
    executar_nivel1: bool = True,
    escalonar: bool = True,
    modelo_path: str | Path | None = None,
    confianca_minima: float = CONFIANCA_MINIMA,
    iou_maximo: float = IOU_MAXIMO,
    dpi_render: int = DPI_RENDER,
    dpi_triagem: int = DPI_TRIAGEM,
    densidade_minima: float = DENSIDADE_TINTA_MINIMA,
    max_paginas: int | None = None,
    fallback_ladrilhos: bool = True,
    dpi_ladrilho: int = DPI_LADRILHO,
) -> ResultadoDeteccao:
    """Detecta assinaturas num PDF pela cascata Nível 0 → Nível 1 (bloqueante).

    `escalonar=False` força o Nível 1 mesmo quando o Nível 0 já concluiu
    (comparação da PoC). Falha do Nível 1 (pesos ausentes, sessão ONNX) **não**
    derruba o veredito: fica registrada em `nivel1_erro`, como o
    `visual_error` do fluxo antigo.

    `max_paginas` limita quantas páginas aproveitáveis vão ao Nível 1 — teto de
    custo por documento em lote (um PDF de 2.000 páginas não pode monopolizar um
    worker). `None` mantém o comportamento original: todas as páginas.

    `fallback_ladrilhos` refaz o Nível 1 em ladrilhos quando **nenhum** dos dois
    níveis achou nada. O gatilho é dividido: aqui entra a metade do Nível 0 (não
    adianta ladrilhar documento que o carimbo digital já resolveu), e a metade do
    Nível 1 fica dentro de `detectar_pdf`, que é quem sabe se achou algo.
    """
    p = Path(path)
    nivel0 = detectar_nivel0(
        p,
        texto_extraido=texto_extraido,
        dpi_triagem=dpi_triagem,
        densidade_minima=densidade_minima,
    )

    if not executar_nivel1 or (escalonar and nivel0.tem_assinatura):
        return ResultadoDeteccao(nivel0=nivel0)

    paginas_alvo = nivel0.paginas_uteis
    if max_paginas is not None:
        paginas_alvo = paginas_alvo[:max_paginas]

    try:
        detector = obter_detector(modelo_path)
        nivel1 = detector.detectar_pdf(
            p,
            paginas=paginas_alvo,
            confianca_minima=confianca_minima,
            iou_maximo=iou_maximo,
            dpi=dpi_render,
            fallback_ladrilhos=fallback_ladrilhos and not nivel0.tem_assinatura,
            dpi_ladrilho=dpi_ladrilho,
        )
    except Exception as exc:
        _logger.warning(
            "poc_assinatura.nivel1_indisponivel",
            document=str(p),
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return ResultadoDeteccao(nivel0=nivel0, nivel1_erro=str(exc))

    return ResultadoDeteccao(nivel0=nivel0, nivel1=nivel1)


async def detectar_assinaturas_async(path: str | Path, **kwargs: Any) -> ResultadoDeteccao:
    """Versão async: a detecção é CPU-bound, então roda numa thread para não
    travar o event loop da API/worker."""
    return await asyncio.to_thread(lambda: detectar_assinaturas(path, **kwargs))


def formatar_contexto(resultado: ResultadoDeteccao) -> str:
    """Bloco `<assinaturas_detectadas>` para o LLM (formato inalterado)."""
    return format_signature_context(resultado.para_dict())
