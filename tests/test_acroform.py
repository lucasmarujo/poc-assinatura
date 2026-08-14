"""Testes da auditoria do AcroForm: o que separa assinado de "assine aqui".

O erro que estes testes existem para impedir é o mais caro possível — o
diagnóstico **invertido**: chamar de campo vazio um PDF assinado (porque o nome
do signatário está vazio) ou de assinado um formulário em branco. Um manda
corrigir código que está certo; o outro deixa falso positivo em produção.

Os PDFs são montados aqui pelo xref: o `add_widget` do PyMuPDF 1.24 quebra ao
criar widget de assinatura, e escrever o objeto direto é o que dá controle sobre
`/V`, `/ByteRange` e `/Contents` — que é justamente o que se quer testar.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from validacao.acroform import (
    GRUPO_COM_OUTRA_EVIDENCIA,
    GRUPO_SO_ACROFORM,
    VEREDITO_ASSINADO,
    VEREDITO_CAMPO_VAZIO,
    VEREDITO_ERRO,
    VEREDITO_INCOMPLETO,
    VEREDITO_SEM_CAMPO,
    CampoAssinatura,
    auditar,
    grupo,
    inspecionar,
    montar_relatorio,
    sortear,
    veredito,
)

# PKCS#7 de mentira, grande o bastante para passar de `MINIMO_PKCS7_BYTES`, com
# um nome em caixa alta no meio como num certificado ICP-Brasil.
_PKCS7 = b"3082" + b"00FF" * 20 + b"4A4F414F2044412053494C5641" + b"00FF" * 20


def _pdf_com_campo_sig(
    destino: Path, *, valor: str | None, contents: bytes = _PKCS7, byte_range: bool = True
) -> Path:
    """PDF de uma página com um campo `/Sig` no AcroForm.

    `valor=None` é o campo em branco (sem `/V`); com valor, o `/V` aponta para
    um dicionário de assinatura montado à mão.
    """
    import pymupdf as _pymupdf

    pymupdf = cast(Any, _pymupdf)
    doc = pymupdf.open()
    try:
        pagina = doc.new_page()
        xref_widget = doc.get_new_xref()
        referencia = ""
        if valor is not None:
            xref_valor = doc.get_new_xref()
            partes = ["/Type /Sig", "/Filter /Adobe.PPKLite", "/SubFilter /adbe.pkcs7.detached"]
            if byte_range:
                partes.append("/ByteRange [0 100 300 200]")
            partes.append(f"/M (D:20250101120000-03'00')/Contents <{contents.decode()}>")
            doc.update_object(xref_valor, "<<" + "".join(partes) + ">>")
            referencia = f"/V {xref_valor} 0 R"
        doc.update_object(
            xref_widget,
            "<</Type /Annot /Subtype /Widget /FT /Sig /T (Assine) "
            f"/Rect [50 50 250 120] /F 4 {referencia}>>",
        )
        doc.xref_set_key(pagina.xref, "Annots", f"[{xref_widget} 0 R]")
        doc.xref_set_key(
            doc.pdf_catalog(), "AcroForm", f"<</Fields [{xref_widget} 0 R] /SigFlags 3>>"
        )
        destino.parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(destino))
    finally:
        doc.close()
    return destino


def _registro(arquivo: str, fontes: list[str]) -> dict[str, Any]:
    return {"arquivo": arquivo, "fontes": fontes, "nivel0_total": 1, "nivel1_total": 0}


# ---------- leitura do PDF ----------------------------------------------------


def test_campo_sem_valor_e_placeholder_nao_assinatura(tmp_path: Path) -> None:
    """O caso que o sinal `pdf_embedded` conta como assinatura e não é."""
    caminho = _pdf_com_campo_sig(tmp_path / "em_branco.pdf", valor=None)

    campos = inspecionar(caminho)

    assert len(campos) == 1
    assert campos[0].tem_valor is False
    assert campos[0].assinado is False
    assert veredito(campos) == VEREDITO_CAMPO_VAZIO


def test_campo_assinado_com_nome_vazio_continua_assinado(tmp_path: Path) -> None:
    """O diagnóstico invertido: `/Name` ausente **não** é campo vazio. O que vale
    é `/V` + `/ByteRange` + PKCS#7 — e o nome real está no certificado."""
    caminho = _pdf_com_campo_sig(tmp_path / "assinado.pdf", valor="sim")

    campos = inspecionar(caminho)

    assert campos[0].tem_valor is True
    assert campos[0].tem_byterange is True
    assert campos[0].nome_declarado is None
    assert campos[0].assinado is True
    assert veredito(campos) == VEREDITO_ASSINADO
    assert "JOAO DA SILVA" in campos[0].nomes_no_certificado


def test_valor_sem_byterange_ou_com_contents_vazio_e_incompleto(tmp_path: Path) -> None:
    sem_byte_range = _pdf_com_campo_sig(tmp_path / "sem_br.pdf", valor="sim", byte_range=False)
    contents_vazio = _pdf_com_campo_sig(tmp_path / "vazio.pdf", valor="sim", contents=b"")

    assert veredito(inspecionar(sem_byte_range)) == VEREDITO_INCOMPLETO
    assert veredito(inspecionar(contents_vazio)) == VEREDITO_INCOMPLETO


