"""Relatórios a partir do checkpoint JSONL.

Três saídas, todas derivadas do mesmo `resultados.jsonl` — o que permite
regerá-las quantas vezes for preciso sem reprocessar documento nenhum
(`processar.py --apenas-relatorio`):

| Arquivo | Para quem |
|---|---|
| `RESUMO.md` | leitura humana: contagens, desempenho, hardware, erros |
| `RESUMO.json` | consumo por outro sistema / comparação entre execuções |
| `COMPLETO.md` | auditoria: **uma linha por documento**, do lote inteiro |

Legenda de status, como pedido: ✅ assinatura encontrada, ❌ processou e não
encontrou, ⚠️ não deu para afirmar (erro, formato não suportado, ou Nível 1
indisponível — que não é o mesmo que "não tem assinatura").

O `COMPLETO.md` carrega todos os registros em memória para ordená-los por
caminho (a ordem de conclusão do pool é imprevisível, e um relatório de
auditoria precisa ser navegável por pasta). São ~1 KB por documento: um lote de
100 mil documentos custa algumas centenas de MB no momento de gerar o relatório,
não durante o processamento.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from statistics import fmean, median
from typing import Any

from processamento.lote import (
    STATUS_ASSINADO,
    STATUS_CONTAINER,
    STATUS_ERRO,
    STATUS_INDETERMINADO,
    STATUS_NAO_SUPORTADO,
    STATUS_SEM_ASSINATURA,
)

EMOJI: dict[str, str] = {
    STATUS_ASSINADO: "✅",
    STATUS_SEM_ASSINATURA: "❌",
    STATUS_INDETERMINADO: "⚠️",
    STATUS_ERRO: "⚠️",
    STATUS_NAO_SUPORTADO: "⚠️",
    STATUS_CONTAINER: "📦",
}

_LIMITE_MAIS_LENTOS = 10
_LIMITE_ERROS_LISTADOS = 15


def ler_registros(jsonl: Path) -> list[dict[str, Any]]:
    """Registros do checkpoint, um por documento, ordenados por caminho.

    Documento reavaliado numa execução seguinte (formato que passou a ser
    suportado) aparece duas vezes no arquivo — vale a última linha, que é a
    mais recente.
    """
    if not jsonl.is_file():
        return []
    por_arquivo: dict[str, dict[str, Any]] = {}
    with jsonl.open(encoding="utf-8") as arquivo:
        for linha in arquivo:
            linha = linha.strip()
            if not linha:
                continue
            try:
                registro = json.loads(linha)
                por_arquivo[registro["arquivo"]] = registro
            except (json.JSONDecodeError, KeyError):
                continue
    return sorted(por_arquivo.values(), key=lambda r: str(r.get("arquivo", "")).lower())


def agregar(registros: list[dict[str, Any]]) -> dict[str, Any]:
    """Consolida o lote inteiro num dicionário — a fonte do `RESUMO.json`."""
    total = len(registros)
    status = Counter(r["status"] for r in registros)
    ok = [r for r in registros if r["status"] in (STATUS_ASSINADO, STATUS_SEM_ASSINATURA)]
    documentos_avaliaveis = total - status.get(STATUS_CONTAINER, 0)

    tempos = [float(r["tempo_ms"]) for r in registros if r["tempo_ms"]]
    paginas = sum(int(r["paginas"]) for r in registros)
    inferencia_ms = sum(float(r["tempo_inferencia_ms"]) for r in registros)
    tempo_total_ms = sum(tempos)

    so_nivel0 = sum(1 for r in registros if r["nivel0_total"] and not r["nivel1_total"])
    so_nivel1 = sum(1 for r in registros if r["nivel1_total"] and not r["nivel0_total"])
    ambos = sum(1 for r in registros if r["nivel0_total"] and r["nivel1_total"])

    # `.get`: checkpoint anterior ao fallback não tem a chave, e `--apenas-relatorio`
    # tem de continuar gerando o relatório dele.
    ladrilhados = [r for r in registros if int(r.get("paginas_ladrilhadas", 0))]
    resgatados = [r for r in ladrilhados if int(r["assinaturas_total"])]

    por_formato: dict[str, dict[str, int]] = {}
    for registro in registros:
        entrada = por_formato.setdefault(
            registro["formato"], {"documentos": 0, "assinados": 0, "sem_assinatura": 0, "alerta": 0}
        )
        entrada["documentos"] += 1
        if registro["status"] == STATUS_ASSINADO:
            entrada["assinados"] += 1
        elif registro["status"] == STATUS_SEM_ASSINATURA:
            entrada["sem_assinatura"] += 1
        else:
            entrada["alerta"] += 1

    return {
        "documentos": total,
        "documentos_avaliaveis": documentos_avaliaveis,
        "status": {chave: status.get(chave, 0) for chave in EMOJI},
        "assinaturas": {
            "total": sum(int(r["assinaturas_total"]) for r in registros),
            "nivel0": sum(int(r["nivel0_total"]) for r in registros),
            "nivel1": sum(int(r["nivel1_total"]) for r in registros),
        },
        "documentos_por_metodo": {
            "somente_nivel0": so_nivel0,
            "somente_nivel1": so_nivel1,
            "ambos": ambos,
        },
        "fallback": {
            "documentos": len(ladrilhados),
            "paginas": sum(int(r.get("paginas_ladrilhadas", 0)) for r in registros),
            "documentos_resgatados": len(resgatados),
            "assinaturas": sum(int(r["assinaturas_total"]) for r in resgatados),
        },
        "paginas": {
            "total": paginas,
            "em_branco": sum(int(r["paginas_em_branco"]) for r in registros),
            "analisadas_nivel1": sum(int(r["paginas_analisadas_nivel1"]) for r in registros),
            "documentos_com_limite_de_paginas": sum(
                1 for r in registros if r.get("paginas_limitadas")
            ),
        },
        "tempo": {
            "total_segundos": round(tempo_total_ms / 1000.0, 1),
            "medio_ms_por_documento": round(fmean(tempos), 1) if tempos else 0.0,
            "mediana_ms_por_documento": round(median(tempos), 1) if tempos else 0.0,
            "p95_ms_por_documento": _percentil(tempos, 95),
            "maximo_ms_por_documento": round(max(tempos), 1) if tempos else 0.0,
            "medio_ms_por_pagina": round(tempo_total_ms / paginas, 1) if paginas else 0.0,
            "inferencia_segundos": round(inferencia_ms / 1000.0, 1),
            "percentual_em_inferencia": (
                round(inferencia_ms / tempo_total_ms * 100, 1) if tempo_total_ms else 0.0
            ),
        },
        "bytes_processados": sum(int(r["tamanho_bytes"]) for r in registros),
        "documentos_com_retry": sum(1 for r in registros if int(r.get("tentativas", 1)) > 1),
        "documentos_completos": len(ok),
        "por_formato": dict(sorted(por_formato.items())),
        "erros_por_tipo": dict(Counter(r["erro_tipo"] for r in registros if r["erro_tipo"])),
    }


def _percentil(valores: list[float], percentil: int) -> float:
    if not valores:
        return 0.0
    ordenados = sorted(valores)
    indice = min(int(len(ordenados) * percentil / 100), len(ordenados) - 1)
    return round(ordenados[indice], 1)


def _celula(texto: Any) -> str:
    """Escapa o que quebraria a tabela Markdown (nome de arquivo com `|`)."""
    return str(texto if texto is not None else "").replace("|", "\\|").replace("\n", " ")


def _observacao(registro: dict[str, Any]) -> str:
    partes: list[str] = []
    if registro.get("observacao"):
        partes.append(str(registro["observacao"]))
    if registro.get("erro"):
        partes.append(str(registro["erro"]))
    if registro.get("nivel1_erro"):
        partes.append(f"Nível 1 indisponível: {registro['nivel1_erro']}")
    if registro.get("paginas_ladrilhadas"):
        resgate = " — assinatura resgatada" if registro.get("assinaturas_total") else ""
        partes.append(f"fallback 3×3 em {registro['paginas_ladrilhadas']} pág{resgate}")
    if registro.get("paginas_limitadas"):
        partes.append(f"Nível 1 limitado às primeiras {registro['paginas_analisadas_nivel1']} págs")
    if int(registro.get("tentativas", 1)) > 1:
        partes.append(f"{registro['tentativas']} tentativas")
    return _celula("; ".join(partes)) or "—"


def montar_completo(registros: list[dict[str, Any]], *, execucao: dict[str, Any]) -> str:
    """`COMPLETO.md`: uma linha por documento do lote."""
    resumo = agregar(registros)
    linhas = [
        "# Relatório completo — detecção de assinatura",
        "",
        f"Execução de {execucao.get('inicio', '?')} a {execucao.get('fim', '?')}. "
        f"{resumo['documentos']} documentos.",
        "",
        "Legenda: ✅ assinatura encontrada · ❌ processado, nenhuma assinatura · "
        "⚠️ não foi possível concluir (erro, formato não suportado ou Nível 1 "
        "indisponível) · 📦 arquivo `.zip`, extraído — o conteúdo tem linha própria.",
        "",
        "**Nível 0** — campos `/Sig` do AcroForm e carimbo de assinatura digital na "
        "camada de texto (Python puro).  ",
        "**Nível 1** — detector visual de rubrica (YOLOv8s ONNX em CPU) nas páginas "
        "não descartadas pelo Nível 0. Documento que zera nos dois níveis é refeito "
        "em ladrilhos 3×3 e DPI maior — sai como `fallback 3×3` na observação.",
        "",
        "| # | | Documento | Formato | Págs | Assinaturas | Nível 0 | Nível 1 | Tempo (ms) | Observação |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for indice, registro in enumerate(registros, start=1):
        linhas.append(
            f"| {indice} "
            f"| {EMOJI.get(registro['status'], '⚠️')} "
            f"| {_celula(registro['arquivo'])} "
            f"| {_celula(registro['formato'])} "
            f"| {registro['paginas']} "
            f"| **{registro['assinaturas_total']}** "
            f"| {registro['nivel0_total']} "
            f"| {registro['nivel1_total']} "
            f"| {registro['tempo_ms']:.0f} "
            f"| {_observacao(registro)} |"
        )
    return "\n".join(linhas) + "\n"


def montar_resumo(registros: list[dict[str, Any]], *, execucao: dict[str, Any]) -> str:
    """`RESUMO.md`: o que interessa para decidir, sem a tabela do lote inteiro."""
    dados = agregar(registros)
    # Denominador é o que dá para avaliar: `.zip` é caixa, não documento.
    total = dados["documentos_avaliaveis"] or 1
    status = dados["status"]
    tempo = dados["tempo"]
    recursos = execucao.get("recursos") or {}

    alertas = status[STATUS_ERRO] + status[STATUS_INDETERMINADO] + status[STATUS_NAO_SUPORTADO]
    linhas = [
        "# Resumo — detecção de assinatura em lote",
        "",
        f"Execução de {execucao.get('inicio', '?')} a {execucao.get('fim', '?')} "
        f"({execucao.get('segundos_totais', 0) / 60:.1f} min de relógio, "
        "processamento sequencial).",
        "",
        "## Veredito",
        "",
        "| | Documentos | % |",
        "|---|---|---|",
        f"| ✅ Assinatura encontrada | {status[STATUS_ASSINADO]} "
        f"| {status[STATUS_ASSINADO] / total:.1%} |",
        f"| ❌ Sem assinatura | {status[STATUS_SEM_ASSINATURA]} "
        f"| {status[STATUS_SEM_ASSINATURA] / total:.1%} |",
        f"| ⚠️ Não concluído | {alertas} | {alertas / total:.1%} |",
        f"| **Total avaliado** | **{dados['documentos_avaliaveis']}** | |",
        "",
        f"O ⚠️ se abre em: {status[STATUS_ERRO]} erro de processamento, "
        f"{status[STATUS_NAO_SUPORTADO]} formato não suportado, "
        f"{status[STATUS_INDETERMINADO]} indeterminado (Nível 1 indisponível).",
        "",
        f"Fora da conta: 📦 {status[STATUS_CONTAINER]} arquivo(s) `.zip`, extraídos "
        f"em {execucao.get('arquivos_extraidos_de_zip', 0)} arquivos que entram no "
        "lote com linha própria.",
        "",
        "## Assinaturas detectadas",
        "",
        f"- Total: **{dados['assinaturas']['total']}** — "
        f"Nível 0: **{dados['assinaturas']['nivel0']}** | "
        f"Nível 1: **{dados['assinaturas']['nivel1']}**",
        f"- Documentos resolvidos só pelo Nível 0: "
        f"**{dados['documentos_por_metodo']['somente_nivel0']}** | "
        f"só pelo Nível 1: **{dados['documentos_por_metodo']['somente_nivel1']}** | "
        f"pelos dois: **{dados['documentos_por_metodo']['ambos']}**",
        f"- Páginas: {dados['paginas']['total']} no total, "
        f"{dados['paginas']['em_branco']} descartadas como em branco pelo Nível 0, "
        f"{dados['paginas']['analisadas_nivel1']} inferidas no Nível 1",
        "",
    ]

    fallback = dados["fallback"]
    if fallback["documentos"]:
        assinados = status[STATUS_ASSINADO]
        sem_fallback = assinados - fallback["documentos_resgatados"]
        linhas += [
            "## Fallback de ladrilhos (3×3)",
            "",
            "Documento que terminou sem nenhuma assinatura é refeito no mesmo Nível 1, "
            "com cada página dividida em 9 ladrilhos sobrepostos e rasterizada em DPI "
            "maior — a rubrica passa a ocupar ~3× mais da entrada do modelo.",
            "",
            f"- Documentos que caíram no fallback: **{fallback['documentos']}** "
            f"({fallback['paginas']} páginas ladrilhadas)",
            f"- Resgatados (viraram ✅): **{fallback['documentos_resgatados']}** — "
            f"{fallback['documentos_resgatados'] / fallback['documentos']:.1%} dos que "
            f"entraram, {fallback['assinaturas']} assinaturas",
            f"- Sem o fallback o lote teria fechado em **{sem_fallback / total:.1%}**; "
            f"com ele, **{assinados / total:.1%}**",
            "",
        ]

    linhas += [
        "## Desempenho",
        "",
        f"- Tempo médio por documento: **{tempo['medio_ms_por_documento']:.0f} ms** "
        f"(mediana {tempo['mediana_ms_por_documento']:.0f} ms, "
        f"p95 {tempo['p95_ms_por_documento']:.0f} ms, "
        f"máximo {tempo['maximo_ms_por_documento']:.0f} ms)",
        f"- Tempo médio por página: **{tempo['medio_ms_por_pagina']:.0f} ms**",
        f"- Tempo somado dos documentos: {tempo['total_segundos']:.0f} s, dos quais "
        f"{tempo['inferencia_segundos']:.0f} s de inferência ONNX "
        f"({tempo['percentual_em_inferencia']:.0f}%)",
        f"- Volume processado: {dados['bytes_processados'] / 1024**3:.2f} GB",
        "",
        "## Hardware durante o processamento",
        "",
    ]

    if recursos.get("amostras"):
        linhas += [
            f"- CPU: **{recursos['cpu_media_pct']:.0f}% em média**, "
            f"pico de {recursos['cpu_maxima_pct']:.0f}% "
            f"({recursos['nucleos_logicos']} núcleos lógicos)",
            f"- RAM: **{recursos['ram_media_pct']:.0f}% em média**, "
            f"pico de {recursos['ram_maxima_pct']:.0f}% "
            f"(de {recursos['ram_total_gb']:.1f} GB)",
            f"- Memória do processamento (pai + workers): "
            f"{recursos['rss_medio_mb']:.0f} MB em média, "
            f"pico de {recursos['rss_maximo_mb']:.0f} MB",
            f"- {recursos['amostras']} amostras a cada "
            f"{recursos['intervalo_segundos']:.0f} s",
        ]
    else:
        linhas.append("- Execução curta demais para amostrar (nenhuma amostra coletada).")

    linhas += [
        "",
        "## Por formato",
        "",
        "| Formato | Documentos | ✅ | ❌ | ⚠️ / 📦 |",
        "|---|---|---|---|---|",
    ]
    for formato, contagem in dados["por_formato"].items():
        linhas.append(
            f"| `{formato}` | {contagem['documentos']} | {contagem['assinados']} "
            f"| {contagem['sem_assinatura']} | {contagem['alerta']} |"
        )

    linhas += ["", "## Erros", ""]
    if dados["erros_por_tipo"]:
        linhas += ["| Tipo | Ocorrências |", "|---|---|"]
        linhas += [
            f"| `{tipo}` | {quantidade} |"
            for tipo, quantidade in sorted(
                dados["erros_por_tipo"].items(), key=lambda item: -item[1]
            )
        ]
        linhas += ["", "Primeiras ocorrências:", ""]
        com_erro = [r for r in registros if r.get("erro")][:_LIMITE_ERROS_LISTADOS]
        linhas += [f"- `{r['arquivo']}` — {r['erro']}" for r in com_erro]
    else:
        linhas.append("Nenhum erro registrado.")

    if dados["documentos_com_retry"]:
        linhas.append("")
        linhas.append(
            f"{dados['documentos_com_retry']} documento(s) precisaram de mais de uma "
            "tentativa."
        )

    mais_lentos = sorted(registros, key=lambda r: -float(r["tempo_ms"]))[:_LIMITE_MAIS_LENTOS]
    if mais_lentos:
        linhas += [
            "",
            "## Documentos mais lentos",
            "",
            "| Documento | Págs | Tempo (ms) |",
            "|---|---|---|",
        ]
        linhas += [
            f"| {_celula(r['arquivo'])} | {r['paginas']} | {r['tempo_ms']:.0f} |"
            for r in mais_lentos
        ]

    linhas += [
        "",
        "## Parâmetros da execução",
        "",
        "```json",
        json.dumps(execucao.get("opcoes") or {}, ensure_ascii=False, indent=2),
        "```",
        "",
        "O detalhe documento a documento está em [`COMPLETO.md`](COMPLETO.md); "
        "os dados brutos, em `resultados.jsonl`.",
    ]
    return "\n".join(linhas) + "\n"


def gerar(jsonl: Path, pasta_saida: Path, execucao: dict[str, Any]) -> dict[str, Path]:
    """Escreve os três relatórios e devolve onde cada um ficou."""
    registros = ler_registros(jsonl)
    pasta_saida.mkdir(parents=True, exist_ok=True)

    caminhos = {
        "resumo_md": pasta_saida / "RESUMO.md",
        "resumo_json": pasta_saida / "RESUMO.json",
        "completo_md": pasta_saida / "COMPLETO.md",
    }
    caminhos["resumo_md"].write_text(montar_resumo(registros, execucao=execucao), encoding="utf-8")
    caminhos["completo_md"].write_text(
        montar_completo(registros, execucao=execucao), encoding="utf-8"
    )
    caminhos["resumo_json"].write_text(
        json.dumps(
            {"execucao": execucao, "resumo": agregar(registros)}, ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )
    return caminhos
