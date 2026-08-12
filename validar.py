"""Mede a acurácia real do lote com rótulo humano sobre uma amostra estratificada.

    poc-assinatura$ .venv/Scripts/python validar.py amostrar      # Windows
    poc-assinatura$ ./.venv/bin/python validar.py amostrar        # Linux/macOS

Três passos:

    1) validar.py amostrar     sorteia a amostra e gera as imagens a rotular
    2) validar.py rotular      abre o rotulador no navegador: S / N / D por
                               documento, gravando a cada clique
    3) validar.py avaliar      cruza rótulo × veredito e escreve VALIDACAO.md

Nada em `files/` é lido para escrita em momento nenhum: a imagem que aparece no
rotulador é uma cópia renderizada em `auditoria/amostra/`, e rotular só a vincula
na pasta do rótulo, sem tirá-la de `pendentes/`.

O `resultados.jsonl` diz o que a tool decidiu; só o rótulo humano diz se ela
acertou. A amostra é estratificada (concentra esforço onde o erro provavelmente
está) e reponderada na hora de somar, então o número global não é enviesado pelo
oversampling. Nada aqui carrega os pesos ONNX: a rotulagem é cega ao veredito de
propósito.

A régua do rotulador, que precisa estar combinada antes de começar:

    TEM assinatura = rubrica manuscrita (caneta, escaneada ou desenhada) em
                     qualquer página, OU carimbo de assinatura digital.
    NÃO tem        = campo vazio, linha pontilhada, "Assinatura: ____",
                     nome apenas digitado.
    Rubrica ilegível ou parcial conta como TEM.
    Não consegue decidir → duvida/ (sai do denominador e é reportado).

Opções de `amostrar`:

    --n-negativos 200      documentos ❌ a sortear (default 200)
    --n-positivos 200      documentos ✅ a sortear (default 200)
    --semente 42           reprodutibilidade; aumentar o n mantém a amostra
                           anterior contida na nova
    --max-paginas-folha 6  páginas por imagem (o rodapé avisa o que sobrou)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from processamento import RAIZ
from processamento.relatorio import ler_registros
from validacao import amostra as amostragem
from validacao import metricas, rotulagem

FILES_DEFAULT = RAIZ / "files"
RESULTADOS_DEFAULT = RAIZ / "resultados"
SAIDA_DEFAULT = RAIZ / "auditoria"
NOME_PARAMETROS = "amostragem.json"


def _preparar_console() -> None:
    """Mesmo motivo de `processar.py`: no Windows o stdout é cp1252 e um nome de
    arquivo acentuado derrubaria o comando com `UnicodeEncodeError`."""
    for fluxo in (sys.stdout, sys.stderr):
        if hasattr(fluxo, "reconfigure"):
            fluxo.reconfigure(encoding="utf-8", errors="replace")


_preparar_console()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    comandos = parser.add_subparsers(dest="comando", required=True)

    for nome in ("amostrar", "rotular", "avaliar"):
        comando = comandos.add_parser(nome)
        comando.add_argument("--files", type=Path, default=FILES_DEFAULT)
        comando.add_argument("--resultados", type=Path, default=RESULTADOS_DEFAULT)
        comando.add_argument("--saida", type=Path, default=SAIDA_DEFAULT)
        if nome == "rotular":
            comando.add_argument("--porta", type=int, default=rotulagem.PORTA_DEFAULT)
            comando.add_argument(
                "--sem-navegador",
                action="store_true",
                help="não abre o navegador sozinho (só imprime a URL)",
            )
        if nome == "amostrar":
            comando.add_argument("--n-negativos", type=int, default=200)
            comando.add_argument("--n-positivos", type=int, default=200)
            comando.add_argument("--semente", type=int, default=42)
            comando.add_argument(
                "--max-paginas-folha", type=int, default=amostragem.MAX_PAGINAS_FOLHA
            )
            comando.add_argument("--dpi-folha", type=int, default=amostragem.DPI_FOLHA)
            comando.add_argument(
                "--minimo-por-estrato", type=int, default=amostragem.MINIMO_POR_ESTRATO
            )

    return parser.parse_args(argv)


def _carregar_registros(pasta_resultados: Path) -> list[dict[str, Any]] | None:
    jsonl = pasta_resultados / "resultados.jsonl"
    if not jsonl.is_file():
        print(
            f"Checkpoint não encontrado: {jsonl}\n"
            "Rode `processar.py` antes, ou aponte `--resultados` para a pasta certa.",
            file=sys.stderr,
        )
        return None
    return ler_registros(jsonl)


def _progresso_hash(feitos: int, total: int) -> None:
    if feitos % 500 and feitos != total:
        return
    print(f"  sha256 {feitos}/{total}", file=sys.stderr)


def amostrar(args: argparse.Namespace) -> int:
    registros = _carregar_registros(args.resultados)
    if registros is None:
        return 1

    avaliaveis = [r for r in registros if amostragem.estrato(r)]
    print(
        f"{len(registros)} registros no checkpoint, {len(avaliaveis)} avaliáveis (✅+❌).\n"
        "Calculando sha256 (cache incremental — só o que falta)...",
        file=sys.stderr,
    )
    hashes = amostragem.atualizar_hashes(
        raiz=args.files,
        relativos=[r["arquivo"] for r in avaliaveis],
        cache=args.saida / amostragem.NOME_HASHES,
        ao_progredir=_progresso_hash,
    )
    distintos = len({hashes[r["arquivo"]] for r in avaliaveis if r["arquivo"] in hashes})
    print(
        f"{len(hashes)} arquivos com hash, {distintos} conteúdos distintos "
        f"({len(avaliaveis) / max(distintos, 1):.1f}× de duplicação).",
        file=sys.stderr,
    )

    selecionados = amostragem.sortear(
        registros,
        hashes,
        n_negativos=args.n_negativos,
        n_positivos=args.n_positivos,
        semente=args.semente,
        minimo_por_estrato=args.minimo_por_estrato,
    )
    if not selecionados:
        print(
            "Nada a amostrar: o checkpoint não tem documento ✅ ou ❌ com hash legível.",
            file=sys.stderr,
        )
        return 1

    def anunciar(feitos: int, total: int, arquivo: str) -> None:
        if feitos % 25 and feitos != total:
            return
        print(f"  [{feitos}/{total}] renderizando... ({arquivo})", file=sys.stderr)

    print(
        f"{len(selecionados)} documentos sorteados. Renderizando as folhas de contato...",
        file=sys.stderr,
    )
    resultado = amostragem.preparar_amostra(
        raiz_files=args.files,
        amostra=selecionados,
        pasta_saida=args.saida,
        max_paginas=args.max_paginas_folha,
        dpi=args.dpi_folha,
        ao_progredir=anunciar,
    )

    # O desenho da amostra vira arquivo: sem a semente e os orçamentos gravados,
    # o `VALIDACAO.md` não teria como declarar sobre o que ele foi calculado.
    (args.saida / NOME_PARAMETROS).write_text(
        json.dumps(
            {
                "semente": args.semente,
                "n_negativos": args.n_negativos,
                "n_positivos": args.n_positivos,
                "minimo_por_estrato": args.minimo_por_estrato,
                "max_paginas_folha": args.max_paginas_folha,
                "dpi_folha": args.dpi_folha,
                "documentos_sorteados": len(selecionados),
                "conteudos_distintos": resultado.conteudos,
                "amostrado_em": datetime.now().isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    pendentes = args.saida / amostragem.NOME_AMOSTRA / amostragem.PASTA_PENDENTES
    print(
        f"\n{resultado.conteudos} conteúdos distintos: {resultado.geradas} imagens novas, "
        f"{resultado.reaproveitadas} já existentes.",
        file=sys.stderr,
    )
    for arquivo, motivo in resultado.falhas[:10]:
        print(f"  [ALERTA] {arquivo} - {motivo}", file=sys.stderr)
    if len(resultado.falhas) > 10:
        print(f"  ... e outras {len(resultado.falhas) - 10} falhas.", file=sys.stderr)

    print(
        "\nAgora rotule:  validar.py rotular\n"
        "  S = rubrica manuscrita ou carimbo digital em qualquer página\n"
        "  N = campo vazio, linha pontilhada, nome só digitado\n"
        "  D = não deu para decidir\n"
        f"(as imagens estão em `{pendentes}`, se preferir mover na mão)\n"
        "Depois: validar.py avaliar",
        file=sys.stderr,
    )
    return 0


def rotular(args: argparse.Namespace) -> int:
    """Sobe o rotulador local e fica servindo até Ctrl+C."""
    import webbrowser

    indice = amostragem.ler_indice(args.saida / amostragem.NOME_INDICE)
    if not indice:
        print(
            f"Amostra não encontrada em {args.saida / amostragem.NOME_INDICE}\n"
            "Rode `validar.py amostrar` antes.",
            file=sys.stderr,
        )
        return 1

    pasta_amostra = args.saida / amostragem.NOME_AMOSTRA
    caminho_rotulos = args.saida / amostragem.NOME_ROTULOS
    parametros = _parametros_da_amostra(args.saida)
    itens = rotulagem.fila(indice, pasta_amostra, semente=parametros.get("semente", 42))
    if not itens:
        print(
            f"Nenhuma imagem em {pasta_amostra / amostragem.PASTA_PENDENTES}. "
            "Rode `validar.py amostrar` para gerá-las.",
            file=sys.stderr,
        )
        return 1

    rotulos = metricas.carregar_rotulos(caminho_rotulos)
    rotulos.update(
        metricas.ler_rotulos(
            pasta_amostra, conhecidos={item.id: item.sha256 for item in itens}
        )
    )
    faltam = sum(1 for item in itens if item.sha256 not in rotulos)

    def anunciar(identificador: str, rotulo: str, decididos: int) -> None:
        print(f"  [{decididos}/{len(itens)}] {identificador} → {rotulo}", file=sys.stderr)

    servidor = rotulagem.criar_servidor(
        itens=itens,
        rotulos=rotulos,
        pasta_amostra=pasta_amostra,
        caminho_rotulos=caminho_rotulos,
        porta=args.porta,
        ao_rotular=anunciar,
    )
    url = f"http://127.0.0.1:{servidor.server_address[1]}/"
    print(
        f"Rotulador em {url}\n"
        f"{len(itens)} imagens na fila, {faltam} sem rótulo.\n"
        "Teclas: S = tem assinatura · N = não tem · D = dúvida · ← volta · → pula.\n"
        "Cada clique grava na hora. Ctrl+C encerra; depois rode `validar.py avaliar`.",
        file=sys.stderr,
    )
    if not args.sem_navegador:
        webbrowser.open(url)

    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrado. Os rótulos estão gravados.", file=sys.stderr)
    finally:
        servidor.server_close()
    return 0


def avaliar(args: argparse.Namespace) -> int:
    registros = _carregar_registros(args.resultados)
    if registros is None:
        return 1

    indice = args.saida / amostragem.NOME_INDICE
    if not indice.is_file():
        print(
            f"Amostra não encontrada: {indice}\nRode `validar.py amostrar` antes.",
            file=sys.stderr,
        )
        return 1

    hashes = amostragem.carregar_hashes(args.saida / amostragem.NOME_HASHES)
    caminhos = metricas.gerar(
        registros=registros,
        hashes=hashes,
        pasta_saida=args.saida,
        execucao={
            **_parametros_da_amostra(args.saida),
            "avaliado_em": datetime.now().isoformat(timespec="seconds"),
            "checkpoint": str(args.resultados / "resultados.jsonl"),
        },
    )
    print("\nRelatórios gerados:", file=sys.stderr)
    for caminho in caminhos.values():
        print(f"  {caminho}", file=sys.stderr)
    return 0


def _parametros_da_amostra(pasta_saida: Path) -> dict[str, Any]:
    """O desenho da amostra, gravado por `amostrar`. Ausente não é erro: o
    relatório só perde a linha que declara a semente."""
    caminho = pasta_saida / NOME_PARAMETROS
    if not caminho.is_file():
        return {}
    try:
        return dict(json.loads(caminho.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return {}


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.comando == "amostrar":
        return amostrar(args)
    if args.comando == "rotular":
        return rotular(args)
    return avaliar(args)


if __name__ == "__main__":
    raise SystemExit(main())
