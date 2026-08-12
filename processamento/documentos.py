"""Descoberta dos arquivos em `files/` e adaptação por formato.

A tool de detecção fala uma língua só: um documento que o PyMuPDF abra e
pagine. Este módulo traduz cada formato de entrada para essa língua, sem tocar
na tool:

| Formato | Tradução |
|---|---|
| `.pdf` | usado direto — é o caminho nativo dos dois níveis |
| imagem (`.jpg`, `.png`, `.tif`…) | usada direto (o PyMuPDF abre imagem como documento de 1 página); só cai para conversão via Pillow quando o MuPDF não conhece o formato (ex.: `.webp`) |
| OOXML (`.docx`, `.pptx`…) | o texto das partes XML vai para o Nível 0; as imagens de `word/media/` ou `ppt/media/` viram um PDF temporário de uma página cada, que é o que o Nível 1 varre |
| `.zip` | extraído numa subpasta ao lado, **antes** da varredura — o que sai da extração é processado como documento comum |

A adaptação do OOXML é onde mora a perda conhecida: assinatura desenhada como
vetor no Word (`w:ink`, EMF/WMF) não é imagem raster e não chega ao Nível 1.
Assinatura escaneada colada no documento — o caso real — chega.
"""

from __future__ import annotations

import html
import re
import shutil
import tempfile
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

EXTENSOES_PDF: frozenset[str] = frozenset({".pdf"})
EXTENSOES_IMAGEM: frozenset[str] = frozenset(
    {".jpg", ".jpeg", ".jfif", ".png", ".tif", ".tiff", ".bmp", ".gif", ".webp", ".pnm", ".pgm"}
)
EXTENSOES_ZIP: frozenset[str] = frozenset({".zip"})


@dataclass(frozen=True)
class FormatoOoxml:
    """Onde ficam o texto e as imagens dentro do zip de um pacote OOXML.

    `prefixo_texto` — prefixo das partes XML com texto. No Word é um arquivo só
        (`word/document.xml`); no PowerPoint é um por slide
        (`ppt/slides/slide1.xml`, `slide2.xml`…).
    """

    prefixo_texto: str
    prefixo_media: str


OOXML: dict[str, FormatoOoxml] = {
    ".docx": FormatoOoxml("word/document.xml", "word/media/"),
    ".docm": FormatoOoxml("word/document.xml", "word/media/"),
    ".pptx": FormatoOoxml("ppt/slides/slide", "ppt/media/"),
    ".pptm": FormatoOoxml("ppt/slides/slide", "ppt/media/"),
}
EXTENSOES_OOXML: frozenset[str] = frozenset(OOXML)
EXTENSOES_SUPORTADAS: frozenset[str] = EXTENSOES_PDF | EXTENSOES_IMAGEM | EXTENSOES_OOXML

# Guardas contra zip bomb: o pacote vem de terceiros e é lido antes de qualquer
# validação. Os limites são generosos para documento real e absurdos para bomba.
LIMITE_XML_BYTES = 64 * 1024 * 1024
LIMITE_MEDIA_BYTES = 256 * 1024 * 1024
LIMITE_MEDIA_ARQUIVOS = 200

# Mesma ideia para o `.zip` solto, que é um contêiner e não um documento: aqui o
# limite é o do lote inteiro de um arquivo, então é bem mais largo.
LIMITE_ZIP_BYTES = 8 * 1024**3
LIMITE_ZIP_ARQUIVOS = 20_000
# Zip dentro de zip acontece; zip dentro de zip dentro de zip já é sinal de
# problema. Três níveis cobrem o real sem virar recursão sem fim.
PROFUNDIDADE_MAXIMA_ZIP = 3

# Imagem menor que isso na mídia do pacote é ícone, marcador ou logo — nunca uma
# rubrica escaneada. Pular economiza uma inferência inteira por imagem.
LARGURA_MINIMA_MEDIA = 64
ALTURA_MINIMA_MEDIA = 32

_TAG = re.compile(r"<[^>]+>")
_QUEBRA_PARAGRAFO = re.compile(r"</[wa]:p>|<[wa]:br\s*/>|<w:cr\s*/>")
_TABULACAO = re.compile(r"<w:tab\s*/>")


