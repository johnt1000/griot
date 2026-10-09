"""griot reads a credential from the OS keychain only when a code path needs
it, and only once per process.

It used to read every known credential at import. On macOS an interpreter
the keychain items do not trust asks the person once per item read, so
every griot process (a search, the MCP server, each indexing child) raised
one password prompt per credential, whether or not it used any.

The fake keyring below counts reads per variable. The suite's own `fail`
backend (conftest.py) stays in force everywhere else; it is replaced here
per test only, with a module that never touches a real keychain.
"""

import collections
import os
import subprocess
import sys
import textwrap

import numpy as np
import pytest

from conftest import GitRepo
from griot import auth, cli, common, index_platform, platforms


class _CountingKeyring:
    """keyring's public API over a dict, recording each variable read."""

    def __init__(self, store: dict[str, str]):
        self.store = dict(store)
        self.reads: list[str] = []

    def get_password(self, service, username):
        self.reads.append(username)
        return self.store.get(username)

    def set_password(self, service, username, password):
        self.store[username] = password

    def delete_password(self, service, username):
        del self.store[username]


ALL_VARS = sorted(set(common.credential_env_vars().values()))


@pytest.fixture
def keychain(monkeypatch):
    """Every credential stored in the keychain only: nothing exported, nothing
    in the file, so each value griot uses has to come from a keychain read."""
    for var in ALL_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(common, "GEMINI_TOKEN", None)
    fake = _CountingKeyring({var: f"kc-{var.lower()}-value" for var in ALL_VARS})
    monkeypatch.setitem(sys.modules, "keyring", fake)
    return fake


# --- the accessor ---------------------------------------------------------------


def test_an_exported_value_wins_and_the_keychain_is_not_read(keychain, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "from-the-shell")

    assert common.credential("GITHUB_TOKEN") == "from-the-shell"
    assert keychain.reads == []


def test_a_keychain_value_is_read_once(keychain):
    assert common.credential("GITHUB_TOKEN") == "kc-github_token-value"
    assert common.credential("GITHUB_TOKEN") == "kc-github_token-value"
    assert keychain.reads == ["GITHUB_TOKEN"]


def test_an_absence_is_remembered_so_a_missing_key_is_not_asked_again(keychain):
    del keychain.store["GITEA_TOKEN"]

    assert common.credential("GITEA_TOKEN") is None
    assert common.credential("GITEA_TOKEN") is None
    assert keychain.reads == ["GITEA_TOKEN"]


def test_a_keychain_value_does_not_reach_the_environment(keychain):
    """Children inherit os.environ: a value read for this process is not
    handed to every process it starts. A child reads what it needs itself."""
    common.credential("GITHUB_TOKEN")

    assert "GITHUB_TOKEN" not in os.environ


def test_a_key_stored_in_this_process_is_what_the_next_read_gives(keychain):
    """`griot auth set` then a read in the same process (the doctor, auth list
    after a set) must not be answered from a remembered absence."""
    del keychain.store["GITEA_TOKEN"]
    assert common.credential("GITEA_TOKEN") is None

    assert common.keychain_set("GITEA_TOKEN", "new-value") is True

    assert common.credential("GITEA_TOKEN") == "new-value"
    assert keychain.reads == ["GITEA_TOKEN"]


def test_a_key_removed_in_this_process_is_not_given_any_more(keychain):
    assert common.credential("GITEA_TOKEN") == "kc-gitea_token-value"

    assert common.keychain_delete("GITEA_TOKEN") == common.KEYCHAIN_DELETED

    assert common.credential("GITEA_TOKEN") is None


def test_a_key_removed_elsewhere_is_not_given_after_a_remove_finds_nothing(keychain):
    """Read earlier in this process, then removed by another one: `griot auth
    remove` finding nothing stored must not leave the old value in use."""
    assert common.credential("GITEA_TOKEN") == "kc-gitea_token-value"
    del keychain.store["GITEA_TOKEN"]

    assert common.keychain_delete("GITEA_TOKEN") == common.KEYCHAIN_NOTHING_STORED

    assert common.credential("GITEA_TOKEN") is None


def test_an_openai_compatible_chat_finds_a_key_kept_only_in_the_keychain(keychain, monkeypatch):
    monkeypatch.setattr(common, "ACTIVE_CHAT_PROFILE_NAME", "groq")
    monkeypatch.setattr(common, "ACTIVE_CHAT_PROFILE", common.CHAT_PROFILES["groq"])
    sent = []

    def post(url, headers=None, json=None, **kwargs):
        sent.append(headers["Authorization"])
        return _Response({"choices": [{"message": {"content": "an answer"}}], "usage": {"total_tokens": 3}})

    monkeypatch.setattr(common, "_http_post", post)

    assert common.chat_completion("a question") == "an answer"

    assert sent == ["Bearer kc-griot_groq_api_key-value"]
    assert keychain.reads == ["GRIOT_GROQ_API_KEY"]


