"""Bucle del agente: observar, localizar, proponer, validar, revertir.

La unica condicion de aceptacion de un parche es que la suite completa pase
dentro del sandbox. Cada validacion consume una unidad del presupuesto.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .proposers import MutationProposer, Observation
from .sandbox import RunResult, Sandbox


@dataclass
class RepairTask:
    name: str
    workdir: str          # directorio de trabajo aislado (copia del proyecto)
    target: str           # archivo a reparar, relativo a workdir
    tests: List[str]      # rutas o node ids de pytest, relativos a workdir
    pytest_args: List[str] = field(default_factory=list)


@dataclass
class AgentConfig:
    budget: int = 100000          # validaciones de parches permitidas
    depth: int = 1                # 1 = parches de una edicion; 2 = encadena una segunda sobre mejoras parciales
    beam: int = 2                 # mejoras parciales que se exploran en profundidad
    budget_per_branch: int = 100  # validaciones por rama en profundidad > 1


@dataclass
class RepairReport:
    task: str
    fixed: bool = False
    patch: Optional[str] = None
    patch_desc: Optional[str] = None
    validations: int = 0          # parches validados hasta el arreglo (o hasta agotar)
    observations: int = 0         # ejecuciones con traza
    initial_failing: int = 0
    n_tests: int = 0
    depth: int = 0
    seconds: float = 0.0
    failure_types: Dict[str, int] = field(default_factory=dict)
    events: List[dict] = field(default_factory=list)
    initial_passed: int = 0
    initial_notrun: int = 0
    status: str = "ok"
    error: Optional[str] = None


class Executor:
    """Envuelve el sandbox con una memoria de resultados por contenido del parche.

    Las ejecuciones son deterministas, asi que un parche identico no se vuelve a
    ejecutar. La memoria puede persistirse en disco entre configuraciones.
    """

    def __init__(self, sandbox: Sandbox, task: RepairTask, cache: Optional[dict] = None):
        self.sandbox, self.task = sandbox, task
        self.cache = cache if cache is not None else {}
        self.hits = self.misses = 0

    def run(self, source: str, trace: bool) -> RunResult:
        key = hashlib.sha1(source.encode()).hexdigest() + ("T" if trace else "F")
        if key in self.cache:
            cached = RunResult.from_json(self.cache[key])
            if cached.status == "ok":
                self.write_source(source)
                self.hits += 1
                return cached
        self.misses += 1
        self.write_source(source)
        res = self.sandbox.run(self.task.workdir, self.task.tests, target=self.task.target,
                               trace=trace, pytest_args=self.task.pytest_args)
        if res.status == "ok":
            self.cache[key] = res.to_json()
        return res

    def write_source(self, source: str):
        with open(os.path.join(self.task.workdir, self.task.target), "w", encoding="utf-8") as f:
            f.write(source)


class Agent:
    def __init__(self, executor: Executor, proposer=None, config: Optional[AgentConfig] = None,
                 log: Optional[Callable[[dict], None]] = None):
        self.ex = executor
        self.proposer = proposer or MutationProposer()
        self.cfg = config or AgentConfig()
        self.log = log

    def _emit(self, report: RepairReport, **ev):
        report.events.append(ev)
        if self.log:
            self.log(ev)

    def repair(self, source: str) -> RepairReport:
        t0 = time.time()
        task = self.ex.task
        report = RepairReport(task=task.name)
        tried = set()
        try:
            self._search(source, report, tried, depth=1, budget=self.cfg.budget)
        finally:
            self.ex.write_source(report.patch if report.fixed else source)
        report.seconds = round(time.time() - t0, 3)
        return report

    def _search(self, source: str, report: RepairReport, tried: set, depth: int, budget: int) -> bool:
        base = self.ex.run(source, trace=True)                      # OBSERVAR
        report.observations += 1
        if depth == 1:
            report.n_tests = len(base.tests)
            report.initial_passed = base.n_passed
            report.initial_notrun = base.n_notrun
            report.initial_failing = len(base.failing)
            for t in base.failing:
                report.failure_types[t.exc_type or "?"] = report.failure_types.get(t.exc_type or "?", 0) + 1
        self._emit(report, step="observe", depth=depth, passed=base.n_passed, failing=len(base.failing),
                   notrun=base.n_notrun,
                   error=base.error, status=base.status, timed_out=base.timed_out)
        if base.all_passed:
            report.fixed, report.patch, report.depth = True, source, depth - 1
            return True
        if base.status != "ok":
            report.status, report.error = base.status, base.error
            return False
        base_pass = set(base.passed)
        obs = Observation(source=source, result=base)
        improving = []
        used = 0
        for cand in self.proposer.propose(obs):                     # LOCALIZAR + PROPONER
            if used >= budget or report.validations >= self.cfg.budget:
                break
            if cand.key in tried:
                continue
            tried.add(cand.key)
            res = self.ex.run(cand.source, trace=False)             # VALIDAR
            used += 1
            report.validations += 1
            ok = res.all_passed
            if not ok:
                self.ex.write_source(source)
            self._emit(report, step="validate", depth=depth, n=report.validations, line=cand.line, op=cand.op,
                       desc=cand.desc, passed=res.n_passed, failing=len(res.failing), notrun=res.n_notrun,
                       accepted=ok,
                       error=res.error, status=res.status, timed_out=res.timed_out)
            if res.status == "error":
                report.status, report.error = res.status, res.error
                return False
            obs.memory.append({"desc": "L%d %s %s" % (cand.line, cand.op, cand.desc), "n_passed": res.n_passed})
            if ok:
                report.fixed, report.patch, report.depth = True, cand.source, depth
                report.patch_desc = "L%d %s: %s" % (cand.line, cand.op, cand.desc)
                return True
            now = set(res.passed)
            if res.status == "ok" and base_pass < now:              # mejora estricta: no rompe nada y arregla algo
                improving.append((len(now), -cand.index, cand))
        if depth < self.cfg.depth:
            improving.sort(key=lambda x: (-x[0], -x[1]))
            for _, _, cand in improving[: self.cfg.beam]:
                self._emit(report, step="descend", depth=depth, desc=cand.desc, line=cand.line)
                if self._search(cand.source, report, tried, depth + 1, self.cfg.budget_per_branch):
                    prefix = "L%d %s: %s" % (cand.line, cand.op, cand.desc)
                    report.patch_desc = f"{prefix} ; {report.patch_desc}" if report.patch_desc else prefix
                    return True
                if report.status != "ok":
                    return False
        return False


def save_events(report: RepairReport, path: str):
    with open(path, "w", encoding="utf-8") as f:
        for ev in report.events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