class FormatoNaoSuportadoError(RuntimeError):
    """Extensão fora do escopo da plataforma (não é PDF, imagem nem DOCX)."""


class DocumentoInvalidoError(RuntimeError):
    """Arquivo do formato certo, mas ilegível (corrompido, vazio, protegido)."""


@dataclass(frozen=True)
class DocumentoPreparado:
    """O documento na forma que a tool entende.

    `path` — arquivo a passar para a detecção (o original ou um temporário).
    `texto` — texto já extraído fora do PDF (DOCX); `None` deixa o Nível 0 ler
        a camada de texto do próprio arquivo.
    `convertido` — `True` quando `path` é temporário e será apagado na saída
        do contexto.
    """

    path: Path
    texto: str | None
    convertido: bool


def classificar(caminho: Path) -> str:
    """`pdf` | `imagem` | `ooxml` | `zip` | `nao_suportado`, pela extensão."""
    sufixo = caminho.suffix.lower()
    if sufixo in EXTENSOES_PDF:
        return "pdf"
    if sufixo in EXTENSOES_IMAGEM:
        return "imagem"
    if sufixo in EXTENSOES_OOXML:
        return "ooxml"
    if sufixo in EXTENSOES_ZIP:
        return "zip"
    return "nao_suportado"


def listar_documentos(raiz: Path) -> list[Path]:
    """Todos os arquivos sob `raiz`, em qualquer profundidade, ordenados.

    Devolve **inclusive** os de formato não suportado: eles precisam aparecer no
    relatório como ⚠️, não sumir em silêncio. Ficam de fora apenas os artefatos
    que nunca são documento — ocultos, temporários do Word (`~$…`) e os
    `:Zone.Identifier` que o Windows anexa a download.
    """
    encontrados = [
        caminho
        for caminho in raiz.rglob("*")
        if caminho.is_file()
        and not caminho.name.startswith(("~$", "."))
        and ":Zone.Identifier" not in caminho.name
    ]
    return sorted(encontrados, key=lambda p: str(p).lower())


@dataclass(frozen=True)
class ZipExpandido:
    """Resultado da expansão de um `.zip` (uma linha do relatório)."""

    origem: Path
    destino: Path
    arquivos: int
    ja_existia: bool
    erro: str | None = None


def _extrair_zip(origem: Path, destino: Path) -> int:
    """Extrai `origem` em `destino`, devolvendo quantos arquivos saíram.

    Extrai primeiro para `<destino>.parcial` e só então renomeia: interrupção no
    meio não pode deixar uma pasta incompleta que, na retomada, passe por
    extração concluída.

    `ZipFile.extractall` já neutraliza caminho absoluto e `..` (zip slip); o que
    falta, e é o que se checa aqui, é o tamanho descomprimido.
    """
    with zipfile.ZipFile(origem) as pacote:
        membros = [info for info in pacote.infolist() if not info.is_dir()]
        if len(membros) > LIMITE_ZIP_ARQUIVOS:
            raise DocumentoInvalidoError(
                f"zip com {len(membros)} arquivos (limite {LIMITE_ZIP_ARQUIVOS})"
            )
        descomprimido = sum(info.file_size for info in membros)
        if descomprimido > LIMITE_ZIP_BYTES:
            raise DocumentoInvalidoError(
                f"zip com {descomprimido / 1024**3:.1f} GB descomprimidos "
                f"(limite {LIMITE_ZIP_BYTES / 1024**3:.0f} GB) — recusado como zip bomb"
            )

        parcial = destino.with_name(destino.name + ".parcial")
        if parcial.exists():
            shutil.rmtree(parcial, ignore_errors=True)
        parcial.mkdir(parents=True)
        pacote.extractall(parcial, members=membros)

    parcial.replace(destino)
    return len(membros)


