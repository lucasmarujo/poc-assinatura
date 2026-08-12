"""Amostragem de uso de hardware durante o lote.

Uma thread no processo pai lê CPU e memória a cada `intervalo` segundos e
guarda só os agregados — o relatório precisa de "quanto da máquina o modelo
consumiu", não da série temporal inteira (que, num lote de horas, seria maior
que o próprio relatório).

A leitura é do **sistema** (não do processo): a inferência roda em workers
separados, e é a saturação da máquina que interessa para dimensionar o
`--workers` na próxima execução.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from statistics import fmean
from types import TracebackType
from typing import Any


@dataclass
class AmostradorRecursos:
    """Amostrador de CPU/RAM com ciclo de vida de context manager."""

    intervalo_segundos: float = 2.0
    _cpu: list[float] = field(default_factory=list, init=False)
    _ram: list[float] = field(default_factory=list, init=False)
    _rss_mb: list[float] = field(default_factory=list, init=False)
    _parar: threading.Event = field(default_factory=threading.Event, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)

    def __enter__(self) -> AmostradorRecursos:
        import psutil

        # `cpu_percent` só devolve valor útil a partir da segunda chamada —
        # a primeira apenas fixa o marco de comparação.
        psutil.cpu_percent(interval=None)
        self._thread = threading.Thread(target=self._amostrar, name="recursos", daemon=True)
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._parar.set()
        if self._thread is not None:
            self._thread.join(timeout=self.intervalo_segundos * 2)

    def _amostrar(self) -> None:
        """Amostra até mandarem parar. Falha de leitura é engolida de propósito:
        observabilidade que derruba a própria execução é pior que a métrica que
        falta — sob pressão de memória é justamente o `children()` que estoura, e
        é justamente aí que o lote precisa continuar."""
        import psutil

        processo = psutil.Process()
        while not self._parar.wait(self.intervalo_segundos):
            try:
                self._cpu.append(psutil.cpu_percent(interval=None))
                self._ram.append(psutil.virtual_memory().percent)
                self._rss_mb.append(round(processo.memory_info().rss / 1024**2, 1))
            except (psutil.Error, OSError):
                continue

    def resumo(self) -> dict[str, Any]:
        """Agregados da execução. Lote curto demais para uma amostra → zeros."""
        import psutil

        return {
            "nucleos_logicos": psutil.cpu_count(logical=True),
            "ram_total_gb": round(psutil.virtual_memory().total / 1024**3, 2),
            "amostras": len(self._cpu),
            "intervalo_segundos": self.intervalo_segundos,
            "cpu_media_pct": _media(self._cpu),
            "cpu_maxima_pct": _maximo(self._cpu),
            "ram_media_pct": _media(self._ram),
            "ram_maxima_pct": _maximo(self._ram),
            "rss_medio_mb": _media(self._rss_mb),
            "rss_maximo_mb": _maximo(self._rss_mb),
        }


def _media(valores: list[float]) -> float:
    return round(fmean(valores), 1) if valores else 0.0


def _maximo(valores: list[float]) -> float:
    return round(max(valores), 1) if valores else 0.0
