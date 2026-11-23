"""Adaptador del benchmark QuixBugs (https://github.com/jkoppel/QuixBugs)."""
from __future__ import annotations

import os
import shutil
from typing import List

from .agent import RepairTask
from .mutators import normalize_source

HELPERS = {"node"}


def list_bugs(root: str) -> List[str]:
    progs = os.path.join(root, "python_programs")
    names = sorted(f[:-3] for f in os.listdir(progs) if f.endswith(".py") and not f.endswith("_test.py"))
    return [n for n in names if n not in HELPERS and os.path.exists(os.path.join(root, "python_testcases", "test_%s.py" % n))]


def buggy_source(root: str, name: str) -> str:
    return normalize_source(open(os.path.join(root, "python_programs", name + ".py")).read())


def reference_source(root: str, name: str) -> str:
    return normalize_source(open(os.path.join(root, "correct_python_programs", name + ".py")).read())


def make_task(root: str, name: str, workroot: str) -> RepairTask:
    """Crea un directorio de trabajo aislado con la suite original de QuixBugs."""
    wd = os.path.join(workroot, name)
    shutil.rmtree(wd, ignore_errors=True)
    os.makedirs(os.path.join(wd, "python_programs"))
    shutil.copy(os.path.join(root, "conftest.py"), wd)
    shutil.copytree(os.path.join(root, "python_testcases"), os.path.join(wd, "python_testcases"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(os.path.join(root, "json_testcases"), os.path.join(wd, "json_testcases"))
    target = os.path.join("python_programs", name + ".py")
    with open(os.path.join(wd, target), "w") as f:
        f.write(buggy_source(root, name))
    return RepairTask(name=name, workdir=wd, target=target,
                      tests=[os.path.join("python_testcases", "test_%s.py" % name)])
