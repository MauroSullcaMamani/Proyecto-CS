"""TDAA: Test-Driven Autonomous Agents.

Framework de reparacion automatica de programas guiado por ejecucion.
El agente observa la suite de pruebas, localiza la falla con las trazas,
propone parches, los valida en un entorno aislado y revierte los que fallan.
"""
from .sandbox import Sandbox, RunResult, TestOutcome
from .localize import rank_lines, ochiai
from .mutators import generate_candidates, normalize_source, Candidate
from .agent import Agent, AgentConfig, RepairTask, RepairReport

__version__ = "0.1.0"
__all__ = ["Sandbox", "RunResult", "TestOutcome", "rank_lines", "ochiai",
           "generate_candidates", "normalize_source", "Candidate",
           "Agent", "AgentConfig", "RepairTask", "RepairReport"]
