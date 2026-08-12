"""Testes da auditoria: estratificação, sorteio, Wilson e reponderação.

O que se protege aqui são as três coisas que, se quebrarem, produzem um número
plausível e errado — o pior desfecho possível num relatório de acurácia:

* o **estrato** de cada registro (é ele que define o peso de reponderação);
* a **reprodutibilidade e o encaixe** do sorteio (aumentar o n não pode
  invalidar rótulo já feito);
* a **reponderação** (amostra concentrada no risco não pode contaminar o total).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conftest import nova_imagem, novo_docx, novo_pdf

from processamento.lote import (
    STATUS_ASSINADO,
    STATUS_ERRO,
    STATUS_NAO_SUPORTADO,
    STATUS_SEM_ASSINATURA,
)
from validacao.amostra import (
    ItemAmostra,
    alocar,
    atualizar_hashes,
    carregar_hashes,
    escrever_indice,
    estrato,
    ler_indice,
    localizar_imagem,
    preparar_amostra,
    renderizar_folha,
    sha256_arquivo,
    sortear,
)
from validacao.metricas import (
    Celulas,
    avaliar,
    carregar_rotulos,
    celula,
    gerar,
    ler_rotulos,
    montar_validacao,
    salvar_rotulos,
    taxas,
    wilson,
)


def _registro(**campos: Any) -> dict[str, Any]:
    base = {
        "arquivo": "a/doc.pdf",
        "formato": ".pdf",
        "tipo": "pdf",
        "tamanho_bytes": 1024,
        "status": STATUS_SEM_ASSINATURA,
        "assinaturas_total": 0,
        "nivel0_total": 0,
        "nivel1_total": 0,
        "paginas": 1,
        "paginas_em_branco": 0,
        "paginas_analisadas_nivel1": 1,
        "paginas_ladrilhadas": 1,
        "paginas_limitadas": False,
    }
    return {**base, **campos}


# ---------- estratos ----------------------------------------------------------


def test_estrato_isola_os_riscos_conhecidos_dos_negativos() -> None:
    assert estrato(_registro(paginas_limitadas=True, paginas=40)) == "neg_paginas_limitadas"
    assert estrato(_registro(tipo="ooxml", formato=".docx")) == "neg_ooxml"
    assert estrato(_registro(tipo="imagem", formato=".png")) == "neg_imagem"
    assert estrato(_registro(paginas=1)) == "neg_pdf_1pag"
    assert estrato(_registro(paginas=2, paginas_analisadas_nivel1=2)) == "neg_pdf_2pag"
    assert estrato(_registro(paginas=9, paginas_analisadas_nivel1=9)) == "neg_pdf_3pag_ou_mais"


def test_negativo_sem_pagina_analisada_e_um_estrato_proprio() -> None:
    """"Zero página analisada" é a tool afirmando ausência sem ter olhado nada —
    evidência muito diferente de um PDF com tinta em que o detector rodou. No lote
    real são 98 `.docx` cuja mídia não rendeu imagem raster e 1 PDF em branco."""
    docx_sem_midia = _registro(
        tipo="ooxml", formato=".docx", paginas=1, paginas_em_branco=1,
        paginas_analisadas_nivel1=0,
    )
    pdf_em_branco = _registro(paginas=2, paginas_em_branco=2, paginas_analisadas_nivel1=0)

    assert estrato(docx_sem_midia) == "neg_sem_pagina_analisada"
    assert estrato(pdf_em_branco) == "neg_sem_pagina_analisada"


def test_estrato_separa_a_evidencia_forte_da_fraca_nos_positivos() -> None:
    forte = _registro(status=STATUS_ASSINADO, nivel0_total=1, assinaturas_total=1)
    resgatado = _registro(
        status=STATUS_ASSINADO, nivel1_total=1, assinaturas_total=1, paginas_ladrilhadas=2
    )
    fraco = _registro(
        status=STATUS_ASSINADO, nivel1_total=1, assinaturas_total=1, paginas_ladrilhadas=0
    )
    varias = _registro(
        status=STATUS_ASSINADO, nivel1_total=4, assinaturas_total=4, paginas_ladrilhadas=0
    )

    assert estrato(forte) == "pos_nivel0"
    assert estrato(resgatado) == "pos_resgatado_fallback"
    assert estrato(fraco) == "pos_nivel1_uma_deteccao"
    assert estrato(varias) == "pos_nivel1_varias"


def test_alerta_fica_fora_da_avaliacao() -> None:
    """⚠️ não é acerto nem erro: entra no relatório como cobertura, não como taxa."""
    assert estrato(_registro(status=STATUS_ERRO)) == ""
    assert estrato(_registro(status=STATUS_NAO_SUPORTADO)) == ""
    assert estrato({"status": "container"}) == ""


# ---------- sorteio -----------------------------------------------------------


def _corpus(quantidade: int = 200) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Metade ❌ de uma página, metade ✅ com Nível 0. Hash único por arquivo."""
    registros = [
        _registro(arquivo=f"neg/{indice:04d}.pdf") for indice in range(quantidade // 2)
    ] + [
        _registro(
            arquivo=f"pos/{indice:04d}.pdf",
            status=STATUS_ASSINADO,
            nivel0_total=1,
            assinaturas_total=1,
        )
        for indice in range(quantidade // 2)
    ]
    hashes = {r["arquivo"]: f"{indice:064x}" for indice, r in enumerate(registros)}
    return registros, hashes


def test_sorteio_e_reprodutivel_pela_semente() -> None:
    registros, hashes = _corpus()
    argumentos = {"n_negativos": 30, "n_positivos": 30, "minimo_por_estrato": 5}

    primeira = sortear(registros, hashes, semente=42, **argumentos)
    segunda = sortear(registros, hashes, semente=42, **argumentos)
    outra = sortear(registros, hashes, semente=7, **argumentos)

    assert [item.arquivo for item in primeira] == [item.arquivo for item in segunda]
    assert [item.arquivo for item in primeira] != [item.arquivo for item in outra]


def test_aumentar_o_orcamento_preserva_a_amostra_anterior() -> None:
    """Rótulo já feito não pode virar lixo quando se decide amostrar mais."""
    registros, hashes = _corpus()

    pequena = sortear(
        registros, hashes, n_negativos=20, n_positivos=20, semente=42, minimo_por_estrato=1
    )
    grande = sortear(
        registros, hashes, n_negativos=60, n_positivos=60, semente=42, minimo_por_estrato=1
    )

    assert {item.arquivo for item in pequena} <= {item.arquivo for item in grande}
    assert len(grande) > len(pequena)


def test_estrato_pequeno_recebe_o_piso_e_nao_uma_sobra() -> None:
    """Os 48 documentos truncados do lote real receberiam 2 documentos numa
    alocação puramente proporcional — e não diriam nada sobre si mesmos."""
    grandes = [_registro(arquivo=f"neg/{i:04d}.pdf") for i in range(1000)]
    pequeno = [
        _registro(arquivo=f"trunc/{i:04d}.pdf", paginas_limitadas=True, paginas=40)
        for i in range(30)
    ]
    registros = grandes + pequeno
    hashes = {r["arquivo"]: f"{i:064x}" for i, r in enumerate(registros)}

    amostra = sortear(
        registros, hashes, n_negativos=100, n_positivos=0, semente=42, minimo_por_estrato=20
    )
    por_estrato = {
        nome: sum(1 for item in amostra if item.estrato == nome)
        for nome in {item.estrato for item in amostra}
    }

    assert por_estrato["neg_paginas_limitadas"] == 20
    assert por_estrato["neg_pdf_1pag"] >= 90


def test_alocacao_nunca_pede_mais_do_que_o_estrato_tem() -> None:
    estratos = {"a": [{}] * 5, "b": [{}] * 500}

    alocado = alocar(estratos, total=100, minimo=20)

    assert alocado["a"] == 5
    assert alocado["b"] <= 500


def test_documento_sem_hash_nao_entra_na_amostra() -> None:
    """Arquivo ilegível não pode virar linha da amostra: não há o que rotular."""
    registros, hashes = _corpus(10)
    del hashes[registros[0]["arquivo"]]

    amostra = sortear(
        registros, hashes, n_negativos=10, n_positivos=10, semente=42, minimo_por_estrato=1
    )

    assert registros[0]["arquivo"] not in {item.arquivo for item in amostra}


# ---------- hash e índice -----------------------------------------------------


def test_hash_e_cacheado_e_incremental(tmp_path: Path) -> None:
    raiz = tmp_path / "files"
    novo_pdf(raiz / "um.pdf", ["conteudo"])
    novo_pdf(raiz / "dois.pdf", ["outro"])
    cache = tmp_path / "hashes.jsonl"

    primeiro = atualizar_hashes(raiz=raiz, relativos=["um.pdf"], cache=cache)
    segundo = atualizar_hashes(raiz=raiz, relativos=["um.pdf", "dois.pdf"], cache=cache)

    assert set(primeiro) == {"um.pdf"}
    assert set(segundo) == {"um.pdf", "dois.pdf"}
    assert carregar_hashes(cache) == segundo
    assert segundo["um.pdf"] == sha256_arquivo(raiz / "um.pdf")


def test_arquivo_ausente_nao_derruba_o_hash(tmp_path: Path) -> None:
    hashes = atualizar_hashes(
        raiz=tmp_path, relativos=["nao-existe.pdf"], cache=tmp_path / "h.jsonl"
    )

    assert hashes == {}


def test_indice_recusa_colisao_de_id(tmp_path: Path) -> None:
    """Dois hashes com o mesmo prefixo trocariam rótulo entre documentos — tem de
    explodir alto, não virar erro silencioso na métrica."""
    import pytest

    itens = [
        ItemAmostra("a.pdf", "f" * 64, "neg_pdf_1pag", STATUS_SEM_ASSINATURA, 1),
        ItemAmostra("b.pdf", "f" * 20 + "0" * 44, "neg_pdf_1pag", STATUS_SEM_ASSINATURA, 1),
    ]

    with pytest.raises(RuntimeError, match="colisão de id"):
        escrever_indice(tmp_path / "indice.jsonl", itens)


def test_indice_faz_ida_e_volta(tmp_path: Path) -> None:
    itens = [ItemAmostra("a/doc.pdf", "ab" * 32, "neg_pdf_2pag", STATUS_SEM_ASSINATURA, 2)]
    caminho = tmp_path / "indice.jsonl"

    escrever_indice(caminho, itens)

    assert ler_indice(caminho) == itens


def test_indice_ignora_linha_corrompida(tmp_path: Path) -> None:
    caminho = tmp_path / "indice.jsonl"
    caminho.write_text('{"quebrado": \n{"sha256": "x"}\n', encoding="utf-8")

    assert ler_indice(caminho) == []


# ---------- folha de contato --------------------------------------------------


def test_folha_de_contato_sai_com_uma_imagem_por_documento(tmp_path: Path) -> None:
    from PIL import Image

    pdf = novo_pdf(tmp_path / "tres.pdf", ["um", "dois", "tres"])
    destino = tmp_path / "folha.jpg"

    assert renderizar_folha(pdf, destino, max_paginas=6) == 3
    with Image.open(destino) as imagem:
        # 3 páginas → grade de 2 colunas, então mais larga que uma página só.
        assert imagem.width > 1100
        assert imagem.format == "JPEG"


def test_folha_avisa_quando_truncou_o_documento(tmp_path: Path) -> None:
    """Rotulador que não sabe que existem mais páginas decidiria "sem assinatura"
    sobre um documento que ele não viu inteiro."""
    from PIL import Image

    pdf = novo_pdf(tmp_path / "muitas.pdf", [f"pagina {i}" for i in range(9)])

    total = renderizar_folha(pdf, tmp_path / "folha.jpg", max_paginas=4)

    assert total == 9
    with Image.open(tmp_path / "folha.jpg") as imagem:
        assert imagem.height > 0


def test_folha_aceita_docx_pelo_mesmo_caminho_do_lote(tmp_path: Path) -> None:
    docx = novo_docx(
        tmp_path / "termo.docx",
        texto="Assinado digitalmente por MARIA SOUZA",
        imagens=[nova_imagem(tmp_path / "rubrica.png")],
    )

    assert renderizar_folha(docx, tmp_path / "folha.jpg") >= 1
    assert (tmp_path / "folha.jpg").is_file()


def test_preparo_renderiza_uma_imagem_por_conteudo_distinto(tmp_path: Path) -> None:
    """A economia da deduplicação: cópia do mesmo conteúdo não vira imagem nova."""
    raiz = tmp_path / "files"
    novo_pdf(raiz / "a" / "doc.pdf", ["mesmo conteudo"])
    (raiz / "b").mkdir(parents=True, exist_ok=True)
    (raiz / "b" / "doc.pdf").write_bytes((raiz / "a" / "doc.pdf").read_bytes())
    digest = sha256_arquivo(raiz / "a" / "doc.pdf") or ""
    amostra = [
        ItemAmostra("a/doc.pdf", digest, "neg_pdf_1pag", STATUS_SEM_ASSINATURA, 1),
        ItemAmostra("b/doc.pdf", digest, "neg_pdf_1pag", STATUS_SEM_ASSINATURA, 1),
    ]

    resultado = preparar_amostra(
        raiz_files=raiz, amostra=amostra, pasta_saida=tmp_path / "auditoria"
    )

    assert resultado.conteudos == 1
    assert resultado.geradas == 1
    assert not resultado.falhas


def test_preparo_nao_re_renderiza_o_que_ja_foi_rotulado(tmp_path: Path) -> None:
    raiz = tmp_path / "files"
    novo_pdf(raiz / "doc.pdf", ["conteudo"])
    digest = sha256_arquivo(raiz / "doc.pdf") or ""
    amostra = [ItemAmostra("doc.pdf", digest, "neg_pdf_1pag", STATUS_SEM_ASSINATURA, 1)]
    saida = tmp_path / "auditoria"

    preparar_amostra(raiz_files=raiz, amostra=amostra, pasta_saida=saida)
    pasta = saida / "amostra"
    imagem = pasta / "pendentes" / f"{digest[:12]}.jpg"
    imagem.replace(pasta / "sem_assinatura" / imagem.name)
    segunda = preparar_amostra(raiz_files=raiz, amostra=amostra, pasta_saida=saida)

    assert segunda.geradas == 0
    assert segunda.reaproveitadas == 1
    assert localizar_imagem(pasta, digest[:12]) == pasta / "sem_assinatura" / imagem.name


def test_documento_ilegivel_vira_falha_e_nao_derruba_o_preparo(tmp_path: Path) -> None:
    raiz = tmp_path / "files"
    raiz.mkdir(parents=True)
    (raiz / "quebrado.pdf").write_bytes(b"nao e um pdf")
    novo_pdf(raiz / "bom.pdf", ["conteudo"])
    amostra = [
        ItemAmostra("quebrado.pdf", "a" * 64, "neg_pdf_1pag", STATUS_SEM_ASSINATURA, 1),
        ItemAmostra(
            "bom.pdf",
            sha256_arquivo(raiz / "bom.pdf") or "",
            "neg_pdf_1pag",
            STATUS_SEM_ASSINATURA,
            1,
        ),
    ]

    resultado = preparar_amostra(
        raiz_files=raiz, amostra=amostra, pasta_saida=tmp_path / "auditoria"
    )

    assert resultado.geradas == 1
    assert [arquivo for arquivo, _ in resultado.falhas] == ["quebrado.pdf"]


# ---------- Wilson e células --------------------------------------------------


def test_wilson_bate_com_o_valor_publicado() -> None:
    baixo, alto = wilson(95, 100)

    assert abs(baixo - 0.8872) < 0.002
    assert abs(alto - 0.9781) < 0.002


def test_wilson_nao_estoura_os_extremos() -> None:
    assert wilson(0, 0) == (0.0, 0.0)
    assert wilson(50, 50)[1] <= 1.0
    assert wilson(0, 50)[0] >= 0.0


def test_wilson_encolhe_com_amostra_maior() -> None:
    estreito = wilson(950, 1000)
    largo = wilson(95, 100)

    assert (estreito[1] - estreito[0]) < (largo[1] - largo[0])


def test_celula_mapeia_veredito_e_rotulo() -> None:
    assert celula(True, True) == Celulas(vp=1.0)
    assert celula(True, False) == Celulas(fp=1.0)
    assert celula(False, True) == Celulas(fn=1.0)
    assert celula(False, False) == Celulas(vn=1.0)


def test_taxas_respondem_as_perguntas_do_relatorio() -> None:
    """90 ✅ certos, 10 ✅ errados, 80 ❌ certos, 20 ❌ errados."""
    matriz = Celulas(vp=90, fp=10, vn=80, fn=20)

    resultado = taxas(matriz, matriz)

    assert abs(resultado["precisao"]["valor"] - 0.90) < 1e-9
    assert abs(resultado["recall"]["valor"] - 90 / 110) < 1e-9
    assert abs(resultado["especificidade"]["valor"] - 80 / 90) < 1e-9
    assert abs(resultado["vpn"]["valor"] - 0.80) < 1e-9
    assert abs(resultado["acuracia"]["valor"] - 0.85) < 1e-9


def test_taxa_sem_denominador_sai_nula_em_vez_de_dividir_por_zero() -> None:
    resultado = taxas(Celulas(vn=10), Celulas(vn=10))

    assert resultado["precisao"]["valor"] is None
    assert resultado["f1"]["valor"] is None
    assert resultado["vpn"]["valor"] == 1.0


# ---------- rótulos e reponderação -------------------------------------------


def test_a_pasta_e_o_rotulo(tmp_path: Path) -> None:
    pasta = tmp_path / "amostra"
    for nome in ("pendentes", "com_assinatura", "sem_assinatura", "duvida"):
        (pasta / nome).mkdir(parents=True)
    (pasta / "com_assinatura" / "aaaaaaaaaaaa.jpg").write_bytes(b"x")
    (pasta / "sem_assinatura" / "bbbbbbbbbbbb.jpg").write_bytes(b"x")
    (pasta / "duvida" / "cccccccccccc.jpg").write_bytes(b"x")
    (pasta / "pendentes" / "dddddddddddd.jpg").write_bytes(b"x")
    conhecidos = {letra * 12: letra * 64 for letra in "abcd"}

    rotulos = ler_rotulos(pasta, conhecidos=conhecidos)

    assert rotulos == {"a" * 64: True, "b" * 64: False, "c" * 64: None}


def test_imagem_orfa_de_outra_semente_e_ignorada(tmp_path: Path) -> None:
    pasta = tmp_path / "amostra"
    (pasta / "com_assinatura").mkdir(parents=True)
    (pasta / "com_assinatura" / "zzzzzzzzzzzz.jpg").write_bytes(b"x")

    assert ler_rotulos(pasta, conhecidos={}) == {}


def test_rotulos_persistidos_nao_carregam_caminho(tmp_path: Path) -> None:
    """São versionáveis: chaveados por sha256, sem nome de trabalhador."""
    caminho = tmp_path / "rotulos.jsonl"

    salvar_rotulos(caminho, {"a" * 64: True, "b" * 64: None})

    assert carregar_rotulos(caminho) == {"a" * 64: True, "b" * 64: None}
    assert "/" not in caminho.read_text(encoding="utf-8")


def test_reponderacao_desfaz_o_oversampling_do_estrato_de_risco() -> None:
    """1000 documentos num estrato que erra 0%, 100 num que erra 50%, com a
    amostra concentrada no segundo. O acerto real é (1000 + 50)/1100 = 95,5%;
    a contagem crua da amostra, que ignora os pesos, diria 30/50 = 60%."""
    registros = [_registro(arquivo=f"bom/{i:04d}.pdf") for i in range(1000)] + [
        _registro(arquivo=f"risco/{i:04d}.pdf", paginas_limitadas=True, paginas=40)
        for i in range(100)
    ]
    hashes = {r["arquivo"]: f"{i:064x}" for i, r in enumerate(registros)}
    indice = [
        ItemAmostra(f"bom/{i:04d}.pdf", hashes[f"bom/{i:04d}.pdf"], "neg_pdf_1pag",
                    STATUS_SEM_ASSINATURA, 1)
        for i in range(10)
    ] + [
        ItemAmostra(f"risco/{i:04d}.pdf", hashes[f"risco/{i:04d}.pdf"],
                    "neg_paginas_limitadas", STATUS_SEM_ASSINATURA, 40)
        for i in range(40)
    ]
    # O estrato bom nunca erra; o de risco erra na metade (tinha assinatura e a
    # tool disse ❌).
    rotulos: dict[str, bool | None] = {item.sha256: False for item in indice[:10]}
    rotulos.update({item.sha256: indice.index(item) % 2 == 0 for item in indice[10:]})

    dados = avaliar(registros=registros, hashes=hashes, indice=indice, rotulos=rotulos)

    assert abs(dados["taxas"]["acuracia"]["valor"] - 1050 / 1100) < 0.01
    assert abs(dados["matriz_estimada"]["falso_negativo"] - 50) < 1.0
    assert dados["por_estrato"]["neg_paginas_limitadas"]["fator"] == 2.5
    # A conta crua da amostra, que a reponderação existe para não deixar sair.
    assert dados["matriz_amostra"]["falso_negativo"] == 20


def test_duvida_sai_do_denominador_e_e_reportada() -> None:
    registros = [_registro(arquivo=f"neg/{i}.pdf") for i in range(10)]
    hashes = {r["arquivo"]: f"{i:064x}" for i, r in enumerate(registros)}
    indice = [
        ItemAmostra(r["arquivo"], hashes[r["arquivo"]], "neg_pdf_1pag", STATUS_SEM_ASSINATURA, 1)
        for r in registros[:4]
    ]
    rotulos: dict[str, bool | None] = {
        indice[0].sha256: False,
        indice[1].sha256: False,
        indice[2].sha256: None,
    }

    dados = avaliar(registros=registros, hashes=hashes, indice=indice, rotulos=rotulos)

    assert dados["amostra"]["rotulados"] == 2
    assert dados["amostra"]["duvidas"] == 1
    assert dados["amostra"]["pendentes"] == 1
    assert dados["taxas"]["vpn"]["valor"] == 1.0


def test_cobertura_conta_a_copia_que_herdou_o_rotulo() -> None:
    """O ganho da deduplicação: rotular um conteúdo valida todas as cópias dele."""
    registros = [_registro(arquivo=f"pasta{i}/mesmo.pdf") for i in range(7)]
    hashes = {r["arquivo"]: "c" * 64 for r in registros}
    indice = [ItemAmostra("pasta0/mesmo.pdf", "c" * 64, "neg_pdf_1pag", STATUS_SEM_ASSINATURA, 1)]

    dados = avaliar(
        registros=registros, hashes=hashes, indice=indice, rotulos={"c" * 64: False}
    )

    assert dados["lote"]["documentos_com_rotulo_direto"] == 7
    assert dados["lote"]["conteudos_distintos_rotulados"] == 1


def test_estrato_sem_rotulo_nao_desaparece_em_silencio() -> None:
    """Estrato sem rótulo não pode ser reponderado, então sai da matriz — e o
    relatório tem de dizer que o número passou a cobrir menos que o lote."""
    registros = [_registro(arquivo=f"neg/{i}.pdf") for i in range(10)] + [
        _registro(arquivo=f"trunc/{i}.pdf", paginas_limitadas=True, paginas=40)
        for i in range(90)
    ]
    hashes = {r["arquivo"]: f"{i:064x}" for i, r in enumerate(registros)}
    indice = [
        ItemAmostra(r["arquivo"], hashes[r["arquivo"]], estrato(r), r["status"], 1)
        for r in registros[:10]
    ]

    dados = avaliar(
        registros=registros,
        hashes=hashes,
        indice=indice,
        rotulos={item.sha256: False for item in indice},
    )

    assert dados["lote"]["populacao_representada"] == 10
    assert dados["lote"]["fracao_representada"] == 0.1
    assert "As taxas cobrem 10.0% do lote" in montar_validacao(dados, execucao={})


def test_alerta_fica_fora_do_denominador_e_aparece_como_cobertura() -> None:
    registros = [
        _registro(arquivo="ok.pdf"),
        _registro(arquivo="quebrado.pdf", status=STATUS_ERRO),
        _registro(arquivo="planilha.xlsx", status=STATUS_NAO_SUPORTADO),
    ]
    hashes = {r["arquivo"]: f"{i:064x}" for i, r in enumerate(registros)}
    indice = [ItemAmostra("ok.pdf", hashes["ok.pdf"], "neg_pdf_1pag", STATUS_SEM_ASSINATURA, 1)]

    dados = avaliar(
        registros=registros, hashes=hashes, indice=indice, rotulos={hashes["ok.pdf"]: False}
    )

    assert dados["lote"]["documentos_avaliaveis"] == 1
    assert dados["lote"]["documentos"] == 3


# ---------- relatório ---------------------------------------------------------


def test_relatorio_sem_rotulo_ensina_o_proximo_passo() -> None:
    registros = [_registro(arquivo="a.pdf")]
    hashes = {"a.pdf": "a" * 64}
    indice = [ItemAmostra("a.pdf", "a" * 64, "neg_pdf_1pag", STATUS_SEM_ASSINATURA, 1)]
    dados = avaliar(registros=registros, hashes=hashes, indice=indice, rotulos={})

    texto = montar_validacao(dados, execucao={"semente": 42})

    assert "Nenhum rótulo ainda" in texto
    assert "pendentes/" in texto


def test_relatorio_traz_vpn_matriz_e_onde_o_erro_esta() -> None:
    registros = [_registro(arquivo=f"neg/{i}.pdf") for i in range(50)] + [
        _registro(arquivo=f"pos/{i}.pdf", status=STATUS_ASSINADO, nivel0_total=1)
        for i in range(50)
    ]
    hashes = {r["arquivo"]: f"{i:064x}" for i, r in enumerate(registros)}
    indice = [
        ItemAmostra(r["arquivo"], hashes[r["arquivo"]], estrato(r), r["status"], 1)
        for r in registros
    ]
    rotulos: dict[str, bool | None] = {
        item.sha256: item.veredito_tool for item in indice
    }
    dados = avaliar(registros=registros, hashes=hashes, indice=indice, rotulos=rotulos)

    texto = montar_validacao(dados, execucao={"semente": 42})

    assert "VPN — quando disse ❌, não tinha mesmo | **100.0%**" in texto
    # "Estimativa" confunde quem leu "rotulei tudo": o relatório tem de dizer que
    # a projeção é da amostra para o lote, e mostrar o medido cru ao lado.
    assert "Medido na amostra, sem projeção: **100 acertos em 100** (100.0%)" in texto
    assert "não os 100 do lote" in texto
    assert "## Matriz de confusão" in texto
    assert "## Onde o erro está" in texto
    assert "## Cobertura" in texto


def test_relatorio_avisa_quando_ha_duvida_demais() -> None:
    registros = [_registro(arquivo=f"neg/{i}.pdf") for i in range(10)]
    hashes = {r["arquivo"]: f"{i:064x}" for i, r in enumerate(registros)}
    indice = [
        ItemAmostra(r["arquivo"], hashes[r["arquivo"]], "neg_pdf_1pag", STATUS_SEM_ASSINATURA, 1)
        for r in registros
    ]
    rotulos: dict[str, bool | None] = {item.sha256: False for item in indice[:5]}
    rotulos.update({item.sha256: None for item in indice[5:]})

    texto = montar_validacao(
        avaliar(registros=registros, hashes=hashes, indice=indice, rotulos=rotulos),
        execucao={"semente": 42},
    )

    assert "ficou em **dúvida**" in texto


def test_gerar_escreve_relatorio_json_e_rotulos(tmp_path: Path) -> None:
    registros = [_registro(arquivo="a.pdf"), _registro(arquivo="b.pdf")]
    hashes = {"a.pdf": "a" * 64, "b.pdf": "b" * 64}
    saida = tmp_path / "auditoria"
    escrever_indice(
        saida / "indice.jsonl",
        [
            ItemAmostra("a.pdf", "a" * 64, "neg_pdf_1pag", STATUS_SEM_ASSINATURA, 1),
            ItemAmostra("b.pdf", "b" * 64, "neg_pdf_1pag", STATUS_SEM_ASSINATURA, 1),
        ],
    )
    pasta = saida / "amostra"
    (pasta / "sem_assinatura").mkdir(parents=True)
    (pasta / "sem_assinatura" / f"{'a' * 12}.jpg").write_bytes(b"x")

    caminhos = gerar(
        registros=registros, hashes=hashes, pasta_saida=saida, execucao={"semente": 42}
    )

    assert caminhos["validacao_md"].is_file()
    dados = json.loads(caminhos["validacao_json"].read_text(encoding="utf-8"))
    assert dados["validacao"]["amostra"]["rotulados"] == 1
    assert carregar_rotulos(caminhos["rotulos"]) == {"a" * 64: False}


def test_rotulo_persistido_sobrevive_a_perda_da_imagem(tmp_path: Path) -> None:
    """Apagar `auditoria/amostra/` não pode apagar o trabalho humano: o
    `rotulos.jsonl` é a memória durável."""
    registros = [_registro(arquivo="a.pdf")]
    hashes = {"a.pdf": "a" * 64}
    saida = tmp_path / "auditoria"
    escrever_indice(
        saida / "indice.jsonl",
        [ItemAmostra("a.pdf", "a" * 64, "neg_pdf_1pag", STATUS_SEM_ASSINATURA, 1)],
    )
    salvar_rotulos(saida / "rotulos.jsonl", {"a" * 64: False})

    caminhos = gerar(
        registros=registros, hashes=hashes, pasta_saida=saida, execucao={"semente": 42}
    )
    dados = json.loads(caminhos["validacao_json"].read_text(encoding="utf-8"))

    assert dados["validacao"]["amostra"]["rotulados"] == 1
