"""Evidencia de ejecución: el vocabulario común entre el sandbox y el agente."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class FeedbackLevel(IntEnum):
    """Cuánta información de la ejecución recibe el agente."""

    BINARY = 0    # solo "la suite pasa / no pasa"
    FAILURE = 1   # + mensaje, tipo de excepción y pila de cada prueba fallida
    SPECTRUM = 2  # + cobertura por prueba (localización basada en espectro)
    ADAPTIVE = 3  # + lo que revelan las ejecuciones de los parches descartados


@dataclass
class TestOutcome:
    __test__ = False
    id: str
    order: int
    outcome: str                      # passed | failed | timeout | skipped
    exc_type: str | None = None
    message: str | None = None
    frames: list = field(default_factory=list)    # [archivo, línea, función]
    covered: list = field(default_factory=list)   # [archivo, línea]
    duration: float = 0.0

    @property
    def failed(self) -> bool:
        return self.outcome in ("failed", "timeout")


@dataclass
class RunResult:
    status: str                       # ok | killed | crashed
    tests: list
    collect_errors: list
    wall: float | None = None
    fail_fast: bool = False

    @classmethod
    def from_json(cls, data: dict, fail_fast: bool = False) -> "RunResult":
        tests = [TestOutcome(**{k: t.get(k) for k in TestOutcome.__dataclass_fields__ if k in t})
                 for t in data.get("tests", [])]
        return cls(data.get("status", "ok"), tests, data.get("collect_errors", []),
                   data.get("wall"), fail_fast)

    def to_json(self) -> dict:
        return {"status": self.status, "wall": self.wall, "collect_errors": self.collect_errors,
                "tests": [t.__dict__ for t in self.tests]}

    # --------------------------------------------------------------- consultas
    @property
    def failing(self) -> list:
        return [t for t in self.tests if t.failed]

    @property
    def passing(self) -> list:
        return [t for t in self.tests if t.outcome == "passed"]

    @property
    def all_passed(self) -> bool:
        return (self.status == "ok" and not self.collect_errors
                and bool(self.passing) and not self.failing)

    @property
    def progress(self) -> int:
        """Pruebas superadas antes del primer fallo (señal graduada barata)."""
        return len(self.passing)

    def signature(self) -> tuple:
        """Huella del fallo: qué prueba cae primero y con qué excepción."""
        if self.status != "ok":
            return (self.status,)
        if self.collect_errors:
            return ("collect",)
        if not self.failing:
            return ("pass",)
        first = min(self.failing, key=lambda t: t.order)
        return (first.id, first.exc_type)

    # ------------------------------------------------------------ presentación
    def render(self, level: FeedbackLevel, max_tests: int = 5) -> str:
        """Texto que se entrega a un proponente basado en LLM."""
        if self.all_passed:
            return "RESULTADO: todas las pruebas pasan."
        if level == FeedbackLevel.BINARY:
            return "RESULTADO: la suite de pruebas falla."
        if self.status != "ok":
            return f"RESULTADO: la ejecución fue abortada ({self.status})."
        lines = [f"RESULTADO: {len(self.failing)} pruebas fallan, {len(self.passing)} pasan."]
        for err in self.collect_errors[:2]:
            lines.append(f"ERROR DE CARGA en {err['nodeid']}: {err['message'][-300:]}")
        for t in self.failing[:max_tests]:
            lines.append(f"- {t.id}: {t.outcome} [{t.exc_type}] {(t.message or '')[:200]}")
            for f, ln, fn in t.frames[-3:]:
                lines.append(f"    en {f}:{ln} ({fn})")
        return "\n".join(lines)
