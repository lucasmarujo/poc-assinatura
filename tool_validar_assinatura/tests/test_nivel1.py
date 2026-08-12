"""Testes do Nível 1 — pré/pós-processamento, varredura do PDF e fallback 3×3.

Nenhum teste carrega os pesos ONNX: a sessão é injetada e devolve tensores
sintéticos no formato do YOLOv8 (`(1, 4+classes, N)`).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from nivel1 import (
    LADRILHOS_POR_LADO,
    TAMANHO_ENTRADA,
    DetectorAssinaturaOnnx,
    caixa_para_pagina,
    nms,
    pos_processar,
    retangulos_ladrilhos,
)


def _saida_yolo(deteccoes: list[tuple[float, float, float, float, float]]) -> np.ndarray:
    """Tensor `(1, 5, N)` com `(cx, cy, w, h, score)` em pixels do espaço 640."""
    total = max(len(deteccoes), 1)
    saida = np.zeros((1, 5, total), dtype=np.float32)
    for indice, valores in enumerate(deteccoes):
        saida[0, :, indice] = valores
    return saida


def _saida_vista_do_ladrilho(
    caixa: tuple[float, float, float, float],
    recorte: tuple[float, float, float, float],
    confianca: float,
) -> np.ndarray:
    """Tensor de uma caixa da página `(left, top, width, height)` como o modelo a
    veria de dentro do ladrilho `recorte` — o caminho inverso de
    `caixa_para_pagina`."""
    left, top, largura, altura = caixa
    x0, y0, x1, y1 = recorte
    lx, lw = (left - x0) / (x1 - x0), largura / (x1 - x0)
    ly, lh = (top - y0) / (y1 - y0), altura / (y1 - y0)
    return _saida_yolo(
        [
            (
                (lx + lw / 2) * TAMANHO_ENTRADA,
                (ly + lh / 2) * TAMANHO_ENTRADA,
                lw * TAMANHO_ENTRADA,
                lh * TAMANHO_ENTRADA,
                confianca,
            )
        ]
    )


class _SessaoFake:
    """Sessão ONNX mínima: registra os tensores recebidos e devolve `saida`."""

    def __init__(self, saida: np.ndarray) -> None:
        self._saida = saida
        self.entradas: list[np.ndarray] = []

    def get_inputs(self) -> list[Any]:
        return [SimpleNamespace(name="images")]

    def run(self, _saidas: Any, feed: dict[str, np.ndarray]) -> list[np.ndarray]:
        self.entradas.append(feed["images"])
        return [self._saida]


class _SessaoRoteiro(_SessaoFake):
    """Uma saída por chamada — o passe normal e cada ladrilho podem devolver
    coisas diferentes. Esgotado o roteiro, repete a última."""

    def __init__(self, saidas: Sequence[np.ndarray]) -> None:
        super().__init__(saidas[-1])
        self._roteiro = list(saidas)

    def run(self, _saidas: Any, feed: dict[str, np.ndarray]) -> list[np.ndarray]:
        self.entradas.append(feed["images"])
        return [self._roteiro[min(len(self.entradas) - 1, len(self._roteiro) - 1)]]


def _detector(saida: np.ndarray) -> tuple[DetectorAssinaturaOnnx, _SessaoFake]:
    sessao = _SessaoFake(saida)
    return DetectorAssinaturaOnnx("modelo-fake.onnx", sessao=sessao), sessao


def _detector_roteiro(
    saidas: Sequence[np.ndarray],
) -> tuple[DetectorAssinaturaOnnx, _SessaoRoteiro]:
    sessao = _SessaoRoteiro(saidas)
    return DetectorAssinaturaOnnx("modelo-fake.onnx", sessao=sessao), sessao


def _contem(
    ladrilho: tuple[float, float, float, float], caixa: tuple[float, float, float, float]
) -> bool:
    """A caixa `(left, top, width, height)` cabe inteira dentro do ladrilho?"""
    x0, y0, x1, y1 = ladrilho
    left, top, largura, altura = caixa
    return x0 <= left and y0 <= top and left + largura <= x1 and top + altura <= y1


def test_pos_processar_converte_para_caixa_relativa() -> None:
    saida = _saida_yolo([(320.0, 320.0, 64.0, 32.0, 0.9)])

    detecoes = pos_processar(saida)

    assert len(detecoes) == 1
    assert detecoes[0]["confidence"] == 0.9
    assert detecoes[0]["bounding_box"] == {
        "left": 0.45,
        "top": 0.475,
        "width": 0.1,
        "height": 0.05,
    }
    assert detecoes[0]["quem_assinou"] is None


def test_pos_processar_descarta_abaixo_da_confianca() -> None:
    saida = _saida_yolo([(320.0, 320.0, 64.0, 32.0, 0.2)])

    assert pos_processar(saida, confianca_minima=0.25) == []
    assert len(pos_processar(saida, confianca_minima=0.1)) == 1


def test_pos_processar_aplica_nms_em_caixas_sobrepostas() -> None:
    """Duas caixas quase idênticas → sobra a de maior confiança."""
    saida = _saida_yolo(
        [
            (320.0, 320.0, 64.0, 32.0, 0.60),
            (322.0, 321.0, 64.0, 32.0, 0.95),
            (100.0, 100.0, 40.0, 20.0, 0.80),
        ]
    )

    detecoes = pos_processar(saida)

    assert [d["confidence"] for d in detecoes] == [0.95, 0.8]


def test_pos_processar_com_saida_invalida_nao_quebra() -> None:
    assert pos_processar(np.zeros((1, 3, 10), dtype=np.float32)) == []
    assert pos_processar(np.zeros((5,), dtype=np.float32)) == []


def test_nms_sem_caixas() -> None:
    assert nms(np.zeros((0, 4)), np.zeros((0,)), 0.5) == []


def test_nms_mantem_caixas_distantes() -> None:
    caixas = np.array([[0.0, 0.0, 0.1, 0.1], [0.5, 0.5, 0.6, 0.6]])
    scores = np.array([0.6, 0.9])

    assert nms(caixas, scores, 0.5) == [1, 0]


def test_detectar_imagem_envia_tensor_normalizado(pdf_formulario: Path) -> None:
    from PIL import Image

    detector, sessao = _detector(_saida_yolo([(320.0, 320.0, 64.0, 32.0, 0.9)]))

    detecoes, tempo_ms = detector.detectar_imagem(Image.new("RGB", (800, 1200), "white"))

    tensor = sessao.entradas[0]
    assert tensor.shape == (1, 3, TAMANHO_ENTRADA, TAMANHO_ENTRADA)
    assert tensor.dtype == np.float32
    assert float(tensor.min()) >= 0.0 and float(tensor.max()) <= 1.0
    assert len(detecoes) == 1
    assert tempo_ms >= 0.0


def test_detectar_pdf_restringe_paginas(pdf_com_pagina_em_branco: Path) -> None:
    """A triagem do Nível 0 entra como `paginas`; o resto nem é rasterizado."""
    detector, sessao = _detector(_saida_yolo([(320.0, 500.0, 64.0, 32.0, 0.8)]))

    resultado = detector.detectar_pdf(pdf_com_pagina_em_branco, paginas=[1])

    assert len(sessao.entradas) == 1
    assert resultado.paginas_analisadas == [1]
    assert resultado.paginas_ignoradas == [2]
    assert resultado.total == 1
    assert resultado.assinaturas[0]["page"] == 1
    assert resultado.tempo_inferencia_ms >= 0.0


def test_detectar_pdf_todas_as_paginas_por_default(pdf_com_pagina_em_branco: Path) -> None:
    detector, sessao = _detector(_saida_yolo([(320.0, 320.0, 64.0, 32.0, 0.8)]))

    resultado = detector.detectar_pdf(pdf_com_pagina_em_branco)

    assert len(sessao.entradas) == 2
    assert resultado.paginas_analisadas == [1, 2]
    assert resultado.paginas_ignoradas == []
    assert [a["page"] for a in resultado.assinaturas] == [1, 2]


def test_detectar_pdf_ignora_pagina_inexistente(pdf_formulario: Path) -> None:
    detector, sessao = _detector(_saida_yolo([]))

    resultado = detector.detectar_pdf(pdf_formulario, paginas=[1, 99])

    assert resultado.paginas_analisadas == [1]
    assert len(sessao.entradas) == 1
    assert resultado.tem_assinatura is False


# ---------- Fallback de ladrilhos ---------------------------------------------


def test_retangulos_ladrilhos_cobrem_a_pagina_com_sobreposicao() -> None:
    ladrilhos = retangulos_ladrilhos()

    assert len(ladrilhos) == LADRILHOS_POR_LADO**2
    assert all(0.0 <= valor <= 1.0 for ladrilho in ladrilhos for valor in ladrilho)
    # A margem não vaza para fora da página: o primeiro começa no canto e o
    # último termina no canto oposto.
    assert ladrilhos[0][:2] == (0.0, 0.0)
    assert ladrilhos[-1][2:] == (1.0, 1.0)
    # Vizinhos se sobrepõem — na horizontal e na vertical.
    assert ladrilhos[1][0] < ladrilhos[0][2]
    assert ladrilhos[LADRILHOS_POR_LADO][1] < ladrilhos[0][3]


def test_caixa_do_ladrilho_volta_para_coordenada_da_pagina() -> None:
    """Detecção no meio do último ladrilho → caixa no canto inferior direito."""
    deteccao = {
        "confidence": 0.9,
        "bounding_box": {"left": 0.4, "top": 0.4, "width": 0.2, "height": 0.2},
        "quem_assinou": None,
    }

    caixa = caixa_para_pagina(deteccao, retangulos_ladrilhos()[-1])["bounding_box"]

    assert caixa["left"] > 0.6 and caixa["top"] > 0.6
    assert caixa["left"] + caixa["width"] <= 1.0
    assert caixa["top"] + caixa["height"] <= 1.0


def test_fallback_ladrilha_a_pagina_quando_o_passe_normal_nao_acha(
    pdf_formulario: Path,
) -> None:
    vazio = _saida_yolo([])
    ladrilhos = retangulos_ladrilhos()
    rubrica = (0.72, 0.80, 0.10, 0.05)
    saidas = [vazio] + [
        _saida_vista_do_ladrilho(rubrica, ladrilho, 0.8)
        if _contem(ladrilho, rubrica)
        else vazio
        for ladrilho in ladrilhos
    ]
    detector, sessao = _detector_roteiro(saidas)

    resultado = detector.detectar_pdf(pdf_formulario, fallback_ladrilhos=True)

    assert len(sessao.entradas) == 1 + LADRILHOS_POR_LADO**2
    assert resultado.paginas_ladrilhadas == [1]
    assert resultado.total == 1
    assert resultado.assinaturas[0]["page"] == 1
    assert resultado.assinaturas[0]["bounding_box"]["left"] == pytest.approx(0.72, abs=0.005)
    assert resultado.assinaturas[0]["bounding_box"]["top"] == pytest.approx(0.80, abs=0.005)


def test_fallback_dedupe_a_mesma_rubrica_vista_por_ladrilhos_vizinhos(
    pdf_formulario: Path,
) -> None:
    """A sobreposição faz a rubrica cair em dois ladrilhos — sobra uma caixa só."""
    ladrilhos = retangulos_ladrilhos()
    rubrica = (0.30, 0.05, 0.06, 0.04)
    assert _contem(ladrilhos[0], rubrica) and _contem(ladrilhos[1], rubrica)

    vazio = _saida_yolo([])
    detector, _ = _detector_roteiro(
        [
            vazio,
            _saida_vista_do_ladrilho(rubrica, ladrilhos[0], 0.6),
            _saida_vista_do_ladrilho(rubrica, ladrilhos[1], 0.9),
            vazio,
        ]
    )

    resultado = detector.detectar_pdf(pdf_formulario, fallback_ladrilhos=True)

    assert resultado.total == 1
    assert resultado.assinaturas[0]["confidence"] == pytest.approx(0.9, abs=0.001)


def test_fallback_nao_roda_quando_o_passe_normal_ja_achou(pdf_formulario: Path) -> None:
    """Ladrilhar página que já devolveu detecção é inferência jogada fora."""
    detector, sessao = _detector(_saida_yolo([(320.0, 320.0, 64.0, 32.0, 0.8)]))

    resultado = detector.detectar_pdf(pdf_formulario, fallback_ladrilhos=True)

    assert len(sessao.entradas) == 1
    assert resultado.paginas_ladrilhadas == []


def test_fallback_desligado_mantem_o_passe_unico(pdf_formulario: Path) -> None:
    detector, sessao = _detector(_saida_yolo([]))

    resultado = detector.detectar_pdf(pdf_formulario)

    assert len(sessao.entradas) == 1
    assert resultado.paginas_ladrilhadas == []
    assert resultado.tem_assinatura is False