def test_the_gemini_chat_finds_a_token_kept_only_in_the_keychain(keychain, monkeypatch):
    """GEMINI_TOKEN is the one credential also held as a module constant
    (what the environment gave at import), so it has its own fallback."""
    assert common.ACTIVE_CHAT_PROFILE["backend"] == "gemini_native"
    sent = []

    def post(url, headers=None, json=None, **kwargs):
        sent.append(headers["x-goog-api-key"])
        return _Response({"candidates": [{"content": {"parts": [{"text": "an answer"}]}}],
                          "usageMetadata": {"totalTokenCount": 3}})

    monkeypatch.setattr(common, "_http_post", post)

    assert common.chat_completion("a question") == "an answer"
    assert common.chat_completion("another question") == "an answer"

    assert sent == ["kc-gemini_token-value"] * 2
    assert keychain.reads == ["GEMINI_TOKEN"]


# --- what each command reads ---------------------------------------------------


class _LocalModel:
    def embed(self, texts, batch_size=None):
        return [np.full(common.EMBED_DIM, (len(t) % 7 + 1) / 10.0) for t in texts]


def test_a_search_with_a_local_profile_reads_no_credential(keychain, monkeypatch, capsys):
    assert common.ACTIVE_PROFILE["backend"] == "local"
    monkeypatch.setattr(common, "get_embed_model", lambda: _LocalModel())
    common.index_documents([{"id": "r:code:a.py:0", "content": "retries with backoff",
                             "metadata": {"source_type": "code", "repo": "r", "file_path": "a.py"}}])
    common.release_lock()

    assert cli.main(["search", "retries"]) == 0

    assert "a.py" in capsys.readouterr().out
    assert keychain.reads == []


class _Response:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def test_a_search_with_the_openai_profile_reads_only_its_key_once(keychain, monkeypatch):
    monkeypatch.setattr(common, "ACTIVE_PROFILE_NAME", "openai-small")
    monkeypatch.setattr(common, "ACTIVE_PROFILE", common.EMBED_PROFILES["openai-small"])
    sent = []

    def post(url, headers=None, json=None, **kwargs):
        sent.append(headers["Authorization"])
        return _Response({"data": [{"index": i, "embedding": [0.1] * 1536} for i in range(len(json["input"]))],
                          "usage": {"total_tokens": 1}})

    monkeypatch.setattr(common, "_http_post", post)

    common.embed_texts(["first search"])
    common.embed_texts(["second search"])

    assert sent == ["Bearer kc-griot_openai_api_key-value"] * 2
    assert keychain.reads == ["GRIOT_OPENAI_API_KEY"]


def test_indexing_a_gitlab_repository_reads_only_the_gitlab_token(keychain, tmp_path, monkeypatch):
    repo = GitRepo(tmp_path / "project")
    repo.commit("first")
    monkeypatch.setattr(index_platform, "_remote_url", lambda repo_path: "https://gitlab.com/group/project.git")
    monkeypatch.setattr(index_platform.common, "index_documents", lambda documents, desc="Indexing": (0, 0, 0))
    monkeypatch.setattr(index_platform.common, "log_run_summary", lambda **fields: None)
    tokens = []

    def get(url, headers, params=None, **kwargs):
        tokens.append(headers.get("PRIVATE-TOKEN"))
        return _Response([])

    monkeypatch.setattr(platforms, "_get_with_retry", get)

    index_platform.main(["--path", str(repo.path)])

    assert tokens and set(tokens) == {"kc-gitlab_personal_access_token-value"}
    assert keychain.reads == ["GITLAB_PERSONAL_ACCESS_TOKEN"]


def test_a_github_fetch_sends_the_token_kept_only_in_the_keychain(keychain, monkeypatch):
    """The adapters that require their token (GitHub, Bitbucket, Azure DevOps,
    Gitea) read it through the same accessor."""
    sent = []

    class _Page(_Response):
        links = {}

    def get(url, headers, params=None, **kwargs):
        sent.append(headers["Authorization"])
        return _Page([])

    monkeypatch.setattr(platforms, "_get_with_retry", get)

    platforms.fetch_pull_requests("github", "group/project")

    assert sent == ["Bearer kc-github_token-value"]
    assert keychain.reads == ["GITHUB_TOKEN"]


