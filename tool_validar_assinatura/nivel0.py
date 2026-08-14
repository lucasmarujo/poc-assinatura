"""Nível 0 — sinais de assinatura de custo zero, em Python puro.

Roda sempre, antes de qualquer inferência. São quatro checagens locais (só
PyMuPDF + numpy, nenhuma chamada de rede/serviço pago):

  1. **Campos `/Sig` no AcroForm** — assinatura criptográfica viva no PDF
     (PAdES/ICP-Brasil). Sinal mais forte quando presente. Reaproveita
     `motor_ia.extractors.signatures.find_embedded_pdf_signatures` e descarta o
     que ela não tem como distinguir: campo de assinatura **em branco**, que é
     widget sem `/V` — o "assine aqui" de um formulário, não uma assinatura.
  2. **Camada de texto — carimbo digital** — "Assinado digitalmente por…",
     "ICP-Brasil", "DN: C=" etc. Reaproveita
     `motor_ia.extractors.signatures.find_digital_signatures`, que já cobre o
     caso brasileiro do PDF assinado e achatado como texto.
  3. **Camada de texto — rótulo de assinatura** — "Assinatura do empregado",
     "Visto do responsável"… Indica que o documento *tem campo* de assinatura,
     não que está assinado. Não entra no veredito; serve para priorizar as
     páginas candidatas do Nível 1 e para auditoria.
  4. **Densidade de tinta / página em branco** — fração de pixels escuros da
     página rasterizada em DPI baixo. Página abaixo do limiar é considerada em
     branco e **não** é enviada ao Nível 1 (é onde mora a economia: verso de
     digitalização, folha separadora, página de fundo branco).

O Nível 0 resolve sozinho os PDFs nativos assinados digitalmente. Documento
escaneado (rubrica de caneta, sem camada de texto) só é resolvido pelo Nível 1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from motor_ia.extractors.signatures import (
    find_digital_signatures,
    find_embedded_pdf_signatures,
)

# Rótulos de campo de assinatura (indício de que se espera assinatura ali).
_ROTULO_ASSINATURA_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"assinatura\s+d[oae]s?\s+\w+",
        r"assinatura[:\s]*_{3,}",
        r"_{5,}\s*\n?\s*assinatura",
        r"\bvisto\s+d[oae]\b",
        r"ciente\s+e\s+de\s+acordo",
        r"recebi\s+(?:o|a|as|os)\s+\w+",
    )
)

# Campo de assinatura no AcroForm (PyMuPDF: PDF_WIDGET_TYPE_SIGNATURE == 6).
# Repetido de `motor_ia.extractors.signatures`, onde é privado.
TIPO_WIDGET_ASSINATURA = 6

# Triagem de tinta: rasteriza em DPI baixo (rápido) e conta pixels escuros.
# 72 DPI numa A4 dá ~0,5 MP — suficiente para distinguir folha em branco de
# folha com conteúdo, e ~10x mais barato que o render de 150 DPI do Nível 1.
DPI_TRIAGEM = 72
# Fundo de digitalização raramente passa de ~230; 200 separa fundo de traço.
LIMIAR_PIXEL_ESCURO = 200
# Fração mínima de pixels escuros para a página ser considerada preenchida.
# 0,05% de uma A4 a 72 DPI ≈ 250 pixels — abaixo disso é sujeira de scanner.
DENSIDADE_TINTA_MINIMA = 0.0005


@dataclass(frozen=True)
class PaginaNivel0:
    """Triagem de uma página: quanta tinta tem e se é aproveitável."""

    numero: int
    densidade_tinta: float
    em_branco: bool
    tem_rotulo_assinatura: bool = False


@dataclass(frozen=True)
class ResultadoNivel0:
    """Sinais do Nível 0 num documento.

    `assinaturas` — soma de `digital` + `embedded`; é o que o nível "conta".
    `paginas_uteis` — páginas não-brancas, entrada do Nível 1.
    """

    digital: list[dict[str, Any]] = field(default_factory=list)
    embedded: list[dict[str, Any]] = field(default_factory=list)
    paginas: list[PaginaNivel0] = field(default_factory=list)
    texto_extraido: str = ""

    @property
    def assinaturas(self) -> list[dict[str, Any]]:
        return [*self.digital, *self.embedded]

    @property
    def total(self) -> int:
        return len(self.digital) + len(self.embedded)

    @property
    def fontes(self) -> list[str]:
        fontes: list[str] = []
        if self.digital:
            fontes.append("digital")
        if self.embedded:
            fontes.append("pdf_embedded")
        return fontes

    @property
    def tem_assinatura(self) -> bool:
        return bool(self.fontes)

    @property
    def paginas_uteis(self) -> list[int]:
        return [p.numero for p in self.paginas if not p.em_branco]

    @property
    def paginas_em_branco(self) -> list[int]:
        return [p.numero for p in self.paginas if p.em_branco]


def encontrar_rotulos_assinatura(texto: str) -> bool:
    """True quando o texto traz rótulo de campo de assinatura (pura, sem I/O)."""
    if not texto:
        return False
    return any(p.search(texto) for p in _ROTULO_ASSINATURA_PATTERNS)


def densidade_tinta(amostras: bytes, *, limiar_pixel: int = LIMIAR_PIXEL_ESCURO) -> float:
    """Fração de pixels escuros num buffer grayscale de 8 bits (pura).

    Buffer vazio → 0.0 (página sem conteúdo rasterizável).
    """
    import numpy as np

    if not amostras:
        return 0.0
    dados = np.frombuffer(amostras, dtype=np.uint8)
    if dados.size == 0:
        return 0.0
    return float((dados < limiar_pixel).mean())


def triar_paginas(
    path: str | Path,
    *,
    dpi: int = DPI_TRIAGEM,
    densidade_minima: float = DENSIDADE_TINTA_MINIMA,
) -> list[PaginaNivel0]:
    """Densidade de tinta por página + marcação de página em branco.

    O rótulo de assinatura é avaliado por página (não no texto do documento
    inteiro) para que o Nível 1 possa priorizar páginas candidatas.
    """
    import pymupdf as _pymupdf

    pymupdf = cast(Any, _pymupdf)
    doc = pymupdf.open(str(path))
    try:
        paginas: list[PaginaNivel0] = []
        for indice in range(doc.page_count):
            pagina = doc[indice]
            pix = pagina.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
            tinta = densidade_tinta(pix.samples)
            paginas.append(
                PaginaNivel0(
                    numero=indice + 1,
                    densidade_tinta=tinta,
                    em_branco=tinta < densidade_minima,
                    tem_rotulo_assinatura=encontrar_rotulos_assinatura(pagina.get_text("text")),
                )
            )
        return paginas
    finally:
        doc.close()


def campos_assinados(path: str | Path) -> set[tuple[int, str | None]]:
    """`(página, nome do campo)` dos campos `/Sig` com assinatura **viva**.

    Assinatura viva é `/V` apontando para um dicionário com `/ByteRange` (o
    trecho do arquivo que foi assinado) e `/Contents` (o PKCS#7). Campo sem
    `/V` é o "assine aqui" de um formulário: existe o lugar, não existe a
    assinatura.

    O nome do signatário **não** entra no critério: `/V /Name` é opcional e vem
    vazio na maioria dos PDFs do ICP-Brasil — em auditoria de 200 documentos
    deste lote, 67% dos campos assinados tinham `/Name` vazio. Usá-lo aqui
    descartaria assinatura válida em massa.
    """
    import pymupdf as _pymupdf

    pymupdf = cast(Any, _pymupdf)
    doc = pymupdf.open(str(path))
    try:
        assinados: set[tuple[int, str | None]] = set()
        for indice in range(doc.page_count):
            for widget in doc[indice].widgets() or []:
                if getattr(widget, "field_type", None) != TIPO_WIDGET_ASSINATURA:
                    continue
                if _tem_assinatura_viva(doc, widget.xref):
                    assinados.add((indice + 1, getattr(widget, "field_name", None)))
        return assinados
    finally:
        doc.close()


def _tem_assinatura_viva(doc: Any, xref_widget: int) -> bool:
    valor = doc.xref_get_key(xref_widget, "V")
    if not valor or valor[0] != "xref":
        return False
    xref_valor = int(valor[1].split()[0])
    byte_range = doc.xref_get_key(xref_valor, "ByteRange")
    if not byte_range or byte_range[0] != "array":
        return False
    numeros = [int(n) for n in re.findall(r"-?\d+", byte_range[1])]
    if len(numeros) != 4 or numeros[0] < 0 or numeros[0] + numeros[1] > numeros[2]:
        return False
    conteudo = doc.xref_get_key(xref_valor, "Contents")
    return bool(conteudo) and conteudo[0] != "null" and bool(conteudo[1].strip("<>() "))


def somente_assinados(
    assinaturas: list[dict[str, Any]], assinados: set[tuple[int, str | None]]
) -> list[dict[str, Any]]:
    """Descarta do resultado do AcroForm os campos em branco (pura).

    Casa por `(página, nome do campo)`. Dois campos sem nome na mesma página
    empatam na chave e passam juntos se um deles estiver assinado — erra para o
    lado de manter a assinatura, que é o lado certo: descartar assinatura viva
    seria trocar um falso positivo raro por um falso negativo.
    """
    return [
        assinatura
        for assinatura in assinaturas
        if (assinatura.get("page"), assinatura.get("campo")) in assinados
    ]


def _texto_camada_digital(path: Path) -> str:
    """Texto da camada digital do PDF (sem OCR, sem custo)."""
    import pymupdf as _pymupdf

    pymupdf = cast(Any, _pymupdf)
    doc = pymupdf.open(str(path))
    try:
        return "\n".join(doc[i].get_text("text") for i in range(doc.page_count))
    finally:
        doc.close()


def detectar_nivel0(
    path: str | Path,
    *,
    texto_extraido: str | None = None,
    dpi_triagem: int = DPI_TRIAGEM,
    densidade_minima: float = DENSIDADE_TINTA_MINIMA,
) -> ResultadoNivel0:
    """Roda os quatro sinais do Nível 0 num PDF.

    `texto_extraido` (opcional): texto já obtido pela pipeline (camada digital
    ou OCR). Quando passado, é usado para os sinais textuais em vez de reler a
    camada do PDF — mais preciso em documento escaneado cujo carimbo só aparece
    após OCR.
    """
    p = Path(path)
    texto = texto_extraido if texto_extraido is not None else _texto_camada_digital(p)
    return ResultadoNivel0(
        digital=find_digital_signatures(texto),
        embedded=somente_assinados(find_embedded_pdf_signatures(p), campos_assinados(p)),
        paginas=triar_paginas(p, dpi=dpi_triagem, densidade_minima=densidade_minima),
        texto_extraido=texto,
    )
