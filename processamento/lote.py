"""Execução do lote: um documento por vez, com checkpoint e retry.

**Sequencial de propósito.** A versão paralela existia e foi removida: um pool
de processos carrega uma sessão ONNX por worker (~0,6 GB de commit cada) e
derruba a máquina com "paging file too small" muito antes de saturar a CPU —
e traz junto pool quebrado, watchdog, worker órfão e diagnóstico de memória,
tudo para acelerar um lote que já cabe numa noite. Aqui roda um processo, uma
sessão, memória constante, e nada disso pode acontecer.

O que sobra é o que importa em lote grande:

* **Checkpoint em JSONL, uma linha por documento, com flush.** Um lote de horas
  não pode perder o trabalho feito numa queda; o reinício pula o que já está no
  arquivo.
* **Erro é por documento.** Documento ruim vira ⚠️ com o motivo na linha dele e
  o lote segue.
* **Retry só do que pode dar certo na segunda vez.** Formato não suportado e
  arquivo corrompido são determinísticos: repetir só queima CPU.
* **Uma execução por vez** (`resultados/.lock`), senão dois lotes duplicam
  registros no mesmo checkpoint.
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter, deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from processamento import documentos
from processamento.documentos import DocumentoInvalidoError, FormatoNaoSuportadoError

_logger = structlog.get_logger(__name__)

STATUS_ASSINADO = "assinado"
STATUS_SEM_ASSINATURA = "sem_assinatura"
STATUS_INDETERMINADO = "indeterminado"
STATUS_ERRO = "erro"
STATUS_NAO_SUPORTADO = "nao_suportado"
# `.zip` não é documento: é caixa. Vira uma linha própria apontando para a pasta
# extraída, sem contaminar as contagens de ✅/❌/⚠️.
STATUS_CONTAINER = "container"


@dataclass(frozen=True)
class OpcoesDeteccao:
    """Parâmetros da detecção, iguais para todo o lote."""

    modelo: str | None = None
    confianca_minima: float = 0.15
    iou_maximo: float = 0.5
    dpi_render: int = 150
    densidade_minima: float = 0.0005
    max_paginas: int | None = 30
    escalonar: bool = False
    executar_nivel1: bool = True
    fallback_ladrilhos: bool = True
    dpi_ladrilho: int = 300


class ExecucaoEmAndamentoError(RuntimeError):
    """Já existe um lote rodando sobre este checkpoint."""


# ---------- Trava de execução -------------------------------------------------


def _dono_da_trava(caminho: Path) -> int | None:
    """PID do processo que segura a trava, ou `None` se a trava está órfã.

    Compara também o instante de criação do processo: em máquina que reusa PID,
    só o par (pid, início) identifica o processo de verdade.
    """
    import psutil

    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
        processo = psutil.Process(int(dados["pid"]))
        if abs(processo.create_time() - float(dados["iniciado_em"])) < 1.0:
            return int(dados["pid"])
    except (OSError, ValueError, KeyError, json.JSONDecodeError, psutil.Error):
        return None
    return None


@contextmanager
def trava_da_execucao(pasta: Path) -> Iterator[None]:
    """Impede duas execuções simultâneas sobre o mesmo checkpoint.

    Dois lotes escrevendo no mesmo `resultados.jsonl` não corrompem o arquivo
    (cada linha é um append), mas duplicam registros e bagunçam as contagens.

    Trava de execução que morreu (queda, kill) é órfã e é assumida sem
    reclamar: o critério é o processo dono estar vivo, não o arquivo existir.
    """
    import psutil

    caminho = pasta / ".lock"
    pasta.mkdir(parents=True, exist_ok=True)
    marca = json.dumps(
        {"pid": os.getpid(), "iniciado_em": psutil.Process().create_time()}, ensure_ascii=False
    )

    for _ in range(2):
        try:
            with caminho.open("x", encoding="utf-8") as arquivo:
                arquivo.write(marca)
            break
        except FileExistsError:
            dono = _dono_da_trava(caminho)
            if dono is not None:
                raise ExecucaoEmAndamentoError(
                    f"já existe um lote rodando neste `{pasta}` (PID {dono}). "
                    "Espere terminar, ou use `--saida` para outra pasta."
                ) from None
            _logger.warning("lote.trava_orfa_assumida", caminho=str(caminho))
            caminho.unlink(missing_ok=True)
    else:
        raise ExecucaoEmAndamentoError(f"não consegui tomar a trava em `{caminho}`.")

    try:
        yield
    finally:
        caminho.unlink(missing_ok=True)


def configurar_log(caminho: Path) -> None:
    """structlog em JSONL, num arquivo só (a execução é de um processo)."""
    caminho.parent.mkdir(parents=True, exist_ok=True)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.WriteLoggerFactory(file=caminho.open("a", encoding="utf-8")),
        cache_logger_on_first_use=True,
    )


# ---------- Um documento ------------------------------------------------------


def _registro_base(relativo: str, caminho: Path) -> dict[str, Any]:
    return {
        "arquivo": relativo,
        "formato": caminho.suffix.lower() or "(sem extensão)",
        "tipo": documentos.classificar(caminho),
        "tamanho_bytes": caminho.stat().st_size if caminho.exists() else 0,
        "status": STATUS_ERRO,
        "assinaturas_total": 0,
        "nivel0_total": 0,
        "nivel1_total": 0,
        "fontes": [],
        "signatarios": [],
        "paginas": 0,
        "paginas_em_branco": 0,
        "paginas_analisadas_nivel1": 0,
        "paginas_ladrilhadas": 0,
        "paginas_limitadas": False,
        "tempo_ms": 0.0,
        "tempo_inferencia_ms": 0.0,
        "nivel1_erro": None,
        "observacao": None,
        "erro": None,
        "erro_tipo": None,
        "retentavel": True,
        "tentativas": 1,
        "processado_em": datetime.now().isoformat(timespec="seconds"),
    }


def processar_documento(caminho: Path, relativo: str, opcoes: OpcoesDeteccao) -> dict[str, Any]:
    """Roda a cascata num documento e devolve a linha do relatório.

    Nenhuma exceção escapa: erro de documento vira registro com `status=erro`,
    porque um documento ruim não pode derrubar o lote.
    """
    from deteccao import detectar_assinaturas

    registro = _registro_base(relativo, caminho)
    inicio = time.perf_counter()

    try:
        with documentos.preparar(caminho) as pronto:
            resultado = detectar_assinaturas(
                pronto.path,
                texto_extraido=pronto.texto,
                executar_nivel1=opcoes.executar_nivel1,
                escalonar=opcoes.escalonar,
                modelo_path=opcoes.modelo,
                confianca_minima=opcoes.confianca_minima,
                iou_maximo=opcoes.iou_maximo,
                dpi_render=opcoes.dpi_render,
                densidade_minima=opcoes.densidade_minima,
                max_paginas=opcoes.max_paginas,
                fallback_ladrilhos=opcoes.fallback_ladrilhos,
                dpi_ladrilho=opcoes.dpi_ladrilho,
            )
    except FormatoNaoSuportadoError as exc:
        registro.update(
            status=STATUS_NAO_SUPORTADO,
            erro=str(exc),
            erro_tipo=type(exc).__name__,
            retentavel=False,
        )
    except DocumentoInvalidoError as exc:
        registro.update(erro=str(exc), erro_tipo=type(exc).__name__, retentavel=False)
    except Exception as exc:
        registro.update(erro=str(exc), erro_tipo=type(exc).__name__)
        _logger.warning(
            "lote.documento_falhou",
            arquivo=relativo,
            error=str(exc),
            error_type=type(exc).__name__,
        )
    else:
        registro.update(_metricas(resultado, max_paginas=opcoes.max_paginas))

    registro["tempo_ms"] = round((time.perf_counter() - inicio) * 1000.0, 1)
    return registro


def _metricas(resultado: Any, *, max_paginas: int | None) -> dict[str, Any]:
    """Traduz o `ResultadoDeteccao` da tool para as colunas do relatório."""
    nivel0, nivel1 = resultado.nivel0, resultado.nivel1
    total = resultado.total_assinaturas
    if total:
        status = STATUS_ASSINADO
    elif resultado.nivel1_erro:
        # Sem evidência visual não dá para afirmar ausência — é indeterminado,
        # não "sem assinatura".
        status = STATUS_INDETERMINADO
    else:
        status = STATUS_SEM_ASSINATURA

    return {
        "status": status,
        "assinaturas_total": total,
        "nivel0_total": nivel0.total,
        "nivel1_total": nivel1.total if nivel1 else 0,
        "fontes": resultado.fontes,
        "signatarios": resultado.signatarios,
        "paginas": len(nivel0.paginas),
        "paginas_em_branco": len(nivel0.paginas_em_branco),
        "paginas_analisadas_nivel1": len(nivel1.paginas_analisadas) if nivel1 else 0,
        # Só é > 0 em documento que zerou no passe normal, então `assinaturas` aqui
        # significa "resgatada pelo fallback" — não precisa marcar detecção a
        # detecção para medir o ganho.
        "paginas_ladrilhadas": len(nivel1.paginas_ladrilhadas) if nivel1 else 0,
        "paginas_limitadas": max_paginas is not None and len(nivel0.paginas_uteis) > max_paginas,
        "tempo_inferencia_ms": nivel1.tempo_inferencia_ms if nivel1 else 0.0,
        "nivel1_erro": resultado.nivel1_erro,
    }


def _registro_zip(expandido: documentos.ZipExpandido, raiz: Path) -> dict[str, Any]:
    """Linha do relatório para um `.zip`: caixa aberta, não documento avaliado."""
    registro = _registro_base(str(expandido.origem.relative_to(raiz)), expandido.origem)
    destino = expandido.destino.relative_to(raiz)
    if expandido.erro:
        registro.update(
            status=STATUS_ERRO,
            erro=f"falha ao extrair o zip: {expandido.erro}",
            erro_tipo="ZipInvalidoError",
            retentavel=False,
        )
        return registro

    registro.update(
        status=STATUS_CONTAINER,
        observacao=(
            f"pasta `{destino}` já existia — o conteúdo tem linha própria"
            if expandido.ja_existia
            else f"extraído em `{destino}` ({expandido.arquivos} arquivos) — "
            "o conteúdo tem linha própria"
        ),
    )
    return registro


# ---------- Checkpoint --------------------------------------------------------


def _voltou_a_ser_suportado(registro: dict[str, Any]) -> bool:
    """Registro antigo de "formato não suportado" cujo formato passou a ser
    suportado — precisa ser reavaliado na retomada em vez de pulado.

    Converge: depois da reavaliação o registro deixa de ser `nao_suportado`, e
    os formatos que continuam fora (`.xls`, `.doc`) seguem pulados para sempre.
    """
    if registro.get("status") != STATUS_NAO_SUPORTADO:
        return False
    formato = registro.get("formato", "")
    return formato in documentos.EXTENSOES_SUPORTADAS or formato in documentos.EXTENSOES_ZIP


def carregar_concluidos(jsonl: Path) -> set[str]:
    """Documentos já gravados no checkpoint (retomada).

    Linha corrompida — o processo pode ter morrido no meio de uma escrita — é
    ignorada: o documento correspondente simplesmente será reprocessado.
    """
    if not jsonl.is_file():
        return set()
    concluidos: set[str] = set()
    with jsonl.open(encoding="utf-8") as arquivo:
        for linha in arquivo:
            linha = linha.strip()
            if not linha:
                continue
            try:
                registro = json.loads(linha)
                if _voltou_a_ser_suportado(registro):
                    continue
                concluidos.add(registro["arquivo"])
            except (json.JSONDecodeError, KeyError):
                continue
    return concluidos


def validar_ambiente(opcoes: OpcoesDeteccao) -> None:
    """Falha cedo se o Nível 1 está ligado e indisponível.

    Sem isso o lote inteiro rodaria e produziria um relatório aparentemente
    completo, com "assinaturas N1 = 0" em todo documento — a degradação
    silenciosa que a tool faz por design em produção é exatamente o que NÃO se
    quer num relatório de auditoria.
    """
    if not opcoes.executar_nivel1:
        return
    from nivel1 import obter_detector

    obter_detector(opcoes.modelo)


# ---------- O lote ------------------------------------------------------------


def processar_lote(
    *,
    raiz: Path,
    saida_jsonl: Path,
    opcoes: OpcoesDeteccao,
    tentativas_maximas: int = 2,
    reprocessar: bool = False,
    ao_progredir: Callable[[dict[str, Any], int, int], None] | None = None,
    ao_listar: Callable[[int, int, int], None] | None = None,
) -> dict[str, Any]:
    """Processa todos os documentos sob `raiz`, gravando um JSONL por documento.

    Devolve os metadados da execução (contagens, tempo, o que foi retomado).
    """
    saida_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with trava_da_execucao(saida_jsonl.parent):
        return _processar_lote(
            raiz=raiz,
            saida_jsonl=saida_jsonl,
            opcoes=opcoes,
            tentativas_maximas=tentativas_maximas,
            reprocessar=reprocessar,
            ao_progredir=ao_progredir,
            ao_listar=ao_listar,
        )


def _processar_lote(
    *,
    raiz: Path,
    saida_jsonl: Path,
    opcoes: OpcoesDeteccao,
    tentativas_maximas: int,
    reprocessar: bool,
    ao_progredir: Callable[[dict[str, Any], int, int], None] | None,
    ao_listar: Callable[[int, int, int], None] | None,
) -> dict[str, Any]:
    """Corpo do lote, já sob a trava de execução."""
    if reprocessar:
        saida_jsonl.unlink(missing_ok=True)

    # Os `.zip` são abertos antes da varredura, senão o que está dentro deles não
    # existiria para ser descoberto.
    zips = documentos.expandir_zips(raiz)
    todos = documentos.listar_documentos(raiz)
    concluidos = carregar_concluidos(saida_jsonl)

    zips_por_relativo = {str(z.origem.relative_to(raiz)): z for z in zips}
    fila: deque[str] = deque(
        relativo
        for relativo in (str(caminho.relative_to(raiz)) for caminho in todos)
        if relativo not in concluidos and relativo not in zips_por_relativo
    )
    pendentes_zip = [
        expandido
        for relativo, expandido in zips_por_relativo.items()
        if relativo not in concluidos
    ]
    total_pendente = len(fila) + len(pendentes_zip)
    _logger.info(
        "lote.iniciado",
        raiz=str(raiz),
        documentos=len(todos),
        zips=len(zips),
        pendentes=total_pendente,
        retomados=len(concluidos),
    )
    if ao_listar is not None:
        ao_listar(len(todos), total_pendente, len(zips))

    # Nome do documento em processamento. Se o lote parecer travado, é este
    # arquivo que diz em qual documento olhar — sem ele, "parou" não tem culpado.
    atual = saida_jsonl.parent / ".atual"
    tentativas: Counter[str] = Counter()
    processados = 0
    inicio = time.perf_counter()

    with saida_jsonl.open("a", encoding="utf-8") as checkpoint:

        def gravar(registro: dict[str, Any]) -> None:
            nonlocal processados
            registro["tentativas"] = tentativas[registro["arquivo"]] + 1
            checkpoint.write(json.dumps(registro, ensure_ascii=False) + "\n")
            checkpoint.flush()
            processados += 1
            if ao_progredir is not None:
                ao_progredir(registro, processados, total_pendente)

        for expandido in pendentes_zip:
            gravar(_registro_zip(expandido, raiz))

        while fila:
            relativo = fila.popleft()
            atual.write_text(relativo, encoding="utf-8")
            registro = processar_documento(raiz / relativo, relativo, opcoes)

            if (
                registro["status"] == STATUS_ERRO
                and registro["retentavel"]
                and tentativas[relativo] < tentativas_maximas
            ):
                # Volta para o fim da fila: é a espera natural entre tentativas e
                # tira da frente o documento que acabou de falhar.
                tentativas[relativo] += 1
                fila.append(relativo)
                _logger.warning(
                    "lote.retry",
                    arquivo=relativo,
                    tentativa=tentativas[relativo],
                    motivo=registro["erro"],
                )
                continue

            gravar(registro)

    atual.unlink(missing_ok=True)
    decorrido = time.perf_counter() - inicio
    _logger.info("lote.concluido", processados=processados, segundos=round(decorrido, 1))
    return {
        "documentos_encontrados": len(todos),
        "zips_expandidos": sum(1 for z in zips if not z.ja_existia and not z.erro),
        "arquivos_extraidos_de_zip": sum(z.arquivos for z in zips),
        "documentos_retomados": len(concluidos),
        "documentos_processados": processados,
        "segundos_totais": round(decorrido, 1),
        "opcoes": asdict(opcoes),
    }
