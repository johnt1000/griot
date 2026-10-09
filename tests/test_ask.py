"""Tests for `griot ask` — dispatching of `--chat-profile` (cli.py, same
mechanism as `--profile` for embedding) and the query log passing the
active chat profile, no longer assuming Gemini (real finding from the
multi-provider chat generalization: log_query() always logged
common.CHAT_MODEL, even with a different provider active).
"""

import importlib
import types

from griot import ask, cli, common


def test_chat_profile_flag_sets_env_var_before_dispatch(monkeypatch):
    monkeypatch.delenv("GRIOT_CHAT_PROFILE", raising=False)
    calls = []
    module = importlib.import_module("griot.ask")

    def fake_main(argv=None):
        import os
        calls.append(os.environ.get("GRIOT_CHAT_PROFILE"))
        return None

    monkeypatch.setattr(module, "main", fake_main)
    rc = cli.main(["ask", "question", "--chat-profile", "openai"])

    assert rc == 0
    assert calls == ["openai"]


def test_chat_profile_flag_equal_syntax(monkeypatch):
    calls = []
    module = importlib.import_module("griot.ask")

    def fake_main(argv=None):
        import os
        calls.append(os.environ.get("GRIOT_CHAT_PROFILE"))
        return None

    monkeypatch.setattr(module, "main", fake_main)
    cli.main(["ask", "question", "--chat-profile=groq"])

    assert calls == ["groq"]


def test_chat_profile_flag_does_not_leak_into_module_argv(monkeypatch):
    calls = []
    module = importlib.import_module("griot.ask")

    def fake_main(argv=None):
        calls.append(argv)
        return None

    monkeypatch.setattr(module, "main", fake_main)
    cli.main(["ask", "question", "--chat-profile", "deepseek", "--limit", "3"])

    assert calls == [["question", "--limit", "3"]]


def test_chat_profile_flag_absent_does_not_set_env_var(monkeypatch):
    monkeypatch.delenv("GRIOT_CHAT_PROFILE", raising=False)
    calls = []
    module = importlib.import_module("griot.ask")

    def fake_main(argv=None):
        import os
        calls.append(os.environ.get("GRIOT_CHAT_PROFILE"))
        return None

    monkeypatch.setattr(module, "main", fake_main)
    cli.main(["ask", "question"])

    assert calls == [None]


def test_log_query_uses_active_chat_profile_model_not_hardcoded_gemini(monkeypatch):
    """Real finding: log_query() used common.CHAT_MODEL (always Gemini) as
    the fallback for 'model' even when a different chat profile was active
    — the log would lie about which model actually answered.

    The search finds one result: a question that finds nothing calls no chat
    model and logs none (tests/test_ask_nothing_found.py)."""
    hit = types.SimpleNamespace(payload={"repo": "alpha", "source_type": "code", "file_path": "a.py",
                                         "content": "retries"}, score=0.9)
    monkeypatch.setattr(common, "search", lambda query, limit=5, diverse=False, mode="vector", **filters: [hit])
    monkeypatch.setattr(common, "chat_completion", lambda prompt, model=None: "answer")
    monkeypatch.setattr(common, "ACTIVE_CHAT_PROFILE_NAME", "groq")
    monkeypatch.setattr(common, "ACTIVE_CHAT_PROFILE", {"model": "llama-3.3-70b-versatile", "backend": "openai_compatible_chat"})

    logged = {}
    monkeypatch.setattr(common, "log_query", lambda **fields: logged.update(fields))

    ask.main(["some question"])

    assert logged["model"] == "llama-3.3-70b-versatile"
    assert logged["chat_profile"] == "groq"


def test_log_query_omits_question_when_disabled(monkeypatch):
    """Finding M1 from the audit (2026-08-19): queries.jsonl stored the full
    question forever — questions about work repos are often sensitive.
    GRIOT_LOG_QUESTIONS=false omits the text."""
    monkeypatch.setenv("GRIOT_LOG_QUESTIONS", "false")
    monkeypatch.setattr(common, "search", lambda query, limit=5, diverse=False, mode="vector", **filters: [])
    monkeypatch.setattr(common, "chat_completion", lambda prompt, model=None: "answer")

    logged = {}
    monkeypatch.setattr(common, "log_query", lambda **fields: logged.update(fields))

    ask.main(["ultra sensitive question"])

    assert "ultra sensitive question" not in str(logged.get("question"))


def test_log_query_keeps_question_by_default(monkeypatch):
    monkeypatch.delenv("GRIOT_LOG_QUESTIONS", raising=False)
    monkeypatch.setattr(common, "search", lambda query, limit=5, diverse=False, mode="vector", **filters: [])
    monkeypatch.setattr(common, "chat_completion", lambda prompt, model=None: "answer")

    logged = {}
    monkeypatch.setattr(common, "log_query", lambda **fields: logged.update(fields))

    ask.main(["some question"])
    assert logged["question"] == "some question"


def test_log_query_includes_spend_today_usd(monkeypatch):
    """Real finding: griot stats only looked at spend_today_usd from
    runs.jsonl (indexing) — chat spend never showed up because ask.py never
    logged that field on the query, even though it's the SAME circuit
    breaker."""
    monkeypatch.setattr(common, "search", lambda query, limit=5, diverse=False, mode="vector", **filters: [])
    monkeypatch.setattr(common, "chat_completion", lambda prompt, model=None: "answer")
    monkeypatch.setattr(common, "get_spend_today", lambda: 0.0042)

    logged = {}
    monkeypatch.setattr(common, "log_query", lambda **fields: logged.update(fields))

    ask.main(["some question"])

    assert logged["spend_today_usd"] == 0.0042
