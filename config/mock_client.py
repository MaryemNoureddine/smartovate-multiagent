"""Client LLM simulé (mode ``LLM_MODE=mock``).

Reproduit de façon déterministe le comportement attendu de chaque agent
(plan, génération de code avec appel d'outil, revue avec une itération de
correction, validation critique, consolidation + TERMINATE). Il permet de
faire tourner toute la chaîne — API, UI, mémoire, tests — sans Azure ni coût.
Le code produit est illustratif : seul le mode ``azure`` traite réellement
des demandes arbitraires.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from typing import Any, AsyncGenerator, Literal, Mapping, Optional, Sequence, Union

from autogen_core import CancellationToken, FunctionCall
from autogen_core.models import (
    AssistantMessage,
    ChatCompletionClient,
    CreateResult,
    FunctionExecutionResultMessage,
    LLMMessage,
    ModelFamily,
    ModelInfo,
    RequestUsage,
    SystemMessage,
    UserMessage,
)
from autogen_core.tools import Tool, ToolSchema
from pydantic import BaseModel

CODE_BLOCK_RE = re.compile(r"```python\n(.*?)```", re.DOTALL)

VALIDATION_MARKER = "# validation des entrées"

# (mots-clés, nom de fonction, signature, corps, validation, test)
TEMPLATES: list[dict[str, Any]] = [
    {
        "keys": ["fibonacci"],
        "code": '''def fibonacci(n: int) -> list[int]:
    """Retourne les n premiers termes de la suite de Fibonacci."""
{validation}    suite: list[int] = []
    a, b = 0, 1
    for _ in range(n):
        suite.append(a)
        a, b = b, a + b
    return suite
''',
        "validation": '''    {marker}
    if not isinstance(n, int) or isinstance(n, bool):
        raise TypeError("n doit être un entier")
    if n < 0:
        raise ValueError("n doit être positif ou nul")
''',
        "test": "print(fibonacci(10))",
    },
    {
        "keys": ["palindrome"],
        "code": '''def est_palindrome(texte: str) -> bool:
    """Indique si un texte est un palindrome (casse, espaces et ponctuation ignorés)."""
{validation}    nettoye = [c.lower() for c in texte if c.isalnum()]
    return nettoye == nettoye[::-1]
''',
        "validation": '''    {marker}
    if not isinstance(texte, str):
        raise TypeError("texte doit être une chaîne")
''',
        "test": "print(est_palindrome('Esope reste ici et se repose'), est_palindrome('Smartovate'))",
    },
    {
        "keys": ["factoriel", "factorial"],
        "code": '''def factorielle(n: int) -> int:
    """Calcule n! de façon itérative."""
{validation}    resultat = 1
    for i in range(2, n + 1):
        resultat *= i
    return resultat
''',
        "validation": '''    {marker}
    if not isinstance(n, int) or isinstance(n, bool):
        raise TypeError("n doit être un entier")
    if n < 0:
        raise ValueError("la factorielle n'est pas définie pour n < 0")
''',
        "test": "print(factorielle(0), factorielle(5), factorielle(10))",
    },
    {
        "keys": ["fusion", "merge", "fusionn"],
        "code": '''def fusionner_listes_triees(a: list[int], b: list[int]) -> list[int]:
    """Fusionne deux listes triées en une seule liste triée (O(n + m))."""
{validation}    i = j = 0
    resultat: list[int] = []
    while i < len(a) and j < len(b):
        if a[i] <= b[j]:
            resultat.append(a[i]); i += 1
        else:
            resultat.append(b[j]); j += 1
    resultat.extend(a[i:])
    resultat.extend(b[j:])
    return resultat
''',
        "validation": '''    {marker}
    if not isinstance(a, list) or not isinstance(b, list):
        raise TypeError("a et b doivent être des listes")
''',
        "test": "print(fusionner_listes_triees([1, 4, 9], [2, 3, 10, 12]))",
    },
    {
        "keys": ["tri", "trier", "sort"],
        "code": '''def trier_dictionnaires(elements: list[dict], cle: str, decroissant: bool = False) -> list[dict]:
    """Trie une liste de dictionnaires selon une clé donnée."""
{validation}    return sorted(elements, key=lambda d: d[cle], reverse=decroissant)
''',
        "validation": '''    {marker}
    if not isinstance(elements, list):
        raise TypeError("elements doit être une liste")
    manquants = [e for e in elements if cle not in e]
    if manquants:
        raise KeyError(f"clé '{{cle}}' absente de {{len(manquants)}} élément(s)")
''',
        "test": "print(trier_dictionnaires([{'nom': 'B', 'age': 31}, {'nom': 'A', 'age': 24}], 'age'))",
    },
    {
        "keys": ["premier", "prime"],
        "code": '''def est_premier(n: int) -> bool:
    """Teste si n est un nombre premier."""
{validation}    if n < 2:
        return False
    i = 2
    while i * i <= n:
        if n % i == 0:
            return False
        i += 1
    return True
''',
        "validation": '''    {marker}
    if not isinstance(n, int) or isinstance(n, bool):
        raise TypeError("n doit être un entier")
''',
        "test": "print([k for k in range(30) if est_premier(k)])",
    },
]

GENERIC_TEMPLATE: dict[str, Any] = {
    "keys": [],
    "code": '''def statistiques(valeurs: list[float]) -> dict[str, float]:
    """Calcule des statistiques descriptives simples (exemple générique du mode simulation)."""
{validation}    n = len(valeurs)
    moyenne = sum(valeurs) / n
    triees = sorted(valeurs)
    mediane = triees[n // 2] if n % 2 else (triees[n // 2 - 1] + triees[n // 2]) / 2
    return {{"n": n, "min": triees[0], "max": triees[-1], "moyenne": moyenne, "mediane": mediane}}
''',
    "validation": '''    {marker}
    if not valeurs:
        raise ValueError("la liste ne doit pas être vide")
''',
    "test": "print(statistiques([3, 1, 4, 1, 5, 9, 2, 6]))",
}


def _text(msg: LLMMessage) -> str:
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            parts.append(getattr(c, "content", None) or getattr(c, "arguments", None) or str(c))
        return "\n".join(str(p) for p in parts)
    return str(content)


def _source(msg: LLMMessage) -> str:
    return getattr(msg, "source", "") or ""


def pick_template(task: str) -> dict[str, Any]:
    low = task.lower()
    for tpl in TEMPLATES:
        if any(k in low for k in tpl["keys"]):
            return tpl
    return GENERIC_TEMPLATE


def render_code(task: str, with_validation: bool) -> tuple[str, str]:
    tpl = pick_template(task)
    validation = tpl["validation"].format(marker=VALIDATION_MARKER) if with_validation else ""
    code = tpl["code"].format(validation=validation) if "{validation}" in tpl["code"] else tpl["code"]
    return code, tpl["test"]


class MockChatCompletionClient(ChatCompletionClient):
    """Client simulé dont la réponse dépend du rôle de l'agent et de l'historique."""

    def __init__(self, role: str = "generic", latency: float | None = None) -> None:
        self.role = role
        self.latency = float(os.getenv("MOCK_LATENCY_SECONDS", "0.4")) if latency is None else latency
        self._model_info = ModelInfo(
            vision=False, function_calling=True, json_output=False, structured_output=False, family=ModelFamily.UNKNOWN
        )
        self._cur = RequestUsage(prompt_tokens=0, completion_tokens=0)
        self._total = RequestUsage(prompt_tokens=0, completion_tokens=0)
        self.calls = 0

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _count(text: str) -> int:
        return max(1, len(text) // 4)

    @staticmethod
    def _task(messages: Sequence[LLMMessage]) -> str:
        for m in messages:
            if isinstance(m, UserMessage) and _source(m) == "user":
                return _text(m)
        for m in messages:
            if isinstance(m, UserMessage):
                return _text(m)
        return ""

    @staticmethod
    def _last_code(messages: Sequence[LLMMessage]) -> str | None:
        for m in reversed(messages):
            blocks = CODE_BLOCK_RE.findall(_text(m))
            if blocks:
                return blocks[-1]
        return None

    def _respond(self, messages: Sequence[LLMMessage], tools: Sequence[Tool | ToolSchema]) -> str | list[FunctionCall]:
        task = self._task(messages)
        history = [(_source(m), _text(m)) for m in messages if not isinstance(m, SystemMessage)]
        all_text = "\n".join(t for _, t in history)
        tool_names = {getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None) for t in tools}

        if self.role == "selector":
            return "orchestrateur"

        if self.role == "orchestrateur":
            signal = None
            for _, text in reversed(history):
                if "REJET HUMAIN" in text or "VERDICT : REJETÉ" in text:
                    signal = "rejet"
                    break
                if "VERDICT : VALIDÉ" in text or "APPROUVÉ HUMAIN" in text:
                    signal = "valide"
                    break
            if signal == "valide":
                code = self._last_code(messages) or ""
                return (
                    "## Résultat final consolidé\n\n"
                    f"Demande : *{task.strip()[:200]}*\n\n"
                    "La solution a été produite par l'Agent Codeur, revue et approuvée par l'Agent Réviseur, "
                    "puis validée par l'Agent Critique (aucune donnée personnelle détectée).\n\n"
                    f"```python\n{code}```\n\n"
                    "TERMINATE"
                )
            if signal == "rejet":
                return (
                    "Instructions correctives pour le Codeur : appliquer l'ensemble des remarques "
                    "(Réviseur, Critique ou validateur humain) et proposer une nouvelle version."
                )
            return (
                "## Plan d'action\n"
                f"Analyse de la demande : *{task.strip()[:200]}*\n\n"
                "1. **Codeur** : écrire une fonction Python typée et documentée, puis l'exécuter sur un exemple.\n"
                "2. **Réviseur** : vérifier la correction, la gestion des cas limites et la sécurité du code.\n"
                "3. **Critique** : valider la qualité globale et l'absence de données personnelles.\n"
                "4. **Orchestrateur** : consolider la réponse finale.\n\n"
                "Codeur, à toi."
            )

        if self.role == "codeur":
            corrections = all_text.count("CORRECTIONS DEMANDÉES")
            with_validation = corrections > 0
            code, test = render_code(task, with_validation)
            human_rejections = all_text.count("REJET HUMAIN")
            if human_rejections:
                code = f"# Révision {human_rejections} suite au retour du validateur humain\n" + code
            last = messages[-1] if messages else None
            if isinstance(last, FunctionExecutionResultMessage):
                output = _text(last).strip()
                header = "Version corrigée (validation des entrées ajoutée)" if with_validation else "Première version"
                return (
                    f"{header} :\n\n```python\n{code}```\n\n"
                    f"Exécution de contrôle dans le bac à sable :\n```\n{output}\n```"
                )
            if "run_python_snippet" in tool_names:
                return [
                    FunctionCall(
                        id=f"call_{uuid.uuid4().hex[:8]}",
                        name="run_python_snippet",
                        arguments=json.dumps({"code": f"{code}\n{test}\n"}),
                    )
                ]
            return f"```python\n{code}```"

        if self.role == "reviseur":
            code = self._last_code(messages) or ""
            if VALIDATION_MARKER in code or "raise " in code:
                return (
                    "Revue du code :\n- Typage et docstring : OK\n- Cas limites et validation des entrées : OK\n"
                    "- Sécurité : aucune instruction dangereuse détectée\n\nAPPROUVÉ"
                )
            return (
                "CORRECTIONS DEMANDÉES :\n"
                "1. Ajouter une validation explicite des entrées (type et domaine de valeurs).\n"
                "2. Lever des exceptions explicites (`TypeError` / `ValueError`) plutôt que de laisser échouer silencieusement."
            )

        if self.role == "critique":
            from tools.base_tools import detect_pii_sync

            last_review = next((t for s, t in reversed(history) if s == "reviseur"), "")
            if "CORRECTIONS DEMANDÉES" in last_review:
                return (
                    "VERDICT : REJETÉ\nPII : AUCUN_PII\n"
                    "Qualité : les corrections demandées par le Réviseur n'ont pas encore été appliquées."
                )
            code = self._last_code(messages) or ""
            pii = detect_pii_sync(code)
            if pii["contains_pii"]:
                return f"VERDICT : REJETÉ\nPII : {', '.join(pii['types'])} détecté(s). Retirer ces données avant publication."
            return (
                "VERDICT : VALIDÉ\nPII : AUCUN_PII\n"
                "Qualité : le code répond à la demande, est lisible, typé et gère les cas limites."
            )

        if self.role == "atelier":
            code = self._last_code(messages) or ""
            return f"Solution produite et approuvée par l'atelier de développement :\n\n```python\n{code}```"

        return "OK"

    # ------------------------------------------------------------- interface
    async def create(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[Tool | ToolSchema] = [],
        tool_choice: Tool | Literal["auto", "required", "none"] = "auto",
        json_output: Optional[bool | type[BaseModel]] = None,
        extra_create_args: Mapping[str, Any] = {},
        cancellation_token: Optional[CancellationToken] = None,
    ) -> CreateResult:
        if self.latency:
            await asyncio.sleep(self.latency)
        self.calls += 1
        content = self._respond(messages, tools)
        prompt_tokens = sum(self._count(_text(m)) for m in messages)
        completion_tokens = self._count(content if isinstance(content, str) else json.dumps([c.arguments for c in content]))
        self._cur = RequestUsage(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
        self._total = RequestUsage(
            prompt_tokens=self._total.prompt_tokens + prompt_tokens,
            completion_tokens=self._total.completion_tokens + completion_tokens,
        )
        return CreateResult(
            finish_reason="function_calls" if isinstance(content, list) else "stop",
            content=content,
            usage=self._cur,
            cached=False,
        )

    async def create_stream(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[Tool | ToolSchema] = [],
        tool_choice: Tool | Literal["auto", "required", "none"] = "auto",
        json_output: Optional[bool | type[BaseModel]] = None,
        extra_create_args: Mapping[str, Any] = {},
        cancellation_token: Optional[CancellationToken] = None,
    ) -> AsyncGenerator[Union[str, CreateResult], None]:
        result = await self.create(messages, tools=tools, cancellation_token=cancellation_token)
        yield result

    async def close(self) -> None:
        return None

    def actual_usage(self) -> RequestUsage:
        return self._cur

    def total_usage(self) -> RequestUsage:
        return self._total

    def count_tokens(self, messages: Sequence[LLMMessage], *, tools: Sequence[Tool | ToolSchema] = []) -> int:
        return sum(self._count(_text(m)) for m in messages)

    def remaining_tokens(self, messages: Sequence[LLMMessage], *, tools: Sequence[Tool | ToolSchema] = []) -> int:
        return 128000 - self.count_tokens(messages)

    @property
    def capabilities(self) -> Any:  # pragma: no cover - API dépréciée
        return self._model_info

    @property
    def model_info(self) -> ModelInfo:
        return self._model_info


__all__ = ["MockChatCompletionClient", "render_code", "pick_template", "AssistantMessage"]
