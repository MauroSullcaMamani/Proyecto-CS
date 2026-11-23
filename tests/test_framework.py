"""Pruebas unitarias del framework (pytest)."""
import os
import textwrap

import pytest

from tdaa import Agent, AgentConfig, Sandbox, generate_candidates, normalize_source, ochiai, rank_lines
from tdaa.agent import Executor, RepairTask
from tdaa.mutators import same_program, statement_lines
from tdaa.proposers import LLMProposer, MutationProposer, Observation, build_prompt, extract_code, failure_class
from tdaa.sandbox import RunResult, TestOutcome

BUGGY = textwrap.dedent('''
    def clamp(x, lo, hi):
        if x < lo:
            return lo
        if x >= hi:
            return hi
        return x

    def count_up(n):
        i = 0
        while i != n:
            i += 2
        return i
''')
TESTS = textwrap.dedent('''
    from mod import clamp
    def test_low():  assert clamp(-1, 0, 10) == 0
    def test_mid():  assert clamp(5, 0, 10) == 5
    def test_high(): assert clamp(11, 0, 10) == 10
    def test_edge(): assert clamp(9.5, 0, 10) == 9.5
    def test_fail(): assert clamp(10, 0, 5) == 7
''')


@pytest.fixture
def project(tmp_path):
    (tmp_path / "mod.py").write_text(normalize_source(BUGGY))
    (tmp_path / "test_mod.py").write_text(TESTS)
    return tmp_path


@pytest.fixture(scope="module")
def sandbox():
    with Sandbox(timeout=1.0) as sb:
        yield sb


def test_normalize_removes_docstring_and_is_idempotent():
    src = 'def f(a):\n    return a\n\n"""doc"""\n'
    n = normalize_source(src)
    assert '"""' not in n and normalize_source(n) == n


def test_same_program_ignores_formatting():
    assert same_program("def f(a):\n  return a+1\n", "def f(a):\n    return a + 1\n")
    assert not same_program("def f(a): return a+1\n", "def f(a): return a-1\n")


def test_candidates_are_unique_valid_and_cover_operators():
    src = normalize_source(BUGGY)
    cands = generate_candidates(src)
    assert len({c.source for c in cands}) == len(cands)
    assert src not in {c.source for c in cands}
    for c in cands:
        compile(c.source, "<c>", "exec")
    assert {"CMP", "CONST", "ARITH", "VAR", "DEL", "BOOL"} <= {c.op for c in cands}


def test_candidate_line_matches_edited_line():
    src = normalize_source(BUGGY)
    for c in generate_candidates(src):
        if c.op in ("CMP", "CONST", "ARITH"):
            a, b = src.splitlines(), c.source.splitlines()
            diff = [i + 1 for i, (x, y) in enumerate(zip(a, b)) if x != y]
            assert diff == [c.line]


def test_ochiai_bounds():
    assert ochiai(0, 5, 3) == 0.0
    assert ochiai(3, 0, 3) == 1.0
    assert 0 < ochiai(3, 3, 3) < 1


def test_sandbox_reports_outcomes_and_trace(project, sandbox):
    res = sandbox.run(str(project), ["test_mod.py"], target="mod.py", trace=True)
    by = {t.id.split("::")[-1]: t for t in res.tests}
    assert by["test_low"].outcome == "passed" and by["test_fail"].outcome == "failed"
    assert by["test_fail"].exc_type == "AssertionError"
    assert not res.all_passed and res.n_passed == 4
    cov = res.coverage[[k for k in res.coverage if k.endswith("test_low")][0]]
    assert 3 in cov and 6 not in cov          # test_low retorna en la linea 3


def test_sandbox_contains_infinite_loop(tmp_path, sandbox):
    (tmp_path / "mod.py").write_text("def f():\n    while True:\n        pass\n")
    (tmp_path / "test_m.py").write_text("from mod import f\ndef test_a(): f()\ndef test_b(): assert True\n")
    res = sandbox.run(str(tmp_path), ["test_m.py"], target="mod.py")
    assert res.tests[0].outcome == "timeout"
    assert res.tests[1].outcome == "notrun"     # corte tras el primer timeout
    assert not res.all_passed


