"""Testes dos relatórios: emojis, agregação e integridade da tabela completa."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from processamento.lote import (
    STATUS_ASSINADO,
    STATUS_ERRO,
    STATUS_INDETERMINADO,
    STATUS_NAO_SUPORTADO,
    STATUS_SEM_ASSINATURA,
)
from processamento.relatorio import agregar, gerar, ler_registros, montar_completo, montar_resumo

_EXECUCAO = {
    "inicio": "2026-08-06T10:00:00",
    "fim": "2026-08-06T10:05:00",
    "segundos_totais": 300.0,
    "workers": 4,
    "opcoes": {"confianca_minima": 0.15},
    "recursos": {
        "nucleos_logicos": 16,
        "ram_total_gb": 32.0,
        "amostras": 150,
        "intervalo_segundos": 2.0,
        "cpu_media_pct": 82.4,
        "cpu_maxima_pct": 99.1,
        "ram_media_pct": 41.0,
        "ram_maxima_pct": 55.2,
        "rss_medio_mb": 2100.0,
        "rss_maximo_mb": 3400.0,
    },
}


def _registro(**campos: Any) -> dict[str, Any]:
    base = {
        "arquivo": "a/doc.pdf",
        "formato": ".pdf",
        "tipo": "pdf",
        "tamanho_bytes": 1024,
        "status": STATUS_ASSINADO,
        "assinaturas_total": 1,
        "nivel0_total": 1,
        "nivel1_total": 0,
        "fontes": ["digital"],
        "signatarios": ["FULANO"],
        "paginas": 2,
        "paginas_em_branco": 1,
        "paginas_analisadas_nivel1": 1,
        "paginas_limitadas": False,
        "tempo_ms": 100.0,
        "tempo_inferencia_ms": 40.0,
        "nivel1_erro": None,
        "erro": None,
        "erro_tipo": None,
        "retentavel": True,
        "tentativas": 1,
        "processado_em": "2026-08-06T10:00:01",
    }
    return {**base, **campos}


def _lote_variado() -> list[dict[str, Any]]:
    return [
        _registro(arquivo="a/assinado-n0.pdf"),
        _registro(
            arquivo="a/assinado-n1.pdf",
            nivel0_total=0,
            nivel1_total=3,
            assinaturas_total=3,
            fontes=["visual"],
            signatarios=[],
        ),
        _registro(
            arquivo="b/sem-assinatura.pdf",
            status=STATUS_SEM_ASSINATURA,
            assinaturas_total=0,
            nivel0_total=0,
            fontes=[],
            signatarios=[],
        ),
        _registro(
            arquivo="b/quebrado.pdf",
            status=STATUS_ERRO,
            assinaturas_total=0,
            nivel0_total=0,
            erro="PDF ilegível ou protegido por senha",
            erro_tipo="DocumentoInvalidoError",
            paginas=0,
            tentativas=3,
        ),
        _registro(
            arquivo="b/planilha.xlsx",
            formato=".xlsx",
            tipo="nao_suportado",
            status=STATUS_NAO_SUPORTADO,
            assinaturas_total=0,
            nivel0_total=0,
            erro="formato `.xlsx`",
            erro_tipo="FormatoNaoSuportadoError",
            paginas=0,
        ),
        _registro(
            arquivo="c/sem-modelo.pdf",
            status=STATUS_INDETERMINADO,
            assinaturas_total=0,
            nivel0_total=0,
            nivel1_erro="modelo ONNX ausente",
        ),
    ]


def _lote_com_fallback() -> list[dict[str, Any]]:
    """O lote variado + os dois desfechos possíveis do fallback de ladrilhos."""
    return [
        *_lote_variado(),
        _registro(
            arquivo="d/resgatado.pdf",
            nivel0_total=0,
            nivel1_total=1,
            assinaturas_total=1,
            fontes=["visual"],
            signatarios=[],
            paginas_ladrilhadas=2,
        ),
        _registro(
            arquivo="d/nem-com-ladrilho.pdf",
            status=STATUS_SEM_ASSINATURA,
            assinaturas_total=0,
            nivel0_total=0,
            fontes=[],
            signatarios=[],
            paginas_ladrilhadas=3,
        ),
    ]


def test_agregacao_separa_assinaturas_por_nivel() -> None:
    dados = agregar(_lote_variado())

    assert dados["documentos"] == 6
    assert dados["assinaturas"] == {"total": 4, "nivel0": 1, "nivel1": 3}
    assert dados["documentos_por_metodo"]["somente_nivel0"] == 1
    assert dados["documentos_por_metodo"]["somente_nivel1"] == 1
    assert dados["status"][STATUS_ASSINADO] == 2
    assert dados["status"][STATUS_SEM_ASSINATURA] == 1
    assert dados["erros_por_tipo"]["DocumentoInvalidoError"] == 1


def test_fallback_conta_quem_entrou_e_quem_foi_resgatado() -> None:
    dados = agregar(_lote_com_fallback())

    assert dados["fallback"] == {
        "documentos": 2,
        "paginas": 5,
        "documentos_resgatados": 1,
        "assinaturas": 1,
    }


def test_resumo_mostra_o_percentual_com_e_sem_o_fallback() -> None:
    """A pergunta que o teste do fallback existe para responder."""
    resumo = montar_resumo(_lote_com_fallback(), execucao=_EXECUCAO)

    assert "## Fallback de ladrilhos (3×3)" in resumo
    assert "Resgatados (viraram ✅): **1**" in resumo
    assert "teria fechado em **25.0%**" in resumo
    assert "com ele, **37.5%**" in resumo


def test_completo_marca_o_documento_resgatado_pelo_fallback() -> None:
    completo = montar_completo(_lote_com_fallback(), execucao=_EXECUCAO)

    assert "fallback 3×3 em 2 pág — assinatura resgatada" in completo
    assert "fallback 3×3 em 3 pág |" in completo


def test_checkpoint_anterior_ao_fallback_continua_gerando_relatorio() -> None:
    """Registro gravado antes desta versão não tem a chave — `--apenas-relatorio`
    sobre ele não pode quebrar."""
    registros = _lote_variado()

    assert agregar(registros)["fallback"]["documentos"] == 0
    assert "## Fallback" not in montar_resumo(registros, execucao=_EXECUCAO)


def test_tabela_completa_tem_uma_linha_por_documento() -> None:
    registros = _lote_variado()

    completo = montar_completo(registros, execucao=_EXECUCAO)
    linhas_tabela = [linha for linha in completo.splitlines() if linha.startswith("| ")]

    assert len(linhas_tabela) == len(registros) + 1  # + cabeçalho
    for registro in registros:
        assert registro["arquivo"] in completo


def test_emojis_distinguem_achou_nao_achou_e_falhou() -> None:
    completo = montar_completo(_lote_variado(), execucao=_EXECUCAO)
    linhas = {
        linha.split("|")[3].strip(): linha.split("|")[2].strip()
        for linha in completo.splitlines()
        if linha.startswith("| ") and "Documento" not in linha
    }

    assert linhas["a/assinado-n0.pdf"] == "✅"
    assert linhas["b/sem-assinatura.pdf"] == "❌"
    assert linhas["b/quebrado.pdf"] == "⚠️"
    assert linhas["b/planilha.xlsx"] == "⚠️"
    assert linhas["c/sem-modelo.pdf"] == "⚠️"


def test_nivel1_indisponivel_nao_vira_documento_sem_assinatura() -> None:
    """⚠️, não ❌: sem o detector visual não dá para afirmar que não há rubrica."""
    completo = montar_completo(_lote_variado(), execucao=_EXECUCAO)

    assert "Nível 1 indisponível: modelo ONNX ausente" in completo


def test_pipe_no_nome_do_arquivo_nao_quebra_a_tabela() -> None:
    completo = montar_completo(
        [_registro(arquivo="a/nome | com pipe.pdf")], execucao=_EXECUCAO
    )
    linha = next(
        linha for linha in completo.splitlines() if "nome" in linha and linha.startswith("| 1 ")
    )

    assert "\\|" in linha
    assert len(re.findall(r"(?<!\\)\|", linha)) == 11


def test_resumo_traz_hardware_e_desempenho() -> None:
    resumo = montar_resumo(_lote_variado(), execucao=_EXECUCAO)

    assert "82% em média" in resumo
    assert "Tempo médio por documento" in resumo
    assert "`.xlsx`" in resumo


def test_resumo_sem_amostras_de_hardware_nao_quebra() -> None:
    resumo = montar_resumo(_lote_variado(), execucao={**_EXECUCAO, "recursos": {}})

    assert "Execução curta demais" in resumo


def test_gerar_escreve_os_tres_arquivos(tmp_path: Path) -> None:
    jsonl = tmp_path / "resultados.jsonl"
    jsonl.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in _lote_variado()), encoding="utf-8"
    )

    caminhos = gerar(jsonl, tmp_path / "saida", _EXECUCAO)

    assert caminhos["resumo_md"].is_file()
    assert caminhos["completo_md"].is_file()
    dados = json.loads(caminhos["resumo_json"].read_text(encoding="utf-8"))
    assert dados["resumo"]["assinaturas"]["nivel1"] == 3
    assert dados["execucao"]["workers"] == 4


def test_registros_saem_ordenados_por_caminho(tmp_path: Path) -> None:
    """A ordem de conclusão do pool é imprevisível; o relatório, não."""
    jsonl = tmp_path / "resultados.jsonl"
    jsonl.write_text(
        "\n".join(
            json.dumps(_registro(arquivo=nome))
            for nome in ("z/ultimo.pdf", "a/primeiro.pdf", "m/meio.pdf")
        ),
        encoding="utf-8",
    )

    assert [r["arquivo"] for r in ler_registros(jsonl)] == [
        "a/primeiro.pdf",
        "m/meio.pdf",
        "z/ultimo.pdf",
    ]