def expandir_zips(
    raiz: Path, *, profundidade_maxima: int = PROFUNDIDADE_MAXIMA_ZIP
) -> list[ZipExpandido]:
    """Extrai todo `.zip` sob `raiz` numa subpasta ao lado, com o nome do arquivo.

    Roda antes da varredura, para que o que sair da extração seja descoberto e
    processado como qualquer outro documento. É idempotente: pasta de destino já
    existente é respeitada, então retomar o lote não re-extrai nada.

    O laço repete porque zip pode conter zip; para quando uma passada não extrai
    mais nada ou quando a profundidade se esgota.
    """
    resultados: list[ZipExpandido] = []
    vistos: set[Path] = set()

    for _ in range(profundidade_maxima):
        pendentes = [
            caminho
            for caminho in sorted(raiz.rglob("*"))
            if caminho.is_file()
            and caminho.suffix.lower() in EXTENSOES_ZIP
            and caminho not in vistos
            and not caminho.name.startswith(("~$", "."))
        ]
        if not pendentes:
            break

        for caminho in pendentes:
            vistos.add(caminho)
            destino = caminho.with_suffix("")
            if destino.is_dir():
                resultados.append(ZipExpandido(caminho, destino, 0, ja_existia=True))
                continue
            if destino.exists():
                destino = caminho.with_name(f"{caminho.stem}__zip")
                if destino.is_dir():
                    resultados.append(ZipExpandido(caminho, destino, 0, ja_existia=True))
                    continue
            try:
                arquivos = _extrair_zip(caminho, destino)
            except (DocumentoInvalidoError, zipfile.BadZipFile, OSError) as exc:
                resultados.append(
                    ZipExpandido(caminho, destino, 0, ja_existia=False, erro=str(exc))
                )
                continue
            resultados.append(ZipExpandido(caminho, destino, arquivos, ja_existia=False))

    return resultados


def _abre_no_pymupdf(caminho: Path) -> bool:
    """O MuPDF consegue abrir e paginar este arquivo?"""
    import pymupdf as _pymupdf

    pymupdf = cast(Any, _pymupdf)
    try:
        doc = pymupdf.open(str(caminho))
    except Exception:
        return False
    try:
        return doc.page_count > 0
    finally:
        doc.close()


def _converter_imagem(origem: Path, destino: Path) -> None:
    """Imagem que o MuPDF não abre → PDF de uma página, via Pillow.

    Cobre `.webp` e variações exóticas de TIFF. Só a primeira página/quadro é
    aproveitada — imagem multi-frame que o MuPDF não abre é rara o bastante
    para não valer um segundo caminho de código.
    """
    import pymupdf as _pymupdf
    from PIL import Image

    pymupdf = cast(Any, _pymupdf)
    try:
        with Image.open(origem) as imagem:
            rgb = imagem.convert("RGB")
            temporario = destino.with_suffix(".png")
            rgb.save(temporario, format="PNG")
    except Exception as exc:
        raise DocumentoInvalidoError(f"imagem ilegível: {exc}") from exc

    doc = pymupdf.open(str(temporario))
    try:
        destino.write_bytes(doc.convert_to_pdf())
    finally:
        doc.close()
        temporario.unlink(missing_ok=True)


def extrair_texto_ooxml(caminho: Path, formato: FormatoOoxml) -> str:
    """Texto das partes XML do pacote (todas as do prefixo), sem parser de XML.

    Regex em vez de `ElementTree`/`lxml` de propósito: o arquivo é de terceiro e
    o parser de XML abre superfície (entidades, aninhamento) que aqui não paga
    por si — o Nível 0 só precisa do texto corrido para casar os carimbos de
    assinatura digital. Como as tags são apenas removidas, o mesmo código serve
    para `<w:t>` do Word e `<a:t>` do PowerPoint.
    """
    try:
        with zipfile.ZipFile(caminho) as pacote:
            partes = sorted(
                (
                    info
                    for info in pacote.infolist()
                    if info.filename.startswith(formato.prefixo_texto)
                    and info.filename.endswith(".xml")
                ),
                key=lambda info: info.filename,
            )
            if not partes:
                raise DocumentoInvalidoError(
                    f"pacote OOXML sem `{formato.prefixo_texto}*.xml`"
                )
            total = sum(info.file_size for info in partes)
            if total > LIMITE_XML_BYTES:
                raise DocumentoInvalidoError(
                    f"XML descomprimido tem {total} bytes (limite {LIMITE_XML_BYTES}) "
                    "— recusado como zip bomb"
                )
            bruto = "\n".join(
                pacote.read(info.filename).decode("utf-8", errors="replace") for info in partes
            )
    except zipfile.BadZipFile as exc:
        raise DocumentoInvalidoError(f"pacote OOXML corrompido: {exc}") from exc

    texto = _QUEBRA_PARAGRAFO.sub("\n", bruto)
    texto = _TABULACAO.sub("\t", texto)
    return html.unescape(_TAG.sub("", texto))


