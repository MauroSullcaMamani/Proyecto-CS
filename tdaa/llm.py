"""Proponente basado en un modelo de lenguaje (API compatible con OpenAI).

Recibe exactamente la misma evidencia que el proponente por reglas, pero en
forma de texto: el nivel de retroalimentación decide cuánto de la ejecución
ve el modelo. Funciona con cualquier servicio que exponga
`POST {base_url}/chat/completions` (OpenAI, OpenRouter, Ollama, vLLM...).

    export TDAA_API_KEY=...
    python -m tdaa repair ... --llm-model MODELO --llm-base-url URL
"""
from __future__ import annotations

import difflib
import json
import os
import re
import urllib.request

from .feedback import FeedbackLevel
from .mutators import Candidate

SYSTEM = (
    "Eres un agente de reparación de programas. Recibes un archivo Python con un defecto "
    "y la evidencia obtenida al ejecutar su suite de pruebas. Devuelve el archivo completo "
    "corregido dentro de un único bloque ```python. Cambia lo mínimo necesario."
)


def first_diff_line(old: str, new: str) -> int:
    a, b = old.splitlines(), new.splitlines()
    for tag, i1, _, _, _ in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if tag != "equal":
            return i1 + 1
    return 1


class LLMProposer:
    def __init__(self, source: str, *, model: str, base_url: str = "https://api.openai.com/v1",
                 api_key: str | None = None, temperature: float = 0.2, history: int = 3,
                 timeout: float = 120.0):
        self.source = source
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or os.environ.get("TDAA_API_KEY", "")
        self.temperature = temperature
        self.history = history
        self.timeout = timeout
        self.attempts = []          # [(candidato, RunResult)]
        self.count = 0

    # -------------------------------------------------- ganchos del agente
    def start(self, baseline, level, target):
        self.baseline, self.level, self.target = baseline, FeedbackLevel(level), target

    def observe(self, candidate, run):
        self.attempts.append((candidate, run))

    def propose(self, priority):
        text = self._chat(self.build_messages(priority))
        code = self.extract_code(text)
        if code is None:
            return None
        self.count += 1
        line = first_diff_line(self.source, code)
        return Candidate(self.count, "LLM", line, line, "parche propuesto por el modelo", code)

    # ------------------------------------------------------------ prompt
    def build_messages(self, priority):
        numbered = "\n".join(f"{i:3d}  {l}" for i, l in enumerate(self.source.splitlines(), 1))
        parts = [f"Archivo `{self.target}`:\n```\n{numbered}\n```",
                 "Evidencia de la ejecución inicial:\n" + self.baseline.render(self.level)]
        if self.level >= FeedbackLevel.SPECTRUM:
            n = len(self.source.splitlines())
            ranked = sorted(range(1, n + 1), key=lambda ln: -priority(ln, ln))[:5]
            parts.append("Líneas más sospechosas según la cobertura de las pruebas: "
                         + ", ".join(map(str, ranked)))
        for cand, run in self.attempts[-self.history:]:
            diff = "\n".join(difflib.unified_diff(self.source.splitlines(),
                                                  cand.source.splitlines(), lineterm="", n=0))
            parts.append("Intento anterior (rechazado):\n```diff\n" + diff + "\n```\n"
                         + run.render(self.level))
        return [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": "\n\n".join(parts)}]

    @staticmethod
    def extract_code(text: str):
        m = re.findall(r"```(?:python)?\n(.*?)```", text or "", flags=re.S)
        return m[-1] if m else None

    def _chat(self, messages):
        body = json.dumps({"model": self.model, "messages": messages,
                           "temperature": self.temperature}).encode()
        req = urllib.request.Request(self.base_url + "/chat/completions", data=body,
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {self.api_key}"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.load(resp)
        return data["choices"][0]["message"]["content"]
