"""Fixtures da plataforma de lote: uma árvore de `files/` sintética.

Sem rede, sem os pesos ONNX (~44 MB) e sem processo filho: o Nível 1 é sempre
substituído por um dublê. O que se testa aqui é a plataforma (descoberta,
adaptação de formato, retry, relatório), não o detector — esse já tem seus
testes em `tool_validar_assinatura/tests/`.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path
from typing import Any, cast

import pytest

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import processamento  # noqa: E402,F401  (bootstrap do `sys.path` da tool)

_TEXTO_CARIMBO = (
    "Documento assinado digitalmente por FULANO DE TAL SILVA\n"
    "conforme MP 2.200-2/2001 (ICP-Brasil)\n"
)

_TEXTO_FORMULARIO = (
    "FICHA DE ENTREGA DE EPI\n"
    "Declaro ter recebido os equipamentos de protecao individual.\n"
    "Assinatura do empregado: ______________________________\n"
)

_DOCUMENT_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    "<w:body><w:p><w:r><w:t>{texto}</w:t></w:r></w:p></w:body></w:document>"
)

_SLIDE_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
    "<p:cSld><a:p><a:r><a:t>{texto}</a:t></a:r></a:p></p:cSld></p:sld>"
)


def novo_pdf(caminho: Path, paginas: list[str], *, com_tinta: bool = True) -> Path:
    """PDF de teste: uma página por texto (texto vazio = folha em branco)."""
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
        caminho.parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(caminho))
    finally:
        doc.close()
    return caminho


def nova_imagem(caminho: Path, *, tamanho: tuple[int, int] = (400, 200)) -> Path:
    from PIL import Image, ImageDraw

    caminho.parent.mkdir(parents=True, exist_ok=True)
    imagem = Image.new("RGB", tamanho, "white")
    ImageDraw.Draw(imagem).line([(20, 150), (380, 60)], fill="black", width=4)
    imagem.save(caminho)
    return caminho


def novo_docx(caminho: Path, *, texto: str, imagens: list[Path] | None = None) -> Path:
    """DOCX mínimo: só o `word/document.xml` e o que for para `word/media/`."""
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(caminho, "w") as zip_docx:
        zip_docx.writestr("word/document.xml", _DOCUMENT_XML.format(texto=texto))
        for indice, imagem in enumerate(imagens or [], start=1):
            zip_docx.writestr(f"word/media/image{indice}{imagem.suffix}", imagem.read_bytes())
    return caminho


def novo_pptx(caminho: Path, *, slides: list[str], imagens: list[Path] | None = None) -> Path:
    """PPTX mínimo: um `ppt/slides/slideN.xml` por slide e o que for p/ `ppt/media/`."""
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(caminho, "w") as zip_pptx:
        for indice, texto in enumerate(slides, start=1):
            zip_pptx.writestr(f"ppt/slides/slide{indice}.xml", _SLIDE_XML.format(texto=texto))
        for indice, imagem in enumerate(imagens or [], start=1):
            zip_pptx.writestr(f"ppt/media/image{indice}{imagem.suffix}", imagem.read_bytes())
    return caminho


def novo_zip(caminho: Path, conteudo: dict[str, Path]) -> Path:
    """Zip com os arquivos indicados (`nome dentro do zip` → arquivo em disco)."""
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(caminho, "w") as pacote:
        for nome, origem in conteudo.items():
            pacote.writestr(nome, origem.read_bytes())
    return caminho


@pytest.fixture
def pdf_carimbo_digital(tmp_path: Path) -> Path:
    return novo_pdf(tmp_path / "carimbo.pdf", [_TEXTO_CARIMBO])


@pytest.fixture
def pdf_formulario(tmp_path: Path) -> Path:
    return novo_pdf(tmp_path / "formulario.pdf", [_TEXTO_FORMULARIO])


@pytest.fixture
def arvore_files(tmp_path: Path) -> Path:
    """`files/` como virá na prática: subpastas, formatos variados e lixo."""
    raiz = tmp_path / "files"
    novo_pdf(raiz / "fornecedor-a" / "contrato.pdf", [_TEXTO_CARIMBO])
    novo_pdf(raiz / "fornecedor-a" / "ficha.pdf", [_TEXTO_FORMULARIO, ""])
    novo_pdf(raiz / "fornecedor-b" / "trabalhadores" / "aso.pdf", [_TEXTO_FORMULARIO])
    nova_imagem(raiz / "fornecedor-b" / "rubrica.png")
    novo_docx(
        raiz / "fornecedor-b" / "termo.docx",
        texto="Assinado digitalmente por MARIA SOUZA",
        imagens=[nova_imagem(tmp_path / "assinatura.png")],
    )
    novo_pptx(
        raiz / "fornecedor-b" / "treinamento.pptx",
        slides=["NR-33 Espaco confinado", "Assinado digitalmente por JOAO LIMA"],
        imagens=[nova_imagem(tmp_path / "slide.png")],
    )
    novo_zip(
        raiz / "fornecedor-c" / "lote.zip",
        {
            "interno/contrato-zipado.pdf": novo_pdf(tmp_path / "z1.pdf", [_TEXTO_CARIMBO]),
            "interno/ficha-zipada.pdf": novo_pdf(tmp_path / "z2.pdf", [_TEXTO_FORMULARIO]),
        },
    )
    (raiz / "fornecedor-b" / "planilha.xlsx").write_bytes(b"nao e um documento suportado")
    (raiz / "fornecedor-a" / "~$rascunho.docx").write_bytes(b"temporario do Word")
    (raiz / "vazio.pdf").write_bytes(b"")
    return raiz