def test_profiles_list_counts_a_key_kept_only_in_the_keychain(keychain, capsys):
    assert cli.main(["profiles", "list"]) == 0

    line = next(line for line in capsys.readouterr().out.splitlines() if line.strip().startswith("openai-small"))
    assert "configured" in line and "missing" not in line


def test_profiles_use_does_not_ask_for_a_key_kept_in_the_keychain(keychain, capsys):
    assert cli.main(["profiles", "use", "openai-small", "--yes"]) == 0

    assert "not set" not in capsys.readouterr().out
    assert keychain.reads == ["GRIOT_OPENAI_API_KEY"]


def test_choosing_a_chat_profile_does_not_ask_for_a_key_kept_in_the_keychain(keychain, monkeypatch, capsys):
    from griot import config

    monkeypatch.setattr(common, "is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail(f"asked: {prompt}"))

    assert config.main(["set", "chat-profile", "groq"]) == 0

    assert "not set" not in capsys.readouterr().out
    assert keychain.reads == ["GRIOT_GROQ_API_KEY"]


@pytest.mark.anyio
async def test_the_profiles_tool_reads_only_the_embedding_profiles_keys(keychain):
    """griot_profiles_list says whether each embedding profile has its key;
    the chat and platform keys are not its business, and each one read can be
    a prompt."""
    from mcp.client.client import Client
    from griot import mcp_server

    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_profiles_list", {})

    paid = {common.credential_env_for_profile(name, profile) for name, profile in common.EMBED_PROFILES.items()} - {None}
    assert sorted(keychain.reads) == sorted(paid)
    assert all(p["credential_configured"] for p in result.structured_content["profiles"])


def test_auth_list_reads_every_credential_once(keychain, capsys):
    assert auth.cmd_list() == 0

    counts = collections.Counter(keychain.reads)
    assert set(counts) == set(ALL_VARS)
    assert max(counts.values()) == 1
    out = capsys.readouterr().out
    assert "✗ missing" not in out


# --- a whole process -----------------------------------------------------------

# A `keyring` module placed first on PYTHONPATH, so the process imports it
# instead of any installed one: it logs each variable read to a file and
# never reaches a real keychain.
_LOGGING_KEYRING = textwrap.dedent("""
    import os
    _LOG = os.environ["GRIOT_TEST_KEYRING_LOG"]

    def get_password(service, username):
        with open(_LOG, "a") as log:
            log.write(username + "\\n")
        return "kc-" + username.lower()

    def set_password(service, username, password):
        raise RuntimeError("the test keyring stores nothing")

    def delete_password(service, username):
        raise RuntimeError("the test keyring stores nothing")
""")


@pytest.fixture
def logged_process(tmp_path):
    """Runs Python code in a new process whose `keyring` logs reads;
    returns a function giving the variables read."""
    module_dir = tmp_path / "fake_keyring"
    module_dir.mkdir()
    (module_dir / "keyring.py").write_text(_LOGGING_KEYRING)
    log = tmp_path / "keyring.log"
    log.touch()
    env = {**os.environ, "GRIOT_TEST_KEYRING_LOG": str(log),
           "PYTHONPATH": os.pathsep.join([str(module_dir), os.environ.get("PYTHONPATH", "")])}
    for var in ALL_VARS:
        env.pop(var, None)

    def run(code: str) -> list[str]:
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120)
        assert done.returncode == 0, done.stderr
        return log.read_text().split()

    return run


def test_importing_griot_reads_no_credential(logged_process):
    reads = logged_process("import griot.common, griot.cli, griot.auth, griot.platforms, griot.doctor, "
                           "griot.index_platform, griot.mcp_server")
    assert reads == []


_MCP_SESSION = textwrap.dedent("""
    import asyncio, os, sys
    from mcp.client.client import Client
    from griot import mcp_server

    log = os.environ["GRIOT_TEST_KEYRING_LOG"]

    async def main():
        async with Client(mcp_server.mcp) as client:
            await client.list_tools()
            await client.call_tool("griot_index_status", {})
            await client.call_tool("griot_spend_status", {})
            before = open(log).read().split()
            assert before == [], f"read before any tool needed a credential: {before}"
            await client.call_tool("griot_auth_guidance", {})

    asyncio.run(main())
""")


def test_an_mcp_server_reads_no_credential_until_a_tool_needs_one(logged_process):
    reads = logged_process(_MCP_SESSION)

    assert sorted(reads) == ALL_VARS
