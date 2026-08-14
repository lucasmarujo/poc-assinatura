"""Testes do Nível 0 — sinais de custo zero."""

from __future__ import annotations

from pathlib import Path

from nivel0 import (
    DENSIDADE_TINTA_MINIMA,
    campos_assinados,
    densidade_tinta,
    detectar_nivel0,
    encontrar_rotulos_assinatura,
    somente_assinados,
    triar_paginas,
)


def test_densidade_tinta_buffer_vazio_e_zero() -> None:
    assert densidade_tinta(b"") == 0.0


def test_densidade_tinta_conta_pixels_escuros() -> None:
    """Metade dos pixels abaixo do limiar → densidade 0,5."""
    amostras = bytes([0, 0, 255, 255])

    assert densidade_tinta(amostras) == 0.5


def test_densidade_tinta_respeita_limiar() -> None:
    """Fundo cinza-claro de digitalização não conta como tinta."""
    amostras = bytes([230, 230, 230, 230])

    assert densidade_tinta(amostras) == 0.0
    assert densidade_tinta(amostras, limiar_pixel=240) == 1.0


def test_encontra_rotulo_de_assinatura() -> None:
    assert encontrar_rotulos_assinatura("Assinatura do empregado: _______") is True
    assert encontrar_rotulos_assinatura("Recebi os equipamentos") is True
    assert encontrar_rotulos_assinatura("") is False
    assert encontrar_rotulos_assinatura("Relatorio mensal de producao") is False


def test_triagem_marca_pagina_em_branco(pdf_com_pagina_em_branco: Path) -> None:
    paginas = triar_paginas(pdf_com_pagina_em_branco)

    assert [p.numero for p in paginas] == [1, 2]
    assert paginas[0].em_branco is False
    assert paginas[0].densidade_tinta > DENSIDADE_TINTA_MINIMA
    assert paginas[1].em_branco is True
    assert paginas[1].densidade_tinta == 0.0


def test_detecta_carimbo_digital_no_texto(pdf_carimbo_digital: Path) -> None:
    resultado = detectar_nivel0(pdf_carimbo_digital)

    assert resultado.total == 1
    assert resultado.fontes == ["digital"]
    assert resultado.tem_assinatura is True
    assert resultado.digital[0]["signatario"] == "FULANO DE TAL SILVA"


def test_formulario_sem_assinatura_nao_dispara_veredito(pdf_formulario: Path) -> None:
    """Rótulo de assinatura é indício de campo, não de assinatura."""
    resultado = detectar_nivel0(pdf_formulario)

    assert resultado.total == 0
    assert resultado.tem_assinatura is False
    assert resultado.paginas[0].tem_rotulo_assinatura is True


def test_campo_de_assinatura_em_branco_nao_e_assinatura(
    pdf_campo_sig_em_branco: Path,
) -> None:
    """O widget `/Sig` sem `/V` é o "assine aqui" de um formulário. Contá-lo
    aprova documento não assinado — era o falso positivo do AcroForm."""
    resultado = detectar_nivel0(pdf_campo_sig_em_branco)

    assert campos_assinados(pdf_campo_sig_em_branco) == set()
    assert resultado.embedded == []
    assert resultado.total == 0
    assert resultado.tem_assinatura is False


def test_campo_assinado_sem_nome_continua_valendo(pdf_campo_sig_assinado: Path) -> None:
    """O outro lado do mesmo corte: `/V` com `/ByteRange` e PKCS#7 é assinatura,
    ainda que `/Name` esteja vazio — que é o normal no ICP-Brasil."""
    resultado = detectar_nivel0(pdf_campo_sig_assinado)

    assert campos_assinados(pdf_campo_sig_assinado) == {(1, "Assinatura1")}
    assert resultado.total == 1
    assert resultado.fontes == ["pdf_embedded"]
    assert resultado.tem_assinatura is True


def test_somente_assinados_erra_para_o_lado_de_manter(  # noqa: D103
) -> None:
    assinaturas = [
        {"tipo": "assinatura_pdf_embedded", "page": 1, "campo": "Assinatura1"},
        {"tipo": "assinatura_pdf_embedded", "page": 2, "campo": "Assinatura2"},
    ]

    assert somente_assinados(assinaturas, {(1, "Assinatura1")}) == [assinaturas[0]]
    assert somente_assinados(assinaturas, set()) == []


def test_paginas_uteis_excluem_as_em_branco(pdf_com_pagina_em_branco: Path) -> None:
    resultado = detectar_nivel0(pdf_com_pagina_em_branco)

    assert resultado.paginas_uteis == [1]
    assert resultado.paginas_em_branco == [2]


def test_texto_extraido_substitui_a_camada_do_pdf(pdf_formulario: Path) -> None:
    """Texto de OCR passado pela pipeline é o usado nos sinais textuais."""
    resultado = detectar_nivel0(
        pdf_formulario, texto_extraido="Assinado digitalmente por MARIA SOUZA"
    )

    assert resultado.total == 1
    assert resultado.digital[0]["signatario"] == "MARIA SOUZA"
