"""Testes da cascata Nível 0 → Nível 1 e do contrato de saída."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from deteccao import detectar_assinaturas, detectar_assinaturas_async, formatar_contexto
from nivel1 import ResultadoNivel1


class _DetectorFake:
    """Detector visual de mentira: registra as páginas pedidas e devolve `retorno`."""

    def __init__(self, retorno: ResultadoNivel1) -> None:
        self._retorno = retorno
        self.chamadas: list[dict[str, Any]] = []

    def detectar_pdf(self, path: Path, **kwargs: Any) -> ResultadoNivel1:
        self.chamadas.append({"path": path, **kwargs})
        return self._retorno


def _assinatura_visual(page: int = 1, confianca: float = 0.87) -> dict[str, Any]:
    return {
        "page": page,
        "confidence": confianca,
        "bounding_box": {"left": 0.1, "top": 0.8, "width": 0.3, "height": 0.06},
        "quem_assinou": None,
    }


def _instalar_detector(monkeypatch: pytest.MonkeyPatch, retorno: ResultadoNivel1) -> _DetectorFake:
    detector = _DetectorFake(retorno)
    monkeypatch.setattr("deteccao.obter_detector", lambda _=None: detector)
    return detector


def test_cascata_para_no_nivel0_quando_ja_ha_assinatura(
    monkeypatch: pytest.MonkeyPatch, pdf_carimbo_digital: Path
) -> None:
    """Carimbo digital no texto resolve o documento — Nível 1 não roda (economia)."""

    def _explode(_: Any = None) -> None:
        raise AssertionError("Nível 1 não deveria ser chamado")

    monkeypatch.setattr("deteccao.obter_detector", _explode)

    resultado = detectar_assinaturas(pdf_carimbo_digital)

    assert resultado.tem_assinatura is True
    assert resultado.fontes == ["digital"]
    assert resultado.nivel1 is None
    assert resultado.total_assinaturas == 1
    assert resultado.signatarios == ["FULANO DE TAL SILVA"]


def test_escalonar_false_roda_os_dois_niveis(
    monkeypatch: pytest.MonkeyPatch, pdf_carimbo_digital: Path
) -> None:
    """Modo comparação da PoC: Nível 1 roda mesmo com o Nível 0 positivo."""
    detector = _instalar_detector(
        monkeypatch,
        ResultadoNivel1(assinaturas=[_assinatura_visual()], paginas_analisadas=[1]),
    )

    resultado = detectar_assinaturas(pdf_carimbo_digital, escalonar=False)

    assert len(detector.chamadas) == 1
    assert resultado.fontes == ["digital", "visual"]
    assert resultado.total_assinaturas == 2


def test_nivel1_recebe_apenas_paginas_nao_brancas(
    monkeypatch: pytest.MonkeyPatch, pdf_com_pagina_em_branco: Path
) -> None:
    detector = _instalar_detector(
        monkeypatch, ResultadoNivel1(assinaturas=[], paginas_analisadas=[1])
    )

    detectar_assinaturas(pdf_com_pagina_em_branco, confianca_minima=0.4, dpi_render=200)

    chamada = detector.chamadas[0]
    assert chamada["paginas"] == [1]
    assert chamada["confianca_minima"] == 0.4
    assert chamada["dpi"] == 200


def test_fallback_nao_dispara_quando_o_nivel0_ja_achou(
    monkeypatch: pytest.MonkeyPatch, pdf_carimbo_digital: Path
) -> None:
    """Ladrilhar documento que o carimbo digital resolveu é inferência jogada fora."""
    detector = _instalar_detector(
        monkeypatch, ResultadoNivel1(assinaturas=[], paginas_analisadas=[1])
    )

    detectar_assinaturas(pdf_carimbo_digital, escalonar=False)

    assert detector.chamadas[0]["fallback_ladrilhos"] is False


def test_fallback_dispara_em_documento_sem_sinal_do_nivel0(
    monkeypatch: pytest.MonkeyPatch, pdf_formulario: Path
) -> None:
    detector = _instalar_detector(
        monkeypatch, ResultadoNivel1(assinaturas=[], paginas_analisadas=[1])
    )

    detectar_assinaturas(pdf_formulario, dpi_ladrilho=200)

    assert detector.chamadas[0]["fallback_ladrilhos"] is True
    assert detector.chamadas[0]["dpi_ladrilho"] == 200


def test_fallback_desligado_nao_chega_ao_detector(
    monkeypatch: pytest.MonkeyPatch, pdf_formulario: Path
) -> None:
    detector = _instalar_detector(
        monkeypatch, ResultadoNivel1(assinaturas=[], paginas_analisadas=[1])
    )

    detectar_assinaturas(pdf_formulario, fallback_ladrilhos=False)

    assert detector.chamadas[0]["fallback_ladrilhos"] is False


def test_executar_nivel1_false_fica_somente_no_nivel0(
    monkeypatch: pytest.MonkeyPatch, pdf_formulario: Path
) -> None:
    def _explode(_: Any = None) -> None:
        raise AssertionError("Nível 1 desligado não deveria ser chamado")

    monkeypatch.setattr("deteccao.obter_detector", _explode)

    resultado = detectar_assinaturas(pdf_formulario, executar_nivel1=False)

    assert resultado.nivel1 is None
    assert resultado.nivel1_erro is None
    assert resultado.tem_assinatura is False


def test_falha_do_nivel1_nao_derruba_o_veredito(
    monkeypatch: pytest.MonkeyPatch, pdf_formulario: Path
) -> None:
    """Pesos ausentes → evidência visual indisponível, Nível 0 segue valendo."""

    def _sem_modelo(_: Any = None) -> None:
        raise RuntimeError("modelo ONNX ausente")

    monkeypatch.setattr("deteccao.obter_detector", _sem_modelo)

    resultado = detectar_assinaturas(pdf_formulario)

    assert resultado.nivel1 is None
    assert resultado.nivel1_erro == "modelo ONNX ausente"
    assert resultado.tem_assinatura is False
    assert "indisponível" in formatar_contexto(resultado)


def test_para_dict_mantem_o_contrato_legado(
    monkeypatch: pytest.MonkeyPatch, pdf_formulario: Path
) -> None:
    """Mesmas chaves que `detect_all_signatures` devolvia, mais o detalhe da PoC."""
    _instalar_detector(
        monkeypatch,
        ResultadoNivel1(assinaturas=[_assinatura_visual()], paginas_analisadas=[1]),
    )

    dados = detectar_assinaturas(pdf_formulario).para_dict()

    assert set(dados) >= {
        "tem_assinatura",
        "fontes",
        "signatarios",
        "visual",
        "digital",
        "embedded",
        "visual_error",
    }
    assert dados["tem_assinatura"] is True
    assert dados["fontes"] == ["visual"]
    assert dados["visual"][0]["page"] == 1
    assert dados["nivel0"]["paginas"][0]["tem_rotulo_assinatura"] is True
    assert dados["nivel1"]["paginas_analisadas"] == [1]


def test_formatar_contexto_gera_bloco_para_o_llm(
    monkeypatch: pytest.MonkeyPatch, pdf_formulario: Path
) -> None:
    _instalar_detector(
        monkeypatch,
        ResultadoNivel1(assinaturas=[_assinatura_visual()], paginas_analisadas=[1]),
    )

    texto = formatar_contexto(detectar_assinaturas(pdf_formulario))

    assert texto.startswith("<assinaturas_detectadas>")
    assert "Possui assinatura: SIM" in texto
    assert "[visual] página 1" in texto


@pytest.mark.asyncio
async def test_versao_async_devolve_o_mesmo_resultado(
    monkeypatch: pytest.MonkeyPatch, pdf_carimbo_digital: Path
) -> None:
    resultado = await detectar_assinaturas_async(pdf_carimbo_digital, executar_nivel1=False)

    assert resultado.tem_assinatura is True
    assert resultado.nivel0.total == 1
