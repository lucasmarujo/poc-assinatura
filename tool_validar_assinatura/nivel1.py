"""Nível 1 — detector visual local (YOLOv8s / Tech4Humans) via ONNX Runtime CPU.

Substitui a feature `SIGNATURES` do AWS Textract, que é cobrada por página
(US$ 3,50/1.000 páginas) e insustentável no volume de produção. Aqui a
detecção roda no próprio worker: a página é rasterizada com PyMuPDF e passa
pelo `tech4humans/yolov8s-signature-detector` exportado em ONNX
(mAP@0.5 ≈ 0,945 no dataset do autor), sem custo por página.

Contrato de saída **idêntico** ao do Textract
(`motor_ia.extractors.textract.parse_signature_blocks`):
`{page, confidence, bounding_box{left,top,width,height}, quem_assinou}`, com
`confidence` em [0,1] e `bounding_box` relativo à página — assim o consumidor
(formatação para o LLM, persistência) não muda ao trocar o detector.
`quem_assinou` é sempre `None`: sem OCR não há texto adjacente para inferir o
signatário — é a informação que se perde ao sair do Textract.

Pré e pós-processamento seguem o `detector.py` oficial do Space
`tech4humans/signature-detection`: entrada 640×640 RGB normalizada em [0,1]
(NCHW), saída `(1, 4+classes, 8400)` com caixas `cx,cy,w,h`, filtro por
confiança e NMS. O NMS é numpy puro para não trazer OpenCV como dependência.

Quando o documento inteiro termina sem nenhuma detecção, entra o fallback de
ladrilhos: cada página analisada é refeita em 3×3 recortes sobrepostos,
rasterizados em DPI maior, e passa pelo **mesmo** detector. É uma questão de
escala — a rubrica ocupa 3× mais da entrada 640×640 —, não um nível novo.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import numpy as np
import structlog
from modelo import resolver_modelo

if TYPE_CHECKING:
    from collections.abc import Sequence

    from PIL import Image

_logger = structlog.get_logger(__name__)

MODELO_NOME = "tech4humans/yolov8s-signature-detector (onnx, cpu)"

# Entrada fixa do modelo exportado (imgsz=640 no export do Tech4Humans).
TAMANHO_ENTRADA = 640
# Confiança mínima calibrada nos documentos da PoC (fichas e contratos de
# trabalhadores, digitalizados). O Space oficial usa 0,25, que aqui perde
# rubrica de traço fino: no lote de 17 documentos, 0,25 acerta 15/17 e 0,15
# acerta 17/17 — sem nenhum falso-positivo nas conferências visuais (os campos
# de assinatura vazios seguem sem detecção). `--conf` no script de teste é a
# alavanca para revisar isso em outro corpus.
CONFIANCA_MINIMA = 0.15
IOU_MAXIMO = 0.5
# DPI do raster enviado ao modelo. 150 numa A4 dá ~1240×1754 px, o que
# preserva o traço da rubrica após a redução para 640×640.
DPI_RENDER = 150

# Fallback: página que não devolveu nada no passe normal é refeita em ladrilhos.
# A A4 inteira comprimida para 640×640 dá ~77 px por polegada do documento; um
# ladrilho de 1/3 do lado ocupa a mesma entrada com ~179 px/pol — 2,3× mais
# resolução efetiva, mesmo modelo, sem nível novo na cascata.
LADRILHOS_POR_LADO = 3
# Rubrica cortada exatamente na divisa sumiria dos dois ladrilhos. 15% de margem
# custa quase nada (o NMS remove a duplicata) e cobre o caso.
SOBREPOSICAO_LADRILHO = 0.15
# 300 DPI numa A4 dá ~1075 px por ladrilho: reduz para 640 em LANCZOS, sem
# ampliar pixel — abaixo disso o ladrilho seria esticado e não haveria ganho.
DPI_LADRILHO = 300

ENV_THREADS = "POC_ASSINATURA_ONNX_THREADS"


@dataclass(frozen=True)
class ResultadoNivel1:
    """Detecções visuais num documento + telemetria da inferência."""

    assinaturas: list[dict[str, Any]] = field(default_factory=list)
    paginas_analisadas: list[int] = field(default_factory=list)
    paginas_ignoradas: list[int] = field(default_factory=list)
    paginas_ladrilhadas: list[int] = field(default_factory=list)
    tempo_inferencia_ms: float = 0.0
    modelo: str = MODELO_NOME

    @property
    def total(self) -> int:
        return len(self.assinaturas)

    @property
    def tem_assinatura(self) -> bool:
        return bool(self.assinaturas)


def _preprocessar(imagem: Image.Image) -> np.ndarray:
    """Imagem PIL → tensor NCHW float32 normalizado, 640×640.

    Reamostragem em LANCZOS, não no BILINEAR do Space oficial: aqui a página A4
    é rasterizada por nós em ~1240×1754 e comprimida para 640×640, e o
    bilinear apaga rubrica de caneta fina (medido nos documentos da PoC —
    `Douglas Martins.pdf` sai de 0 para 2 detecções, `Matheus Oliveira da
    Silva.pdf` de 0 para 1, sem mexer na confiança mínima).
    """
    from PIL import Image as _Image

    redimensionada = imagem.convert("RGB").resize(
        (TAMANHO_ENTRADA, TAMANHO_ENTRADA), _Image.Resampling.LANCZOS
    )
    dados = np.asarray(redimensionada, dtype=np.float32) / 255.0
    return np.expand_dims(np.transpose(dados, (2, 0, 1)), axis=0)


def _iou(caixa: np.ndarray, outras: np.ndarray) -> np.ndarray:
    """IoU de uma caixa `(x1,y1,x2,y2)` contra um array `(N,4)`."""
    x1 = np.maximum(caixa[0], outras[:, 0])
    y1 = np.maximum(caixa[1], outras[:, 1])
    x2 = np.minimum(caixa[2], outras[:, 2])
    y2 = np.minimum(caixa[3], outras[:, 3])
    intersecao = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
    area = (caixa[2] - caixa[0]) * (caixa[3] - caixa[1])
    areas = (outras[:, 2] - outras[:, 0]) * (outras[:, 3] - outras[:, 1])
    uniao = area + areas - intersecao
    return np.where(uniao > 0, intersecao / uniao, 0.0)


def nms(caixas: np.ndarray, scores: np.ndarray, iou_maximo: float) -> list[int]:
    """Non-Maximum Suppression em numpy puro.

    `caixas` em `(N,4)` formato `(x1,y1,x2,y2)`. Devolve os índices mantidos,
    da maior para a menor confiança.
    """
    if caixas.size == 0:
        return []
    ordem = np.argsort(scores)[::-1]
    mantidos: list[int] = []
    while ordem.size > 0:
        atual = int(ordem[0])
        mantidos.append(atual)
        if ordem.size == 1:
            break
        restantes = ordem[1:]
        sobreposicao = _iou(caixas[atual], caixas[restantes])
        ordem = restantes[sobreposicao <= iou_maximo]
    return mantidos


def pos_processar(
    saida: np.ndarray,
    *,
    confianca_minima: float = CONFIANCA_MINIMA,
    iou_maximo: float = IOU_MAXIMO,
) -> list[dict[str, Any]]:
    """Saída bruta do YOLOv8 → caixas relativas à página, já filtradas por NMS.

    Trabalha em coordenadas relativas ([0,1], dividindo pelo lado da entrada):
    o resize é uniforme nos dois eixos, então o IoU é preservado e o resultado
    fica independente do DPI de renderização.
    """
    # Só a dimensão de batch sai — `np.squeeze` sem eixo colapsaria também a de
    # propostas quando o modelo devolve uma única caixa.
    predicoes = np.asarray(saida)
    if predicoes.ndim == 3:
        predicoes = predicoes[0]
    if predicoes.ndim != 2 or predicoes.shape[0] < 5:
        return []
    predicoes = predicoes.T

    scores = predicoes[:, 4:].max(axis=1)
    candidatas = scores >= confianca_minima
    if not candidatas.any():
        return []

    scores = scores[candidatas]
    cx, cy, largura, altura = (predicoes[candidatas, i] / TAMANHO_ENTRADA for i in range(4))
    caixas = np.stack(
        [cx - largura / 2, cy - altura / 2, cx + largura / 2, cy + altura / 2], axis=1
    )

    detecoes: list[dict[str, Any]] = []
    for indice in nms(caixas, scores, iou_maximo):
        x1, y1, x2, y2 = (float(np.clip(v, 0.0, 1.0)) for v in caixas[indice])
        detecoes.append(
            {
                "confidence": round(float(scores[indice]), 4),
                "bounding_box": {
                    "left": round(x1, 4),
                    "top": round(y1, 4),
                    "width": round(x2 - x1, 4),
                    "height": round(y2 - y1, 4),
                },
                "quem_assinou": None,
            }
        )
    return detecoes


def retangulos_ladrilhos(
    *, lado: int = LADRILHOS_POR_LADO, sobreposicao: float = SOBREPOSICAO_LADRILHO
) -> list[tuple[float, float, float, float]]:
    """Os `lado²` ladrilhos da página, como frações `(x0, y0, x1, y1)` dela.

    Cada ladrilho é alargado em `sobreposicao` do próprio passo para os dois
    lados, presa a margem nas bordas da página. Vizinhos se sobrepõem de
    propósito: assinatura em cima de uma divisa aparece inteira em pelo menos um
    ladrilho.
    """
    passo = 1.0 / lado
    margem = passo * sobreposicao
    return [
        (
            max(coluna * passo - margem, 0.0),
            max(linha * passo - margem, 0.0),
            min((coluna + 1) * passo + margem, 1.0),
            min((linha + 1) * passo + margem, 1.0),
        )
        for linha in range(lado)
        for coluna in range(lado)
    ]


def caixa_para_pagina(
    deteccao: dict[str, Any], recorte: tuple[float, float, float, float]
) -> dict[str, Any]:
    """Detecção relativa ao ladrilho → relativa à página (pura).

    `pos_processar` já devolve caixa em [0,1] do que foi inferido, então basta
    reposicionar e reescalar pelo retângulo do ladrilho — o squash para 640×640
    não entra na conta.
    """
    x0, y0, x1, y1 = recorte
    largura, altura = x1 - x0, y1 - y0
    caixa = deteccao["bounding_box"]
    return {
        **deteccao,
        "bounding_box": {
            "left": round(x0 + caixa["left"] * largura, 4),
            "top": round(y0 + caixa["top"] * altura, 4),
            "width": round(caixa["width"] * largura, 4),
            "height": round(caixa["height"] * altura, 4),
        },
    }


def _deduplicar(detecoes: list[dict[str, Any]], iou_maximo: float) -> list[dict[str, Any]]:
    """Remove a mesma rubrica vista por ladrilhos vizinhos (mesmo NMS, agora em
    coordenada de página)."""
    if len(detecoes) < 2:
        return detecoes
    caixas = np.array(
        [
            [
                d["bounding_box"]["left"],
                d["bounding_box"]["top"],
                d["bounding_box"]["left"] + d["bounding_box"]["width"],
                d["bounding_box"]["top"] + d["bounding_box"]["height"],
            ]
            for d in detecoes
        ],
        dtype=np.float64,
    )
    scores = np.array([d["confidence"] for d in detecoes], dtype=np.float64)
    return [detecoes[indice] for indice in nms(caixas, scores, iou_maximo)]


def _threads_configuradas() -> int | None:
    bruto = os.getenv(ENV_THREADS)
    if not bruto:
        return None
    try:
        valor = int(bruto)
    except ValueError:
        return None
    return valor if valor > 0 else None


class DetectorAssinaturaOnnx:
    """Sessão ONNX Runtime carregada uma vez e reusada entre documentos.

    Carregar o `.onnx` (~44 MB) custa centenas de ms; num worker que processa
    milhares de documentos a sessão tem de ser singleton — ver `obter_detector`.
    """

    def __init__(
        self,
        modelo_path: str | Path,
        *,
        threads: int | None = None,
        sessao: Any | None = None,
    ) -> None:
        self.modelo_path = Path(modelo_path)
        threads_efetivas = threads if threads is not None else _threads_configuradas()
        if sessao is not None:
            # Sessão injetada (testes) — não carrega os pesos nem o ONNX Runtime.
            self._sessao = sessao
        else:
            import onnxruntime as ort

            opcoes = ort.SessionOptions()
            # A arena de memória do CPU EP aloca em blocos grandes e não devolve
            # ao SO. Com uma sessão isso é irrelevante; num pool de processos
            # cada worker multiplica o commit e o Windows fecha o lote com
            # "paging file too small". Desligar custa poucos % de latência e é o
            # que permite paralelizar por processo.
            opcoes.enable_cpu_mem_arena = False
            if threads_efetivas:
                opcoes.intra_op_num_threads = threads_efetivas
                opcoes.inter_op_num_threads = 1
            self._sessao = ort.InferenceSession(
                str(self.modelo_path), opcoes, providers=["CPUExecutionProvider"]
            )
            _logger.info(
                "poc_assinatura.nivel1_sessao_carregada",
                modelo=str(self.modelo_path),
                threads=threads_efetivas,
            )
        self._nome_entrada = self._sessao.get_inputs()[0].name

    def detectar_imagem(
        self,
        imagem: Image.Image,
        *,
        confianca_minima: float = CONFIANCA_MINIMA,
        iou_maximo: float = IOU_MAXIMO,
    ) -> tuple[list[dict[str, Any]], float]:
        """Detecta assinaturas numa página já rasterizada.

        Devolve `(detecções, tempo_de_inferência_ms)`.
        """
        tensor = _preprocessar(imagem)
        inicio = time.perf_counter()
        saida = self._sessao.run(None, {self._nome_entrada: tensor})
        decorrido_ms = (time.perf_counter() - inicio) * 1000.0
        detecoes = pos_processar(saida[0], confianca_minima=confianca_minima, iou_maximo=iou_maximo)
        return detecoes, decorrido_ms

    def _detectar_ladrilhos(
        self,
        pagina: Any,
        *,
        confianca_minima: float,
        iou_maximo: float,
        dpi: int,
    ) -> tuple[list[dict[str, Any]], float]:
        """Segunda passada numa página: um recorte por ladrilho, em DPI maior.

        Cada ladrilho é rasterizado com `clip`, então o custo de memória continua
        o de uma página — não se materializa a página inteira em 300 DPI.
        """
        import pymupdf as _pymupdf
        from PIL import Image as _Image

        pymupdf = cast(Any, _pymupdf)
        area = pagina.rect
        encontradas: list[dict[str, Any]] = []
        total_ms = 0.0
        for fracoes in retangulos_ladrilhos():
            x0, y0, x1, y1 = fracoes
            recorte = pymupdf.Rect(
                area.x0 + x0 * area.width,
                area.y0 + y0 * area.height,
                area.x0 + x1 * area.width,
                area.y0 + y1 * area.height,
            )
            pix = pagina.get_pixmap(dpi=dpi, clip=recorte)
            imagem = _Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            detecoes, decorrido_ms = self.detectar_imagem(
                imagem, confianca_minima=confianca_minima, iou_maximo=iou_maximo
            )
            total_ms += decorrido_ms
            encontradas += [caixa_para_pagina(d, fracoes) for d in detecoes]
        return _deduplicar(encontradas, iou_maximo), total_ms

    def detectar_pdf(
        self,
        path: str | Path,
        *,
        paginas: Sequence[int] | None = None,
        confianca_minima: float = CONFIANCA_MINIMA,
        iou_maximo: float = IOU_MAXIMO,
        dpi: int = DPI_RENDER,
        fallback_ladrilhos: bool = False,
        dpi_ladrilho: int = DPI_LADRILHO,
    ) -> ResultadoNivel1:
        """Roda o detector nas páginas do PDF.

        `paginas` (1-based) restringe a inferência — é por aqui que a triagem
        do Nível 0 exclui páginas em branco. `None` = todas as páginas.

        `fallback_ladrilhos` liga a segunda passada: **só** quando o documento
        inteiro terminou sem nenhuma detecção, cada página analisada é refeita em
        ladrilhos, em DPI maior. Documento que já achou não paga nada.
        """
        import pymupdf as _pymupdf
        from PIL import Image as _Image

        pymupdf = cast(Any, _pymupdf)
        doc = pymupdf.open(str(path))
        try:
            selecionadas = (
                sorted({p for p in paginas if 1 <= p <= doc.page_count})
                if paginas is not None
                else list(range(1, doc.page_count + 1))
            )
            ignoradas = [p for p in range(1, doc.page_count + 1) if p not in set(selecionadas)]

            assinaturas: list[dict[str, Any]] = []
            total_ms = 0.0
            for numero in selecionadas:
                pix = doc[numero - 1].get_pixmap(dpi=dpi)
                imagem = _Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                detecoes, decorrido_ms = self.detectar_imagem(
                    imagem, confianca_minima=confianca_minima, iou_maximo=iou_maximo
                )
                total_ms += decorrido_ms
                for deteccao in detecoes:
                    assinaturas.append({"page": numero, **deteccao})

            ladrilhadas: list[int] = []
            if fallback_ladrilhos and not assinaturas:
                for numero in selecionadas:
                    detecoes, decorrido_ms = self._detectar_ladrilhos(
                        doc[numero - 1],
                        confianca_minima=confianca_minima,
                        iou_maximo=iou_maximo,
                        dpi=dpi_ladrilho,
                    )
                    total_ms += decorrido_ms
                    ladrilhadas.append(numero)
                    for deteccao in detecoes:
                        assinaturas.append({"page": numero, **deteccao})

            assinaturas.sort(key=lambda a: (a["page"], a["bounding_box"]["top"]))
            return ResultadoNivel1(
                assinaturas=assinaturas,
                paginas_analisadas=selecionadas,
                paginas_ignoradas=ignoradas,
                paginas_ladrilhadas=ladrilhadas,
                tempo_inferencia_ms=round(total_ms, 2),
            )
        finally:
            doc.close()


@lru_cache(maxsize=1)
def _detector_cacheado(modelo_path: str) -> DetectorAssinaturaOnnx:
    return DetectorAssinaturaOnnx(modelo_path)


def obter_detector(modelo_path: str | Path | None = None) -> DetectorAssinaturaOnnx:
    """Detector singleton (sessão ONNX reusada). Resolve/baixa os pesos se preciso."""
    return _detector_cacheado(str(resolver_modelo(modelo_path)))
