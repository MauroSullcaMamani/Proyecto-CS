"""Operadores de edicion sobre el AST que generan parches candidatos.

Son operadores genericos de la literatura de mutacion (reemplazo de operadores
relacionales y aritmeticos, de constantes, de variables, intercambio de
argumentos, borrado de sentencias). No incluyen conocimiento de ningun benchmark.
"""
from __future__ import annotations

import ast
import copy
import hashlib
from dataclasses import dataclass
from typing import List, Optional

OPERATORS = ["CMP", "OFFBY1", "CONST", "ARITH", "BOOL", "ARGSWAP", "CALLSWAP", "VAR", "DEL"]

_CMP = [ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq]
_CMP_PAIRS = {ast.In: [ast.NotIn], ast.NotIn: [ast.In], ast.Is: [ast.IsNot], ast.IsNot: [ast.Is]}
_ARITH = [ast.Add, ast.Sub, ast.Mult, ast.FloorDiv, ast.Div, ast.Mod]
_BIT = [ast.BitAnd, ast.BitOr, ast.BitXor]
_SHIFT = [ast.LShift, ast.RShift]
_CALLSWAP = {"min": "max", "max": "min", "append": "extend", "extend": "append",
             "any": "all", "all": "any", "add": "update", "update": "add"}


@dataclass
class Candidate:
    line: int            # linea (en el fuente normalizado) que modifica
    op: str              # operador que lo genero
    desc: str            # descripcion legible del cambio
    source: str          # programa completo ya parchado
    index: int = 0       # posicion en el orden de generacion

    @property
    def key(self) -> str:
        return hashlib.sha1(self.source.encode()).hexdigest()[:16]


def normalize_source(src: str) -> str:
    """Quita docstrings de modulo y reescribe con ast.unparse (una sentencia por linea)."""
    tree = ast.parse(src)
    tree.body = [n for n in tree.body
                 if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                         and isinstance(n.value.value, str))]
    return ast.unparse(tree) + "\n"


def same_program(a: str, b: str) -> bool:
    try:
        return ast.dump(ast.parse(normalize_source(a))) == ast.dump(ast.parse(normalize_source(b)))
    except SyntaxError:
        return False


def statement_lines(src: str) -> List[int]:
    """Lineas que contienen sentencias dentro de funciones (candidatas a edicion)."""
    lines = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.stmt) and not isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)):
            lines.add(node.lineno)
    return sorted(lines)


