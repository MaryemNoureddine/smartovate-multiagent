import pytest
from autogen_core.models import FunctionExecutionResult, FunctionExecutionResultMessage, UserMessage

from config.mock_client import MockChatCompletionClient, pick_template, render_code
from config.model_client import create_model_client


def test_factory_mock_mode(settings):
    assert isinstance(create_model_client("codeur", settings), MockChatCompletionClient)


def test_factory_azure_requires_credentials(settings):
    settings.llm_mode = "azure"
    with pytest.raises(RuntimeError):
        create_model_client("codeur", settings)


def test_factory_azure_client_built(settings):
    from autogen_ext.models.openai import AzureOpenAIChatCompletionClient
    from pydantic import SecretStr

    settings.llm_mode = "azure"
    settings.azure_openai_endpoint = "https://exemple.openai.azure.com/"
    settings.azure_openai_api_key = SecretStr("fake")
    client = create_model_client("codeur", settings)
    assert isinstance(client, AzureOpenAIChatCompletionClient)
    assert client.model_info["function_calling"]


def test_effective_mode_auto(settings):
    from pydantic import SecretStr

    settings.llm_mode = "auto"
    assert settings.effective_llm_mode == "mock"
    settings.azure_openai_endpoint = "https://x"
    settings.azure_openai_api_key = SecretStr("k")
    assert settings.effective_llm_mode == "azure"


def test_templates():
    assert "fibonacci" in render_code("fibonacci", False)[0]
    code, _ = render_code("fibonacci", True)
    assert "raise ValueError" in code
    assert pick_template("quelque chose d'inédit")["keys"] == []


async def test_mock_codeur_tool_flow():
    client = MockChatCompletionClient("codeur", latency=0)
    from autogen_core.tools import FunctionTool
    from tools.base_tools import run_python_snippet

    tool = FunctionTool(run_python_snippet, description="x")
    res = await client.create([UserMessage(content="fibonacci", source="user")], tools=[tool])
    assert res.finish_reason == "function_calls"
    res2 = await client.create([
        UserMessage(content="fibonacci", source="user"),
        FunctionExecutionResultMessage(content=[FunctionExecutionResult(content="[0, 1]", call_id="1", name="run_python_snippet", is_error=False)]),
    ])
    assert "```python" in res2.content
    assert client.total_usage().completion_tokens > 0
    assert client.count_tokens([UserMessage(content="abcd" * 10, source="u")]) == 10
    assert client.remaining_tokens([]) == 128000
    stream = [c async for c in client.create_stream([UserMessage(content="x", source="user")])]
    assert stream[-1].content


async def test_mock_critique_rejects_pii_and_other_roles():
    crit = MockChatCompletionClient("critique", latency=0)
    res = await crit.create([UserMessage(content="```python\nEMAIL = 'a@b.fr'\n```", source="codeur")])
    assert "REJETÉ" in res.content
    assert (await MockChatCompletionClient("selector", 0).create([])).content == "orchestrateur"
    assert (await MockChatCompletionClient("autre", 0).create([])).content == "OK"
    assert "```python" in (await MockChatCompletionClient("codeur", 0).create(
        [UserMessage(content="tri", source="user")])).content
