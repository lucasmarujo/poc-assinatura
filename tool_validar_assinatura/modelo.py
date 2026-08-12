"""Resolução do arquivo de pesos do detector visual local (Nível 1).

O Nível 1 roda o `tech4humans/yolov8s-signature-detector` exportado para ONNX
(`yolov8s.onnx`, ~44 MB) via ONNX Runtime em CPU. Como o repositório no Hugging
Face é *gated* (`gated: auto` — exige conta logada que aceitou os termos AGPL-3.0
do modelo), a resolução do arquivo é explícita e em cascata:

  1. caminho passado pelo chamador (`--modelo` no script de teste);
  2. env `POC_ASSINATURA_MODELO_PATH`;
  3. cache local da PoC (`tool_validar_assinatura/models/yolov8s.onnx`);
  4. download do Hugging Face usando o token (`HF_TOKEN`,
     `HUGGINGFACE_HUB_TOKEN` ou `~/.cache/huggingface/token`).

Em produção o passo 4 não deve existir: a imagem carrega o `.onnx` junto do
build (ou o baixa de um bucket interno) e aponta `POC_ASSINATURA_MODELO_PATH`.
Baixar do HF em runtime acopla o boot do worker a um serviço externo.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import structlog

_logger = structlog.get_logger(__name__)

HF_REPO_ID = "tech4humans/yolov8s-signature-detector"
HF_FILENAME = "yolov8s.onnx"
HF_REVISION = "main"

ENV_MODELO_PATH = "POC_ASSINATURA_MODELO_PATH"
ENV_TOKENS_HF = ("HF_TOKEN", "HUGGINGFACE_HUB_TOKEN", "HUGGING_FACE_HUB_TOKEN")

DIRETORIO_CACHE = Path(__file__).resolve().parent / "models"
CAMINHO_CACHE = DIRETORIO_CACHE / HF_FILENAME

# O `.onnx` do yolov8s tem ~44 MB. Qualquer coisa muito menor é página de erro
# do HF salva como arquivo (401/403 devolvem corpo texto curto).
_TAMANHO_MINIMO_BYTES = 10 * 1024 * 1024

_TIMEOUT_DOWNLOAD_SEGUNDOS = 300.0
_CHUNK_BYTES = 1024 * 1024


class ModeloIndisponivelError(RuntimeError):
    """Os pesos do detector visual não estão acessíveis localmente."""


def _token_hf() -> str | None:
    """Token do Hugging Face por env ou pelo cache do `huggingface-cli login`."""
    for nome in ENV_TOKENS_HF:
        valor = os.getenv(nome)
        if valor and valor.strip():
            return valor.strip()
    cache_token = Path.home() / ".cache" / "huggingface" / "token"
    if cache_token.is_file():
        conteudo = cache_token.read_text(encoding="utf-8").strip()
        if conteudo:
            return conteudo
    return None


def _url_hf() -> str:
    return f"https://huggingface.co/{HF_REPO_ID}/resolve/{HF_REVISION}/{HF_FILENAME}"


def _mensagem_acesso_negado(status: int) -> str:
    return (
        f"Download de `{HF_FILENAME}` recusado pelo Hugging Face (HTTP {status}). "
        f"O repositório `{HF_REPO_ID}` é gated: é preciso (1) aceitar os termos em "
        f"https://huggingface.co/{HF_REPO_ID} com a conta logada e (2) usar um token "
        "com permissão de leitura de repositórios gated públicos "
        "(`HF_TOKEN=hf_...`). Alternativa: baixar o arquivo manualmente e apontar "
        f"`{ENV_MODELO_PATH}=/caminho/{HF_FILENAME}` (ou `--modelo`)."
    )


def baixar_modelo(destino: Path | None = None) -> Path:
    """Baixa `yolov8s.onnx` do Hugging Face para o cache local da PoC.

    Escreve num `.part` e renomeia no fim, para que uma interrupção não deixe
    um arquivo truncado passando por cache válido.
    """
    alvo = destino or CAMINHO_CACHE
    alvo.parent.mkdir(parents=True, exist_ok=True)
    parcial = alvo.with_suffix(alvo.suffix + ".part")

    token = _token_hf()
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    _logger.info("poc_assinatura.modelo_download_iniciado", repo=HF_REPO_ID, destino=str(alvo))
    try:
        with (
            httpx.Client(follow_redirects=True, timeout=_TIMEOUT_DOWNLOAD_SEGUNDOS) as client,
            client.stream("GET", _url_hf(), headers=headers) as resposta,
        ):
            if resposta.status_code in (401, 403):
                raise ModeloIndisponivelError(_mensagem_acesso_negado(resposta.status_code))
            resposta.raise_for_status()
            with parcial.open("wb") as fh:
                for chunk in resposta.iter_bytes(_CHUNK_BYTES):
                    fh.write(chunk)
    except httpx.HTTPError as exc:
        parcial.unlink(missing_ok=True)
        raise ModeloIndisponivelError(
            f"Falha de rede ao baixar `{HF_FILENAME}` de {HF_REPO_ID}: {exc}"
        ) from exc

    tamanho = parcial.stat().st_size
    if tamanho < _TAMANHO_MINIMO_BYTES:
        parcial.unlink(missing_ok=True)
        raise ModeloIndisponivelError(
            f"Arquivo baixado tem só {tamanho} bytes — esperado ~44 MB. "
            "Provavelmente é uma resposta de erro do Hugging Face, não os pesos."
        )

    parcial.replace(alvo)
    _logger.info("poc_assinatura.modelo_download_ok", destino=str(alvo), bytes=tamanho)
    return alvo


def resolver_modelo(caminho: str | Path | None = None, *, permitir_download: bool = True) -> Path:
    """Devolve o caminho local dos pesos ONNX, baixando-os se necessário.

    Levanta `ModeloIndisponivelError` quando o arquivo não existe e o download
    não é possível (sem rede, sem token, ou token sem acesso ao repo gated).
    """
    if caminho:
        explicito = Path(caminho).expanduser()
        if not explicito.is_file():
            raise ModeloIndisponivelError(f"Modelo ONNX não encontrado em `{explicito}`.")
        return explicito

    do_ambiente = os.getenv(ENV_MODELO_PATH)
    if do_ambiente:
        do_env = Path(do_ambiente).expanduser()
        if not do_env.is_file():
            raise ModeloIndisponivelError(
                f"`{ENV_MODELO_PATH}` aponta para `{do_env}`, que não existe."
            )
        return do_env

    if CAMINHO_CACHE.is_file():
        return CAMINHO_CACHE

    if not permitir_download:
        raise ModeloIndisponivelError(
            f"Modelo ONNX ausente em `{CAMINHO_CACHE}` e download desabilitado."
        )
    return baixar_modelo()


if __name__ == "__main__":
    print(resolver_modelo())
