"""Teste da PoC de detecção de assinatura nos documentos de trabalhadores.

Roda os **dois níveis** em cada PDF (sem cascata, para poder comparar) e grava
um relatório Markdown com a contagem de assinaturas por nível, além de um JSON
com o detalhe de cada detecção.

    motor-ia$ ./.venv/bin/python poc-assinatura/tool_validar_assinatura/testar_documentos.py

Opções úteis na calibração:

    --conf 0.35            confiança mínima do detector visual (default 0.25)
    --iou 0.5              IoU máximo do NMS
    --dpi 200              DPI do raster enviado ao modelo (default 150)
    --densidade-minima     limiar de página em branco (default 0.0005)
    --modelo /caminho.onnx pesos ONNX (default: cache da PoC / download do HF)
    --sem-nivel1           só Nível 0 (útil sem os pesos do modelo)
    --docs <dir>           pasta com os PDFs (default: ../doc trabalhadores)
    --saida <arquivo.md>   relatório (default: resultados/RESULTADOS.md)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

PASTA_POC = Path(__file__).resolve().parent


def _preparar_imports() -> None:
    """Permite rodar o script direto, sem instalar nada: coloca o `src` do
    motor-ia e a própria pasta da PoC no `sys.path`."""
    raiz_motor_ia = PASTA_POC.parents[1]
    for caminho in (raiz_motor_ia / "src", PASTA_POC):
        if str(caminho) not in sys.path:
            sys.path.insert(0, str(caminho))


_preparar_imports()


from deteccao import ResultadoDeteccao, detectar_assinaturas  # noqa: E402
from nivel0 import DENSIDADE_TINTA_MINIMA  # noqa: E402
from nivel1 import (  # noqa: E402
    CONFIANCA_MINIMA,
    DPI_RENDER,
    IOU_MAXIMO,
    MODELO_NOME,
    obter_detector,
)

from motor_ia.pricing import compute_textract_cost  # noqa: E402

DOCUMENTOS_DEFAULT = PASTA_POC.parent / "doc trabalhadores"
SAIDA_DEFAULT = PASTA_POC / "resultados" / "RESULTADOS.md"


@dataclass(frozen=True)
class LinhaResultado:
    """Uma linha do relatório: o que cada nível achou num documento."""

    documento: str
    paginas: int
    paginas_em_branco: int
    paginas_uteis: int
    paginas_nivel1: int
    nivel0_total: int
    nivel0_fontes: str
    nivel1_total: int
    tempo_total_ms: float
    tempo_inferencia_ms: float
    nivel1_erro: str | None
    resultado: dict[str, Any]

    @property
    def veredito(self) -> str:
        if self.nivel0_total and self.nivel1_total:
            return "assinado (N0+N1)"
        if self.nivel0_total:
            return "assinado (N0)"
        if self.nivel1_total:
            return "assinado (N1)"
        if self.nivel1_erro:
            return "indeterminado"
        return "sem assinatura"


def _processar(
    pdf: Path,
    *,
    args: argparse.Namespace,
) -> LinhaResultado:
    inicio = time.perf_counter()
    resultado: ResultadoDeteccao = detectar_assinaturas(
        pdf,
        executar_nivel1=not args.sem_nivel1,
        escalonar=False,
        modelo_path=args.modelo,
        confianca_minima=args.conf,
        iou_maximo=args.iou,
        dpi_render=args.dpi,
        densidade_minima=args.densidade_minima,
    )
    decorrido_ms = (time.perf_counter() - inicio) * 1000.0

    nivel1 = resultado.nivel1
    return LinhaResultado(
        documento=pdf.name,
        paginas=len(resultado.nivel0.paginas),
        paginas_em_branco=len(resultado.nivel0.paginas_em_branco),
        paginas_uteis=len(resultado.nivel0.paginas_uteis),
        paginas_nivel1=len(nivel1.paginas_analisadas) if nivel1 else 0,
        nivel0_total=resultado.nivel0.total,
        nivel0_fontes=", ".join(resultado.nivel0.fontes) or "—",
        nivel1_total=nivel1.total if nivel1 else 0,
        tempo_total_ms=round(decorrido_ms, 1),
        tempo_inferencia_ms=nivel1.tempo_inferencia_ms if nivel1 else 0.0,
        nivel1_erro=resultado.nivel1_erro,
        resultado=resultado.para_dict(),
    )


def _tabela(linhas: list[LinhaResultado]) -> list[str]:
    md = [
        "| # | Documento | Págs | Em branco | Págs no N1 | **Assinaturas N0** "
        "| **Assinaturas N1** | Fontes N0 | Veredito | Tempo (ms) |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for indice, linha in enumerate(linhas, start=1):
        md.append(
            f"| {indice} | {linha.documento} | {linha.paginas} | "
            f"{linha.paginas_em_branco} | {linha.paginas_nivel1} | "
            f"**{linha.nivel0_total}** | **{linha.nivel1_total}** | "
            f"{linha.nivel0_fontes} | {linha.veredito} | {linha.tempo_total_ms:.0f} |"
        )
    return md


def _resumo(linhas: list[LinhaResultado]) -> list[str]:
    documentos = len(linhas)
    paginas = sum(linha.paginas for linha in linhas)
    em_branco = sum(linha.paginas_em_branco for linha in linhas)
    paginas_uteis = sum(linha.paginas_uteis for linha in linhas)
    paginas_nivel1 = sum(linha.paginas_nivel1 for linha in linhas)
    total_n0 = sum(linha.nivel0_total for linha in linhas)
    total_n1 = sum(linha.nivel1_total for linha in linhas)
    docs_n0 = sum(1 for linha in linhas if linha.nivel0_total)
    docs_n1 = sum(1 for linha in linhas if linha.nivel1_total)
    docs_assinados = sum(1 for linha in linhas if linha.nivel0_total or linha.nivel1_total)
    tempo_total = sum(linha.tempo_total_ms for linha in linhas)
    tempo_inferencia = sum(linha.tempo_inferencia_ms for linha in linhas)
    erros = [linha for linha in linhas if linha.nivel1_erro]

    # Comparativo de custo: o fluxo antigo pagava Textract AnalyzeDocument
    # (feature SIGNATURES) por página de todo documento inspecionado.
    custo_textract = compute_textract_cost(paginas, mode="analyze_signatures")
    custo_textract_triado = compute_textract_cost(paginas_uteis, mode="analyze_signatures")

    md = [
        "## Resumo",
        "",
        f"- Documentos processados: **{documentos}** ({paginas} páginas)",
        f"- Páginas descartadas como em branco pelo Nível 0: **{em_branco}** "
        f"({em_branco / paginas:.1%} das páginas)"
        if paginas
        else "- Páginas: 0",
        f"- Páginas aproveitáveis (não brancas): **{paginas_uteis}** | "
        f"efetivamente inferidas no Nível 1: **{paginas_nivel1}**",
        f"- Assinaturas detectadas — Nível 0: **{total_n0}** | Nível 1: **{total_n1}**",
        f"- Documentos com assinatura — Nível 0: **{docs_n0}/{documentos}** | "
        f"Nível 1: **{docs_n1}/{documentos}** | consolidado: **{docs_assinados}/{documentos}**",
        f"- Tempo total: **{tempo_total / 1000:.1f}s** "
        f"({tempo_total / max(paginas, 1):.0f} ms/página), dos quais "
        f"{tempo_inferencia / 1000:.1f}s de inferência ONNX",
        f"- Custo desta execução: **US$ 0,00** (CPU local). O mesmo lote no Textract "
        f"`AnalyzeDocument/SIGNATURES` custaria **US$ {custo_textract:.4f}** "
        f"({paginas} págs) — ou US$ {custo_textract_triado:.4f} já com a triagem de "
        "páginas em branco do Nível 0.",
    ]
    if paginas:
        por_mil = custo_textract / Decimal(paginas) * Decimal(1000)
        md.append(
            f"- Extrapolação: a cada 100.000 páginas/mês, o Textract custaria "
            f"US$ {por_mil * Decimal(100):.2f} contra US$ 0,00 de licença aqui "
            "(só CPU do worker)."
        )
    if erros:
        md += [
            "",
            f"> ⚠️ Nível 1 indisponível em {len(erros)} documento(s): {erros[0].nivel1_erro}",
        ]
    return md


def _detalhe(linhas: list[LinhaResultado]) -> list[str]:
    md = ["## Detalhe por documento", ""]
    for linha in linhas:
        resultado = linha.resultado
        md.append(f"### {linha.documento}")
        md.append("")
        md.append(
            f"- Veredito: **{linha.veredito}** | páginas: {linha.paginas} "
            f"(em branco: {linha.paginas_em_branco})"
        )
        paginas = resultado["nivel0"]["paginas"]
        md.append(
            "- Densidade de tinta por página: "
            + ", ".join(
                f"p{p['numero']}={p['densidade_tinta']:.4f}"
                + ("(branco)" if p["em_branco"] else "")
                + ("(rótulo)" if p["tem_rotulo_assinatura"] else "")
                for p in paginas
            )
        )
        if resultado["digital"]:
            md.append("- Nível 0 — carimbo digital no texto:")
            md += [
                f"  - {item.get('signatario') or '(sem nome)'} — "
                f"`{(item.get('evidencia') or '')[:110]}`"
                for item in resultado["digital"]
            ]
        if resultado["embedded"]:
            md.append("- Nível 0 — campo `/Sig` no AcroForm:")
            md += [
                f"  - página {item.get('page')} — campo `{item.get('campo')}`"
                for item in resultado["embedded"]
            ]
        if resultado["visual"]:
            md.append("- Nível 1 — rubricas detectadas:")
            md += [
                f"  - página {item['page']} — confiança {item['confidence']:.2f} — "
                f"caixa (l={item['bounding_box']['left']:.2f}, "
                f"t={item['bounding_box']['top']:.2f}, "
                f"w={item['bounding_box']['width']:.2f}, "
                f"h={item['bounding_box']['height']:.2f})"
                for item in resultado["visual"]
            ]
        if not (resultado["digital"] or resultado["embedded"] or resultado["visual"]):
            md.append("- Nenhuma assinatura detectada por nenhum dos níveis.")
        if linha.nivel1_erro:
            md.append(f"- ⚠️ Nível 1 não executou: {linha.nivel1_erro}")
        md.append("")
    return md


def montar_markdown(linhas: list[LinhaResultado], *, args: argparse.Namespace) -> str:
    """Relatório completo: parâmetros, tabela por documento, resumo e detalhe."""
    cabecalho = [
        "# PoC — detecção de assinatura sem Textract",
        "",
        f"Gerado em {datetime.now():%d/%m/%Y %H:%M} por "
        "`poc-assinatura/tool_validar_assinatura/testar_documentos.py`.",
        "",
        "**Nível 0** (Python puro, custo zero): campos `/Sig` no AcroForm, carimbo de "
        "assinatura digital na camada de texto, densidade de tinta e descarte de "
        "páginas em branco.  ",
        f"**Nível 1** (CPU local, custo zero por página): `{MODELO_NOME}` via ONNX "
        "Runtime, apenas nas páginas aproveitadas pelo Nível 0.",
        "",
        "## Parâmetros da execução",
        "",
        f"- Documentos: `{args.docs}`",
        f"- Nível 1: {'desligado' if args.sem_nivel1 else 'ligado'}"
        + (f" | pesos: `{args.modelo}`" if args.modelo else ""),
        f"- Confiança mínima: `{args.conf}` | IoU máximo: `{args.iou}` | "
        f"DPI do raster: `{args.dpi}`",
        f"- Limiar de página em branco (densidade de tinta): `{args.densidade_minima}`",
        "",
        "## Resultados por documento",
        "",
    ]
    return "\n".join([*cabecalho, *_tabela(linhas), "", *_resumo(linhas), "", *_detalhe(linhas)])


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs", type=Path, default=DOCUMENTOS_DEFAULT)
    parser.add_argument("--saida", type=Path, default=SAIDA_DEFAULT)
    parser.add_argument("--modelo", type=str, default=None)
    parser.add_argument("--conf", type=float, default=CONFIANCA_MINIMA)
    parser.add_argument("--iou", type=float, default=IOU_MAXIMO)
    parser.add_argument("--dpi", type=int, default=DPI_RENDER)
    parser.add_argument("--densidade-minima", type=float, default=DENSIDADE_TINTA_MINIMA)
    parser.add_argument("--sem-nivel1", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    pasta = args.docs.expanduser()
    if not pasta.is_dir():
        print(f"Pasta de documentos não encontrada: {pasta}", file=sys.stderr)
        return 1
    pdfs = sorted(pasta.glob("*.pdf"), key=lambda p: p.name.lower())
    if not pdfs:
        print(f"Nenhum PDF em {pasta}", file=sys.stderr)
        return 1

    # Falha cedo se o Nível 1 está ligado mas indisponível (onnxruntime ausente,
    # pesos não baixados). Sem isso o lote rodaria inteiro e produziria um
    # relatório aparentemente completo com "Assinaturas N1 = 0" em todo
    # documento — a degradação silenciosa que a tool faz por design em produção
    # é exatamente o que NÃO se quer num relatório de PoC.
    if not args.sem_nivel1:
        try:
            obter_detector(args.modelo)
        except Exception as exc:
            print(
                f"Nível 1 indisponível ({type(exc).__name__}): {exc}\n"
                "Instale a dependência (`uv pip install onnxruntime`), resolva os pesos "
                "(`python modelo.py`) ou rode com `--sem-nivel1` para gerar só o Nível 0.",
                file=sys.stderr,
            )
            return 1

    linhas: list[LinhaResultado] = []
    for indice, pdf in enumerate(pdfs, start=1):
        print(f"[{indice}/{len(pdfs)}] {pdf.name}", file=sys.stderr)
        linha = _processar(pdf, args=args)
        print(
            f"    N0={linha.nivel0_total} N1={linha.nivel1_total} ({linha.tempo_total_ms:.0f} ms)",
            file=sys.stderr,
        )
        linhas.append(linha)

    args.saida.parent.mkdir(parents=True, exist_ok=True)
    args.saida.write_text(montar_markdown(linhas, args=args), encoding="utf-8")

    saida_json = args.saida.with_suffix(".json")
    saida_json.write_text(
        json.dumps(
            [
                {
                    "documento": linha.documento,
                    "nivel0_total": linha.nivel0_total,
                    "nivel1_total": linha.nivel1_total,
                    "tempo_total_ms": linha.tempo_total_ms,
                    "tempo_inferencia_ms": linha.tempo_inferencia_ms,
                    "nivel1_erro": linha.nivel1_erro,
                    "deteccao": linha.resultado,
                }
                for linha in linhas
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"\nRelatório: {args.saida}\nJSON: {saida_json}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
