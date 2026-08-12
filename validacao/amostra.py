"""Sorteio da amostra a auditar e as imagens que o humano vai rotular.

Três etapas, todas derivadas do `resultados.jsonl` — nenhuma reprocessa
documento:

1. **sha256 de cada documento**, em cache incremental. É o que permite rotular
   uma vez e valer para todas as cópias. O proxy barato `(tamanho, formato,
   páginas)` foi medido e **funde arquivos diferentes** (2.891 grupos contra
   3.423 hashes reais no lote), o que propagaria rótulo errado — daí o hash de
   verdade.
2. **Estratos**, tirados de campos que já estão no checkpoint. Servem para
   concentrar o esforço humano onde o erro provavelmente está (o fallback 3×3,
   que dispara 9× por página; o documento truncado em 30 páginas, que o Nível 1
   nem olhou) sem enviesar o total — a reponderação em `metricas.py` desfaz o
   oversampling.
3. **Uma folha de contato por conteúdo distinto**, sem nenhuma marca do que a
   tool decidiu. Rotulador que vê o veredito não mede nada.

A rotulagem em si não tem código: o humano move a imagem de `pendentes/` para
`com_assinatura/`, `sem_assinatura/` ou `duvida/`. **A pasta é o rótulo** — o
Explorer/Finder já é um visualizador com setas e miniaturas, e o estado fica no
sistema de arquivos, retomável por construção.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from math import ceil
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from processamento import documentos
from processamento.lote import STATUS_ASSINADO, STATUS_SEM_ASSINATURA

if TYPE_CHECKING:
    from PIL.Image import Image

# Prefixo do sha256 que nomeia a imagem. 12 hex = 48 bits: com as ~30 mil
# imagens que o maior lote geraria, a chance de colisão é ~1e-6 — e
# `escrever_indice` levanta se ela acontecer, em vez de trocar dois rótulos.
TAMANHO_ID = 12

# Piso de amostra por estrato. Sem ele, estrato pequeno e perigoso (os 48
# documentos truncados em 30 páginas, os 101 `.docx`) receberia 1 ou 2
# documentos e não diria nada sobre si mesmo.
MINIMO_POR_ESTRATO = 20

# A folha de contato mostra no máximo estas páginas; o rodapé avisa quando
# sobrou documento. Cobre 80% do lote real inteiro (1 ou 2 páginas) e mantém a
# imagem num tamanho que o visualizador do SO abre sem engasgar.
MAX_PAGINAS_FOLHA = 6
DPI_FOLHA = 150
# Largura máxima por página, conforme quantas colunas a grade vai ter. Rubrica
# de traço fino tem de sobreviver à redução: abaixo de ~900 px numa A4 ela
# começa a desaparecer, e o rotulador passaria a errar por resolução.
LARGURA_POR_COLUNA: dict[int, int] = {1: 1400, 2: 1100, 3: 900}
MARGEM_FOLHA = 10
ALTURA_RODAPE = 26
QUALIDADE_JPEG = 80

PASTA_PENDENTES = "pendentes"
# Pasta de destino → rótulo. `None` é "não consigo decidir": sai do denominador
# das métricas e é reportado à parte (dúvida demais = métrica não confiável).
PASTAS_ROTULO: dict[str, bool | None] = {
    "com_assinatura": True,
    "sem_assinatura": False,
    "duvida": None,
}

NOME_INDICE = "indice.jsonl"
NOME_HASHES = "hashes.jsonl"
NOME_ROTULOS = "rotulos.jsonl"
NOME_AMOSTRA = "amostra"

# Estratos, na ordem em que saem no relatório. A descrição é o que justifica o
# estrato existir — é ela que o `VALIDACAO.md` mostra ao lado da taxa de erro.
DESCRICAO_ESTRATO: dict[str, str] = {
    "neg_paginas_limitadas": "❌ truncado em `--max-paginas` — o Nível 1 não olhou o resto",
    "neg_sem_pagina_analisada": (
        "❌ nenhuma página chegou ao Nível 1 — a ausência não foi verificada "
        "(`.docx` sem imagem raster, folha em branco)"
    ),
    "neg_ooxml": "❌ `.docx`/`.pptx` com imagem — assinatura vetorial (`w:ink`) não chega ao Nível 1",
    "neg_imagem": "❌ imagem solta (só o Nível 1 tem o que fazer)",
    "neg_pdf_1pag": "❌ PDF de 1 página",
    "neg_pdf_2pag": "❌ PDF de 2 páginas",
    "neg_pdf_3pag_ou_mais": "❌ PDF de 3 páginas ou mais",
    "pos_resgatado_fallback": "✅ resgatado pelo fallback 3×3 — 9 inferências por página",
    "pos_nivel1_uma_deteccao": "✅ só o Nível 1, uma única detecção — evidência mais fraca",
    "pos_nivel1_varias": "✅ só o Nível 1, duas detecções ou mais",
    "pos_nivel0": "✅ com sinal do Nível 0 (`/Sig` ou carimbo digital)",
}


def estrato(registro: dict[str, Any]) -> str:
    """Estrato de um registro do checkpoint, ou `""` para o que não se avalia.

    Pura, e só lê campos que o `resultados.jsonl` já tem — estratificar não
    custa uma inferência. A ordem dos testes é a ordem do risco: o que tem um
    motivo conhecido para errar sai antes do que é só volume.
    """
    status = registro.get("status")

    if status == STATUS_SEM_ASSINATURA:
        if registro.get("paginas_limitadas"):
            return "neg_paginas_limitadas"
        # Zero página analisada é o caso mais grave de todos: a tool afirmou
        # ausência sem ter olhado nada. No lote real são 98 `.docx` cuja mídia não
        # rendeu imagem raster (a página em branco de fallback do adaptador OOXML)
        # e 1 PDF de folha branca — evidências muito diferentes de um PDF com
        # tinta em que o detector rodou e não achou.
        if not int(registro.get("paginas_analisadas_nivel1") or 0):
            return "neg_sem_pagina_analisada"
        tipo = registro.get("tipo")
        if tipo == "ooxml":
            return "neg_ooxml"
        if tipo == "imagem":
            return "neg_imagem"
        paginas = int(registro.get("paginas") or 0)
        if paginas <= 1:
            return "neg_pdf_1pag"
        if paginas == 2:
            return "neg_pdf_2pag"
        return "neg_pdf_3pag_ou_mais"

    if status == STATUS_ASSINADO:
        if int(registro.get("nivel0_total") or 0):
            return "pos_nivel0"
        if int(registro.get("paginas_ladrilhadas") or 0):
            return "pos_resgatado_fallback"
        if int(registro.get("nivel1_total") or 0) == 1:
            return "pos_nivel1_uma_deteccao"
        return "pos_nivel1_varias"

    return ""


@dataclass(frozen=True)
class ItemAmostra:
    """Um documento sorteado. O rótulo mora no hash; a contagem, no documento."""

    arquivo: str
    sha256: str
    estrato: str
    status: str
    paginas: int

    @property
    def id(self) -> str:
        return self.sha256[:TAMANHO_ID]

    @property
    def veredito_tool(self) -> bool:
        """O que a tool afirmou: `True` = ✅ assinatura encontrada."""
        return self.status == STATUS_ASSINADO


# ---------- sha256 em cache ----------------------------------------------------


def sha256_arquivo(caminho: Path) -> str | None:
    """sha256 do arquivo, em blocos de 1 MB. `None` quando ilegível."""
    digest = hashlib.sha256()
    try:
        with caminho.open("rb") as arquivo:
            for bloco in iter(lambda: arquivo.read(1 << 20), b""):
                digest.update(bloco)
    except OSError:
        return None
    return digest.hexdigest()


def carregar_hashes(cache: Path) -> dict[str, str]:
    """Mapa `arquivo relativo → sha256` já calculado (linha ruim é ignorada)."""
    if not cache.is_file():
        return {}
    hashes: dict[str, str] = {}
    with cache.open(encoding="utf-8") as arquivo:
        for linha in arquivo:
            linha = linha.strip()
            if not linha:
                continue
            try:
                registro = json.loads(linha)
                hashes[registro["arquivo"]] = registro["sha256"]
            except (json.JSONDecodeError, KeyError):
                continue
    return hashes


def atualizar_hashes(
    *,
    raiz: Path,
    relativos: Iterable[str],
    cache: Path,
    ao_progredir: Callable[[int, int], None] | None = None,
) -> dict[str, str]:
    """Completa o cache com o que falta e devolve o mapa inteiro.

    Append com `flush` a cada arquivo, como o checkpoint do lote: hashear 50 GB
    leva minutos e uma interrupção não pode custar o trabalho todo.
    """
    hashes = carregar_hashes(cache)
    pendentes = [relativo for relativo in relativos if relativo not in hashes]
    if not pendentes:
        return hashes

    cache.parent.mkdir(parents=True, exist_ok=True)
    with cache.open("a", encoding="utf-8") as arquivo:
        for feitos, relativo in enumerate(pendentes, start=1):
            digest = sha256_arquivo(raiz / relativo)
            if digest is not None:
                hashes[relativo] = digest
                arquivo.write(
                    json.dumps({"arquivo": relativo, "sha256": digest}, ensure_ascii=False) + "\n"
                )
                arquivo.flush()
            if ao_progredir is not None:
                ao_progredir(feitos, len(pendentes))
    return hashes


# ---------- sorteio -----------------------------------------------------------


def alocar(estratos: dict[str, list[dict[str, Any]]], total: int, minimo: int) -> dict[str, int]:
    """Quantos documentos sortear em cada estrato.

    Proporcional ao tamanho do estrato, com piso de `minimo` — o piso é o que
    dá voz ao estrato pequeno e perigoso. Ele faz o total estourar um pouco o
    orçamento pedido, de propósito: cortar de volta custaria mais código do que
    o overshoot custa de tempo humano, e a reponderação corrige o viés.
    """
    documentos_totais = sum(len(itens) for itens in estratos.values())
    if not documentos_totais:
        return {}
    return {
        nome: min(len(itens), max(minimo, round(total * len(itens) / documentos_totais)))
        for nome, itens in sorted(estratos.items())
    }


def sortear(
    registros: list[dict[str, Any]],
    hashes: dict[str, str],
    *,
    n_negativos: int,
    n_positivos: int,
    semente: int = 42,
    minimo_por_estrato: int = MINIMO_POR_ESTRATO,
) -> list[ItemAmostra]:
    """Amostra estratificada de documentos, reprodutível pela semente.

    Sorteia **documentos** (é o que se quer estimar), não conteúdos: conteúdo
    repetido 96 vezes tem 96 vezes mais chance de entrar, que é exatamente o
    peso dele no lote. A economia da duplicação aparece depois, na hora de
    renderizar — um arquivo por hash.

    Embaralha e corta em vez de `sample`: aumentar o orçamento depois produz uma
    amostra que **contém** a anterior, então nenhum rótulo já feito se perde.
    A semente é derivada do nome do estrato para que mexer num estrato não
    remexa o sorteio dos outros.
    """
    por_estrato: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for registro in registros:
        nome = estrato(registro)
        if nome and hashes.get(registro.get("arquivo", "")):
            por_estrato[nome].append(registro)

    amostra: list[ItemAmostra] = []
    for prefixo, orcamento in (("neg_", n_negativos), ("pos_", n_positivos)):
        lado = {nome: itens for nome, itens in por_estrato.items() if nome.startswith(prefixo)}
        for nome, quantidade in alocar(lado, orcamento, minimo_por_estrato).items():
            ordem = sorted(lado[nome], key=lambda registro: str(registro["arquivo"]))
            random.Random(f"{semente}:{nome}").shuffle(ordem)
            amostra += [
                ItemAmostra(
                    arquivo=registro["arquivo"],
                    sha256=hashes[registro["arquivo"]],
                    estrato=nome,
                    status=registro["status"],
                    paginas=int(registro.get("paginas") or 0),
                )
                for registro in ordem[:quantidade]
            ]
    return sorted(amostra, key=lambda item: (item.estrato, item.arquivo))


# ---------- índice da amostra -------------------------------------------------


def escrever_indice(caminho: Path, amostra: list[ItemAmostra]) -> None:
    """Grava o índice da amostra e recusa colisão de `id`.

    Dois hashes com o mesmo prefixo de 12 hex trocariam os rótulos entre dois
    documentos diferentes. É praticamente impossível, e é exatamente por isso
    que tem de explodir alto se acontecer, em vez de virar um erro silencioso na
    métrica.
    """
    por_id: dict[str, str] = {}
    for item in amostra:
        existente = por_id.setdefault(item.id, item.sha256)
        if existente != item.sha256:
            raise RuntimeError(
                f"colisão de id `{item.id}` entre {existente} e {item.sha256} — "
                f"aumente TAMANHO_ID em validacao/amostra.py"
            )

    caminho.parent.mkdir(parents=True, exist_ok=True)
    with caminho.open("w", encoding="utf-8") as arquivo:
        for item in amostra:
            arquivo.write(
                json.dumps(
                    {
                        "id": item.id,
                        "sha256": item.sha256,
                        "arquivo": item.arquivo,
                        "estrato": item.estrato,
                        "status": item.status,
                        "paginas": item.paginas,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )


def ler_indice(caminho: Path) -> list[ItemAmostra]:
    """A amostra gravada. Linha corrompida é ignorada, como no checkpoint."""
    if not caminho.is_file():
        return []
    itens: list[ItemAmostra] = []
    with caminho.open(encoding="utf-8") as arquivo:
        for linha in arquivo:
            linha = linha.strip()
            if not linha:
                continue
            try:
                registro = json.loads(linha)
                itens.append(
                    ItemAmostra(
                        arquivo=registro["arquivo"],
                        sha256=registro["sha256"],
                        estrato=registro["estrato"],
                        status=registro["status"],
                        paginas=int(registro.get("paginas") or 0),
                    )
                )
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
    return itens


# ---------- folha de contato --------------------------------------------------


def _colunas(quantidade: int) -> int:
    if quantidade <= 1:
        return 1
    if quantidade <= 4:
        return 2
    return 3


def _pagina_para_imagem(pagina: Any, *, dpi: int, largura_maxima: int) -> Image:
    """Uma página rasterizada, reduzida só se passar da largura alvo.

    Nunca amplia: esticar pixel não devolve traço que o raster não tem, e o
    arquivo triplicaria de tamanho por nada.
    """
    from PIL import Image as _Image

    pix = pagina.get_pixmap(dpi=dpi)
    imagem = _Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    if imagem.width > largura_maxima:
        altura = max(1, round(imagem.height * largura_maxima / imagem.width))
        imagem = imagem.resize((largura_maxima, altura), _Image.Resampling.LANCZOS)
    return imagem


def _montar_grade(miniaturas: list[Image], *, rodape: str) -> Image:
    """Grade branca com as páginas e um rodapé de texto (nunca o veredito)."""
    from PIL import Image as _Image
    from PIL import ImageDraw

    colunas = _colunas(len(miniaturas))
    linhas = ceil(len(miniaturas) / colunas)
    largura_celula = max(imagem.width for imagem in miniaturas)
    altura_celula = max(imagem.height for imagem in miniaturas)

    folha = _Image.new(
        "RGB",
        (
            colunas * largura_celula + (colunas + 1) * MARGEM_FOLHA,
            linhas * altura_celula + (linhas + 1) * MARGEM_FOLHA + ALTURA_RODAPE,
        ),
        "white",
    )
    for indice, imagem in enumerate(miniaturas):
        coluna, linha = indice % colunas, indice // colunas
        folha.paste(
            imagem,
            (
                MARGEM_FOLHA + coluna * (largura_celula + MARGEM_FOLHA),
                MARGEM_FOLHA + linha * (altura_celula + MARGEM_FOLHA),
            ),
        )
    ImageDraw.Draw(folha).text(
        (MARGEM_FOLHA, folha.height - ALTURA_RODAPE + 6), rodape, fill="black"
    )
    return folha


def renderizar_folha(
    origem: Path,
    destino: Path,
    *,
    max_paginas: int = MAX_PAGINAS_FOLHA,
    dpi: int = DPI_FOLHA,
) -> int:
    """Documento → uma JPEG em grade, para o rotulador julgar de relance.

    Devolve o total de páginas do documento, que pode ser maior que o mostrado —
    o rodapé da imagem diz quantas ficaram de fora, senão o rotulador decidiria
    "não tem assinatura" sobre um documento que ele não viu inteiro.

    Aceita qualquer formato que o lote aceita: passa pelo mesmo
    `documentos.preparar`, então DOCX e imagem exótica chegam aqui já como
    documento paginado. Levanta `DocumentoInvalidoError` /
    `FormatoNaoSuportadoError` como o lote.
    """
    import pymupdf as _pymupdf

    pymupdf = cast(Any, _pymupdf)
    destino.parent.mkdir(parents=True, exist_ok=True)

    # Tudo dentro do `with`: para OOXML e imagem convertida, `pronto.path` é
    # temporário e desaparece na saída do contexto.
    with documentos.preparar(origem) as pronto:
        doc = pymupdf.open(str(pronto.path))
        try:
            total = doc.page_count
            if not total:
                raise documentos.DocumentoInvalidoError("documento sem páginas")
            mostradas = min(total, max_paginas)
            largura = LARGURA_POR_COLUNA[_colunas(mostradas)]
            miniaturas = [
                _pagina_para_imagem(doc[indice], dpi=dpi, largura_maxima=largura)
                for indice in range(mostradas)
            ]
        finally:
            doc.close()

        rodape = (
            f"{mostradas} de {total} paginas" if total > mostradas else f"{total} pagina(s)"
        )
        folha = _montar_grade(miniaturas, rodape=rodape)
        folha.save(destino, format="JPEG", quality=QUALIDADE_JPEG, optimize=True)
    return total


# ---------- preparação da pasta de rotulagem ----------------------------------


def pastas_de_rotulagem(pasta_amostra: Path) -> dict[str, Path]:
    """Cria (se preciso) e devolve `pendentes/` + uma pasta por rótulo."""
    nomes = [PASTA_PENDENTES, *PASTAS_ROTULO]
    pastas = {nome: pasta_amostra / nome for nome in nomes}
    for caminho in pastas.values():
        caminho.mkdir(parents=True, exist_ok=True)
    return pastas


def localizar_imagem(pasta_amostra: Path, identificador: str) -> Path | None:
    """Onde a imagem deste conteúdo está — inclusive se já foi rotulada."""
    for nome in (PASTA_PENDENTES, *PASTAS_ROTULO):
        caminho = pasta_amostra / nome / f"{identificador}.jpg"
        if caminho.is_file():
            return caminho
    return None


@dataclass(frozen=True)
class ResultadoPreparo:
    """O que saiu do preparo da amostra."""

    conteudos: int
    geradas: int
    reaproveitadas: int
    falhas: list[tuple[str, str]]


def preparar_amostra(
    *,
    raiz_files: Path,
    amostra: list[ItemAmostra],
    pasta_saida: Path,
    max_paginas: int = MAX_PAGINAS_FOLHA,
    dpi: int = DPI_FOLHA,
    ao_progredir: Callable[[int, int, str], None] | None = None,
) -> ResultadoPreparo:
    """Grava o índice e renderiza uma imagem por conteúdo distinto.

    Idempotente: conteúdo cuja imagem já existe em qualquer das pastas —
    inclusive já rotulado — não é renderizado de novo. Rodar de novo com
    orçamento maior só acrescenta.

    Positivos e negativos caem juntos em `pendentes/`, com nome derivado do hash:
    o rotulador não tem como saber o que a tool decidiu, que é o que mantém o
    rótulo independente.
    """
    pasta_amostra = pasta_saida / NOME_AMOSTRA
    pastas = pastas_de_rotulagem(pasta_amostra)
    escrever_indice(pasta_saida / NOME_INDICE, amostra)

    por_conteudo = {item.sha256: item for item in amostra}
    geradas = reaproveitadas = 0
    falhas: list[tuple[str, str]] = []

    for feitos, item in enumerate(sorted(por_conteudo.values(), key=lambda i: i.id), start=1):
        if ao_progredir is not None:
            ao_progredir(feitos, len(por_conteudo), item.arquivo)
        if localizar_imagem(pasta_amostra, item.id) is not None:
            reaproveitadas += 1
            continue
        try:
            renderizar_folha(
                raiz_files / item.arquivo,
                pastas[PASTA_PENDENTES] / f"{item.id}.jpg",
                max_paginas=max_paginas,
                dpi=dpi,
            )
        except Exception as exc:
            falhas.append((item.arquivo, f"{type(exc).__name__}: {exc}"))
            continue
        geradas += 1

    return ResultadoPreparo(
        conteudos=len(por_conteudo),
        geradas=geradas,
        reaproveitadas=reaproveitadas,
        falhas=falhas,
    )
