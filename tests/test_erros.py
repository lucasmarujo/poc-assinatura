"""Testes da triagem: a página que mostra *qual* foi o erro, não quantos.

O que se protege aqui é o que a página precisa acertar para servir de base de
classificação de causa:

* a **classe** de cada cartão (trocar falso ✅ com falso ❌ inverte o diagnóstico);
* a **evidência do Nível 0** aparecer sempre, inclusive quando é "não" — é ela
  que separa erro do detector visual de erro do sinal criptográfico;
* o **conteúdo duplicado** virar um cartão só, com a contagem de cópias.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from processamento.lote import STATUS_ASSINADO, STATUS_SEM_ASSINATURA
from validacao.amostra import ItemAmostra, pastas_de_rotulagem
from validacao.erros import coletar, detalhes, gerar, ids_para_rever, montar_pagina


def _item(indice: int, *, assinado: bool, arquivo: str | None = None) -> ItemAmostra:
    return ItemAmostra(
        arquivo=arquivo or f"pasta/doc{indice}.pdf",
        sha256=f"{indice:012x}" + "0" * 52,
        estrato="pos_nivel0" if assinado else "neg_pdf_1pag",
        status=STATUS_ASSINADO if assinado else STATUS_SEM_ASSINATURA,
        paginas=1,
    )


def _registro(item: ItemAmostra, **campos: Any) -> dict[str, Any]:
    return {
        "arquivo": item.arquivo,
        "formato": ".pdf",
        "tipo": "pdf",
        "status": item.status,
        "assinaturas_total": 1 if item.veredito_tool else 0,
        "nivel0_total": 0,
        "nivel1_total": 1 if item.veredito_tool else 0,
        "fontes": ["visual"] if item.veredito_tool else [],
        "signatarios": [],
        "paginas": 1,
        "paginas_em_branco": 0,
        "paginas_analisadas_nivel1": 1,
        "paginas_ladrilhadas": 0,
        "paginas_limitadas": False,
        **campos,
    }


def _amostra(tmp_path: Path, itens: list[ItemAmostra]) -> Path:
    pastas = pastas_de_rotulagem(tmp_path / "amostra")
    for item in itens:
        (pastas["pendentes"] / f"{item.id}.jpg").write_bytes(b"jpeg-falso")
    return tmp_path


# ---------- classificação -----------------------------------------------------


def test_classe_separa_falso_positivo_de_falso_negativo(tmp_path: Path) -> None:
    falso_positivo = _item(1, assinado=True)
    falso_negativo = _item(2, assinado=False)
    acerto = _item(3, assinado=True)
    itens = [falso_positivo, falso_negativo, acerto]
    saida = _amostra(tmp_path, itens)

    cartoes = coletar(
        registros=[_registro(item) for item in itens],
        indice=itens,
        rotulos={
            falso_positivo.sha256: False,
            falso_negativo.sha256: True,
            acerto.sha256: True,
        },
        pasta_saida=saida,
        raiz_files=tmp_path / "files",
    )

    por_id = {cartao["id"]: cartao for cartao in cartoes}
    assert por_id[falso_positivo.id]["classe"] == "fp"
    assert por_id[falso_negativo.id]["classe"] == "fn"
    assert por_id[acerto.id]["classe"] == "vp"
    # Erro primeiro: a lista tem 400+ cartões e é o erro que se veio ver.
    assert [cartao["classe"] for cartao in cartoes][:2] == ["fn", "fp"]


def test_duvida_vira_cartao_e_conteudo_sem_rotulo_fica_de_fora(tmp_path: Path) -> None:
    duvidoso, sem_rotulo = _item(1, assinado=True), _item(2, assinado=False)
    itens = [duvidoso, sem_rotulo]
    saida = _amostra(tmp_path, itens)

    cartoes = coletar(
        registros=[_registro(item) for item in itens],
        indice=itens,
        rotulos={duvidoso.sha256: None},
        pasta_saida=saida,
        raiz_files=tmp_path / "files",
    )

    assert [(cartao["id"], cartao["classe"]) for cartao in cartoes] == [(duvidoso.id, "duvida")]


def test_conteudo_duplicado_vira_um_cartao_com_a_contagem_de_copias(tmp_path: Path) -> None:
    original = _item(1, assinado=False, arquivo="a/doc.pdf")
    copia = ItemAmostra(**{**original.__dict__, "arquivo": "b/doc.pdf"})
    saida = _amostra(tmp_path, [original])

    cartoes = coletar(
        registros=[_registro(original), _registro(copia)],
        indice=[original, copia],
        rotulos={original.sha256: True},
        pasta_saida=saida,
        raiz_files=tmp_path / "files",
    )

    assert len(cartoes) == 1
    formato = dict(cartoes[0]["detalhes"])["Formato"]
    assert "2 cópia(s)" in formato


# ---------- evidência ---------------------------------------------------------


def test_detalhes_declaram_acroform_mesmo_quando_ausente() -> None:
    sem_acroform = dict(_registro(_item(1, assinado=True)))
    com_acroform = {
        **sem_acroform,
        "nivel0_total": 2,
        "fontes": ["digital", "pdf_embedded"],
        "signatarios": ["FULANO DE TAL"],
    }

    linhas_sem = dict(detalhes(sem_acroform, copias=1))
    linhas_com = dict(detalhes(com_acroform, copias=1))

    assert linhas_sem["Nível 0 — AcroForm `/Sig`"].startswith("não")
    assert linhas_sem["Nível 0 — carimbo digital no texto"] == "não"
    assert linhas_com["Nível 0 — AcroForm `/Sig`"].startswith("sim")
    assert linhas_com["Nível 0 — carimbo digital no texto"] == "sim"
    assert linhas_com["Signatários lidos"] == "FULANO DE TAL"


def test_cartao_sem_imagem_renderizada_nao_derruba_a_coleta(tmp_path: Path) -> None:
    item = _item(1, assinado=False)
    pastas_de_rotulagem(tmp_path / "amostra")  # pastas existem, imagem não

    cartoes = coletar(
        registros=[_registro(item)],
        indice=[item],
        rotulos={item.sha256: True},
        pasta_saida=tmp_path,
        raiz_files=tmp_path / "files",
    )

    assert cartoes[0]["imagem"] is None
    assert "sem imagem renderizada" in montar_pagina(cartoes)


# ---------- página ------------------------------------------------------------


def test_pagina_embute_os_cartoes_como_json_valido(tmp_path: Path) -> None:
    item = _item(1, assinado=True)
    saida = _amostra(tmp_path, [item])

    cartoes = coletar(
        registros=[_registro(item)],
        indice=[item],
        rotulos={item.sha256: False},
        pasta_saida=saida,
        raiz_files=tmp_path / "files",
    )
    pagina = montar_pagina(cartoes)

    inicio = pagina.index("const DADOS = Object.assign(") + len("const DADOS = Object.assign(")
    embutido = pagina[inicio : pagina.index(", {legendas:", inicio)]
    assert json.loads(embutido)[0]["id"] == item.id
    assert "__DADOS__" not in pagina


# ---------- volta para o rotulador --------------------------------------------


def test_ids_para_rever_pega_so_o_que_pede_rotulo_novo(tmp_path: Path) -> None:
    """`modelo` e `documento` ficam de fora: neles o rótulo foi dado por bom, e
    trazê-los de volta seria reetiquetar até a tool concordar."""
    triagem = tmp_path / "triagem-erros.json"
    triagem.write_text(
        json.dumps(
            [
                {"id": "aaaaaaaaaaaa", "classe": "fn", "estrato": "x", "causa": "rever"},
                {"id": "bbbbbbbbbbbb", "classe": "fp", "estrato": "x", "causa": "rotulagem"},
                {"id": "cccccccccccc", "classe": "fn", "estrato": "x", "causa": "modelo"},
                {"id": "dddddddddddd", "classe": "fp", "estrato": "x", "causa": "documento"},
            ]
        ),
        encoding="utf-8",
    )

    assert ids_para_rever(triagem) == ["aaaaaaaaaaaa", "bbbbbbbbbbbb"]
    assert ids_para_rever(triagem, causas={"modelo"}) == ["cccccccccccc"]


def test_gerar_escreve_o_html_e_conta_por_classe(tmp_path: Path) -> None:
    falso_negativo, acerto = _item(1, assinado=False), _item(2, assinado=True)
    itens = [falso_negativo, acerto]
    saida = _amostra(tmp_path, itens)
    from validacao.amostra import NOME_INDICE, NOME_ROTULOS, escrever_indice

    escrever_indice(saida / NOME_INDICE, itens)
    (saida / NOME_ROTULOS).write_text(
        "\n".join(
            json.dumps({"sha256": item.sha256, "tem_assinatura": True}) for item in itens
        ),
        encoding="utf-8",
    )

    caminho, contagem = gerar(
        registros=[_registro(item) for item in itens],
        pasta_saida=saida,
        raiz_files=tmp_path / "files",
    )

    assert caminho.is_file()
    assert contagem["fn"] == 1 and contagem["vp"] == 1
    assert "Triagem dos erros" in caminho.read_text(encoding="utf-8")
