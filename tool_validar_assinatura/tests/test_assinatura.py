"""Testes da tool `validar_assinatura` na versão sem Textract."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from assinatura import (
    SignatureToolResult,
    ValidarAssinaturaArgs,
    ValidarAssinaturaTool,
    executar_deteccao_assinatura,
)

from motor_ia.agent.tools import ToolContext


class _StorageFake:
    """Storage mínimo: devolve os bytes de `conteudo` ou levanta `erro`."""

    def __init__(self, conteudo: bytes | None = None, erro: Exception | None = None) -> None:
        self._conteudo = conteudo
        self._erro = erro
        self.chaves: list[str] = []

    async def get(self, key: str) -> bytes:
        self.chaves.append(key)
        if self._erro is not None:
            raise self._erro
        assert self._conteudo is not None
        return self._conteudo


@pytest.mark.asyncio
async def test_sem_documento_nao_detecta() -> None:
    """Sem documento no contexto → nada a detectar, mensagem clara."""
    result = await executar_deteccao_assinatura()

    assert result == SignatureToolResult(deteccao=None, texto=result.texto)
    assert "nenhum documento" in result.texto.lower()


@pytest.mark.asyncio
async def test_detecta_no_documento(pdf_carimbo_digital: Path) -> None:
    """Com documento disponível → roda a cascata e injeta o bloco de evidência."""
    result = await executar_deteccao_assinatura(
        documento_path=str(pdf_carimbo_digital), incluir_visual=False
    )

    assert result.deteccao is not None
    assert result.deteccao["tem_assinatura"] is True
    assert "<assinaturas_detectadas>" in result.texto
    assert "FULANO DE TAL SILVA" in result.texto


@pytest.mark.asyncio
async def test_texto_extraido_e_repassado_para_a_deteccao(
    monkeypatch: pytest.MonkeyPatch, pdf_formulario: Path
) -> None:
    async def _fake(path: Any, **kwargs: Any) -> Any:
        assert kwargs["texto_extraido"] == "Assinado digitalmente por MARIA SOUZA"
        assert kwargs["executar_nivel1"] is False
        raise RuntimeError("parou aqui de propósito")

    monkeypatch.setattr("assinatura.detectar_assinaturas_async", _fake)

    result = await executar_deteccao_assinatura(
        documento_path=str(pdf_formulario),
        documento_texto="Assinado digitalmente por MARIA SOUZA",
        incluir_visual=False,
    )

    assert result.deteccao is None


@pytest.mark.asyncio
async def test_deteccao_falha_nao_quebra(
    monkeypatch: pytest.MonkeyPatch, pdf_formulario: Path
) -> None:
    """Falha na detecção não derruba — devolve deteccao=None e mensagem clara."""

    async def _broken(*a: Any, **k: Any) -> Any:
        raise RuntimeError("pymupdf explodiu")

    monkeypatch.setattr("assinatura.detectar_assinaturas_async", _broken)

    result = await executar_deteccao_assinatura(documento_path=str(pdf_formulario))

    assert result.deteccao is None
    assert "falhou" in result.texto.lower()


@pytest.mark.asyncio
async def test_tool_sem_s3_key_reporta_ausencia_de_documento() -> None:
    tool = ValidarAssinaturaTool()

    texto = await tool.run(ValidarAssinaturaArgs(), ToolContext())

    assert "nenhum documento" in texto.lower()


@pytest.mark.asyncio
async def test_tool_baixa_anexo_e_detecta(pdf_carimbo_digital: Path) -> None:
    storage = _StorageFake(conteudo=pdf_carimbo_digital.read_bytes())
    tool = ValidarAssinaturaTool(storage=storage)

    texto = await tool.run(
        ValidarAssinaturaArgs(s3_key="clientes/1/carimbo.pdf", incluir_visual=False),
        ToolContext(),
    )

    assert storage.chaves == ["clientes/1/carimbo.pdf"]
    assert "Possui assinatura: SIM" in texto


@pytest.mark.asyncio
async def test_tool_com_storage_indisponivel_degrada() -> None:
    tool = ValidarAssinaturaTool(storage=_StorageFake(erro=RuntimeError("s3 down")))

    texto = await tool.run(ValidarAssinaturaArgs(s3_key="clientes/1/x.pdf"), ToolContext())

    assert "evidência indisponível" in texto
