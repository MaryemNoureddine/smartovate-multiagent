"""Exécution isolée du code généré par l'Agent Codeur (correctif du Bug 3).

Quatre modes, sélectionnés par ``CODE_EXECUTOR`` :

- ``sandbox`` (défaut) : sous-processus Python isolé (``-I``), répertoire
  temporaire, environnement vidé, limites CPU/mémoire/fichiers (Linux),
  timeout. Pratique en local et sur Azure Container Apps, mais ce n'est pas
  une isolation forte : réservé au développement.
- ``docker`` : conteneur Docker jetable (équivalent v0.4 de
  ``use_docker=True``). Nécessite un démon Docker accessible.
- ``aca`` : Azure Container Apps **Dynamic Sessions** — bac à sable managé
  Hyper-V, solution recommandée en production sur Azure (piste MAS-TK-21).
- ``disabled`` : aucune exécution.

Dans tous les modes, une analyse statique (``analyze_code_safety``) bloque
au préalable les imports et appels dangereux.
"""
from __future__ import annotations

import asyncio
import ast
import logging
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 4000

FORBIDDEN_MODULES = {
    "os", "sys", "subprocess", "shutil", "socket", "ctypes", "multiprocessing", "threading",
    "pathlib", "importlib", "pickle", "marshal", "shelve", "requests", "urllib", "http", "ftplib",
    "smtplib", "telnetlib", "signal", "resource", "builtins", "glob", "tempfile", "pty", "fcntl",
    "asyncio", "concurrent", "code", "codeop", "inspect", "gc", "webbrowser", "winreg", "_winapi",
}
FORBIDDEN_CALLS = {
    "eval", "exec", "compile", "__import__", "open", "input", "breakpoint", "globals", "locals",
    "vars", "setattr", "delattr", "getattr", "memoryview", "exit", "quit",
}


@dataclass
class ExecutionResult:
    exit_code: int
    output: str
    mode: str

    def as_text(self) -> str:
        status = "OK" if self.exit_code == 0 else f"ERREUR (code {self.exit_code})"
        return f"[exécution {self.mode} : {status}]\n{self.output}".strip()


def analyze_code_safety(code: str) -> dict:
    """Analyse statique (AST) : détecte imports, appels et attributs dangereux."""
    issues: list[str] = []
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return {"safe": False, "issues": [f"Erreur de syntaxe ligne {exc.lineno} : {exc.msg}"]}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FORBIDDEN_MODULES:
                    issues.append(f"import interdit : {alias.name} (ligne {node.lineno})")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in FORBIDDEN_MODULES:
                issues.append(f"import interdit : {node.module} (ligne {node.lineno})")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
            issues.append(f"appel interdit : {node.func.id}() (ligne {node.lineno})")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__") and node.attr.endswith("__"):
            if node.attr not in {"__name__", "__doc__", "__init__", "__str__", "__repr__", "__eq__", "__lt__"}:
                issues.append(f"accès à un attribut spécial interdit : {node.attr} (ligne {node.lineno})")
        elif isinstance(node, ast.Name) and node.id in {"__builtins__", "__loader__", "__spec__"}:
            issues.append(f"accès interdit : {node.id} (ligne {node.lineno})")
    return {"safe": not issues, "issues": issues}


def _truncate(text: str) -> str:
    if len(text) > MAX_OUTPUT_CHARS:
        return text[:MAX_OUTPUT_CHARS] + f"\n... [sortie tronquée, {len(text)} caractères]"
    return text


def _limit_resources(timeout: int) -> None:  # pragma: no cover - exécuté dans le processus enfant
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout))
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
    os.setsid()


async def _run_sandbox(code: str, timeout: int) -> ExecutionResult:
    with tempfile.TemporaryDirectory(prefix="mas_exec_") as tmp:
        script = Path(tmp) / "snippet.py"
        script.write_text(code, encoding="utf-8")
        kwargs: dict = {}
        if sys.platform != "win32":
            kwargs["preexec_fn"] = lambda: _limit_resources(timeout)
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-B", str(script),
            cwd=tmp,
            env={"PATH": "/usr/bin:/bin", "PYTHONIOENCODING": "utf-8"} if sys.platform != "win32" else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **kwargs,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return ExecutionResult(124, f"Délai d'exécution dépassé ({timeout} s)", "sandbox")
        return ExecutionResult(proc.returncode or 0, _truncate(out.decode("utf-8", "replace")), "sandbox")


async def _run_docker(code: str, settings: Settings) -> ExecutionResult:  # pragma: no cover - requiert Docker
    from autogen_core import CancellationToken
    from autogen_core.code_executor import CodeBlock
    from autogen_ext.code_executors.docker import DockerCommandLineCodeExecutor

    with tempfile.TemporaryDirectory(prefix="mas_docker_") as tmp:
        async with DockerCommandLineCodeExecutor(
            image=settings.docker_image, timeout=settings.code_timeout_seconds, work_dir=tmp
        ) as executor:
            res = await executor.execute_code_blocks([CodeBlock(code=code, language="python")], CancellationToken())
    return ExecutionResult(res.exit_code, _truncate(res.output), "docker")


async def _run_aca(code: str, settings: Settings) -> ExecutionResult:  # pragma: no cover - requiert Azure
    from autogen_core import CancellationToken
    from autogen_core.code_executor import CodeBlock
    from autogen_ext.code_executors.azure import ACADynamicSessionsCodeExecutor
    from azure.identity.aio import DefaultAzureCredential

    if not settings.aca_pool_management_endpoint:
        return ExecutionResult(1, "ACA_POOL_MANAGEMENT_ENDPOINT non configuré", "aca")
    async with DefaultAzureCredential() as credential:
        with tempfile.TemporaryDirectory(prefix="mas_aca_") as tmp:
            executor = ACADynamicSessionsCodeExecutor(
                pool_management_endpoint=settings.aca_pool_management_endpoint,
                credential=credential,
                timeout=settings.code_timeout_seconds,
                work_dir=tmp,
            )
            await executor.start()
            try:
                res = await executor.execute_code_blocks(
                    [CodeBlock(code=code, language="python")], CancellationToken()
                )
            finally:
                await executor.stop()
    return ExecutionResult(res.exit_code, _truncate(res.output), "aca")


async def execute_code(code: str, settings: Settings | None = None) -> ExecutionResult:
    """Point d'entrée unique : analyse statique puis exécution dans le mode configuré."""
    settings = settings or get_settings()
    mode = settings.code_executor
    if mode == "disabled":
        return ExecutionResult(1, "Exécution de code désactivée (CODE_EXECUTOR=disabled)", mode)

    safety = analyze_code_safety(code)
    if not safety["safe"]:
        logger.warning("Code refusé par l'analyse statique : %s", safety["issues"])
        return ExecutionResult(126, "Code refusé par l'analyse de sécurité :\n- " + "\n- ".join(safety["issues"]), mode)

    if mode == "docker":
        return await _run_docker(code, settings)
    if mode == "aca":
        return await _run_aca(code, settings)
    return await _run_sandbox(code, settings.code_timeout_seconds)