def _scope_names(tree: ast.AST) -> List[str]:
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.arg) and node.arg not in names:
            names.append(node.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id not in names:
            names.append(node.id)
    return names


def _plus(node: ast.expr, k: int) -> ast.expr:
    return ast.BinOp(left=node, op=ast.Add() if k > 0 else ast.Sub(), right=ast.Constant(abs(k)))


def _mutations(tree: ast.Module):
    """Genera (indice_de_nodo, linea, op, desc, funcion_que_muta_la_copia)."""
    nodes = list(ast.walk(tree))
    # alcance de nombres: el de la funcion de nivel superior que contiene al nodo
    scope = {}
    for top in tree.body:
        top_names = _scope_names(top)
        for sub in ast.walk(top):
            scope[id(sub)] = top_names
    funcs = {n.name for n in nodes if isinstance(n, ast.FunctionDef)}
    parent = {}
    for p in nodes:
        for c in ast.iter_child_nodes(p):
            parent[id(c)] = p

    def line_of(n):
        while not hasattr(n, "lineno"):
            n = parent[id(n)]
        return n.lineno

    for i, n in enumerate(nodes):
        if isinstance(n, ast.Compare):
            for j, op in enumerate(n.ops):
                alts = [c for c in _CMP if not isinstance(op, c)] if type(op) in _CMP else _CMP_PAIRS.get(type(op), [])
                for alt in alts:
                    yield i, n.lineno, "CMP", "%s -> %s" % (type(op).__name__, alt.__name__), \
                        (lambda m, j=j, alt=alt: m.ops.__setitem__(j, alt()))
        if isinstance(n, (ast.BinOp, ast.AugAssign)):
            for fam in (_ARITH, _BIT, _SHIFT):
                if type(n.op) in fam:
                    for alt in fam:
                        if not isinstance(n.op, alt):
                            yield i, n.lineno, "ARITH", "%s -> %s" % (type(n.op).__name__, alt.__name__), \
                                (lambda m, alt=alt: setattr(m, "op", alt()))
        if isinstance(n, ast.Constant) and type(n.value) in (int, bool):
            if isinstance(n.value, bool):
                vals = [not n.value]
            else:
                vals = [n.value + 1, n.value - 1] + ([1 - n.value] if n.value in (0, 1) else [])
            seen = set()
            for v in vals:
                if v != n.value and v not in seen:
                    seen.add(v)
                    yield i, line_of(n), "CONST", "%r -> %r" % (n.value, v), (lambda m, v=v: setattr(m, "value", v))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "range":
            for j in range(len(n.args)):
                for k in (1, -1):
                    yield i, n.lineno, "OFFBY1", "range arg%d %+d" % (j, k), \
                        (lambda m, j=j, k=k: m.args.__setitem__(j, _plus(m.args[j], k)))
        if isinstance(n, ast.Subscript):
            if isinstance(n.slice, ast.Slice):
                for attr in ("lower", "upper"):
                    if getattr(n.slice, attr) is not None:
                        for k in (1, -1):
                            yield i, n.lineno, "OFFBY1", "slice %s %+d" % (attr, k), \
                                (lambda m, attr=attr, k=k: setattr(m.slice, attr, _plus(getattr(m.slice, attr), k)))
            elif not isinstance(n.slice, (ast.Tuple, ast.Constant)) or (
                    isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, int)):
                if not (isinstance(n.slice, ast.Constant)):
                    for k in (1, -1):
                        yield i, n.lineno, "OFFBY1", "index %+d" % k, \
                            (lambda m, k=k: setattr(m, "slice", _plus(m.slice, k)))
        if isinstance(n, (ast.If, ast.While)) and not isinstance(n.test, ast.Constant):
            yield i, n.lineno, "BOOL", "negar condicion", \
                (lambda m: setattr(m, "test", m.test.operand if isinstance(m.test, ast.UnaryOp) and isinstance(m.test.op, ast.Not)
                                   else ast.UnaryOp(op=ast.Not(), operand=m.test)))
        if isinstance(n, ast.BoolOp):
            yield i, n.lineno, "BOOL", "and <-> or", \
                (lambda m: setattr(m, "op", ast.Or() if isinstance(m.op, ast.And) else ast.And()))
            for j in range(len(n.values)):
                def drop(m, j=j):
                    vals = m.values[:j] + m.values[j + 1:]
                    if len(vals) == 1:
                        m.__class__ = vals[0].__class__
                        m.__dict__.clear()
                        m.__dict__.update(vals[0].__dict__)
                    else:
                        m.values = vals
                yield i, n.lineno, "BOOL", "quitar operando %d" % j, drop
        if isinstance(n, ast.Call):
            for j in range(len(n.args) - 1):
                if not any(isinstance(a, ast.Starred) for a in n.args):
                    yield i, n.lineno, "ARGSWAP", "intercambiar args %d,%d" % (j, j + 1), \
                        (lambda m, j=j: m.args.__setitem__(slice(j, j + 2), [m.args[j + 1], m.args[j]]))
            f = n.func
            fname = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
            if fname in _CALLSWAP:
                new = _CALLSWAP[fname]
                yield i, n.lineno, "CALLSWAP", "%s -> %s" % (fname, new), \
                    (lambda m, new=new: setattr(m.func, "id" if isinstance(m.func, ast.Name) else "attr", new))
        names = scope.get(id(n), [])
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in names and n.id not in funcs:
            for other in names:
                if other != n.id and other not in funcs:
                    yield i, n.lineno, "VAR", "%s -> %s" % (n.id, other), (lambda m, other=other: setattr(m, "id", other))
        if isinstance(n, ast.stmt) and not isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom, ast.Return, ast.Pass)):
            def delete(m):
                m.__class__ = ast.Pass
                keep = {k: v for k, v in m.__dict__.items() if k in ("lineno", "col_offset", "end_lineno", "end_col_offset")}
                m.__dict__.clear()
                m.__dict__.update(keep)
            yield i, n.lineno, "DEL", "borrar sentencia", delete


def generate_candidates(src: str, only_lines: Optional[List[int]] = None) -> List[Candidate]:
    """Devuelve todos los parches de una edicion sobre `src` (ya normalizado)."""
    tree = ast.parse(src)
    out, seen = [], {src}
    for i, line, op, desc, fn in _mutations(tree):
        if only_lines is not None and line not in only_lines:
            continue
        t2 = copy.deepcopy(tree)
        try:
            fn(list(ast.walk(t2))[i])
            ast.fix_missing_locations(t2)
            new = ast.unparse(t2) + "\n"
            compile(new, "<cand>", "exec")
        except Exception:
            continue
        if new in seen:
            continue
        seen.add(new)
        out.append(Candidate(line, op, desc, new, len(out)))
    return out
