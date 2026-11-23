"""Entorno aislado de ejecucion de pruebas.

`Sandbox.run` ejecuta pytest en un subproceso supervisado y devuelve los
resultados de las pruebas junto con la traza de líneas ejecutadas.
El aislamiento es a nivel de proceso; no es un contenedor.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ._process import run_worker


@dataclass
class TestOutcome:
    __test__ = False  # evita que pytest la recolecte
    id: str
    outcome: str                      # passed | failed | timeout | error | skipped | notrun
    exc_type: Optional[str] = None
    exc_msg: Optional[str] = None
    exc_line: Optional[int] = None    # linea del archivo objetivo donde se origino la falla
    duration: float = 0.0

    @property
    def ok(self) -> bool:
        return self.outcome in ("passed", "skipped")


@dataclass
class RunResult:
    tests: List[TestOutcome] = field(default_factory=list)
    coverage: Dict[str, Dict[int, int]] = field(default_factory=dict)  # prueba -> {linea: veces}
    error: Optional[str] = None
    status: str = "ok"  # ok | timeout (sesión interrumpida) | collection-error | error

    def __post_init__(self):
        if self.error and self.status == "ok":
            self.status = "timeout" if self.error == "wall-timeout" else "error"

    @property
    def timed_out(self) -> bool:
        return self.status == "timeout" or any(t.outcome == "timeout" for t in self.tests)

    @property
    def passed(self) -> List[str]:
        return [t.id for t in self.tests if t.outcome == "passed"]

    @property
    def failing(self) -> List[TestOutcome]:
        return [t for t in self.tests if not t.ok]

    @property
    def all_passed(self) -> bool:
        return self.status == "ok" and bool(self.tests) and not self.failing

    @property
    def n_passed(self) -> int:
        return len(self.passed)

    @property
    def n_notrun(self) -> int:
        return sum(t.outcome == "notrun" for t in self.tests)

    def to_json(self) -> dict:
        return {"tests": [t.__dict__ for t in self.tests],
                "coverage": {k: {str(a): b for a, b in v.items()} for k, v in self.coverage.items()},
                "error": self.error, "status": self.status}

    @classmethod
    def from_json(cls, d: dict) -> "RunResult":
        fields = TestOutcome.__dataclass_fields__
        tests = [TestOutcome(**{k: v for k, v in t.items() if k in fields}) for t in d.get("tests", [])]
        cov = {k: {int(a): b for a, b in v.items()} for k, v in d.get("coverage", {}).items()}
        return cls(tests, cov, d.get("error"), d.get("status", "ok"))


class Sandbox:
    """Ejecutor de pruebas con aislamiento por proceso."""

    def __init__(self, timeout: float = 2.0, wall: float = 20.0, mem_mb: int = 1024,
                 max_timeouts: int = 1):
        self.timeout, self.wall = float(timeout), float(wall)
        self.mem_mb, self.max_timeouts = int(mem_mb), int(max_timeouts)
        if not (math.isfinite(self.timeout) and self.timeout >= 0
                and math.isfinite(self.wall) and self.wall > 0
                and self.mem_mb > 0 and self.max_timeouts >= 0):
            raise ValueError("Límites de tiempo o memoria inválidos")
        self.n_runs = 0

    def run(self, cwd: str, tests: List[str], target: Optional[str] = None, trace: bool = False,
            pytest_args: Optional[List[str]] = None) -> RunResult:
        self.n_runs += 1
        req = {"cwd": os.path.abspath(cwd), "tests": tests, "trace": trace,
               "target": os.path.abspath(os.path.join(cwd, target)) if target else None,
               "timeout": self.timeout, "wall": self.wall,
               "mem_mb": self.mem_mb, "max_timeouts": 0 if trace else self.max_timeouts,
               "pytest_args": pytest_args or []}
        return RunResult.from_json(run_worker(req))

    def close(self):
        """Cada llamada a run libera su propio proceso y sus archivos temporales."""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
