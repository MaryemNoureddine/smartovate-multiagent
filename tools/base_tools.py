"""Outils exposés aux agents (exigence F2).

Chaque outil est typé, documenté et décoré par ``robust_tool`` qui impose :
- un timeout (30 s par défaut),
- un retry exponentiel à 3 tentatives sur les erreurs transitoires,
- la journalisation de l'entrée, de la sortie, de la durée et du statut.
"""
from __future__ import annotations

import ast
import asyncio
import functools
import logging
import operator
import re
import time
from contextvars import ContextVar
from typing import Any, Awaitable, Callable, TypeVar

from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from core.observability import log_event
from tools.code_execution import analyze_code_safety, execute_code

logger = logging.getLogger(__name__)

# Journal des appels d'outils de la tâche courante (rempli par robust_tool).
tool_calls_var: ContextVar[list[dict] | None] = ContextVar("tool_calls", default=None)

T = TypeVar("T")


class TransientToolError(Exception):
    """Erreur temporaire justifiant un nouvel essai."""


def robust_tool(timeout: float = 30.0, attempts: int = 3) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            start = time.perf_counter()
            status, result, attempt_no = "error", None, 0
            try:
                async for attempt in AsyncRetrying(
                    stop=stop_after_attempt(attempts),
                    wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
                    retry=retry_if_exception_type((TransientToolError, ConnectionError, asyncio.TimeoutError)),
                    reraise=True,
                ):
                    with attempt:
                        attempt_no = attempt.retry_state.attempt_number
                        result = await asyncio.wait_for(func(*args, **kwargs), timeout=timeout)
                status = "ok"
                return result  # type: ignore[return-value]
            finally:
                duration_ms = round((time.perf_counter() - start) * 1000, 1)
                entry = {
                    "tool": func.__name__,
                    "input": {k: str(v)[:500] for k, v in kwargs.items()} or [str(a)[:500] for a in args],
                    "output": str(result)[:1000] if result is not None else None,
                    "status": status,
                    "attempts": attempt_no,
                    "duration_ms": duration_ms,
                    "ts": time.time(),
                }
                calls = tool_calls_var.get()
                if calls is not None:
                    calls.append(entry)
                log_event(logger, f"outil {func.__name__} : {status}", tool=func.__name__, status=status,
                          duration_ms=duration_ms, attempts=attempt_no)

        return wrapper

    return decorator


# --------------------------------------------------------------------------- calcul
_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
    ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def _safe_eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _safe_eval(node.left), _safe_eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 100:
            raise ValueError("exposant trop grand")
        return _OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError("expression non autorisée")


def calculate_sync(expression: str) -> str:
    try:
        return str(_safe_eval(ast.parse(expression, mode="eval")))
    except ZeroDivisionError:
        return "Erreur : division par zéro"
    except (ValueError, SyntaxError) as exc:
        return f"Erreur : {exc}"


@robust_tool(timeout=5)
async def calculate(expression: str) -> str:
    """Évalue une expression arithmétique (+ - * / // % **) de façon sûre, sans eval()."""
    return calculate_sync(expression)


# --------------------------------------------------------------------------- PII
PII_PATTERNS: dict[str, re.Pattern[str]] = {
    "EMAIL": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    "TELEPHONE": re.compile(r"(?<!\w)(?:\+\d{1,3}[\s.-]?)?(?:\(?\d{1,4}\)?[\s.-]?){2,4}\d{2,4}(?!\w)"),
    "IBAN": re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}(?:\s?[A-Z0-9]{1,4})?\b"),
    "CARTE_BANCAIRE": re.compile(r"\b(?:\d[ -]?){13,19}\b"),
    "ADRESSE_IP": re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"),
    "CLE_API": re.compile(r"\b(?:[A-Fa-f0-9]{32}|[A-Za-z0-9]{84}|sk-[A-Za-z0-9_-]{20,})\b"),
    "NUMERO_SECU_FR": re.compile(r"\b[12]\s?\d{2}\s?(?:0[1-9]|1[0-2])\s?\d{2}\s?\d{3}\s?\d{3}\s?\d{2}\b"),
}


def _is_phone(value: str) -> bool:
    return 9 <= sum(c.isdigit() for c in value) <= 13


def _luhn_ok(digits: str) -> bool:
    nums = [int(c) for c in digits if c.isdigit()]
    if not 13 <= len(nums) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(nums)):
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def detect_pii_sync(text: str) -> dict:
    """Détecte les données personnelles / secrets. Retourne types et extraits masqués."""
    found: dict[str, list[str]] = {}
    for kind, pattern in PII_PATTERNS.items():
        for match in pattern.findall(text):
            value = match if isinstance(match, str) else "".join(match)
            if kind == "CARTE_BANCAIRE" and not _luhn_ok(value):
                continue
            if kind == "TELEPHONE" and not _is_phone(value):
                continue
            masked = value[:2] + "***" + value[-2:] if len(value) > 6 else "***"
            found.setdefault(kind, []).append(masked)
    return {"contains_pii": bool(found), "types": sorted(found), "samples": found}


def redact_pii(text: str) -> str:
    """Remplace les données personnelles détectées par un marqueur [TYPE]."""
    order = ["CLE_API", "IBAN", "CARTE_BANCAIRE", "NUMERO_SECU_FR", "EMAIL", "ADRESSE_IP", "TELEPHONE"]
    for kind in order:
        pattern = PII_PATTERNS[kind]
        if kind == "CARTE_BANCAIRE":
            text = pattern.sub(lambda m: f"[{kind}]" if _luhn_ok(m.group(0)) else m.group(0), text)
        elif kind == "TELEPHONE":
            text = pattern.sub(lambda m: f"[{kind}]" if _is_phone(m.group(0)) else m.group(0), text)
        else:
            text = pattern.sub(f"[{kind}]", text)
    return text


@robust_tool(timeout=5)
async def detect_pii(text: str) -> str:
    """Analyse un texte et indique s'il contient des données personnelles (email, téléphone, IBAN, carte, IP, clé d'API...)."""
    res = detect_pii_sync(text)
    if not res["contains_pii"]:
        return "AUCUN_PII"
    return "PII_DETECTE : " + ", ".join(res["types"])


# --------------------------------------------------------------------------- code
@robust_tool(timeout=10)
async def check_code_safety(code: str) -> str:
    """Analyse statiquement un code Python et liste les instructions dangereuses (imports système, eval, open...)."""
    res = analyze_code_safety(code)
    if res["safe"]:
        return "SÛR : aucune instruction dangereuse détectée"
    return "DANGEREUX :\n- " + "\n- ".join(res["issues"])


@robust_tool(timeout=45)
async def run_python_snippet(code: str) -> str:
    """Exécute un script Python autonome dans un environnement isolé et retourne sa sortie standard.
    Le script doit afficher (print) les résultats à vérifier. Imports système, réseau et fichiers interdits."""
    result = await execute_code(code)
    return result.as_text()
