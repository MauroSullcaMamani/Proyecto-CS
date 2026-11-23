"""Punto de entrada interno para una sola ejecución de pytest."""
import json
import math
import os
import sys
from pathlib import Path


def _apply_posix_limits(request):
    import resource

    def limit(kind, requested):
        soft, hard = resource.getrlimit(kind)
        cap = min(value for value in (soft, hard, requested) if value != resource.RLIM_INFINITY)
        resource.setrlimit(kind, (cap, cap))

    limit(resource.RLIMIT_AS, request["mem_mb"] * 1024 * 1024)
    limit(resource.RLIMIT_CPU, math.ceil(request["wall"]) + 2)


def _run_pytest(request, scratch):
    import pytest

    from .pytest_plugin import TracePlugin

    with (scratch / "progress.jsonl").open("w", encoding="utf-8", newline="\n") as progress:
        plugin = TracePlugin(request["target"], request["trace"], request["timeout"],
                             request["max_timeouts"], progress)
        args = [*request["tests"], "-q", "-p", "no:cacheprovider",
                "--basetemp", str(scratch / "pytest"), *request["pytest_args"]]
        code = pytest.main(args, plugins=[plugin])

    error = None
    status = "ok"
    if plugin.collect_errors and code in (pytest.ExitCode.TESTS_FAILED, pytest.ExitCode.INTERRUPTED):
        status = "collection-error"
        error = "Falló la colección de pytest:\n" + "\n".join(plugin.collect_errors[:3])
    elif code not in (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED) or not plugin.tests:
        status = "error"
        error = f"pytest terminó con código {int(code)} ({getattr(code, 'name', 'desconocido')})"
        if not plugin.tests:
            error += " sin resultados de pruebas"
    elif code == pytest.ExitCode.TESTS_FAILED and all(
        test["outcome"] in ("passed", "skipped") for test in plugin.tests
    ):
        status = "error"
        error = "pytest indicó fallos sin registrar ninguna prueba fallida"

    seen = {test["id"] for test in plugin.tests}
    plugin.tests.extend({"id": item, "outcome": "notrun"} for item in plugin.items if item not in seen)
    return {"tests": plugin.tests, "coverage": plugin.coverage, "status": status, "error": error}


def main(directory):
    from ._process import run_error

    scratch = Path(directory)
    try:
        request = json.loads((scratch / "request.json").read_text(encoding="utf-8"))
        if sys.version_info < (3, 12):
            raise RuntimeError(f"TDAA requiere Python 3.12 o superior (actual: {sys.version.split()[0]})")
        if os.name == "posix":
            _apply_posix_limits(request)
        # Restaura rutas de importación configuradas por el usuario después
        # de aplicar los límites. El arranque con -E evita cargarlas antes.
        if os.environ.get("PYTHONPATH"):
            root = Path(__file__).resolve().parent.parent
            paths = (str(root / entry) for entry in os.environ["PYTHONPATH"].split(os.pathsep))
            sys.path[1:1] = paths
        # Carga pytest antes de añadir el proyecto a sys.path.
        from . import pytest_plugin  # noqa: F401

        os.chdir(request["cwd"])
        sys.path.insert(0, request["cwd"])
        # -B evita escrituras. Un prefijo nuevo también impide leer bytecode
        # obsoleto del candidato al validar parches del mismo tamaño.
        sys.pycache_prefix = str(scratch / "pycache")
        if "legacy_order" in request:
            from .worker import run_order

            data = run_order(request["legacy_order"], request["cwd"], scratch)
        else:
            data = _run_pytest(request, scratch)
    except BaseException:
        import traceback

        data = run_error(traceback.format_exc(limit=8))
    (scratch / "result.json").write_text(json.dumps(data), encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1])
