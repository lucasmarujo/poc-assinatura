"""Fixtures da PoC: PDFs sintéticos e bootstrap de imports.

Os testes rodam sem os pesos ONNX (~44 MB) e sem AWS — o detector visual é
sempre exercitado com uma sessão injetada.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, cast

import pytest

PASTA_POC = Path(__file__).resolve().parents[1]


def _preparar_imports() -> None:
    """`src` do motor-ia + pasta da PoC no `sys.path` (mesma abordagem dos
    scripts em `examples/`)."""
    raiz_motor_ia = PASTA_POC.parents[1]
    for caminho in (raiz_motor_ia / "src", PASTA_POC):
        if str(caminho) not in sys.path:
            sys.path.insert(0, str(caminho))


_preparar_imports()

_TEXTO_CARIMBO = (
    "Documento assinado digitalmente por FULANO DE TAL SILVA\n"
    "conforme MP 2.200-2/2001 (ICP-Brasil)\n"
    "Codigo de validacao: 8F2A-11BC\n"
)

_TEXTO_FORMULARIO = (
    "FICHA DE ENTREGA DE EPI\n"
    "Declaro ter recebido os equipamentos de protecao individual\n"
    "relacionados neste documento e me comprometo a utiliza-los.\n"
    "Assinatura do empregado: ______________________________\n"
)


def _novo_pdf(caminho: Path, paginas: list[str], *, com_tinta: bool = True) -> Path:
    """PDF de teste: uma página por texto em `paginas` (texto vazio = folha em
    branco). `com_tinta` desenha um bloco preto para garantir densidade de tinta
    acima do limiar mesmo em página sem texto (simula digitalização)."""
    import pymupdf as _pymupdf

    pymupdf = cast(Any, _pymupdf)
    doc = pymupdf.open()
    try:
        for texto in paginas:
            pagina = doc.new_page()
            if texto:
                pagina.insert_textbox(
                    pymupdf.Rect(50, 60, 545, 400), texto, fontsize=12, lineheight=1.6
                )
                if com_tinta:
                    pagina.draw_rect(pymupdf.Rect(50, 450, 545, 620), fill=(0.1, 0.1, 0.1))
        doc.save(str(caminho))
    finally:
        doc.close()
    return caminho


def _novo_pdf_com_campo_sig(
    caminho: Path, *, assinado: bool, contents: str = "3082" + "00FF" * 40
) -> Path:
    """PDF com um campo `/Sig` no AcroForm, assinado ou em branco.

    Montado pelo xref e não por `add_widget`: o PyMuPDF 1.24 quebra ao criar
    widget de assinatura, e escrever o objeto direto é o que dá controle sobre
    `/V`, `/ByteRange` e `/Contents` — que é o que o Nível 0 passou a exigir.
    """
    import pymupdf as _pymupdf

    pymupdf = cast(Any, _pymupdf)
    doc = pymupdf.open()
    try:
        pagina = doc.new_page()
        xref_widget = doc.get_new_xref()
        referencia = ""
        if assinado:
            xref_valor = doc.get_new_xref()
            doc.update_object(
                xref_valor,
                "<</Type /Sig /Filter /Adobe.PPKLite /SubFilter /adbe.pkcs7.detached "
                f"/ByteRange [0 100 300 200] /Contents <{contents}>>>",
            )
            referencia = f"/V {xref_valor} 0 R"
        doc.update_object(
            xref_widget,
            "<</Type /Annot /Subtype /Widget /FT /Sig /T (Assinatura1) "
            f"/Rect [50 50 250 120] /F 4 {referencia}>>",
        )
        doc.xref_set_key(pagina.xref, "Annots", f"[{xref_widget} 0 R]")
        doc.xref_set_key(
            doc.pdf_catalog(), "AcroForm", f"<</Fields [{xref_widget} 0 R] /SigFlags 3>>"
        )
        doc.save(str(caminho))
    finally:
        doc.close()
    return caminho


@pytest.fixture
def pdf_campo_sig_em_branco(tmp_path: Path) -> Path:
    """Formulário com campo de assinatura nunca preenchido (widget sem `/V`)."""
    return _novo_pdf_com_campo_sig(tmp_path / "campo_em_branco.pdf", assinado=False)


@pytest.fixture
def pdf_campo_sig_assinado(tmp_path: Path) -> Path:
    """Campo de assinatura com PKCS#7 e `/Name` ausente — o caso do ICP-Brasil."""
    return _novo_pdf_com_campo_sig(tmp_path / "campo_assinado.pdf", assinado=True)


@pytest.fixture
def pdf_carimbo_digital(tmp_path: Path) -> Path:
    """PDF nativo com carimbo textual de assinatura digital (caso do Nível 0)."""
    return _novo_pdf(tmp_path / "carimbo.pdf", [_TEXTO_CARIMBO])


@pytest.fixture
def pdf_formulario(tmp_path: Path) -> Path:
    """Formulário com rótulo de assinatura, mas sem assinatura alguma."""
    return _novo_pdf(tmp_path / "formulario.pdf", [_TEXTO_FORMULARIO])


@pytest.fixture
def pdf_com_pagina_em_branco(tmp_path: Path) -> Path:
    """Duas páginas: a primeira preenchida, a segunda em branco."""
    return _novo_pdf(tmp_path / "com_branco.pdf", [_TEXTO_FORMULARIO, ""])