def test_sandbox_contains_memory_bomb(tmp_path):
    (tmp_path / "mod.py").write_text("def f():\n    return [0] * (10 ** 10)\n")
    (tmp_path / "test_m.py").write_text("from mod import f\ndef test_a(): f()\n")
    with Sandbox(mem_mb=512) as sb:
        res = sb.run(str(tmp_path), ["test_m.py"], target="mod.py")
    assert res.tests[0].exc_type == "MemoryError"


def test_rank_lines_puts_failing_only_line_first():
    res = RunResult(tests=[TestOutcome("a", "passed"), TestOutcome("b", "failed", "AssertionError", "", 7)],
                    coverage={"a": {1: 1, 2: 1}, "b": {1: 1, 7: 1}})
    assert rank_lines(res, [1, 2, 7])[0] == 7
    assert rank_lines(res, [1, 2, 7], use_coverage=False)[0] == 7     # por traza de error


def test_failure_class():
    res = RunResult(tests=[TestOutcome("a", "timeout", "Timeout"), TestOutcome("b", "failed", "IndexError")] +
                    [TestOutcome("c", "timeout", "Timeout")])
    assert failure_class(res) == "loop"


def test_agent_fixes_bug_and_leaves_patched_file(project, sandbox):
    (project / "test_mod.py").write_text(TESTS.replace("def test_fail(): assert clamp(10, 0, 5) == 7", "def test_eq(): assert clamp(10, 0, 10) == 10\ndef test_top(): assert clamp(10.0, 0, 10) == 10.0") + "\ndef test_type(): assert type(clamp(3, 0, 3)) is int\n")
    # el defecto real: no hay; se inyecta uno
    src = normalize_source(BUGGY).replace("x >= hi", "x < hi")
    task = RepairTask("clamp", str(project), "mod.py", ["test_mod.py"])
    rep = Agent(Executor(sandbox, task)).repair(src)
    assert rep.fixed and rep.validations >= 1
    assert (project / "mod.py").read_text() == rep.patch
    assert sandbox.run(str(project), ["test_mod.py"]).all_passed


def test_agent_reverts_when_no_patch_exists(project, sandbox):
    src = normalize_source(BUGGY)       # test_fail es insatisfacible con una edicion
    task = RepairTask("clamp", str(project), "mod.py", ["test_mod.py"])
    rep = Agent(Executor(sandbox, task), config=AgentConfig(budget=15)).repair(src)
    assert not rep.fixed and rep.validations == 15
    assert (project / "mod.py").read_text() == src


def test_executor_does_not_rerun_identical_patch(project, sandbox):
    task = RepairTask("clamp", str(project), "mod.py", ["test_mod.py"])
    ex = Executor(sandbox, task)
    src = normalize_source(BUGGY)
    ex.run(src, False), ex.run(src, False)
    assert (ex.misses, ex.hits) == (1, 1)


def test_blind_and_guided_search_same_space():
    src = normalize_source(BUGGY)
    res = RunResult(tests=[TestOutcome("t", "failed", "AssertionError", "", 4)], coverage={"t": {2: 1, 4: 1}})
    obs = Observation(src, res)
    a = [c.key for c in MutationProposer(localization="none", seed=1).propose(obs)]
    b = [c.key for c in MutationProposer(localization="coverage").propose(obs)]
    assert sorted(a) == sorted(b) and a != b
    first = next(iter(MutationProposer(localization="coverage").propose(obs)))
    assert first.line == 4


def test_llm_proposer_with_fake_client(project, sandbox):
    src = normalize_source(BUGGY).replace("x >= hi", "x < hi")
    fixed = normalize_source(BUGGY).replace("x >= hi", "x > hi")
    prompts = []

    def fake(prompt):
        prompts.append(prompt)
        return "Aqui va:\n```python\n%s```" % (src if len(prompts) == 1 else fixed)   # el primer intento no arregla nada

    (project / "test_mod.py").write_text(TESTS.replace("def test_fail(): assert clamp(10, 0, 5) == 7", ""))
    task = RepairTask("clamp", str(project), "mod.py", ["test_mod.py"])
    rep = Agent(Executor(sandbox, task), LLMProposer(fake, attempts=3)).repair(src)
    assert rep.fixed and rep.validations == 2
    assert "Lineas mas sospechosas" in prompts[0] and "parche LLM intento 1" in prompts[1]


def test_extract_code():
    assert extract_code("x\n```python\na = 1\n```\ny") == "a = 1\n"
    assert extract_code("sin bloque") is None


def test_statement_lines_skip_def():
    assert 1 not in statement_lines("def f():\n    return 1\n")