def _pdf_das_imagens_ooxml(caminho: Path, destino: Path, formato: FormatoOoxml) -> None:
    """Imagens da mídia do pacote → um PDF temporário, uma página por imagem.

    É esse PDF que vai ao Nível 1: num DOCX ou PPTX, a assinatura escaneada é
    uma imagem embutida. Sem imagem aproveitável, gera uma página em branco — o
    Nível 0 a descarta na triagem e o Nível 1 não roda.
    """
    import pymupdf as _pymupdf

    pymupdf = cast(Any, _pymupdf)
    saida = pymupdf.open()
    try:
        with zipfile.ZipFile(caminho) as zip_docx:
            candidatas = [
                info
                for info in zip_docx.infolist()
                if info.filename.startswith(formato.prefixo_media)
                and Path(info.filename).suffix.lower() in EXTENSOES_IMAGEM
            ][:LIMITE_MEDIA_ARQUIVOS]

            acumulado = 0
            for info in candidatas:
                acumulado += info.file_size
                if acumulado > LIMITE_MEDIA_BYTES:
                    break
                dados = zip_docx.read(info.filename)
                # `pymupdf.open` sobre stream é preguiçoso: o formato só é
                # validado ao tocar na primeira página. Mídia corrompida dentro
                # de um DOCX bom é comum — pular a imagem, não perder o
                # documento.
                imagem = None
                try:
                    imagem = pymupdf.open(
                        stream=dados, filetype=Path(info.filename).suffix.lstrip(".")
                    )
                    caixa = imagem[0].rect
                    if caixa.width < LARGURA_MINIMA_MEDIA or caixa.height < ALTURA_MINIMA_MEDIA:
                        continue
                    pagina = pymupdf.open("pdf", imagem.convert_to_pdf())
                    try:
                        saida.insert_pdf(pagina)
                    finally:
                        pagina.close()
                except Exception:
                    continue
                finally:
                    if imagem is not None:
                        imagem.close()

        if saida.page_count == 0:
            saida.new_page()
        saida.save(str(destino))
    finally:
        saida.close()


@contextmanager
def preparar(origem: Path) -> Iterator[DocumentoPreparado]:
    """Entrega o documento pronto para a detecção e limpa o temporário no fim.

    Levanta `FormatoNaoSuportadoError` para extensão fora do escopo e
    `DocumentoInvalidoError` para arquivo ilegível — o lote distingue os dois no
    relatório e só o segundo justifica retry.
    """
    tipo = classificar(origem)
    if tipo in ("nao_suportado", "zip"):
        raise FormatoNaoSuportadoError(f"formato `{origem.suffix.lower() or 'sem extensão'}`")
    if origem.stat().st_size == 0:
        raise DocumentoInvalidoError("arquivo vazio (0 bytes)")

    if tipo == "pdf":
        if not _abre_no_pymupdf(origem):
            raise DocumentoInvalidoError("PDF ilegível ou protegido por senha")
        yield DocumentoPreparado(path=origem, texto=None, convertido=False)
        return

    if tipo == "imagem" and _abre_no_pymupdf(origem):
        yield DocumentoPreparado(path=origem, texto=None, convertido=False)
        return

    with tempfile.TemporaryDirectory(prefix="poc-assinatura-") as pasta:
        destino = Path(pasta) / "documento.pdf"
        if tipo == "imagem":
            _converter_imagem(origem, destino)
            texto = None
        else:
            formato = OOXML[origem.suffix.lower()]
            texto = extrair_texto_ooxml(origem, formato)
            _pdf_das_imagens_ooxml(origem, destino, formato)
        yield DocumentoPreparado(path=destino, texto=texto, convertido=True)
