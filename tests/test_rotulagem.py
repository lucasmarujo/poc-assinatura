"""Testes do rotulador local: fila cega, gravação a cada clique e as fronteiras.

Três coisas aqui, se quebrarem, estragam a métrica em silêncio:

* a **fila em blocos** (todos os ✅ seguidos) faz o rotulador adivinhar;
* a **página revelando o veredito** contamina o rótulo;
* o **id vindo da URL** é fronteira de confiança — sem validação, `../` lê
  qualquer arquivo da máquina.

E uma que estraga o dataset: rotular **não pode** mover nada de `pendentes/`,
nem tocar em `files/`.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from processamento.lote import STATUS_ASSINADO, STATUS_SEM_ASSINATURA
from validacao.amostra import ItemAmostra, pastas_de_rotulagem
from validacao.metricas import carregar_rotulos, ler_rotulos
from validacao.rotulagem import (
    aplicar_rotulo,
    criar_servidor,
    fila,
    gravar_rotulo,
    montar_pagina,
)


def _item(indice: int, *, assinado: bool = False) -> ItemAmostra:
    # Índice nos 12 primeiros hex: garante id distinto por item, que é o que o
    # nome da imagem usa.
    return ItemAmostra(
        arquivo=f"pasta/doc{indice}.pdf",
        sha256=f"{indice:012x}" + "0" * 52,
        estrato="pos_nivel0" if assinado else "neg_pdf_1pag",
        status=STATUS_ASSINADO if assinado else STATUS_SEM_ASSINATURA,
        paginas=1,
    )


def _amostra_com_imagens(tmp_path: Path, itens: list[ItemAmostra]) -> Path:
    pasta = tmp_path / "amostra"
    pastas = pastas_de_rotulagem(pasta)
    for item in itens:
        (pastas["pendentes"] / f"{item.id}.jpg").write_bytes(b"jpeg-falso")
    return pasta


# ---------- fila --------------------------------------------------------------


def test_fila_embaralha_para_nao_entregar_positivos_em_bloco(tmp_path: Path) -> None:
    itens = [_item(i, assinado=i >= 10) for i in range(20)]
    pasta = _amostra_com_imagens(tmp_path, itens)

    ordenada = [item.id for item in sorted(itens, key=lambda i: i.id)]
    embaralhada = [item.id for item in fila(itens, pasta, semente=42)]

    assert sorted(embaralhada) == sorted(ordenada)
    assert embaralhada != ordenada


def test_fila_e_deterministica_pela_semente(tmp_path: Path) -> None:
    itens = [_item(i) for i in range(12)]
    pasta = _amostra_com_imagens(tmp_path, itens)

    assert [i.id for i in fila(itens, pasta, semente=42)] == [
        i.id for i in fila(itens, pasta, semente=42)
    ]
    assert [i.id for i in fila(itens, pasta, semente=42)] != [
        i.id for i in fila(itens, pasta, semente=7)
    ]


def test_fila_colapsa_copias_do_mesmo_conteudo(tmp_path: Path) -> None:
    """Uma imagem por conteúdo: rotular a mesma coisa duas vezes é trabalho jogado."""
    item = _item(1)
    copia = ItemAmostra("outra/pasta/doc.pdf", item.sha256, item.estrato, item.status, 1)
    pasta = _amostra_com_imagens(tmp_path, [item])

    assert len(fila([item, copia], pasta, semente=42)) == 1


def test_fila_ignora_item_sem_imagem(tmp_path: Path) -> None:
    itens = [_item(1), _item(2)]
    pasta = _amostra_com_imagens(tmp_path, [itens[0]])

    assert [i.id for i in fila(itens, pasta, semente=42)] == [itens[0].id]


def test_fila_recorta_aos_ids_marcados_na_triagem(tmp_path: Path) -> None:
    """Modo revisão: só os documentos marcados, mesmo já tendo rótulo."""
    itens = [_item(i) for i in range(5)]
    pasta = _amostra_com_imagens(tmp_path, itens)
    marcados = {itens[1].id, itens[3].id}

    recortada = fila(itens, pasta, semente=42, apenas=marcados)

    assert {item.id for item in recortada} == marcados


# ---------- página ------------------------------------------------------------


def test_pagina_nao_revela_o_veredito_da_tool() -> None:
    """A página recebe só o id. Sem estrato, sem status, sem caminho — rotulador
    que sabe o que a tool decidiu não mede nada."""
    itens = [_item(1, assinado=True), _item(2)]

    pagina = montar_pagina(itens, {})

    assert itens[0].id in pagina
    assert "assinado" not in pagina
    assert "pos_nivel0" not in pagina
    assert "neg_pdf_1pag" not in pagina
    assert "pasta/doc1.pdf" not in pagina


def test_pagina_ja_vem_com_o_que_foi_rotulado_antes() -> None:
    """Reabrir o rotulador tem de continuar de onde parou, não recomeçar."""
    itens = [_item(1), _item(2), _item(3)]

    pagina = montar_pagina(itens, {itens[0].sha256: True, itens[1].sha256: None})

    dados = json.loads(pagina.split("const DADOS = ", 1)[1].split(";\n", 1)[0])
    assert dados["rotulos"] == {itens[0].id: "sim", itens[1].id: "duvida"}
    assert len(dados["fila"]) == 3
    assert dados["revisar"] is False


def test_pagina_em_revisao_abre_no_primeiro_mesmo_com_tudo_rotulado() -> None:
    """Sem o flag, a página pularia para o fim: na revisão todos já têm rótulo."""
    itens = [_item(1), _item(2)]

    pagina = montar_pagina(itens, {item.sha256: True for item in itens}, revisar=True)

    dados = json.loads(pagina.split("const DADOS = ", 1)[1].split(";\n", 1)[0])
    assert dados["revisar"] is True
    assert len(dados["fila"]) == 2


# ---------- aplicar rótulo ----------------------------------------------------


def test_rotular_vincula_e_preserva_o_pendente(tmp_path: Path) -> None:
    """O pedido explícito: o original renderizado não se move."""
    item = _item(1)
    pasta = _amostra_com_imagens(tmp_path, [item])

    destino = aplicar_rotulo(pasta, item.id, "sim")

    assert destino == pasta / "com_assinatura" / f"{item.id}.jpg"
    assert destino.is_file()
    assert (pasta / "pendentes" / f"{item.id}.jpg").is_file()
    assert ler_rotulos(pasta, conhecidos={item.id: item.sha256}) == {item.sha256: True}


def test_mudar_de_ideia_nao_deixa_dois_rotulos_em_disco(tmp_path: Path) -> None:
    """Com a imagem em duas pastas de rótulo, qual vale passaria a depender da
    ordem de iteração — o rótulo tem de sair da pasta antiga."""
    item = _item(1)
    pasta = _amostra_com_imagens(tmp_path, [item])

    aplicar_rotulo(pasta, item.id, "sim")
    aplicar_rotulo(pasta, item.id, "nao")

    assert not (pasta / "com_assinatura" / f"{item.id}.jpg").exists()
    assert (pasta / "sem_assinatura" / f"{item.id}.jpg").is_file()
    assert ler_rotulos(pasta, conhecidos={item.id: item.sha256}) == {item.sha256: False}


def test_rotular_recusa_id_fora_do_formato(tmp_path: Path) -> None:
    """Fronteira de confiança: o id vem da URL."""
    pasta = _amostra_com_imagens(tmp_path, [_item(1)])

    with pytest.raises(ValueError, match="id inválido"):
        aplicar_rotulo(pasta, "../../etc/passwd", "sim")


def test_os_tres_rotulos_batem_com_as_tres_pastas() -> None:
    """`ROTULOS_VALIDOS` e `PASTAS_ROTULO` são dois mapas do mesmo domínio: se um
    ganhar uma opção e o outro não, um rótulo válido não teria onde ser gravado."""
    from validacao.amostra import PASTAS_ROTULO
    from validacao.rotulagem import _PASTA_DO_ROTULO, ROTULOS_VALIDOS

    assert set(_PASTA_DO_ROTULO) == set(ROTULOS_VALIDOS)
    assert set(_PASTA_DO_ROTULO.values()) == set(PASTAS_ROTULO)
    for rotulo, pasta in _PASTA_DO_ROTULO.items():
        assert PASTAS_ROTULO[pasta] is ROTULOS_VALIDOS[rotulo]


def test_rotular_recusa_rotulo_desconhecido(tmp_path: Path) -> None:
    item = _item(1)
    pasta = _amostra_com_imagens(tmp_path, [item])

    with pytest.raises(ValueError, match="rótulo desconhecido"):
        aplicar_rotulo(pasta, item.id, "talvez")


def test_gravacao_e_append_e_a_ultima_linha_vence(tmp_path: Path) -> None:
    caminho = tmp_path / "rotulos.jsonl"

    gravar_rotulo(caminho, "a" * 64, "sim")
    gravar_rotulo(caminho, "a" * 64, "nao")

    assert carregar_rotulos(caminho) == {"a" * 64: False}


# ---------- servidor ----------------------------------------------------------


@pytest.fixture
def servidor_em_pe(tmp_path: Path):
    """Servidor real numa porta efêmera, encerrado no fim do teste."""
    itens = [_item(i) for i in range(3)]
    pasta = _amostra_com_imagens(tmp_path, itens)
    servidor = criar_servidor(
        itens=itens,
        rotulos={},
        pasta_amostra=pasta,
        caminho_rotulos=tmp_path / "rotulos.jsonl",
        porta=0,
    )
    thread = threading.Thread(target=servidor.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{servidor.server_address[1]}", itens, pasta, tmp_path
    finally:
        servidor.shutdown()
        servidor.server_close()
        thread.join(timeout=5)


def _postar(url: str, corpo: dict[str, str]) -> int:
    pedido = urllib.request.Request(
        url,
        data=json.dumps(corpo).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(pedido, timeout=5) as resposta:
            return int(resposta.status)
    except urllib.error.HTTPError as erro:
        return int(erro.code)


def test_servidor_entrega_pagina_e_imagem(servidor_em_pe) -> None:
    base, itens, _, _ = servidor_em_pe

    with urllib.request.urlopen(f"{base}/", timeout=5) as resposta:
        assert b"Rotular assinaturas" in resposta.read()
    with urllib.request.urlopen(
        f"{base}/amostra/pendentes/{itens[0].id}.jpg", timeout=5
    ) as resposta:
        assert resposta.read() == b"jpeg-falso"
        assert resposta.headers["Content-Type"] == "image/jpeg"


def test_um_clique_grava_pasta_e_jsonl(servidor_em_pe) -> None:
    base, itens, pasta, tmp_path = servidor_em_pe

    assert _postar(f"{base}/api/rotulo", {"id": itens[0].id, "rotulo": "sim"}) == 200

    assert (pasta / "com_assinatura" / f"{itens[0].id}.jpg").is_file()
    assert carregar_rotulos(tmp_path / "rotulos.jsonl") == {itens[0].sha256: True}


def test_servidor_recusa_id_desconhecido_e_travessia(servidor_em_pe) -> None:
    base, _, _, _ = servidor_em_pe

    assert _postar(f"{base}/api/rotulo", {"id": "f" * 12, "rotulo": "sim"}) == 400
    assert _postar(f"{base}/api/rotulo", {"id": "../segredo", "rotulo": "sim"}) == 400

    with pytest.raises(urllib.error.HTTPError) as erro:
        urllib.request.urlopen(f"{base}/amostra/pendentes/nao-e-um-id.jpg", timeout=5)
    assert erro.value.code == 400


def test_servidor_recusa_rotulo_invalido(servidor_em_pe) -> None:
    base, itens, _, _ = servidor_em_pe

    assert _postar(f"{base}/api/rotulo", {"id": itens[0].id, "rotulo": "talvez"}) == 400


def test_servidor_so_escuta_em_localhost(tmp_path: Path) -> None:
    """Documento de trabalhador não pode ficar exposto na rede por descuido de bind."""
    itens = [_item(1)]
    servidor = criar_servidor(
        itens=itens,
        rotulos={},
        pasta_amostra=_amostra_com_imagens(tmp_path, itens),
        caminho_rotulos=tmp_path / "rotulos.jsonl",
        porta=0,
    )
    try:
        assert servidor.server_address[0] == "127.0.0.1"
    finally:
        servidor.server_close()
