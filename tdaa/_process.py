"""Inicio, supervisión y limpieza de un proceso de pruebas."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


# Arranca fuera del proyecto para importar TDAA antes del código candidato.
_BOOTSTRAP = (
    "import sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from tdaa._zygote import main\n"
    "main(sys.argv[2])"
)
_ROOT = str(Path(__file__).resolve().parent.parent)
_POLL_INTERVAL = 0.02


def run_error(message, status="error"):
    return {"tests": [], "coverage": {}, "status": status, "error": message}


class _Progress:
    """Lee los registros completos que el plugin va dejando antes del informe final."""

    def __init__(self, path):
        self.path = path
        self.offset = 0
        self.items = []
        self.active = None
        self.deadline = float("inf")
        self.tests = []
        self.coverage = {}

    def update(self):
        if not self.path.exists():
            return
        with self.path.open("rb") as stream:
            stream.seek(self.offset)
            for line in stream:
                if not line.endswith(b"\n"):
                    break
                event = json.loads(line)
                self.offset += len(line)
                if "items" in event:
                    self.items = event["items"]
                elif "active" in event:
                    self.active = event["active"]
                    self.deadline = event["deadline"] if event["deadline"] is not None else float("inf")
                else:
                    self.tests.append(event["test"])
                    self.coverage.update(event["coverage"])
                    self.active = None
                    self.deadline = float("inf")

    def timed_out(self, reason):
        data = run_error(reason, status="timeout")
        data["tests"] = self.tests.copy()
        data["coverage"] = self.coverage
        if self.active is not None:
            data["tests"].append({"id": self.active, "outcome": "timeout",
                                  "exc_type": "Timeout", "exc_msg": reason})
        seen = {test["id"] for test in data["tests"]}
        data["tests"].extend({"id": nodeid, "outcome": "notrun"}
                             for nodeid in self.items if nodeid not in seen)
        return data


def _wait(proc, deadline, progress):
    while proc.poll() is None:
        progress.update()
        now = time.monotonic()
        if now >= deadline:
            return progress.timed_out("wall-timeout: se agotó el tiempo de la sesión")
        if now >= progress.deadline:
            return progress.timed_out(f"test-timeout: {progress.active} no terminó")
        try:
            proc.wait(timeout=min(_POLL_INTERVAL, deadline - now, progress.deadline - now))
        except subprocess.TimeoutExpired:
            pass
    return None


def _stop(proc, job):
    try:
        if job is not None:
            job.close()
    finally:
        if proc is not None:
            if os.name == "posix":
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)


def _diagnostic(path):
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - 2000))
        return stream.read(2000).decode("utf-8", errors="replace").strip()


def run_worker(request):
    """Ejecuta una solicitud con un límite de pared independiente de pytest."""
    try:
        with tempfile.TemporaryDirectory(prefix="tdaa_run_") as temp:
            scratch = Path(temp)
            (scratch / "request.json").write_text(json.dumps(request), encoding="utf-8")
            progress = _Progress(scratch / "progress.jsonl")
            diagnostic = scratch / "stderr.log"
            proc = job = None
            deadline = time.monotonic() + request["wall"]
            try:
                options = {"start_new_session": True}
                if os.name == "nt":
                    from ._winjob import CREATE_SUSPENDED, WindowsJob, resume_suspended_process

                    job = WindowsJob(request["mem_mb"] * 1024 * 1024)
                    # El lanzador de un venv puede crear otro python.exe incluso
                    # antes de que arranque el intérprete que ejecutará pytest.
                    options = {"creationflags": subprocess.CREATE_NO_WINDOW | CREATE_SUSPENDED}
                with diagnostic.open("wb") as stderr:
                    proc = subprocess.Popen(
                        [sys.executable, "-E", "-P", "-B", "-X", "utf8",
                         "-c", _BOOTSTRAP, _ROOT, temp],
                        cwd=scratch, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=stderr, **options,
                    )
                if job is not None:
                    job.attach(proc.pid)
                    resume_suspended_process(proc.pid)
                timed_out = _wait(proc, deadline, progress)
            finally:
                _stop(proc, job)

            if timed_out:
                return timed_out
            report = scratch / "result.json"
            if proc.returncode != 0 or not report.is_file():
                detail = _diagnostic(diagnostic)
                return run_error(f"El proceso pytest salió con código {proc.returncode}"
                                 + (f"\n{detail}" if detail else " sin emitir un informe"))
            data = json.loads(report.read_text(encoding="utf-8"))
            if data.get("error"):
                detail = _diagnostic(diagnostic)
                if detail:
                    data["error"] += f"\n{detail}"
            return data
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return run_error(f"Error del supervisor de pytest ({type(exc).__name__}): {exc}")
