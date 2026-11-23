"""Generadores de parches. El agente no depende de ninguno en particular.

`MutationProposer` es determinista y no usa modelos de lenguaje: enumera
ediciones del AST y las ordena con las senales de ejecucion disponibles.
`LLMProposer` construye un prompt con la misma observacion y delega en un
cliente de chat (cualquier funcion `str -> str`).
"""
from __future__ import annotations

import json
import random
import re
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Iterator, List, Optional

from .localize import rank_lines, suspiciousness
from .mutators import OPERATORS, Candidate, generate_candidates, statement_lines
from .sandbox import RunResult

# Prioridad de operadores segun la clase de falla dominante (heuristica fijada a priori).
FAILURE_PRIORS = {
    "loop": ["CMP", "BOOL", "ARITH", "CONST", "OFFBY1", "ARGSWAP", "VAR", "CALLSWAP", "DEL"],
    "bounds": ["OFFBY1", "CMP", "CONST", "ARITH", "BOOL", "VAR", "ARGSWAP", "CALLSWAP", "DEL"],
    "type": ["VAR", "ARGSWAP", "CALLSWAP", "CMP", "OFFBY1", "CONST", "ARITH", "BOOL", "DEL"],
    "value": list(OPERATORS),
}
_CLASS = {"Timeout": "loop", "RecursionError": "loop", "MemoryError": "loop",
          "IndexError": "bounds", "KeyError": "bounds", "StopIteration": "bounds", "ValueError": "bounds",
          "TypeError": "type", "AttributeError": "type", "NameError": "type", "UnboundLocalError": "type"}


def failure_class(result: RunResult) -> str:
    counts = {}
    for t in result.failing:
        c = _CLASS.get(t.exc_type or "", "value")
        counts[c] = counts.get(c, 0) + 1
    return max(counts, key=counts.get) if counts else "value"


@dataclass
class Observation:
    source: str
    result: RunResult
    memory: List[dict] = field(default_factory=list)   # parches ya probados y su resultado


@dataclass
class MutationProposer:
    localization: str = "coverage"     # none | traceback | coverage
    operator_guidance: bool = True
    seed: int = 0

    def propose(self, obs: Observation) -> Iterator[Candidate]:
        lines = statement_lines(obs.source)
        cands = generate_candidates(obs.source)
        if self.localization == "none":
            order = list(lines)
            random.Random(self.seed).shuffle(order)
        else:
            order = rank_lines(obs.result, lines, use_traceback=True,
                               use_coverage=self.localization == "coverage",
                               use_hits=self.localization == "coverage")
        pos = {ln: i for i, ln in enumerate(order)}
        ops = FAILURE_PRIORS[failure_class(obs.result)] if self.operator_guidance else OPERATORS
        oppos = {o: i for i, o in enumerate(ops)}
        cands.sort(key=lambda c: (pos.get(c.line, len(pos)), oppos[c.op], c.index))
        yield from cands


PROMPT = """Eres un agente de reparacion de programas. Corrige el archivo para que pasen todas las pruebas.
Devuelve el archivo completo corregido dentro de un unico bloque ```python.

## Archivo (con numeros de linea)
{code}

## Pruebas que fallan ({n_fail} de {n_total})
{failures}

## Lineas mas sospechosas segun la traza de ejecucion (Ochiai)
{suspects}

## Intentos anteriores que no funcionaron
{memory}
"""


def build_prompt(obs: Observation, top: int = 5) -> str:
    lines = obs.source.splitlines()
    code = "\n".join("%3d  %s" % (i + 1, l) for i, l in enumerate(lines))
    fails = "\n".join("- %s: %s en linea %s: %s" % (t.id.split("::")[-1], t.exc_type, t.exc_line, (t.exc_msg or "")[:120])
                      for t in obs.result.failing[:8]) or "(ninguna)"
    susp = suspiciousness(obs.result, statement_lines(obs.source))
    best = sorted(susp, key=lambda k: -susp[k])[:top]
    suspects = "\n".join("- linea %d (%.2f): %s" % (ln, susp[ln], lines[ln - 1].strip()) for ln in best) or "(sin traza)"
    mem = "\n".join("- %s -> pasan %s pruebas" % (m["desc"], m["n_passed"]) for m in obs.memory[-5:]) or "(ninguno)"
    return PROMPT.format(code=code, n_fail=len(obs.result.failing), n_total=len(obs.result.tests),
                         failures=fails, suspects=suspects, memory=mem)


def extract_code(reply: str) -> Optional[str]:
    m = re.search(r"```(?:python)?\s*\n(.*?)```", reply, re.S)
    return (m.group(1).rstrip() + "\n") if m else None


@dataclass
class LLMProposer:
    """Proponedor con modelo de lenguaje. `client` recibe el prompt y devuelve texto."""
    client: Callable[[str], str]
    attempts: int = 5

    def propose(self, obs: Observation) -> Iterator[Candidate]:
        for i in range(self.attempts):
            code = extract_code(self.client(build_prompt(obs)))   # obs.memory crece entre intentos
            if code:
                yield Candidate(line=0, op="LLM", desc="parche LLM intento %d" % (i + 1), source=code, index=i)


def http_chat_client(base_url: str, api_key: str, model: str, temperature: float = 0.2) -> Callable[[str], str]:
    """Cliente minimo para cualquier API compatible con /chat/completions."""
    def call(prompt: str) -> str:
        body = json.dumps({"model": model, "temperature": temperature,
                           "messages": [{"role": "user", "content": prompt}]}).encode()
        req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions", data=body,
                                     headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)["choices"][0]["message"]["content"]
    return call
