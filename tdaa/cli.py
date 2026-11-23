"""Linea de comandos: `python -m tdaa fix ARCHIVO --tests RUTA [--project DIR]`."""
from __future__ import annotations

import argparse
import difflib
import os
import shutil
import sys
import tempfile
import tokenize

from .agent import Agent, AgentConfig, Executor, RepairTask
from .mutators import normalize_source
from .proposers import MutationProposer
from .sandbox import Sandbox


def _execution_state(event):
    if event["status"] == "error":
        return "error de infraestructura"
    if event["status"] == "collection-error":
        return "error de colección"
    if event["timed_out"]:
        return "timeout"
    return ""


def _print_event(ev):
    detail = _execution_state(ev) if "status" in ev else ""
    suffix = f"  [{detail}]" if detail else ""
    pending = ev.get("notrun", 0)
    counts = f"pasan={ev['passed']}  fallan={ev['failing'] - pending}" if "passed" in ev else ""
    if pending:
        counts += f"  pendientes={pending}"
    if ev["step"] == "observe":
        print(f"[observar]  profundidad={ev['depth']}  {counts}{suffix}")
    elif ev["step"] == "validate":
        mark = "ACEPTADO" if ev["accepted"] else "revertido"
        print("[validar %3d] L%-3d %-8s %s  %s  %s%s"
              % (ev["n"], ev["line"], ev["op"], ev["desc"], counts, mark, suffix))
    elif ev["step"] == "descend":
        print("[iterar]    conserva mejora parcial en L%d (%s) y vuelve a observar" % (ev["line"], ev["desc"]))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tdaa", description="Agente de reparacion guiado por pruebas")
    sub = ap.add_subparsers(dest="cmd", required=True)
    fx = sub.add_parser("fix", help="repara un archivo hasta que su suite de pytest pase")
    fx.add_argument("target", help="archivo a reparar (relativo al proyecto)")
    fx.add_argument("--tests", nargs="+", required=True, help="rutas de pruebas (relativas al proyecto)")
    fx.add_argument("--project", default=".", help="raiz del proyecto (se copia a un directorio temporal)")
    fx.add_argument("--budget", type=int, default=300)
    fx.add_argument("--depth", type=int, default=2)
    fx.add_argument("--localization", choices=["none", "traceback", "coverage"], default="coverage")
    fx.add_argument("--write", action="store_true", help="escribe el parche aceptado en el proyecto original")
    fx.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    try:
        with tempfile.TemporaryDirectory(prefix="tdaa_") as work:
            wd = os.path.join(work, "proj")
            shutil.copytree(a.project, wd, ignore=shutil.ignore_patterns(
                "__pycache__", ".git", ".pytest_cache", ".venv"))
            with tokenize.open(os.path.join(wd, a.target)) as original:
                source = normalize_source(original.read())
            task = RepairTask(name=a.target, workdir=wd, target=a.target, tests=a.tests)
            with Sandbox() as sb:
                agent = Agent(Executor(sb, task), MutationProposer(localization=a.localization),
                              AgentConfig(budget=a.budget, depth=a.depth), log=None if a.quiet else _print_event)
                rep = agent.repair(source)
    except OSError as exc:
        print(f"RESULTADO: error de infraestructura al preparar o ejecutar el proyecto\n{exc}",
              file=sys.stderr)
        return 2
    print()
    counts = (f"al inicio pasaban {rep.initial_passed} "
              f"y fallaban {rep.initial_failing - rep.initial_notrun}")
    if rep.initial_notrun:
        counts += f", pendientes {rep.initial_notrun}"
    if rep.error:
        state = _execution_state({"status": rep.status, "timed_out": rep.status == "timeout"})
        print(f"RESULTADO: {state} tras {rep.validations} validaciones ({counts})\n{rep.error}",
              file=sys.stderr)
        return 2
    if rep.fixed:
        if rep.patch_desc:
            print(f"RESULTADO: reparado con {rep.validations} validaciones en {rep.seconds:.1f} s"
                  f" ({counts})  ->  {rep.patch_desc}")
        else:
            print(f"RESULTADO: la suite ya pasaba ({rep.initial_passed} aprobadas, 0 validaciones)")
        sys.stdout.writelines(difflib.unified_diff(source.splitlines(True), rep.patch.splitlines(True),
                                                   "a/" + a.target, "b/" + a.target))
        if a.write:
            with open(os.path.join(a.project, a.target), "w", encoding="utf-8") as f:
                f.write(rep.patch)
    else:
        print(f"RESULTADO: sin parche tras {rep.validations} validaciones ({counts})")
    return 0 if rep.fixed else 1


if __name__ == "__main__":
    sys.exit(main())