def test_pdf_sem_campo_de_assinatura(tmp_path: Path) -> None:
    from conftest import novo_pdf

    campos = inspecionar(novo_pdf(tmp_path / "comum.pdf", ["um texto qualquer"]))

    assert campos == []
    assert veredito(campos) == VEREDITO_SEM_CAMPO


def test_um_campo_assinado_basta_para_o_documento(tmp_path: Path) -> None:
    campos = [
        CampoAssinatura(pagina=1, nome="A", tem_valor=False, tem_byterange=False, bytes_pkcs7=0),
        CampoAssinatura(
            pagina=2, nome="B", tem_valor=True, tem_byterange=True, bytes_pkcs7=4096
        ),
    ]

    assert veredito(campos) == VEREDITO_ASSINADO


# ---------- amostra -----------------------------------------------------------


def test_grupo_separa_evidencia_unica_de_evidencia_acompanhada() -> None:
    assert grupo(_registro("a.pdf", ["pdf_embedded"])) == GRUPO_SO_ACROFORM
    assert (
        grupo(_registro("b.pdf", ["pdf_embedded", "visual"])) == GRUPO_COM_OUTRA_EVIDENCIA
    )
    assert grupo(_registro("c.pdf", ["visual"])) is None


def test_sorteio_deduplica_por_conteudo() -> None:
    """Contrato copiado 96 vezes tomaria a amostra inteira e mediria um PDF só."""
    registros = [_registro(f"copia{i}.pdf", ["pdf_embedded"]) for i in range(5)]
    registros.append(_registro("outro.pdf", ["pdf_embedded"]))
    hashes = {f"copia{i}.pdf": "mesmo-conteudo" for i in range(5)}
    hashes["outro.pdf"] = "conteudo-distinto"

    amostra = sortear(registros, n_por_grupo=10, semente=42, hashes=hashes)

    assert len(amostra[GRUPO_SO_ACROFORM]) == 2


def test_sorteio_e_deterministico_e_respeita_o_orcamento() -> None:
    registros = [_registro(f"doc{i}.pdf", ["pdf_embedded"]) for i in range(30)]

    primeira = sortear(registros, n_por_grupo=5, semente=42)
    segunda = sortear(registros, n_por_grupo=5, semente=42)
    outra_semente = sortear(registros, n_por_grupo=5, semente=7)

    assert primeira == segunda
    assert len(primeira[GRUPO_SO_ACROFORM]) == 5
    assert primeira != outra_semente


# ---------- auditoria e relatório ---------------------------------------------


@pytest.fixture
def lote(tmp_path: Path) -> tuple[Path, list[dict[str, Any]]]:
    raiz = tmp_path / "files"
    _pdf_com_campo_sig(raiz / "assinado.pdf", valor="sim")
    _pdf_com_campo_sig(raiz / "em_branco.pdf", valor=None)
    registros = [
        _registro("assinado.pdf", ["pdf_embedded"]),
        _registro("em_branco.pdf", ["pdf_embedded"]),
        _registro("sumiu.pdf", ["pdf_embedded", "visual"]),
    ]
    return raiz, registros


def test_auditar_classifica_cada_documento_e_nao_para_no_erro(
    lote: tuple[Path, list[dict[str, Any]]],
) -> None:
    """PDF ilegível vira `erro` na linha dele: parar a auditoria por causa de um
    arquivo perdeu os outros 49."""
    raiz, registros = lote

    linhas = auditar(registros=registros, raiz_files=raiz, n_por_grupo=5)

    por_arquivo = {linha["arquivo"]: linha for linha in linhas}
    assert por_arquivo["assinado.pdf"]["veredito"] == VEREDITO_ASSINADO
    assert por_arquivo["em_branco.pdf"]["veredito"] == VEREDITO_CAMPO_VAZIO
    assert por_arquivo["sumiu.pdf"]["veredito"] == VEREDITO_ERRO
    assert por_arquivo["sumiu.pdf"]["erro"]


def test_relatorio_projeta_o_falso_positivo_para_o_lote(
    lote: tuple[Path, list[dict[str, Any]]],
) -> None:
    raiz, registros = lote
    linhas = auditar(registros=registros, raiz_files=raiz, n_por_grupo=5)
    from collections import Counter

    relatorio = montar_relatorio(
        linhas, populacao=Counter({GRUPO_SO_ACROFORM: 2000}), semente=42
    )

    # 1 de 2 no grupo crítico está em branco → metade dos 2000.
    assert "1 de 2" in relatorio
    assert "1,000" in relatorio
    assert "assinado.pdf" in relatorio
    # E o contraponto: o campo assinado do lote tem `/Name` vazio.
    assert "## O nome vazio não é campo vazio" in relatorio
    assert "1 deles (100%) têm `/Name` vazio" in relatorio
