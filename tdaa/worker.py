"""Adaptador del protocolo de evidencia de TDAA, una línea JSON por orden."""
from __future__ import annotations

import json
import os
import sys
import time

from ._process import run_worker


def run_order(order, root, scratch):
    import pytest

    from .pytest_plugin import EvidencePlugin

    started = time.monotonic()
    plugin = EvidencePlugin(
        [os.path.join(root, target) for target in order["targets"]],
        trace=order.get("trace", False),
        test_timeout=order.get("test_timeout", 2.0),
    )
    plugin.root = root
    args = ["-q", "-p", "no:cacheprovider", "--no-header", "-o", "addopts=",
            "--basetemp", str(scratch / "pytest")]
    if order.get("fail_fast"):
        args.append("-x")
    args += order["test_args"]
    try:
        code = int(pytest.main(args, plugins=[plugin]))
    except BaseException as exc:
        code = -1
        plugin.collect_errors.append({"nodeid": "<session>", "message": repr(exc)[:500]})
    data = plugin.result(started)
    data.update(exit_code=code, status="ok")
    return data


def main():
    root = os.path.realpath(sys.argv[1])
    for line in sys.stdin:
        order = json.loads(line)
        data = run_worker({"cwd": root, "legacy_order": order,
                           "wall": float(order.get("wall_timeout", 120.0)),
                           "mem_mb": int(order.get("mem_mb", 1024))})
        if data["status"] != "ok":
            data = {"tests": [], "collect_errors": [
                        {"nodeid": "<session>", "message": data["error"]}],
                    "exit_code": -9, "wall": None,
                    "status": "killed" if data["status"] == "timeout" else "crashed"}
        print(json.dumps(data), flush=True)


if __name__ == "__main__":
    main()
