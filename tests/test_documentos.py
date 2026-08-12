"""Testes da descoberta de arquivos e da adaptação por formato."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from conftest import nova_imagem, novo_docx, novo_pdf, novo_pptx

from processamento.documentos import (
    OOXML,
    DocumentoInvalidoError,
    FormatoNaoSuportadoError,
    classificar,
    expandir_zips,
    extrair_texto_ooxml,
    listar_documentos,
    preparar,
)


def test_lista_recursivamente_e_ignora_temporarios(arvore_files: Path) -> None:
    nomes = {str(p.relative_to(arvore_files)) for p in listar_documentos(arvore_files)}

    assert "fornecedor-b\\trabalhadores\\aso.pdf".replace("\\", "/") in {
        n.replace("\\", "/") for n in nomes
    }
    assert not any(n.startswith("~$") or "~$" in n for n in nomes)


def test_formato_nao_suportado_entra_na_lista_mas_nao_e_processado(arvore_files: Path) -> None:
    """Nada some do relatório: o `.xlsx` é listado e vira ⚠️ na preparação."""
    listados = {p.name for p in listar_documentos(arvore_files)}
    assert "planilha.xlsx" in listados

    with pytest.raises(FormatoNaoSuportadoError):
        with preparar(arvore_files / "fornecedor-b" / "planilha.xlsx"):
            pass


def test_classificar_por_extensao() -> None:
    assert classificar(Path("a.PDF")) == "pdf"
    assert classificar(Path("a.jpeg")) == "imagem"
    assert classificar(Path("a.docx")) == "ooxml"
    assert classificar(Path("a.pptx")) == "ooxml"
    assert classificar(Path("a.zip")) == "zip"
    assert classificar(Path("a.xls")) == "nao_suportado"


def test_arquivo_vazio_e_invalido(arvore_files: Path) -> None:
    with pytest.raises(DocumentoInvalidoError):
        with preparar(arvore_files / "vazio.pdf"):
            pass


def test_pdf_passa_direto_sem_conversao(pdf_formulario: Path) -> None:
    with preparar(pdf_formulario) as pronto:
        assert pronto.path == pdf_formulario
        assert pronto.convertido is False
        assert pronto.texto is None


def test_imagem_e_aberta_como_documento_de_uma_pagina(tmp_path: Path) -> None:
    import pymupdf

    imagem = nova_imagem(tmp_path / "rubrica.png")

    with preparar(imagem) as pronto:
        doc = pymupdf.open(str(pronto.path))
        try:
            assert doc.page_count == 1
        finally:
            doc.close()


def test_imagem_desconhecida_do_mupdf_cai_no_pillow(tmp_path: Path) -> None:
    """WEBP: o MuPDF não abre, o Pillow abre — vira PDF temporário."""
    from PIL import Image

    caminho = tmp_path / "rubrica.webp"
    Image.new("RGB", (300, 120), "white").save(caminho, format="WEBP")

    with preparar(caminho) as pronto:
        assert pronto.convertido is True
        assert pronto.path.suffix == ".pdf"
        assert pronto.path.is_file()


def test_docx_entrega_texto_e_pdf_das_imagens(tmp_path: Path) -> None:
    import pymupdf

    docx = novo_docx(
        tmp_path / "termo.docx",
        texto="Assinado digitalmente por MARIA SOUZA",
        imagens=[nova_imagem(tmp_path / "a.png"), nova_imagem(tmp_path / "b.png")],
    )

    with preparar(docx) as pronto:
        assert "MARIA SOUZA" in (pronto.texto or "")
        assert pronto.convertido is True
        doc = pymupdf.open(str(pronto.path))
        try:
            assert doc.page_count == 2
        finally:
            doc.close()


def test_docx_sem_imagem_gera_pagina_em_branco(tmp_path: Path) -> None:
    import pymupdf

    docx = novo_docx(tmp_path / "sem-midia.docx", texto="Contrato de prestacao")

    with preparar(docx) as pronto:
        doc = pymupdf.open(str(pronto.path))
        try:
            assert doc.page_count == 1
        finally:
            doc.close()


def test_docx_ignora_imagem_pequena_demais_para_ser_rubrica(tmp_path: Path) -> None:
    """Ícone e logo não valem uma inferência."""
    import pymupdf

    docx = novo_docx(
        tmp_path / "com-icone.docx",
        texto="Contrato",
        imagens=[nova_imagem(tmp_path / "icone.png", tamanho=(16, 16))],
    )

    with preparar(docx) as pronto:
        doc = pymupdf.open(str(pronto.path))
        try:
            assert doc.page_count == 1
            assert not doc[0].get_images()
        finally:
            doc.close()


def test_temporario_e_apagado_ao_sair_do_contexto(tmp_path: Path) -> None:
    docx = novo_docx(tmp_path / "termo.docx", texto="Contrato")

    with preparar(docx) as pronto:
        temporario = pronto.path
        assert temporario.is_file()

    assert not temporario.exists()


def test_docx_corrompido_vira_documento_invalido(tmp_path: Path) -> None:
    caminho = tmp_path / "corrompido.docx"
    caminho.write_bytes(b"PK\x03\x04 isto nao e um zip valido")

    with pytest.raises(DocumentoInvalidoError):
        extrair_texto_ooxml(caminho, OOXML[".docx"])


def test_pptx_junta_o_texto_de_todos_os_slides(tmp_path: Path) -> None:
    pptx = novo_pptx(
        tmp_path / "treinamento.pptx",
        slides=["Slide um", "Assinado digitalmente por JOAO LIMA"],
    )

    texto = extrair_texto_ooxml(pptx, OOXML[".pptx"])

    assert "Slide um" in texto
    assert "JOAO LIMA" in texto


def test_pptx_entrega_texto_e_pdf_das_imagens(tmp_path: Path) -> None:
    import pymupdf

    pptx = novo_pptx(
        tmp_path / "treinamento.pptx",
        slides=["Treinamento"],
        imagens=[nova_imagem(tmp_path / "a.png"), nova_imagem(tmp_path / "b.png")],
    )

    with preparar(pptx) as pronto:
        assert "Treinamento" in (pronto.texto or "")
        doc = pymupdf.open(str(pronto.path))
        try:
            assert doc.page_count == 2
        finally:
            doc.close()


def test_zip_e_extraido_em_subpasta_ao_lado(arvore_files: Path) -> None:
    expandidos = expandir_zips(arvore_files)

    assert len(expandidos) == 1
    assert expandidos[0].arquivos == 2
    assert (arvore_files / "fornecedor-c" / "lote" / "interno" / "contrato-zipado.pdf").is_file()


def test_extrair_zip_e_idempotente(arvore_files: Path) -> None:
    """Retomada não pode re-extrair (e nem sobrescrever) o que já saiu."""
    expandir_zips(arvore_files)

    segunda = expandir_zips(arvore_files)

    assert segunda[0].ja_existia is True
    assert segunda[0].arquivos == 0


def test_documentos_do_zip_entram_na_varredura(arvore_files: Path) -> None:
    expandir_zips(arvore_files)

    nomes = {p.name for p in listar_documentos(arvore_files)}

    assert {"contrato-zipado.pdf", "ficha-zipada.pdf"} <= nomes


def test_zip_dentro_de_zip_e_expandido(tmp_path: Path) -> None:
    from conftest import novo_zip

    raiz = tmp_path / "files"
    interno = novo_zip(tmp_path / "interno.zip", {"doc.pdf": novo_pdf(tmp_path / "d.pdf", ["x"])})
    novo_zip(raiz / "externo.zip", {"interno.zip": interno})

    expandir_zips(raiz)

    assert (raiz / "externo" / "interno" / "doc.pdf").is_file()


def test_zip_corrompido_vira_erro_e_nao_derruba_os_outros(tmp_path: Path) -> None:
    from conftest import novo_zip

    raiz = tmp_path / "files"
    raiz.mkdir(parents=True)
    (raiz / "quebrado.zip").write_bytes(b"PK\x03\x04 nao e zip")
    novo_zip(raiz / "bom.zip", {"doc.pdf": novo_pdf(tmp_path / "d.pdf", ["x"])})

    expandidos = {e.origem.name: e for e in expandir_zips(raiz)}

    assert expandidos["quebrado.zip"].erro is not None
    assert expandidos["bom.zip"].erro is None
    assert (raiz / "bom" / "doc.pdf").is_file()


def test_zip_absurdo_e_recusado_como_zip_bomb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from conftest import novo_zip

    monkeypatch.setattr("processamento.documentos.LIMITE_ZIP_BYTES", 16)
    raiz = tmp_path / "files"
    novo_zip(raiz / "bomba.zip", {"doc.pdf": novo_pdf(tmp_path / "d.pdf", ["conteudo"])})

    expandidos = expandir_zips(raiz)

    assert "zip bomb" in (expandidos[0].erro or "")
    assert not (raiz / "bomba").exists()


def test_docx_com_xml_absurdo_e_recusado_como_zip_bomb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zip bomb: muito byte descomprimido a partir de pouquíssimo comprimido.

    O limite é rebaixado no teste para não escrever 64 MB em disco só para
    provar a guarda.
    """
    monkeypatch.setattr("processamento.documentos.LIMITE_XML_BYTES", 1024)
    caminho = tmp_path / "bomba.docx"
    with zipfile.ZipFile(caminho, "w", compression=zipfile.ZIP_DEFLATED) as zip_docx:
        zip_docx.writestr("word/document.xml", "A" * 2048)

    with pytest.raises(DocumentoInvalidoError, match="zip bomb"):
        extrair_texto_ooxml(caminho, OOXML['.docx'])


def test_texto_do_docx_separa_paragrafos(tmp_path: Path) -> None:
    caminho = tmp_path / "paragrafos.docx"
    with zipfile.ZipFile(caminho, "w") as zip_docx:
        zip_docx.writestr(
            "word/document.xml",
            "<w:document><w:body>"
            "<w:p><w:r><w:t>Primeira linha</w:t></w:r></w:p>"
            "<w:p><w:r><w:t>Assinado digitalmente por JOAO &amp; CIA</w:t></w:r></w:p>"
            "</w:body></w:document>",
        )

    texto = extrair_texto_ooxml(caminho, OOXML['.docx'])

    assert "Primeira linha\n" in texto
    assert "JOAO & CIA" in texto
