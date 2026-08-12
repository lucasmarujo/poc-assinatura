"""Testes da execução em lote: statuses, checkpoint, retomada, retry e trava.

O lote roda com o Nível 1 desligado — o percurso todo é exercitado de verdade,
sem depender dos pesos ONNX de 44 MB.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from processamento.lote import (
    STATUS_ASSINADO,
    STATUS_CONTAINER,
    STATUS_ERRO,
    STATUS_NAO_SUPORTADO,
    STATUS_SEM_ASSINATURA,
    ExecucaoEmAndamentoError,
    OpcoesDeteccao,
    carregar_concluidos,
    processar_lote,
    trava_da_execucao,
)

OPCOES_SEM_NIVEL1 = OpcoesDeteccao(executar_nivel1=False)


def _rodar(raiz: Path, saida: Path, **extras: Any) -> dict[str, Any]:
    return processar_lote(
        raiz=raiz,
        saida_jsonl=saida / "resultados.jsonl",
        opcoes=OPCOES_SEM_NIVEL1,
        **extras,
    )


def _por_arquivo(jsonl: Path) -> dict[str, dict[str, Any]]:
    registros = [json.loads(linha) for linha in jsonl.read_text(encoding="utf-8").splitlines()]
    return {Path(r["arquivo"]).name: r for r in registros}


def test_lote_processa_a_arvore_inteira_e_classifica_cada_documento(
    arvore_files: Path, tmp_path: Path
) -> None:
    saida = tmp_path / "resultados"

    metadados = _rodar(arvore_files, saida)
    registros = _por_arquivo(saida / "resultados.jsonl")

    assert metadados["documentos_encontrados"] == len(registros)
    assert registros["contrato.pdf"]["status"] == STATUS_ASSINADO
    assert registros["contrato.pdf"]["nivel0_total"] == 1
    assert registros["ficha.pdf"]["status"] == STATUS_SEM_ASSINATURA
    assert registros["termo.docx"]["status"] == STATUS_ASSINADO
    assert registros["termo.docx"]["signatarios"] == ["MARIA SOUZA"]
    assert registros["planilha.xlsx"]["status"] == STATUS_NAO_SUPORTADO
    assert registros["vazio.pdf"]["status"] == STATUS_ERRO
    assert "~$rascunho.docx" not in registros
    assert registros["treinamento.pptx"]["status"] == STATUS_ASSINADO
    assert registros["treinamento.pptx"]["signatarios"] == ["JOAO LIMA"]


def test_zip_e_extraido_e_o_conteudo_processado(arvore_files: Path, tmp_path: Path) -> None:
    """O `.zip` vira 📦 apontando para a pasta; os PDFs de dentro viram linha."""
    saida = tmp_path / "resultados"

    _rodar(arvore_files, saida)
    registros = _por_arquivo(saida / "resultados.jsonl")

    assert registros["lote.zip"]["status"] == STATUS_CONTAINER
    assert "extraído em" in registros["lote.zip"]["observacao"]
    assert registros["contrato-zipado.pdf"]["status"] == STATUS_ASSINADO
    assert registros["ficha-zipada.pdf"]["status"] == STATUS_SEM_ASSINATURA


def test_cada_documento_gera_exatamente_uma_linha_no_checkpoint(
    arvore_files: Path, tmp_path: Path
) -> None:
    """Documento gravado duas vezes (o retry que também grava no caller) inflaria
    todas as contagens do relatório."""
    saida = tmp_path / "resultados"

    metadados = _rodar(arvore_files, saida)
    linhas = (saida / "resultados.jsonl").read_text(encoding="utf-8").strip().splitlines()
    arquivos = [json.loads(linha)["arquivo"] for linha in linhas]

    assert len(arquivos) == len(set(arquivos)) == metadados["documentos_encontrados"]


def test_registro_traz_a_contagem_de_paginas_ladrilhadas(
    arvore_files: Path, tmp_path: Path
) -> None:
    """Com o Nível 1 desligado ninguém ladrilha, mas a coluna tem de existir —
    é dela que o relatório tira o ganho do fallback."""
    saida = tmp_path / "resultados"

    _rodar(arvore_files, saida)
    registros = _por_arquivo(saida / "resultados.jsonl")

    assert registros["ficha.pdf"]["paginas_ladrilhadas"] == 0


def test_paginas_em_branco_sao_contadas_na_triagem(arvore_files: Path, tmp_path: Path) -> None:
    _rodar(arvore_files, tmp_path / "resultados")
    registros = _por_arquivo(tmp_path / "resultados" / "resultados.jsonl")

    assert registros["ficha.pdf"]["paginas"] == 2
    assert registros["ficha.pdf"]["paginas_em_branco"] == 1


def test_erro_de_documento_nao_derruba_o_lote(arvore_files: Path, tmp_path: Path) -> None:
    """`vazio.pdf` falha; todos os outros documentos continuam sendo processados."""
    registros = _rodar(arvore_files, tmp_path / "resultados")

    assert registros["documentos_processados"] == registros["documentos_encontrados"]


def test_segunda_execucao_retoma_do_checkpoint(arvore_files: Path, tmp_path: Path) -> None:
    saida = tmp_path / "resultados"
    _rodar(arvore_files, saida)

    metadados = _rodar(arvore_files, saida)

    assert metadados["documentos_processados"] == 0
    assert metadados["documentos_retomados"] == metadados["documentos_encontrados"]


def test_reprocessar_ignora_o_checkpoint(arvore_files: Path, tmp_path: Path) -> None:
    saida = tmp_path / "resultados"
    _rodar(arvore_files, saida)

    metadados = _rodar(arvore_files, saida, reprocessar=True)

    assert metadados["documentos_retomados"] == 0
    assert metadados["documentos_processados"] == metadados["documentos_encontrados"]


def test_checkpoint_ignora_linha_truncada(tmp_path: Path) -> None:
    """Queda no meio de uma escrita não pode invalidar a retomada inteira."""
    jsonl = tmp_path / "resultados.jsonl"
    jsonl.write_text(
        '{"arquivo": "a.pdf", "status": "assinado"}\n'
        '{"arquivo": "b.pd\n'
        '{"arquivo": "c.pdf", "status": "sem_assinatura"}\n',
        encoding="utf-8",
    )

    assert carregar_concluidos(jsonl) == {"a.pdf", "c.pdf"}


def test_formato_que_passou_a_ser_suportado_e_reavaliado_na_retomada(tmp_path: Path) -> None:
    """`.pptx` marcado como não suportado numa execução antiga precisa voltar à
    fila; `.xls`, que segue fora, não."""
    jsonl = tmp_path / "resultados.jsonl"
    jsonl.write_text(
        '{"arquivo": "a.pptx", "status": "nao_suportado", "formato": ".pptx"}\n'
        '{"arquivo": "b.xls", "status": "nao_suportado", "formato": ".xls"}\n'
        '{"arquivo": "c.pdf", "status": "assinado", "formato": ".pdf"}\n',
        encoding="utf-8",
    )

    assert carregar_concluidos(jsonl) == {"b.xls", "c.pdf"}


def test_duas_execucoes_simultaneas_sao_recusadas(tmp_path: Path) -> None:
    """Dois lotes no mesmo checkpoint duplicariam registros e dobrariam a CPU."""
    with trava_da_execucao(tmp_path):
        with pytest.raises(ExecucaoEmAndamentoError, match="já existe um lote"):
            with trava_da_execucao(tmp_path):
                pass


def test_trava_e_liberada_ao_fim_da_execucao(tmp_path: Path) -> None:
    with trava_da_execucao(tmp_path):
        assert (tmp_path / ".lock").is_file()

    assert not (tmp_path / ".lock").exists()
    with trava_da_execucao(tmp_path):
        pass


def test_trava_orfa_de_processo_morto_e_assumida(tmp_path: Path) -> None:
    """Queda de energia deixa a trava para trás; o lote seguinte não pode
    ficar refém dela."""
    (tmp_path / ".lock").write_text(
        json.dumps({"pid": 999_999, "iniciado_em": 0.0}), encoding="utf-8"
    )

    with trava_da_execucao(tmp_path):
        pass


def test_falha_transitoria_e_retentada_e_o_documento_conclui(
    arvore_files: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A primeira leitura de `contrato.pdf` explode; a segunda funciona."""
    import deteccao

    original = deteccao.detectar_assinaturas
    falhou: list[str] = []

    def instavel(path: Any, **kwargs: Any) -> Any:
        if "contrato" in str(path) and not falhou:
            falhou.append("sim")
            raise OSError("disco ocupado")
        return original(path, **kwargs)

    monkeypatch.setattr("deteccao.detectar_assinaturas", instavel)
    saida = tmp_path / "resultados"

    _rodar(arvore_files, saida, tentativas_maximas=2)
    registros = _por_arquivo(saida / "resultados.jsonl")

    assert falhou, "o teste não exercitou a falha"
    assert registros["contrato.pdf"]["status"] == STATUS_ASSINADO
    assert registros["contrato.pdf"]["tentativas"] == 2


def test_falha_persistente_vira_erro_depois_das_tentativas(
    arvore_files: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def sempre_falha(path: Any, **kwargs: Any) -> Any:
        raise OSError("disco morreu")

    monkeypatch.setattr("deteccao.detectar_assinaturas", sempre_falha)
    saida = tmp_path / "resultados"

    _rodar(arvore_files, saida, tentativas_maximas=2)
    registros = _por_arquivo(saida / "resultados.jsonl")

    assert registros["contrato.pdf"]["status"] == STATUS_ERRO
    assert registros["contrato.pdf"]["tentativas"] == 3
    assert registros["contrato.pdf"]["erro_tipo"] == "OSError"


def test_erro_deterministico_nao_e_retentado(arvore_files: Path, tmp_path: Path) -> None:
    """Arquivo vazio não melhora na segunda tentativa — repetir só queima CPU."""
    saida = tmp_path / "resultados"

    _rodar(arvore_files, saida)
    registros = _por_arquivo(saida / "resultados.jsonl")

    assert registros["vazio.pdf"]["status"] == STATUS_ERRO
    assert registros["vazio.pdf"]["tentativas"] == 1
