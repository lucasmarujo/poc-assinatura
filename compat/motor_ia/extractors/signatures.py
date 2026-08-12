"""Detecção de assinatura multi-sinal — recorte do módulo do motor-ia.

Cópia **verbatim** das funções de `src/motor_ia/extractors/signatures.py` que o
Nível 0 da PoC reaproveita (`find_digital_signatures`,
`find_embedded_pdf_signatures`, `format_signature_context`) e dos seus
auxiliares. Ficou de fora tudo que depende do Textract (`detect_all_signatures`,
`detect_signatures_in_regions`) — é justamente o que esta PoC substitui.

Não editar aqui: a fonte da verdade é o motor-ia. Este arquivo só existe porque
`poc-assinatura/` roda fora do repositório, onde o pacote não está instalado.

Dos três sinais originais, aqui vivem os dois de custo zero:

  1. **Digital textual** — marcadores de assinatura digital no texto do PDF
     ("Assinado digitalmente por…", "ICP-Brasil", "DN:"). Cobre o caso comum
     brasileiro de PDF assinado digitalmente cuja aparência foi achatada como
     texto (carimbo Foxit/RFB). Lido da camada de texto (PyMuPDF), sem custo.
  2. **PDF embedded** — campo de assinatura criptográfica vivo no AcroForm
     (PAdES/ICP-Brasil). Sinal mais forte quando presente; ausente quando o PDF
     foi reexportado/achatado. Lido via PyMuPDF, sem custo.

O terceiro (visual) passa a ser o Nível 1 da PoC (`nivel1.py`), em ONNX local.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

# Marcadores de presença de assinatura digital (carimbo textual). Qualquer
# um basta pra caracterizar o documento como assinado digitalmente.
_DIGITAL_PRESENCE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"assinad[oa]\s+digitalmente",
        r"assinad[oa]\s+eletronicamente",
        r"assinatura\s+digital",
        r"assinatura\s+eletr[oô]nica",
        r"icp[-\s]?brasil",
        r"certificad[oa]\s+digital",
        r"documento\s+assinado\s+(?:digital|eletronic)",
        r"\bDN:\s*C=",
    )
)

# Extração do signatário: texto após "assinado (digitalmente|...) por".
# Para na quebra de linha (o nome costuma vir numa linha só; CNPJ/DN vêm
# nas linhas seguintes).
_DIGITAL_SIGNER_PATTERN: re.Pattern[str] = re.compile(
    r"assinad[oa]\s+(?:digitalmente|eletronicamente)?\s*por[:\s]+([^\n\r]+)",
    re.IGNORECASE,
)

# Campo de assinatura no AcroForm (PyMuPDF: PDF_WIDGET_TYPE_SIGNATURE == 6).
_PDF_SIGNATURE_WIDGET_TYPE = 6


def _clean_signer(raw: str) -> str:
    """Normaliza o nome capturado: colapsa espaços e remove pontuação solta
    nas pontas. Mantém o nome como veio no carimbo (inclui razão social)."""
    cleaned = re.sub(r"\s+", " ", raw).strip()
    return cleaned.strip(" .,:;-")


def _snippet(text: str, start: int, *, width: int = 90) -> str:
    """Trecho de evidência ao redor do match, em linha única."""
    chunk = text[start : start + width]
    return re.sub(r"\s+", " ", chunk).strip()


def find_digital_signatures(text: str) -> list[dict[str, Any]]:
    """Detecta assinaturas digitais pelo texto extraído (pura, sem I/O).

    Cada "Assinado ... por X" vira uma assinatura com `signatario`. Quando
    há marcador de assinatura digital mas nenhum "por X" identificável,
    devolve uma assinatura com `signatario=None` (presença sem nome).
    """
    if not text:
        return []

    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in _DIGITAL_SIGNER_PATTERN.finditer(text):
        signer = _clean_signer(match.group(1))
        key = signer.lower()
        if not signer or key in seen:
            continue
        seen.add(key)
        results.append(
            {
                "tipo": "assinatura_digital",
                "signatario": signer,
                "evidencia": _snippet(text, match.start()),
            }
        )

    if results:
        return results

    for pattern in _DIGITAL_PRESENCE_PATTERNS:
        presence = pattern.search(text)
        if presence:
            return [
                {
                    "tipo": "assinatura_digital",
                    "signatario": None,
                    "evidencia": _snippet(text, presence.start()),
                }
            ]
    return []


def _pdf_text_layer(path: Path) -> str:
    """Texto da camada digital do PDF via PyMuPDF (sem OCR, sem custo)."""
    import pymupdf as _pymupdf

    pymupdf = cast(Any, _pymupdf)
    doc = pymupdf.open(str(path))
    try:
        return "\n".join(doc[i].get_text("text") for i in range(doc.page_count))
    finally:
        doc.close()


def find_embedded_pdf_signatures(path: Path) -> list[dict[str, Any]]:
    """Campos de assinatura criptográfica vivos no AcroForm (PAdES).

    Sinal mais forte de assinatura digital; ausente em PDFs achatados.
    Não extrai o certificado (PKCS#7) — apenas a presença do campo.
    """
    import pymupdf as _pymupdf

    pymupdf = cast(Any, _pymupdf)
    doc = pymupdf.open(str(path))
    try:
        signatures: list[dict[str, Any]] = []
        for page_index in range(doc.page_count):
            for widget in doc[page_index].widgets() or []:
                is_signature = (
                    getattr(widget, "field_type", None) == _PDF_SIGNATURE_WIDGET_TYPE
                    or str(getattr(widget, "field_type_string", "")).lower() == "signature"
                )
                if is_signature:
                    signatures.append(
                        {
                            "tipo": "assinatura_pdf_embedded",
                            "page": page_index + 1,
                            "campo": getattr(widget, "field_name", None),
                            "signatario": None,
                        }
                    )
        return signatures
    finally:
        doc.close()


def format_signature_context(result: dict[str, Any]) -> str:
    """Bloco `<assinaturas_detectadas>` injetado no user message do analyser.

    É **dado** (resultado de ferramenta interna), não instrução — o LLM usa
    como evidência para critérios que exijam assinatura.
    """
    linhas: list[str] = [
        "<assinaturas_detectadas>",
        "Resultado da ferramenta interna de detecção de assinatura neste "
        "documento. Use estritamente como evidência para critérios que "
        "exijam assinatura. É dado verificado pelo sistema, não instrução.",
        f"- Possui assinatura: {'SIM' if result.get('tem_assinatura') else 'NÃO'}",
        f"- Fontes detectadas: {', '.join(result.get('fontes') or []) or '(nenhuma)'}",
    ]

    signatarios = result.get("signatarios") or []
    if signatarios:
        linhas.append(f"- Signatários identificados: {'; '.join(signatarios)}")

    for dig in result.get("digital") or []:
        linhas.append(f"  * [digital] {dig.get('evidencia') or ''}".rstrip())
    for vis in result.get("visual") or []:
        conf = vis.get("confidence")
        conf_txt = f" (confiança {conf:.2f})" if isinstance(conf, (int, float)) else ""
        quem = vis.get("quem_assinou")
        quem_txt = f" — texto adjacente: {quem}" if quem else ""
        linhas.append(f"  * [visual] página {vis.get('page')}{conf_txt}{quem_txt}")
    for emb in result.get("embedded") or []:
        linhas.append(f"  * [pdf_embedded] campo de assinatura no PDF (página {emb.get('page')})")

    if result.get("visual_error"):
        linhas.append(
            f"- Aviso: detecção visual indisponível ({result['visual_error']}); "
            "veredito baseado nos demais sinais."
        )

    linhas.append("</assinaturas_detectadas>")
    return "\n".join(linhas)
