"""Testes da resolução dos pesos ONNX (sem tocar a rede)."""

from __future__ import annotations

from pathlib import Path

import pytest
from modelo import ENV_MODELO_PATH, ModeloIndisponivelError, resolver_modelo


def _pesos_falsos(tmp_path: Path, nome: str = "yolov8s.onnx") -> Path:
    caminho = tmp_path / nome
    caminho.write_bytes(b"onnx")
    return caminho


def test_caminho_explicito_tem_prioridade(tmp_path: Path) -> None:
    pesos = _pesos_falsos(tmp_path)

    assert resolver_modelo(pesos) == pesos


def test_caminho_explicito_inexistente_falha(tmp_path: Path) -> None:
    with pytest.raises(ModeloIndisponivelError, match="não encontrado"):
        resolver_modelo(tmp_path / "ausente.onnx")


def test_usa_variavel_de_ambiente(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pesos = _pesos_falsos(tmp_path)
    monkeypatch.setenv(ENV_MODELO_PATH, str(pesos))

    assert resolver_modelo() == pesos


def test_variavel_de_ambiente_invalida_falha(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(ENV_MODELO_PATH, str(tmp_path / "ausente.onnx"))

    with pytest.raises(ModeloIndisponivelError, match=ENV_MODELO_PATH):
        resolver_modelo()


def test_usa_cache_local_da_poc(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(ENV_MODELO_PATH, raising=False)
    cache = _pesos_falsos(tmp_path)
    monkeypatch.setattr("modelo.CAMINHO_CACHE", cache)

    assert resolver_modelo() == cache


def test_sem_cache_e_sem_download_falha(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(ENV_MODELO_PATH, raising=False)
    monkeypatch.setattr("modelo.CAMINHO_CACHE", tmp_path / "vazio.onnx")

    with pytest.raises(ModeloIndisponivelError, match="download desabilitado"):
        resolver_modelo(permitir_download=False)
