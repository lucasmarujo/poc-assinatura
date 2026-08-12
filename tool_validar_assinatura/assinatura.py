"""Ferramenta de detecção de assinatura — versão da PoC, sem Textract.

Mesmo contrato da tool em produção (`motor_ia.tools.assinatura`): mesmo `name`,
mesmos `Args`, mesmo texto de retorno (`<assinaturas_detectadas>`). O que muda é
**quem detecta**: no lugar da feature `SIGNATURES` do AWS Textract (cobrada por
página), a detecção é a cascata local Nível 0 → Nível 1 (`deteccao.py`).

Continua valendo a divisão de responsabilidade original: a tool **só detecta**.
Decidir se o requisito exige assinatura é da LLM (pipeline de análise) ou do
usuário no chat.
"""

from __future__ import annotations

import os
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from deteccao import detectar_assinaturas_async, formatar_contexto
from pydantic import BaseModel, Field

from motor_ia.agent.tools import Tool, ToolContext

if TYPE_CHECKING:
    from motor_ia.storage.types import Storage

_logger = structlog.get_logger(__name__)


# ---------- Núcleo: detecção ---------------------------------------------------


@dataclass(frozen=True)
class SignatureToolResult:
    """Resultado consolidado da ferramenta de detecção de assinatura.

    `deteccao` — resultado da cascata em formato dicionário (ou `None` quando
        não há documento disponível, ou a detecção falhou).
    `texto` — bloco pronto para devolver ao LLM como `tool_result`.
    """

    deteccao: dict[str, Any] | None
    texto: str


_TEXTO_SEM_DOCUMENTO = (
    "Resultado da ferramenta de detecção de assinatura: nenhum documento está "
    "disponível para detecção neste contexto."
)

_TEXTO_DETECCAO_FALHOU = (
    "Resultado da ferramenta de detecção de assinatura: a detecção falhou neste "
    "documento — trate como evidência indisponível."
)


async def executar_deteccao_assinatura(
    *,
    documento_path: str | None = None,
    documento_texto: str | None = None,
    incluir_visual: bool = True,
) -> SignatureToolResult:
    """Detecta assinaturas no documento (fonte única, usada pela tool e pelo
    analisador).

    `incluir_visual=False` mantém a detecção apenas no Nível 0 (nenhuma
    inferência). Sem documento → mensagem clara. Falha da detecção não derruba
    o fluxo (devolve `deteccao=None`).
    """
    if not documento_path:
        return SignatureToolResult(deteccao=None, texto=_TEXTO_SEM_DOCUMENTO)

    try:
        resultado = await detectar_assinaturas_async(
            Path(documento_path).expanduser(),
            texto_extraido=documento_texto,
            executar_nivel1=incluir_visual,
        )
    except Exception as e:
        _logger.warning(
            "assinatura.detect_failed",
            document=documento_path,
            error=str(e),
            error_type=type(e).__name__,
        )
        return SignatureToolResult(deteccao=None, texto=_TEXTO_DETECCAO_FALHOU)

    return SignatureToolResult(deteccao=resultado.para_dict(), texto=formatar_contexto(resultado))


# ---------- Tool --------------------------------------------------------------


class ValidarAssinaturaArgs(BaseModel):
    s3_key: str | None = Field(
        default=None,
        description=(
            "Chave S3 do documento anexado a inspecionar — copie o `s3_key=...` "
            "do bloco [Anexos da conversa atual]. Quando ausente, a ferramenta usa "
            "o documento ativo do contexto (análise), se houver."
        ),
    )
    incluir_visual: bool = Field(
        default=True,
        description=(
            "Inclui a detecção visual de rubrica (modelo local em CPU, sem custo "
            "por página). Desligue para checar apenas assinatura digital/embedded."
        ),
    )


class ValidarAssinaturaTool(Tool):
    name = "validar_assinatura"
    description = (
        "Detect signatures in a document and report whether signatures are present, "
        "their sources (visual detection / digital text markers / embedded PDF fields) "
        "and signatories. Call it when a compliance requirement (requisito) depends on a "
        "signature being present — BEFORE concluding such a criterion. When the "
        "conversation has an attachment, pass its `s3_key` (from the [Anexos da "
        "conversa atual] block) so the tool can fetch and inspect that file; otherwise "
        "it uses the active-context document, if any. The tool only detects signatures "
        "— deciding whether the requirement needs one is your job."
    )
    Args = ValidarAssinaturaArgs

    def __init__(self, *, storage: Storage | None = None) -> None:
        self._storage = storage

    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        assert isinstance(args, ValidarAssinaturaArgs)

        # Documento a inspecionar: baixa o anexo do S3 pela chave informada pelo
        # modelo. Sem `s3_key`, não há documento local para detecção.
        documento_path: str | None = None
        tmp_path: str | None = None
        if args.s3_key:
            tmp_path = await self._baixar_anexo(args.s3_key)
            if tmp_path is None:
                return (
                    "validar_assinatura: não consegui acessar o anexo "
                    f"`{args.s3_key}` no storage — trate como evidência indisponível."
                )
            documento_path = tmp_path

        try:
            result = await executar_deteccao_assinatura(
                documento_path=documento_path,
                incluir_visual=args.incluir_visual,
            )
        finally:
            if tmp_path:
                with suppress(OSError):
                    os.unlink(tmp_path)
        return result.texto

    async def _baixar_anexo(self, s3_key: str) -> str | None:
        """Baixa o objeto S3 pra um arquivo temporário local e devolve o path
        (a detecção precisa do arquivo p/ raster e campos do AcroForm). `None` em
        falha — a tool degrada para evidência indisponível."""
        if self._storage is None:
            return None
        try:
            data = await self._storage.get(s3_key)
        except Exception as e:
            _logger.warning(
                "assinatura.anexo_download_failed",
                s3_key=s3_key,
                error=str(e),
                error_type=type(e).__name__,
            )
            return None
        suffix = Path(s3_key).suffix or ".pdf"
        fd, path = tempfile.mkstemp(suffix=suffix)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
        except OSError:
            with suppress(OSError):
                os.unlink(path)
            return None
        return path
