import asyncio

import pytest

from tools import base_tools
from tools.base_tools import (
    TransientToolError,
    calculate,
    check_code_safety,
    detect_pii,
    detect_pii_sync,
    redact_pii,
    robust_tool,
    run_python_snippet,
    tool_calls_var,
)
from tools.code_execution import analyze_code_safety, execute_code


async def test_calculate_ok_and_rejections():
    assert await calculate("2 + 3 * 4") == "14"
    assert await calculate("2 ** 10") == "1024"
    assert "division par zéro" in await calculate("1/0")
    assert "non autorisée" in await calculate("__import__('os').system('ls')")
    assert "exposant trop grand" in await calculate("2 ** 1000")


def test_detect_pii_types():
    text = "Contact : jean.dupont@example.com, tél +33 6 12 34 56 78, IP 192.168.1.10, carte 4111 1111 1111 1111"
    res = detect_pii_sync(text)
    assert res["contains_pii"]
    assert {"EMAIL", "TELEPHONE", "ADRESSE_IP", "CARTE_BANCAIRE"} <= set(res["types"])
    assert "jean.dupont" not in str(res["samples"])  # valeurs masquées


def test_detect_pii_clean_code_and_luhn():
    clean = "def fibonacci(n):\n    return [0, 1, 1, 2, 3, 5, 8, 13, 21, 34]\nprint(fibonacci(10))"
    assert not detect_pii_sync(clean)["contains_pii"]
    assert "CARTE_BANCAIRE" not in detect_pii_sync("numéro 1234 5678 9012 3456")["types"]


async def test_detect_pii_tool_and_redact():
    assert await detect_pii("Bonjour le monde") == "AUCUN_PII"
    assert (await detect_pii("écrire à a@b.fr")).startswith("PII_DETECTE")
    assert redact_pii("mail a@b.fr et IP 10.0.0.1") == "mail [EMAIL] et IP [ADRESSE_IP]"
    assert "[CARTE_BANCAIRE]" in redact_pii("carte 4111111111111111")
    assert "1234567890123456" in redact_pii("ref 1234567890123456")


def test_analyze_code_safety():
    assert analyze_code_safety("x = sum(range(10))\nprint(x)")["safe"]
    bad = analyze_code_safety("import os\nos.remove('x')\nopen('f','w')\n().__class__.__subclasses__()")
    assert not bad["safe"]
    assert len(bad["issues"]) >= 3
    assert not analyze_code_safety("from subprocess import run")["safe"]
    assert "syntaxe" in analyze_code_safety("def (:")["issues"][0]


async def test_check_code_safety_tool():
    assert (await check_code_safety("print(1)")).startswith("SÛR")
    assert (await check_code_safety("import shutil")).startswith("DANGEREUX")


async def test_run_python_snippet_executes_in_sandbox():
    out = await run_python_snippet("print(sum(range(5)))")
    assert "OK" in out and "10" in out


async def test_dangerous_code_is_refused_before_execution():
    res = await execute_code("import os\nos.system('rm -rf /')")
    assert res.exit_code == 126
    assert "refusé" in res.output


async def test_runtime_error_and_timeout(settings):
    res = await execute_code("raise ValueError('boom')")
    assert res.exit_code != 0 and "boom" in res.output
    settings.code_timeout_seconds = 1
    res = await execute_code("while True:\n    pass", settings)
    assert res.exit_code in (124, -9, 137) or "Délai" in res.output


async def test_execution_disabled(settings):
    settings.code_executor = "disabled"
    res = await execute_code("print(1)", settings)
    assert res.exit_code == 1 and "désactivée" in res.output


async def test_output_truncated():
    res = await execute_code("print('x' * 10000)")
    assert "tronquée" in res.output


async def test_robust_tool_retries_and_journals():
    calls = {"n": 0}

    @robust_tool(timeout=1, attempts=3)
    async def flaky(value: str) -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise TransientToolError("temporaire")
        return value.upper()

    journal: list = []
    tool_calls_var.set(journal)
    assert await flaky(value="ok") == "OK"
    assert calls["n"] == 3
    assert journal[-1]["status"] == "ok" and journal[-1]["attempts"] == 3


async def test_robust_tool_timeout_is_reported():
    @robust_tool(timeout=0.05, attempts=2)
    async def slow() -> str:
        await asyncio.sleep(1)
        return "jamais"

    journal: list = []
    tool_calls_var.set(journal)
    with pytest.raises(asyncio.TimeoutError):
        await slow()
    assert journal[-1]["status"] == "error" and journal[-1]["attempts"] == 2


def test_tools_have_schema_for_autogen():
    from autogen_core.tools import FunctionTool

    tool = FunctionTool(base_tools.run_python_snippet, description=base_tools.run_python_snippet.__doc__)
    assert "code" in tool.schema["parameters"]["properties"]
