import pytest
from autogen_agentchat.messages import TextMessage

from agents.team import build_team, make_selector
from agents.termination import RepetitionTermination, ReviewLoopTermination
from autogen_agentchat.base import TerminatedException


def msg(source, content):
    return TextMessage(source=source, content=content)


def test_selector_rules():
    sel = make_selector(human_in_the_loop=False)
    assert sel([]) == "orchestrateur"
    assert sel([msg("user", "tâche")]) == "orchestrateur"
    assert sel([msg("orchestrateur", "plan")]) == "codeur"
    assert sel([msg("codeur", "code")]) == "reviseur"
    assert sel([msg("reviseur", "CORRECTIONS DEMANDÉES : 1.")]) == "codeur"
    assert sel([msg("reviseur", "RAS\nAPPROUVÉ")]) == "critique"
    assert sel([msg("reviseur", "hmm")]) == "codeur"
    assert sel([msg("critique", "VERDICT : VALIDÉ")]) == "orchestrateur"
    assert sel([msg("critique", "VERDICT : REJETÉ")]) == "orchestrateur"
    assert sel([msg("inconnu", "?")]) is None
    sel_h = make_selector(human_in_the_loop=True)
    assert sel_h([msg("critique", "VERDICT : VALIDÉ")]) == "validateur_humain"
    assert sel_h([msg("validateur_humain", "APPROUVÉ HUMAIN")]) == "orchestrateur"


async def test_review_loop_termination():
    cond = ReviewLoopTermination(max_rounds=1)
    assert await cond([msg("reviseur", "CORRECTIONS DEMANDÉES : a")]) is None
    stop = await cond([msg("reviseur", "CORRECTIONS DEMANDÉES : b")])
    assert stop and "EARLY_STOP" in stop.content and cond.terminated
    with pytest.raises(TerminatedException):
        await cond([msg("codeur", "x")])
    await cond.reset()
    assert not cond.terminated


async def test_repetition_termination():
    cond = RepetitionTermination()
    assert await cond([msg("codeur", "même   réponse")]) is None
    assert await cond([msg("reviseur", "APPROUVÉ")]) is None
    assert await cond([msg("codeur", "nouvelle version")]) is None
    assert await cond([msg("reviseur", "APPROUVÉ")]) is None  # même revue sur un code différent : OK
    stop = await cond([msg("codeur", "nouvelle  version")])    # cycle reviseur/codeur identique
    assert stop and "boucle" in stop.content and "reviseur -> codeur" in stop.content
    with pytest.raises(TerminatedException):
        await cond([msg("codeur", "x")])
    await cond.reset()
    assert not cond.terminated
    assert await cond([msg("a", "x")]) is None
    assert (await cond([msg("a", "x")])) is not None


@pytest.mark.parametrize("pattern", ["selector", "sequential", "nested"])
async def test_full_team_patterns_reach_terminate(settings, pattern):
    team = build_team(settings, pattern=pattern)
    result = await team.run(task="Écris une fonction Python qui calcule la suite de Fibonacci")
    assert "TERMINATE" in result.stop_reason
    texts = [m.to_text() for m in result.messages if hasattr(m, "to_text")]
    assert any("VERDICT : VALIDÉ" in t for t in texts)
    assert "def fibonacci" in texts[-1]
    sources = {m.source for m in result.messages}
    assert {"orchestrateur", "critique"} <= sources


async def test_team_with_human_in_the_loop(settings):
    settings.human_in_the_loop = True
    team = build_team(settings, pattern="selector")
    result = await team.run(task="Écris une fonction factorielle")
    assert "TERMINATE" in result.stop_reason
    assert any(m.source == "validateur_humain" for m in result.messages)


async def test_max_messages_guard(settings):
    settings.max_messages = 3
    result = await build_team(settings).run(task="Écris une fonction palindrome")
    assert "Maximum number of messages" in result.stop_reason
