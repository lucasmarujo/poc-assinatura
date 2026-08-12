"""Processa todos os documentos de `files/` com a tool de detecção de assinatura.

    poc-assinatura$ .venv/Scripts/python processar.py          # Windows
    poc-assinatura$ ./.venv/bin/python processar.py            # Linux/macOS

Varre `files/` inteira (subpastas em qualquer profundidade), extrai os `.zip` e
roda a cascata Nível 0 → Nível 1 em cada PDF, DOCX, PPTX ou imagem, **um
documento por vez**, escrevendo em `resultados/`:

    RESUMO.md         contagens, desempenho, hardware e erros
    RESUMO.json       o mesmo, para consumo por outro sistema
    COMPLETO.md       uma linha por documento (✅ / ❌ / ⚠️)
    resultados.jsonl  checkpoint — é dele que os relatórios são gerados
    logs/             eventos em JSONL, um arquivo por processo

O `resultados.jsonl` é também a retomada: interrompendo o lote (Ctrl+C, queda de
energia), basta rodar de novo — os documentos já processados são pulados.

Opções mais usadas:

    --max-paginas 30       teto de páginas por documento no Nível 1 (0 = sem teto)
    --escalonar            para no Nível 0 quando ele já achou (mais rápido,
                           mas o relatório deixa de comparar N0 × N1)
    --sem-nivel1           só o Nível 0 (não precisa dos pesos ONNX)
    --sem-fallback         desliga o segundo passe em ladrilhos 3×3 (mais rápido,
                           mas volta a perder rubrica de traço fino)
    --reprocessar          ignora o checkpoint e recomeça do zero
    --apenas-relatorio     regera os relatórios do checkpoint, sem processar
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from processamento import RAIZ
from processamento.lote import (
    ExecucaoEmAndamentoError,
    OpcoesDeteccao,
    configurar_log,
    processar_lote,
    validar_ambiente,
)
from processamento.recursos import AmostradorRecursos
from processamento.relatorio import EMOJI, gerar

FILES_DEFAULT = RAIZ / "files"
SAIDA_DEFAULT = RAIZ / "resultados"


def _preparar_console() -> None:
    """Console em UTF-8 e tolerante a caractere que ele não sabe desenhar.

    No Windows o stdout sai em cp1252 com `errors='strict'`: um `→` no texto de
    ajuda derruba o programa com `UnicodeEncodeError` antes de qualquer
    processamento. `errors="replace"` garante que nenhuma saída de console —
    nome de arquivo acentuado, emoji de status — consiga interromper um lote de
    horas. Os relatórios são escritos em UTF-8 à parte e não dependem disso.
    """
    for fluxo in (sys.stdout, sys.stderr):
        if hasattr(fluxo, "reconfigure"):
            fluxo.reconfigure(encoding="utf-8", errors="replace")


_preparar_console()

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--files", type=Path, default=FILES_DEFAULT)
    parser.add_argument("--saida", type=Path, default=SAIDA_DEFAULT)
    parser.add_argument("--modelo", type=str, default=None)
    parser.add_argument("--conf", type=float, default=0.15)
    parser.add_argument("--iou", type=float, default=0.5)
    parser.add_argument("--dpi", type=int, default=150)
    parser.add_argument("--densidade-minima", type=float, default=0.0005)
    parser.add_argument("--max-paginas", type=int, default=30, help="0 = sem teto")
    parser.add_argument("--escalonar", action="store_true")
    parser.add_argument("--sem-nivel1", action="store_true")
    parser.add_argument(
        "--sem-fallback",
        action="store_true",
        help="desliga o segundo passe em ladrilhos 3×3 nos documentos sem assinatura",
    )
    parser.add_argument("--dpi-fallback", type=int, default=300, help="DPI de cada ladrilho")
    parser.add_argument("--tentativas", type=int, default=2, help="retentativas por documento")
    parser.add_argument("--intervalo-progresso", type=int, default=25)
    parser.add_argument("--reprocessar", action="store_true")
    parser.add_argument("--apenas-relatorio", action="store_true")
    return parser.parse_args(argv)


def _opcoes(args: argparse.Namespace) -> OpcoesDeteccao:
    return OpcoesDeteccao(
        modelo=args.modelo,
        confianca_minima=args.conf,
        iou_maximo=args.iou,
        dpi_render=args.dpi,
        densidade_minima=args.densidade_minima,
        max_paginas=args.max_paginas if args.max_paginas > 0 else None,
        escalonar=args.escalonar,
        executar_nivel1=not args.sem_nivel1,
        fallback_ladrilhos=not args.sem_fallback,
        dpi_ladrilho=args.dpi_fallback,
    )


def _progresso(intervalo: int, inicio: float) -> Any:
    """Callback de progresso: o que falhou aparece na hora, o resto a cada
    `intervalo` documentos.

    Sem emoji aqui de propósito: o console do Windows é cp1252 e degradaria
    "⚠️" para escape. Nos relatórios (`.md`, escritos em UTF-8) o emoji sai
    inteiro.
    """

    def imprimir(registro: dict[str, Any], feitos: int, total: int) -> None:
        if EMOJI.get(registro["status"], "⚠️") == "⚠️":
            print(
                f"  [ALERTA] {registro['arquivo']} - "
                f"{registro['erro'] or registro['status']}",
                file=sys.stderr,
            )
        # O primeiro documento sai sempre: é o sinal de que os workers subiram e
        # o lote está andando. Sem isso o console fica mudo durante a carga do
        # modelo em cada worker, e um lote longo parece travado.
        if feitos > 1 and feitos % intervalo and feitos != total:
            return
        decorrido = time.perf_counter() - inicio
        ritmo = feitos / decorrido if decorrido else 0.0
        # Estimativa só depois da primeira janela: extrapolar o ritmo de um
        # documento (que ainda carrega o boot dos workers) daria um número
        # assustador e errado.
        estimativa = (
            f", restam ~{(total - feitos) / ritmo / 60:.0f} min"
            if feitos >= intervalo and ritmo
            else ""
        )
        print(
            f"[{feitos}/{total}] {ritmo:.1f} doc/s — "
            f"decorrido {decorrido / 60:.1f} min{estimativa}",
            file=sys.stderr,
        )

    return imprimir


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    pasta_files = args.files.expanduser()
    pasta_saida = args.saida.expanduser()
    pasta_logs = pasta_saida / "logs"
    jsonl = pasta_saida / "resultados.jsonl"
    execucao_json = pasta_saida / "execucao.json"

    pasta_saida.mkdir(parents=True, exist_ok=True)
    configurar_log(pasta_logs / "lote.jsonl")

    if args.apenas_relatorio:
        execucao = _ler_execucao(execucao_json)
        caminhos = gerar(jsonl, pasta_saida, execucao)
        _imprimir_saidas(caminhos)
        return 0

    if not pasta_files.is_dir():
        print(f"Pasta de documentos não encontrada: {pasta_files}", file=sys.stderr)
        return 1

    opcoes = _opcoes(args)
    try:
        validar_ambiente(opcoes)
    except Exception as exc:
        print(
            f"Nível 1 indisponível ({type(exc).__name__}): {exc}\n"
            "Resolva os pesos (`python tool_validar_assinatura/modelo.py`), aponte "
            "`POC_ASSINATURA_MODELO_PATH`/`--modelo`, ou rode com `--sem-nivel1`.",
            file=sys.stderr,
        )
        return 1

    inicio_relogio = datetime.now()
    inicio = time.perf_counter()
    print(
        f"Varrendo `{pasta_files}` e extraindo os `.zip` "
        f"(Nível 1 {'desligado' if args.sem_nivel1 else 'ligado'})...",
        file=sys.stderr,
    )

    def anunciar(encontrados: int, pendentes: int, zips: int) -> None:
        print(
            f"{encontrados} arquivos encontrados ({zips} zips), "
            f"{encontrados - pendentes} já no checkpoint. "
            f"Processando {pendentes} em sequência...",
            file=sys.stderr,
        )

    with AmostradorRecursos() as amostrador:
        try:
            metadados = processar_lote(
                raiz=pasta_files,
                saida_jsonl=jsonl,
                opcoes=opcoes,
                tentativas_maximas=args.tentativas,
                reprocessar=args.reprocessar,
                ao_progredir=_progresso(max(args.intervalo_progresso, 1), inicio),
                ao_listar=anunciar,
            )
        except ExecucaoEmAndamentoError as exc:
            print(f"\n{exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print(
                "\nInterrompido. O checkpoint foi preservado — rode de novo para "
                "continuar de onde parou.",
                file=sys.stderr,
            )
            return 130

    recursos = amostrador.resumo()
    if not recursos["amostras"]:
        # Execução que só retomou o checkpoint (nada a processar) não tem o que
        # amostrar — preserva o hardware medido na execução que fez o trabalho.
        recursos = _ler_execucao(execucao_json).get("recursos") or recursos

    execucao: dict[str, Any] = {
        **metadados,
        "inicio": inicio_relogio.isoformat(timespec="seconds"),
        "fim": datetime.now().isoformat(timespec="seconds"),
        "recursos": recursos,
    }
    execucao_json.write_text(
        json.dumps(execucao, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    caminhos = gerar(jsonl, pasta_saida, execucao)
    _imprimir_saidas(caminhos)
    return 0


def _ler_execucao(caminho: Path) -> dict[str, Any]:
    if not caminho.is_file():
        return {}
    try:
        return dict(json.loads(caminho.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return {}


def _imprimir_saidas(caminhos: dict[str, Path]) -> None:
    print("\nRelatórios gerados:", file=sys.stderr)
    for caminho in caminhos.values():
        print(f"  {caminho}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
