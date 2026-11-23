"""Localizacion de fallas a partir de senales de ejecucion.

Tres senales, de menor a mayor visibilidad:
  1. traza de error  : linea del archivo objetivo donde se lanzo la excepcion;
  2. cobertura (SBFL): sospecha Ochiai por linea, a partir de que pruebas la ejecutan;
  3. conteo de visitas: en fallas por tiempo, las lineas del bucle caliente.
"""
from __future__ import annotations

import math
from typing import Dict, Iterable, List

from .sandbox import RunResult


def ochiai(ef: int, ep: int, total_failed: int) -> float:
    """Sospecha Ochiai: ef / sqrt(total_fallidas * (ef + ep))."""
    if ef == 0 or total_failed == 0:
        return 0.0
    return ef / math.sqrt(total_failed * (ef + ep))


def suspiciousness(result: RunResult, lines: Iterable[int]) -> Dict[int, float]:
    failed = [t.id for t in result.tests if not t.ok]
    passed = result.passed
    out = {}
    for ln in lines:
        ef = sum(1 for t in failed if result.coverage.get(t, {}).get(ln))
        ep = sum(1 for t in passed if result.coverage.get(t, {}).get(ln))
        out[ln] = ochiai(ef, ep, len(failed))
    return out


def rank_lines(result: RunResult, lines: List[int], use_traceback: bool = True,
               use_coverage: bool = True, use_hits: bool = True) -> List[int]:
    """Ordena las lineas candidatas de mas a menos sospechosa.

    La clave de orden combina, en este orden de prioridad: sospecha Ochiai,
    aparicion en la traza de error y conteo de visitas en pruebas con timeout.
    Los empates se resuelven por el orden recibido en `lines` (estable).
    """
    susp = suspiciousness(result, lines) if use_coverage else {ln: 0.0 for ln in lines}
    tb: Dict[int, int] = {}
    hot: Dict[int, int] = {}
    for t in result.failing:
        if use_traceback and t.exc_line and t.outcome != "timeout":
            tb[t.exc_line] = tb.get(t.exc_line, 0) + 1
        if use_hits and t.outcome == "timeout":
            for ln, n in result.coverage.get(t.id, {}).items():
                hot[ln] = max(hot.get(ln, 0), n)
    max_hot = max(hot.values(), default=0) or 1
    n_fail = len(result.failing) or 1
    pos = {ln: i for i, ln in enumerate(lines)}

    def key(ln):
        return (-round(susp.get(ln, 0.0), 6), -tb.get(ln, 0) / n_fail,
                -round(hot.get(ln, 0) / max_hot, 2), pos[ln])

    return sorted(lines, key=key)
