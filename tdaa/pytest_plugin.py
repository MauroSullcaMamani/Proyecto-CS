"""Plugin de pytest que convierte una ejecución de pruebas en evidencia estructurada.

Por cada prueba registra:
  * el veredicto (passed / failed / timeout / error),
  * el tipo y el mensaje de la excepción,
  * los marcos de la pila que caen dentro de los archivos objetivo,
  * (opcional) las líneas de los archivos objetivo que la prueba ejecutó.

El plugin no depende del resto del paquete: se instancia dentro del proceso
aislado y su única salida es un diccionario serializable a JSON.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from contextlib import contextmanager

import pytest


class TestTimeout(Exception):
    """Se lanza dentro de la prueba cuando agota su tiempo.

    Al ser una excepción ordinaria conserva la pila, de modo que el informe
    indica en qué línea del programa estaba el flujo de control (por ejemplo,
    dentro de un bucle infinito).
    """

    __test__ = False  # evita que pytest la tome por una clase de pruebas


@contextmanager
def _test_deadline(deadline):
    """Interrumpe Python; el supervisor externo cubre bloqueos sin retorno."""
    if deadline is None:
        yield
        return

    def expired(*_):
        raise TestTimeout("La prueba superó su tiempo límite")

    if hasattr(signal, "setitimer"):
        old_handler = signal.signal(signal.SIGALRM, expired)
        old_timer = signal.setitimer(signal.ITIMER_REAL, max(1e-6, deadline - time.monotonic()))
        try:
            yield
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old_handler)
            if old_timer[0]:
                signal.setitimer(signal.ITIMER_REAL, *old_timer)
    else:
        old_trace = sys.gettrace()

        def check_deadline(frame, event, arg):
            if time.monotonic() >= deadline:
                expired()
            return check_deadline

        sys.settrace(check_deadline)
        try:
            yield
        finally:
            sys.settrace(old_trace)


def _path(filename):
    return os.path.normcase(os.path.realpath(filename))


@contextmanager
def _monitor_lines(callback):
    mon = sys.monitoring
    tool = next(tool for tool in (mon.COVERAGE_ID, 3, 4, 0, 2, 5)
                if mon.get_tool(tool) is None)
    mon.use_tool_id(tool, "tdaa")
    try:
        mon.register_callback(tool, mon.events.LINE, callback)
        mon.set_events(tool, mon.events.LINE)
        mon.restart_events()
        yield
    finally:
        mon.set_events(tool, 0)
        mon.register_callback(tool, mon.events.LINE, None)
        mon.free_tool_id(tool)


class TracePlugin:
    """Resultados por prueba, trazas de excepción y visitas por línea."""

    HIT_CAP = 2000
    # Da tiempo a pytest para registrar un timeout recuperable antes de matar el proceso.
    REPORT_GRACE = 0.25

    def __init__(self, target, trace, timeout, max_timeouts, progress):
        self.target = _path(target) if target else None
        self.trace = trace
        self.timeout = timeout
        self.max_timeouts = max_timeouts
        self.progress = progress
        self.tests = []
        self.coverage = {}
        self.collect_errors = []
        self.items = []
        self.n_timeouts = 0
        self._target_files = {}

    def _write_progress(self, **event):
        self.progress.write(json.dumps(event) + "\n")
        self.progress.flush()

    def pytest_collection_finish(self, session):
        self.items = [item.nodeid for item in session.items]
        self._write_progress(items=self.items)

    def pytest_collectreport(self, report):
        if report.failed:
            self.collect_errors.append(str(report.longrepr)[-1500:])

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item):
        self._current = None
        self._cut = self.max_timeouts > 0 and self.n_timeouts >= self.max_timeouts
        self._deadline = time.monotonic() + self.timeout if self.timeout else None
        deadline = self._deadline + self.REPORT_GRACE if self._deadline is not None else None
        self._write_progress(active=item.nodeid, deadline=deadline)
        yield
        if self._current is not None:
            self.tests.append(self._current)
            self.n_timeouts += self._current["outcome"] == "timeout"
            hits = {item.nodeid: self.coverage[item.nodeid]} if item.nodeid in self.coverage else {}
            self._write_progress(test=self._current, coverage=hits)

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_setup(self, item):
        if self._cut:
            pytest.skip("tdaa: corte por exceso de timeouts")

    def _on_line(self, code, line):
        filename = code.co_filename
        if filename not in self._target_files:
            self._target_files[filename] = _path(filename) == self.target
        if not self._target_files[filename]:
            return sys.monitoring.DISABLE
        n = self._hits.get(line, 0) + 1
        self._hits[line] = n
        if n >= self.HIT_CAP:
            return sys.monitoring.DISABLE
        return None

    @contextmanager
    def _trace_lines(self, nodeid):
        self._hits = {}
        try:
            with _monitor_lines(self._on_line):
                yield
        finally:
            self.coverage[nodeid] = self._hits

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_call(self, item):
        if self.trace and self.target:
            with self._trace_lines(item.nodeid):
                with _test_deadline(self._deadline):
                    yield
        else:
            with _test_deadline(self._deadline):
                yield

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        outcome = yield
        rep = outcome.get_result()
        if rep.when != "call" and not rep.failed and not (rep.when == "setup" and rep.skipped):
            return
        rec = {"id": item.nodeid, "duration": round(rep.duration, 4), "outcome": rep.outcome}
        if rep.skipped and self._cut:
            rec["outcome"] = "notrun"
        if rep.failed:
            ei = call.excinfo
            timed_out = ei is not None and isinstance(ei.value, TestTimeout)
            rec["outcome"] = "timeout" if timed_out else ("failed" if rep.when == "call" else "error")
            rec["exc_type"] = "Timeout" if timed_out else (ei.typename if ei else "Unknown")
            rec["exc_msg"] = str(ei.value if ei else rep.longrepr)[:300]
            if ei is not None and self.target:
                for entry in ei.traceback:
                    if _path(str(entry.path)) == self.target:
                        rec["exc_line"] = entry.lineno + 1
        if self._current is None or self._current["outcome"] != "timeout":
            self._current = rec


class EvidencePlugin:
    def __init__(self, targets, trace=False, test_timeout=2.0):
        self.targets = {_path(t) for t in targets}
        self.trace = trace
        self.test_timeout = float(test_timeout)
        self.tests = []
        self.collect_errors = []
        self._lines = None
        self._order = 0

    # ------------------------------------------------------------------ traza
    def _on_line(self, code, line):
        filename = _path(code.co_filename)
        if filename in self.targets:
            self._lines.add((filename, line))
        return sys.monitoring.DISABLE

    # ------------------------------------------------------------------ hooks
    def pytest_collectreport(self, report):
        if report.failed:
            self.collect_errors.append(
                {"nodeid": report.nodeid, "message": str(report.longrepr)[-1500:]}
            )

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_call(self, item):
        limit = self.test_timeout * (2 if self.trace else 1)
        deadline = time.monotonic() + limit if limit else None
        if self.trace:
            self._lines = set()
            with _monitor_lines(self._on_line):
                with _test_deadline(deadline):
                    yield
        else:
            with _test_deadline(deadline):
                yield

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        outcome = yield
        report = outcome.get_result()
        if report.when == "call" or (report.when == "setup" and not report.passed):
            self.tests.append(self._record(item, call, report))

    # -------------------------------------------------------------- registros
    def _record(self, item, call, report):
        rec = {
            "id": item.nodeid,
            "order": self._order,
            "outcome": report.outcome,
            "duration": round(getattr(report, "duration", 0.0), 4),
            "exc_type": None,
            "message": None,
            "frames": [],
            "covered": [],
        }
        self._order += 1
        if self.trace and self._lines is not None:
            rec["covered"] = sorted(
                [os.path.relpath(f, self.root), ln] for f, ln in self._lines
            )
            self._lines = None
        if report.skipped:
            return rec
        exc = call.excinfo
        if exc is not None and not report.passed:
            rec["exc_type"] = exc.type.__name__
            rec["message"] = str(exc.value)[:600]
            if exc.type is TestTimeout:
                rec["outcome"] = "timeout"
            frames = []
            for entry in exc.traceback:
                path = _path(str(entry.path))
                if path in self.targets:
                    frames.append(
                        [os.path.relpath(path, self.root), entry.lineno + 1, entry.name]
                    )
            # Una recursión infinita produce cientos de marcos idénticos.
            rec["frames"] = frames[-12:]
        return rec

    def result(self, started):
        return {
            "tests": self.tests,
            "collect_errors": self.collect_errors,
            "wall": round(time.monotonic() - started, 3),
        }
