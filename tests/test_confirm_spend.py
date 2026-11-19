"""Confirmation before spending (decided 2026-11-19): griot asks only before
what is irreversible or spends money at or above GRIOT_CONFIRM_SPEND_USD
(default $0.50, estimated). Below it, the estimate is said and the run goes
on; a local profile never asks and says nothing about cost.

- `griot index all` asks ONCE, for the sum of every source, before the first
  source embeds; `griot index <source>` asks for that source.
- Without a terminal, at or above the threshold: exit 2, nothing embedded,
  and the command with --yes as the last line.
- `griot quality-check --from-history` follows the same rule.
- The MCP side has no new dialog: griot_quality_check's result carries `paid`
  and its estimate; griot_index_repo's question names the estimate."""

import importlib
import json
import os
import pty
import select
import subprocess
import sys
from pathlib import Path

import pytest
from mcp.client.client import Client

from griot import cli, common, config, doctor, jobs, logdb, mcp_server, quality_check

PAID = {"backend": "openai_compatible", "price_per_1m_tokens": 3_500_000.0}  # $1 per character, estimated
DOCS_CONFIG = Path(__file__).resolve().parent.parent / "docs" / "configuration.md"


# --- the setting -------------------------------------------------------------------------------


def test_the_setting_is_in_the_template_written_out_never_empty():
    entry = next(e for e in common.ENV_TEMPLATE_SETTINGS if e[0] == "GRIOT_CONFIRM_SPEND_USD")
    assert entry[1] == "0.50" and entry[3] is False, "an empty VAR= would be float('') on every command"
    assert "--yes" in entry[2]
    assert common.CONFIRM_SPEND_USD == 0.5


def test_the_setting_is_known_to_griot_config_and_listed_in_the_docs():
    setting = config.find("confirm-spend")
    assert setting is not None and setting.variable == "GRIOT_CONFIRM_SPEND_USD"
    assert setting.kind == "threshold"
    assert config.find("GRIOT_CONFIRM_SPEND_USD") is setting
    assert "`GRIOT_CONFIRM_SPEND_USD`" in DOCS_CONFIG.read_text()


def test_raising_the_threshold_asks_and_lowering_it_does_not():
    setting = config.find("confirm-spend")
    question = config._question(setting, "0.50", "5")
    assert question == "Raise confirm-spend from 0.50 to 5? A paid run estimated below $5 then starts without asking."
    # A configured limit is a known value: never rounded to cents (to zero).
    assert config._question(setting, "0", "0.0004") == ("Raise confirm-spend from 0 to 0.0004? A paid run estimated "
                                                        "below $0.0004 then starts without asking.")
    assert config._question(setting, "0.50", "0.10") is None
    assert config._question(setting, "0.50", "0.50") is None


def test_a_server_environment_cannot_raise_the_threshold(monkeypatch):
    monkeypatch.setattr(config, "persisted", lambda setting: "0.50")
    reason, _ = config.measured_from_environment(config.find("confirm-spend"), "100")
    assert reason == "it raises what a paid run may cost without asking"
    reason, _ = config.measured_from_environment(config.find("confirm-spend"), "0.10")
    assert reason is None


@pytest.mark.parametrize("estimate,asks", [(0.49, False), (0.50, True), (0.51, True), (None, True)])
def test_the_threshold_comparison(monkeypatch, estimate, asks):
    monkeypatch.setattr(common, "CONFIRM_SPEND_USD", 0.50)
    assert common.spend_needs_asking(estimate) is asks


@pytest.mark.parametrize("amount,text", [(1.84, "$1.84"), (0.01, "$0.01"), (0.0042, "$0.004"),
                                         (0.0001, "under $0.001"), (12345.6, "$12,345.60")])
def test_one_amount_format(amount, text):
    assert common.format_usd(amount) == text


@pytest.mark.parametrize("amount,text", [(1.84, "about $1.84"), (0.0042, "about $0.004"),
                                         (0.0001, "under $0.001"), (0.0, "about $0.00")])
def test_an_estimate_reads_as_one_phrase(amount, text):
    """"about under $0.001" is not English: the phrase carries its own
    qualifier, and every surface uses it."""
    assert common.about_usd(amount) == text


@pytest.mark.parametrize("estimate", [0.00001234, 0.0000001, 2.5e-7])
def test_a_paid_estimate_is_never_rounded_to_free(estimate):
    """An agent reading 0.0 reads a paid run as free."""
    assert common.estimate_rounded(estimate) > 0
    assert common.estimate_rounded(estimate) == pytest.approx(estimate, rel=0.05)


def test_a_rounded_estimate_keeps_four_decimals_and_none_and_zero():
    assert common.estimate_rounded(1.234567) == 1.2346
    assert common.estimate_rounded(0.0) == 0.0
    assert common.estimate_rounded(None) is None


def test_paid_is_one_definition():
    assert common.profile_is_paid({"backend": "local"}) is False
    assert common.profile_is_paid({"backend": "openai_compatible"}) is True
    assert common.profile_is_paid({"backend": "direct"}) is True


# --- common.confirm without a terminal names the command ---------------------------------------


def test_confirm_without_a_terminal_ends_with_the_command_and_its_yes(monkeypatch, capsys):
    monkeypatch.setattr(common, "is_interactive", lambda: False)
    assert common.confirm("Q?", what="doing x would cost money", command="griot index all --repo r") == 2
    err = capsys.readouterr().err
    assert err.startswith("Error: doing x would cost money, and there is no terminal to ask on. Nothing was changed.")
    assert err.rstrip().splitlines()[-1] == "  griot index all --repo r --yes"


# --- griot index -------------------------------------------------------------------------------


@pytest.fixture
def embedded(monkeypatch):
    """Every text embedded, in order. Nothing reaches an API."""
    texts = []

    def fake(batch, **kwargs):
        texts.extend(batch)
        return [[0.1] * common.EMBED_DIM for _ in batch]
    monkeypatch.setattr(common, "embed_texts", fake)
    return texts


@pytest.fixture
def repo(git_repo):
    git_repo.commit("Add the parser", filename="parser.py", content="def parse(text):\n    return text.split()\n")
    common.REPOS_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    common.REPOS_JSON_PATH.write_text(json.dumps([str(git_repo.path)]))
    return git_repo


def _paid(monkeypatch, threshold=0.50, price=PAID["price_per_1m_tokens"]):
    monkeypatch.setattr(common, "ACTIVE_PROFILE", {**common.ACTIVE_PROFILE, **PAID, "price_per_1m_tokens": price})
    monkeypatch.setattr(common, "ACTIVE_PROFILE_NAME", "openai-small")
    monkeypatch.setattr(common, "CONFIRM_SPEND_USD", threshold)
    # At $1 a character every estimate here is far above the real default
    # ceiling, which the gate checks before asking (see _ceiling).
    monkeypatch.setattr(common, "SPEND_CEILING_USD", 1e12)


def _no_terminal(monkeypatch):
    monkeypatch.setattr(common, "is_interactive", lambda: False)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("asked without a terminal"))


def _terminal(monkeypatch, answer):
    asked = []

    def fake_input(prompt=""):
        asked.append(prompt)
        return answer
    monkeypatch.setattr(common, "is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", fake_input)
    return asked


def _dry_chars(monkeypatch, tmp_path, *argv) -> dict:
    """What the dry run reports per source: the same numbers the gate reads."""
    report = tmp_path / "dry.jsonl"
    monkeypatch.setenv("GRIOT_DRY_RUN_REPORT", str(report))
    try:
        assert cli.main([*argv, "--dry-run"]) == 0
    finally:
        monkeypatch.delenv("GRIOT_DRY_RUN_REPORT")
    rows = [json.loads(line) for line in report.read_text().splitlines() if line.strip()]
    report.unlink()
    return {row["source"]: row for row in rows}


def _chunks(n: int) -> str:
    return f"{n:,} chunk{'s' if n != 1 else ''}"


def _runs():
    return logdb.read_recent(common.LOG_DIR, "runs", limit=100) if (common.LOG_DIR / "logs.db").exists() else []


def test_below_the_threshold_the_estimate_is_said_on_stderr_and_the_run_goes_on(repo, embedded, monkeypatch,
                                                                                capsys, tmp_path):
    _paid(monkeypatch, threshold=1e12)
    _no_terminal(monkeypatch)
    chars = _dry_chars(monkeypatch, tmp_path, "index", "code", "--repo", "repo")["code"]
    capsys.readouterr()
    assert cli.main(["index", "code", "--repo", "repo"]) == 0
    out, err = capsys.readouterr()
    assert embedded, "below the threshold the run goes on"
    expected = (f"Embedding {_chunks(chars['to_embed'])} of code from repo with paid profile 'openai-small': "
                f"{common.about_usd(chars['to_embed_chars'])}, estimated from their size.")
    assert expected in err.splitlines(), err
    assert "Embedding" not in out, "an estimate is not the result"
    lines = err.splitlines()
    # Said before the dry run reads everything, so a long read does not look hung.
    assert lines.index("Estimating the cost of code from repo with paid profile 'openai-small'...") < \
        lines.index(expected)


def test_at_the_threshold_without_a_terminal_nothing_is_embedded_and_the_command_is_last(repo, embedded,
                                                                                        monkeypatch, capsys,
                                                                                        tmp_path):
    chars = _dry_chars(monkeypatch, tmp_path, "index", "code", "--repo", "repo")["code"]["to_embed_chars"]
    _paid(monkeypatch)
    estimate = common.estimated_embedding_cost_usd(chars)
    monkeypatch.setattr(common, "CONFIRM_SPEND_USD", estimate)  # the estimate is exactly the threshold
    _no_terminal(monkeypatch)
    capsys.readouterr()
    assert cli.main(["index", "code", "--repo", "repo"]) == 2
    out, err = capsys.readouterr()
    assert embedded == [], "the embedder is never called"
    assert out == ""
    lines = err.rstrip().splitlines()
    assert lines[-1] == "  griot index code --repo repo --yes"
    assert lines[-2] == "Run it in an interactive terminal, or pass --yes:"
    assert lines[0] == "Estimating the cost of code from repo with paid profile 'openai-small'..."
    assert lines[1].startswith("Error: embedding ")
    assert (f"would cost {common.about_usd(estimate)} (estimated), at or above confirm-spend "
            f"({common.format_usd(estimate)})") in lines[1]
    assert "Nothing was changed." in lines[1]
    assert _runs() == [], "a refusal is not a failed run"


def test_above_the_threshold_index_all_asks_once_for_every_source_before_any_embeds(repo, embedded, monkeypatch,
                                                                                  capsys, tmp_path):
    rows = _dry_chars(monkeypatch, tmp_path, "index", "all", "--repo", "repo", "--sources", "code,commits")
    total = sum(row["to_embed_chars"] for row in rows.values())
    count = sum(row["to_embed"] for row in rows.values())
    _paid(monkeypatch, threshold=0.0)
    asked = _terminal(monkeypatch, "n")
    capsys.readouterr()
    assert cli.main(["index", "all", "--repo", "repo", "--sources", "code,commits"]) == 1
    out, err = capsys.readouterr()
    assert len(asked) == 1, asked
    assert asked[0] == (f"Embed {_chunks(count)} (code, commits) from repo with paid profile 'openai-small'? "
                        f"{common.about_usd(total).capitalize()}, estimated from their size; the spend cannot be "
                        f"undone. [y/N] ")
    assert embedded == []
    assert "failed" not in err, "a decline is not a failed source"
    assert err.strip().splitlines() == [
        "Estimating the cost of code, commits from repo with paid profile 'openai-small'...",
        "Aborted. Nothing was changed."]
    assert _runs() == []


def test_a_yes_at_the_terminal_runs_every_source(repo, embedded, monkeypatch, capsys):
    _paid(monkeypatch, threshold=0.0)
    asked = _terminal(monkeypatch, "y")
    assert cli.main(["index", "all", "--repo", "repo", "--sources", "code,commits"]) == 0
    assert len(asked) == 1 and embedded


def test_yes_answers_for_a_script_and_nothing_is_asked(repo, embedded, monkeypatch, capsys):
    _paid(monkeypatch, threshold=0.0)
    _no_terminal(monkeypatch)
    monkeypatch.setenv("GRIOT_QUIET", "0")  # --quiet sets it for the process; restored after the test
    capsys.readouterr()
    assert cli.main(["index", "code", "--repo", "repo", "--yes", "--quiet"]) == 0
    assert embedded
    # What runs without a question leaves a trace: a script's log keeps what it
    # cost, --quiet or not (the progress line before the estimate is silenced).
    err = capsys.readouterr().err
    assert "Estimating the cost" not in err
    assert any(line.startswith("Embedding ") and "with paid profile 'openai-small': " in line
               and line.endswith(", estimated from their size.") for line in err.splitlines()), err


def _credential_unset(monkeypatch):
    monkeypatch.setattr(common, "ACTIVE_PROFILE", {**common.ACTIVE_PROFILE, "api_key_env": "GRIOT_OPENAI_API_KEY"})
    monkeypatch.delenv("GRIOT_OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(common, "credential", lambda env_var: None)  # nor in the keychain


def test_a_missing_credential_is_said_before_asking(repo, embedded, monkeypatch, capsys):
    """A yes could not change the outcome: the run would fail on the
    credential. So the cheap check comes first, and its fix is the last line."""
    _paid(monkeypatch, threshold=0.0)
    _credential_unset(monkeypatch)
    asked = _terminal(monkeypatch, "y")
    capsys.readouterr()
    assert cli.main(["index", "code", "--repo", "repo"]) == 1
    out, err = capsys.readouterr()
    assert asked == [] and embedded == []
    assert err.rstrip().splitlines() == [
        "Error: paid profile 'openai-small' needs GRIOT_OPENAI_API_KEY, and it is not set. Nothing was changed.",
        "  griot auth set openai"], err
    assert out == ""


def test_a_missing_credential_without_a_terminal_does_not_point_at_yes(repo, embedded, monkeypatch, capsys):
    _paid(monkeypatch, threshold=0.0)
    _credential_unset(monkeypatch)
    _no_terminal(monkeypatch)
    capsys.readouterr()
    assert cli.main(["index", "all", "--repo", "repo"]) == 1
    err = capsys.readouterr().err
    assert err.rstrip().splitlines()[-1] == "  griot auth set openai", err
    assert embedded == []


def test_an_estimate_that_fails_ends_with_a_command(repo, embedded, monkeypatch, capsys):
    _paid(monkeypatch, threshold=0.0)
    _no_terminal(monkeypatch)

    def broken(*args, **kwargs):
        raise RuntimeError("the disk is gone")
    monkeypatch.setattr(common, "dry_run", broken)
    capsys.readouterr()
    assert cli.main(["index", "code", "--repo", "repo"]) == 1
    err = capsys.readouterr().err
    assert "could not estimate what 'griot index code' would embed: the disk is gone. Nothing was changed." in err
    assert err.rstrip().splitlines()[-1] == "  griot doctor"


def test_index_all_takes_yes_too(repo, embedded, monkeypatch, capsys):
    _paid(monkeypatch, threshold=0.0)
    _no_terminal(monkeypatch)
    assert cli.main(["index", "all", "--repo", "repo", "--sources", "code", "--yes"]) == 0
    assert embedded


def test_the_refusal_of_index_all_rebuilds_the_command_from_what_was_typed(repo, embedded, monkeypatch, capsys):
    _paid(monkeypatch, threshold=0.0)
    _no_terminal(monkeypatch)
    capsys.readouterr()
    assert cli.main(["index", "all", "--sources", "code,commits", "--repo", "repo", "--prune"]) == 2
    last = capsys.readouterr().err.rstrip().splitlines()[-1]
    assert last == "  griot index all --repo repo --prune --sources code,commits --yes"
    assert embedded == []


def test_a_local_profile_never_asks_and_says_nothing_about_cost(repo, embedded, monkeypatch, capsys):
    monkeypatch.setattr(common, "CONFIRM_SPEND_USD", 0.0)
    _no_terminal(monkeypatch)
    assert not common.profile_is_paid(common.ACTIVE_PROFILE)
    capsys.readouterr()
    assert cli.main(["index", "code", "--repo", "repo"]) == 0
    assert embedded
    out, err = capsys.readouterr()
    assert "paid profile" not in err and "$" not in err + out


def test_the_dry_run_says_the_cost_on_a_paid_profile_and_never_asks(repo, embedded, monkeypatch, capsys,
                                                                   tmp_path):
    _paid(monkeypatch, threshold=0.0)
    _no_terminal(monkeypatch)
    capsys.readouterr()
    assert cli.main(["index", "code", "--repo", "repo", "--dry-run"]) == 0
    out, _ = capsys.readouterr()
    chars = _dry_chars(monkeypatch, tmp_path, "index", "code", "--repo", "repo")["code"]["to_embed_chars"]
    assert (f"[dry-run] About {common.format_usd(chars)} to embed them with paid profile 'openai-small', "
            f"estimated from their size.") in out.splitlines()
    assert embedded == []


def test_the_dry_run_says_when_a_paid_profile_has_no_price(repo, embedded, monkeypatch, capsys):
    _paid(monkeypatch, price=None)
    assert cli.main(["index", "code", "--repo", "repo", "--dry-run"]) == 0
    assert "[dry-run] Cost unknown: profile 'openai-small' has no price set." in capsys.readouterr().out


def test_an_unknown_cost_on_a_paid_profile_asks(repo, embedded, monkeypatch, capsys):
    _paid(monkeypatch, threshold=1e12, price=None)
    _no_terminal(monkeypatch)
    capsys.readouterr()
    assert cli.main(["index", "code", "--repo", "repo"]) == 2
    err = capsys.readouterr().err
    assert "cost unknown: profile 'openai-small' has no price set" in err
    assert embedded == []


def test_nothing_pending_neither_asks_nor_says_a_cost(repo, embedded, monkeypatch, capsys):
    assert cli.main(["index", "code", "--repo", "repo"]) == 0  # indexed once, locally
    embedded.clear()
    _paid(monkeypatch, threshold=0.0)
    _no_terminal(monkeypatch)
    capsys.readouterr()
    # The paid profile reads the same collection here: everything is up to date.
    assert cli.main(["index", "code", "--repo", "repo"]) == 0
    assert embedded == [] and "Embedding" not in capsys.readouterr().err


def test_the_help_of_every_index_command_names_yes_and_the_dry_run_cost(capsys):
    for argv in (["index", "all", "--help"], *(["index", source, "--help"] for source in cli.INDEX_SOURCES)):
        with pytest.raises(SystemExit):
            cli.main(argv)
        # Without whitespace: argparse wraps lines at a hyphen too.
        text = "".join(capsys.readouterr().out.split())
        assert "--yes" in text and "confirm-spend" in text, argv
        if argv[1] != "all":
            assert "theirestimatedcost" in text and "nolocalCPU" not in text, argv


# --- a real terminal ---------------------------------------------------------------------------

_DRIVER = """
import sys
from griot import cli, common
common.ACTIVE_PROFILE = {**common.ACTIVE_PROFILE, "backend": "openai_compatible", "price_per_1m_tokens": 3500000.0}
common.ACTIVE_PROFILE_NAME = "openai-small"
common.CONFIRM_SPEND_USD = 0.0
common.SPEND_CEILING_USD = 1e12
def embed(texts, **kw):
    with open(sys.argv[1], "a") as f:
        f.write("embedded\\n")
    return [[0.1] * common.EMBED_DIM for _ in texts]
common.embed_texts = embed
raise SystemExit(cli.main(sys.argv[2:]))
"""


def _in_a_terminal(tmp_path, argv, keys: bytes):
    script = tmp_path / "driver.py"
    script.write_text(_DRIVER)
    marker = tmp_path / "embedded.txt"
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen([sys.executable, str(script), str(marker), *argv], stdin=slave, stdout=slave,
                                stderr=slave, env={**os.environ}, close_fds=True)
        os.close(slave)
        os.write(master, keys)
        out = b""
        while True:
            ready, _, _ = select.select([master], [], [], 60)
            if not ready:
                proc.kill()
                pytest.fail(f"timed out; so far: {out!r}")
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        code = proc.wait(timeout=60)
    finally:
        os.close(master)
    return code, out.decode(errors="replace"), marker.exists()


@pytest.mark.parametrize("keys,code,spent", [(b"y\n", 0, True), (b"n\n", 1, False)])
def test_the_question_at_a_real_terminal(repo, tmp_path, keys, code, spent):
    got, out, embedded = _in_a_terminal(tmp_path, ["index", "code", "--repo", "repo"], keys)
    assert got == code, out
    assert embedded is spent
    assert "Embed " in out and "of code from repo with paid profile 'openai-small'?" in out
    assert "the spend cannot be undone. [y/N]" in out


# --- quality-check --from-history -------------------------------------------------------------


@pytest.fixture
def history(repo, embedded, monkeypatch):
    """`repo`, indexed as code with a fake embedder; the texts embedded are
    cleared, so what the check embeds is all that is left in the list."""
    assert cli.main(["index", "code", "--repo", "repo"]) == 0
    common.release_lock()
    embedded.clear()
    return embedded


def test_from_history_below_the_threshold_says_the_estimate_and_measures(history, monkeypatch, capsys):
    _paid(monkeypatch, threshold=1e12)
    _no_terminal(monkeypatch)
    capsys.readouterr()
    quality_check.main(["--from-history", "--mode", "vector"])
    out, err = capsys.readouterr()
    estimate = common.estimated_embedding_cost_usd(len("Add the parser"))
    assert (f"Embedding 1 commit message from repo with paid profile 'openai-small': "
            f"{common.about_usd(estimate)}, estimated from their size.") in err.splitlines(), err
    assert history == ["Add the parser"]


def test_from_history_above_the_threshold_without_a_terminal_refuses_before_embedding(history, monkeypatch,
                                                                                    capsys):
    _paid(monkeypatch, threshold=0.0)
    _no_terminal(monkeypatch)
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        quality_check.main(["--from-history", "--mode", "vector", "--repo", "repo", "--limit", "5"])
    assert exc.value.code == 2
    out, err = capsys.readouterr()
    assert history == []
    assert out == ""
    assert err.rstrip().splitlines()[-1] == ("  griot quality-check --from-history --repo repo --limit 5 "
                                             "--mode vector --yes")


def test_from_history_refusal_keeps_json_in_the_command(history, monkeypatch, capsys):
    """A script that pastes the line must get the JSON it asked for."""
    _paid(monkeypatch, threshold=0.0)
    _no_terminal(monkeypatch)
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        quality_check.main(["--from-history", "--json", "--limit", "5"])
    assert exc.value.code == 2
    out, err = capsys.readouterr()
    assert out == "" and history == []
    assert err.rstrip().splitlines()[-1] == "  griot quality-check --from-history --limit 5 --json --yes"


def test_from_history_missing_credential_is_said_before_asking(history, monkeypatch, capsys):
    _paid(monkeypatch, threshold=0.0)
    _credential_unset(monkeypatch)
    asked = _terminal(monkeypatch, "y")
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        quality_check.main(["--from-history", "--mode", "vector"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert asked == [] and history == []
    assert err.rstrip().splitlines()[-1] == "  griot auth set openai"


def test_from_history_asks_at_a_terminal_and_a_no_measures_nothing(history, monkeypatch, capsys):
    _paid(monkeypatch, threshold=0.0)
    asked = _terminal(monkeypatch, "n")
    with pytest.raises(SystemExit) as exc:
        quality_check.main(["--from-history", "--mode", "vector"])
    assert exc.value.code == 1
    assert asked == [f"Embed 1 commit message from repo with paid profile 'openai-small'? "
                     f"{common.about_usd(common.estimated_embedding_cost_usd(14)).capitalize()}, estimated from "
                     f"their size; the spend cannot be undone. [y/N] "]
    assert history == []


def test_from_history_yes_answers(history, monkeypatch, capsys):
    _paid(monkeypatch, threshold=0.0)
    _no_terminal(monkeypatch)
    quality_check.main(["--from-history", "--mode", "vector", "--yes", "--json"])
    out, err = capsys.readouterr()
    payload = json.loads(out)
    assert payload["paid"] is True and history
    assert "Embedding 1 commit message from repo" in err, "--yes says the estimate on both commands"


def test_from_history_unknown_cost_on_a_paid_profile_asks(history, monkeypatch, capsys):
    _paid(monkeypatch, threshold=1e12, price=None)
    _no_terminal(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        quality_check.main(["--from-history", "--mode", "vector"])
    assert exc.value.code == 2
    assert "cost unknown: profile 'openai-small' has no price set" in capsys.readouterr().err
    assert history == []


def test_from_history_json_says_paid_and_rounds_like_the_preview(history, monkeypatch, capsys):
    _paid(monkeypatch, threshold=1e12, price=0.02)
    quality_check.main(["--from-history", "--mode", "vector", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["paid"] is True
    # The real openai-small price: a few millionths of a dollar, never shown as 0.
    assert payload["estimated_cost_usd"] == common.estimate_rounded(common.estimated_embedding_cost_usd(14))
    assert payload["estimated_cost_usd"] > 0


def test_from_history_errors_go_to_stderr_without_json(monkeypatch, capsys):
    common.get_client()  # the collection exists, nothing is registered
    common.REPOS_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    common.REPOS_JSON_PATH.write_text("[]")
    capsys.readouterr()
    with pytest.raises(SystemExit):
        quality_check.main(["--from-history", "--repo", "nope"])
    out, err = capsys.readouterr()
    assert "Error:" in err and "Error:" not in out


# --- doctor ------------------------------------------------------------------------------------


def test_doctor_says_when_a_paid_run_asks_first(monkeypatch):
    _paid(monkeypatch)
    monkeypatch.setattr(common, "CONFIRM_SPEND_USD", 0.50)
    monkeypatch.setattr(common, "get_spend_today", lambda: 0.12)
    monkeypatch.setattr(common, "SPEND_CEILING_USD", 3.0)
    check = doctor.check_spend(common)
    assert check["detail"] == "$0.12 today of a $3.00 daily ceiling; a paid run estimated at $0.50 or more asks first"


# --- MCP ---------------------------------------------------------------------------------------


def test_the_index_job_answers_the_cli_question_and_the_preview_never_asks(monkeypatch, tmp_path):
    """The MCP confirmation already happened when the run starts, and the
    run has no terminal: without --yes it would exit 2 after the person
    said yes."""
    captured = []

    class FakeProc:
        pid = 1

        def poll(self):
            return None

    real_popen = jobs.subprocess.Popen
    monkeypatch.setattr(jobs.subprocess, "Popen",
                        lambda cmd, **kw: real_popen(cmd, **kw) if cmd[0] == "git" else (captured.append(cmd) or FakeProc()))
    monkeypatch.setattr(jobs.time, "sleep", lambda s: None)
    monkeypatch.setattr(common, "index_lock_status", lambda: {"running": False, "pid": None, "path": None})
    monkeypatch.setattr(jobs, "running_index_job", lambda: None)
    monkeypatch.setattr(common, "release_client", lambda: None)
    assert jobs.start_index_job(None, ["code"])["started"] is True
    assert "--yes" in captured[0]

    seen = []
    monkeypatch.setattr(jobs, "_run_dry_run", lambda argv, env, timeout: seen.append(argv) or
                        subprocess.CompletedProcess(argv, 0, "", ""))
    monkeypatch.setattr(jobs, "index_job_refusal", lambda path, **kw: None)
    jobs.run_index_preview(str(tmp_path), ["code"], release=lambda: None)
    assert "--dry-run" in seen[0] and "--yes" not in seen[0]


@pytest.fixture
def quality_index(embedded, monkeypatch):
    common.index_documents([
        {"id": f"r:code:f{i}.py:0", "content": f"content number {i}",
         "metadata": {"source_type": "code", "repo": "r", "file_path": f"f{i}.py", "chunk_index": 0}}
        for i in range(3)])
    common.release_client()
    embedded.clear()


@pytest.mark.anyio
async def test_quality_check_through_the_protocol_on_a_local_profile_is_free(quality_index):
    async with Client(mcp_server.mcp) as client:
        out = (await client.call_tool("griot_quality_check", {"sample_size": 3, "golden_set": False}))
    assert not out.is_error, out.content
    assert out.structured_content["paid"] is False
    assert out.structured_content["estimated_cost_usd"] is None


@pytest.mark.anyio
async def test_quality_check_through_the_protocol_says_what_it_cost_on_a_paid_profile(quality_index,
                                                                                    monkeypatch):
    _paid(monkeypatch, price=0.02)
    async with Client(mcp_server.mcp) as client:
        out = await client.call_tool("griot_quality_check", {"sample_size": 3, "golden_set": False})
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert not out.is_error, out.content
    chars = sum(len(f"content number {i}") for i in range(3))
    assert out.structured_content["paid"] is True
    expected = common.estimated_embedding_cost_usd(chars)
    assert out.structured_content["estimated_cost_usd"] == common.estimate_rounded(expected)
    assert out.structured_content["estimated_cost_usd"] > 0, "a paid call read as free"
    description = " ".join(tools["griot_quality_check"].description.split())
    assert "`estimated_cost_usd`" in description and "nothing asks first" in description


@pytest.mark.anyio
async def test_quality_check_on_nothing_indexed_ends_with_the_command():
    async with Client(mcp_server.mcp) as client:
        out = await client.call_tool("griot_quality_check", {"sample_size": 3})
    text = out.content[0].text
    assert out.is_error
    assert text.rstrip().splitlines()[-1] == "  griot index all", text


@pytest.fixture
def server_with_index(monkeypatch):
    monkeypatch.setenv("GRIOT_MCP_ENABLE_INDEX", "true")
    importlib.reload(mcp_server)
    try:
        yield mcp_server
    finally:
        monkeypatch.delenv("GRIOT_MCP_ENABLE_INDEX", raising=False)
        importlib.reload(mcp_server)


@pytest.fixture
def allowed(monkeypatch, tmp_path):
    path = tmp_path / "my-app"
    path.mkdir()
    monkeypatch.setattr(jobs, "index_job_refusal", lambda p, **kw: None)
    return str(path)


def _preview(monkeypatch, result):
    calls = []

    def fake(path, sources=None, **kw):
        calls.append((path, sources))
        return {"ok": True, "reason": None, "profile": common.ACTIVE_PROFILE_NAME,
                "paid": common.profile_is_paid(common.ACTIVE_PROFILE), "estimated_cost_usd": None, "to_embed": 0,
                **result}
    monkeypatch.setattr(jobs, "run_index_preview", fake)
    return calls


@pytest.mark.anyio
async def test_the_index_question_names_the_estimate_on_a_paid_profile(server_with_index, allowed, monkeypatch):
    server = server_with_index
    _paid(monkeypatch)
    calls = _preview(monkeypatch, {"estimated_cost_usd": 1.84, "to_embed": 41300})
    monkeypatch.setattr(jobs, "start_index_job", lambda *a, **k: pytest.fail("started without a confirmation"))
    async with Client(server.mcp) as client:
        out = await client.call_tool("griot_index_repo", {"path": allowed})
    text = out.structured_content["reason"]
    assert text.startswith(f"Index {mcp_server._shown(allowed)} with paid profile 'openai-small': about $1.84 to embed 41,300 "
                           f"chunks, estimated from their size; the spend cannot be undone. It also removes "
                           f"indexed points whose source is gone."), text
    assert len(calls) == 1, "the preview runs once per call"
    assert len(text) < 2000


@pytest.mark.anyio
async def test_a_tiny_estimate_in_the_index_question_is_not_free(server_with_index, allowed, monkeypatch):
    _paid(monkeypatch)
    _preview(monkeypatch, {"estimated_cost_usd": common.estimate_rounded(0.0000025), "to_embed": 2})
    async with Client(server_with_index.mcp) as client:
        out = await client.call_tool("griot_index_repo", {"path": allowed})
    text = out.structured_content["reason"]
    assert text.startswith(f"Index {mcp_server._shown(allowed)} with paid profile 'openai-small': under $0.001 to "
                           f"embed 2 chunks, estimated from their size;"), text
    assert "$0.00 " not in text


@pytest.mark.anyio
async def test_nothing_pending_in_the_index_question_bills_nothing(server_with_index, allowed, monkeypatch):
    _paid(monkeypatch)
    _preview(monkeypatch, {"estimated_cost_usd": 0.0, "to_embed": 0})
    async with Client(server_with_index.mcp) as client:
        out = await client.call_tool("griot_index_repo", {"path": allowed})
    text = out.structured_content["reason"]
    assert text.startswith(f"Index {mcp_server._shown(allowed)} with paid profile 'openai-small': nothing is "
                           f"pending to embed, so nothing is billed."), text
    assert "cannot be undone" not in text and "0 chunks" not in text


@pytest.mark.anyio
async def test_the_index_question_says_when_the_cost_could_not_be_estimated(server_with_index, allowed,
                                                                           monkeypatch):
    _paid(monkeypatch)
    _preview(monkeypatch, {"ok": False, "reason": "the dry run did not finish in 300 seconds."})
    async with Client(server_with_index.mcp) as client:
        out = await client.call_tool("griot_index_repo", {"path": allowed})
    text = out.structured_content["reason"]
    assert (f"Index {mcp_server._shown(allowed)} with paid profile 'openai-small'; its cost could not be estimated (the dry run "
            f"did not finish in 300 seconds.). The spend cannot be undone.") in text, text


@pytest.mark.anyio
async def test_the_index_question_on_a_local_profile_runs_no_preview(server_with_index, allowed, monkeypatch):
    calls = _preview(monkeypatch, {})
    async with Client(server_with_index.mcp) as client:
        out = await client.call_tool("griot_index_repo", {"path": allowed})
    text = out.structured_content["reason"]
    assert text.startswith(f"Index {mcp_server._shown(allowed)} with local profile '{common.ACTIVE_PROFILE_NAME}' (nothing is "
                           f"billed)."), text
    assert calls == []


@pytest.mark.anyio
async def test_confirm_true_starts_without_a_preview(server_with_index, allowed, monkeypatch):
    _paid(monkeypatch)
    calls = _preview(monkeypatch, {"estimated_cost_usd": 1.84, "to_embed": 41300})
    started = []
    monkeypatch.setattr(jobs, "start_index_job", lambda path, sources, **kw: started.append(path) or
                        {"started": True, "path": path, "pid": 1, "sources": ["code"], "reason": None})
    async with Client(server_with_index.mcp) as client:
        out = await client.call_tool("griot_index_repo", {"path": allowed, "confirm": True})
    assert out.structured_content["started"] is True
    assert calls == [], "nobody reads the question, so its estimate is not paid for"


@pytest.mark.anyio
@pytest.mark.parametrize("protocol", ["legacy", "2026-07-28"])
async def test_a_client_that_asks_shows_the_estimate_and_the_preview_runs_once(server_with_index, allowed,
                                                                             monkeypatch, protocol):
    from mcp_types import ElicitResult

    _paid(monkeypatch)
    calls = _preview(monkeypatch, {"estimated_cost_usd": 0.0042, "to_embed": 1})
    started = []
    monkeypatch.setattr(jobs, "start_index_job", lambda path, sources, **kw: started.append(path) or
                        {"started": True, "path": path, "pid": 1, "sources": ["code"], "reason": None})
    asked = []

    async def ask(ctx, params):
        asked.append(params.message)
        return ElicitResult(action="accept", content={})

    mcp_server._index_questions.clear()
    async with Client(server_with_index.mcp, mode=protocol, elicitation_callback=ask) as client:
        out = await client.call_tool("griot_index_repo", {"path": allowed, "sources": ["code"]})
    assert out.structured_content["started"] is True, out.content
    assert asked == [f"Index {mcp_server._shown(allowed)} with paid profile 'openai-small': about $0.004 to embed 1 chunk, estimated "
                     f"from their size; the spend cannot be undone. It also removes indexed points whose source "
                     f"is gone."]
    assert calls == [(allowed, ["code"])], "one dry run, whatever the number of round trips"
    assert started == [allowed]


# --- round 3: what the gate checks before asking, and what it keeps ---------------------------


@pytest.mark.parametrize("argv", [["index", "all"], ["index", "code"]])
def test_a_paid_profile_with_nothing_registered_gets_the_sources_own_error(embedded, monkeypatch, capsys, argv):
    """A new user who picked a paid profile and registered nothing yet: the
    gate names no repository it cannot read, and the source's own error
    speaks (the same one a local profile or --dry-run prints)."""
    _paid(monkeypatch, threshold=0.0)
    _no_terminal(monkeypatch)
    assert not common.REPOS_JSON_PATH.exists()
    capsys.readouterr()
    assert cli.main(argv) == 1
    out, err = capsys.readouterr()
    # The sources' shared sentence for "nothing registered yet" (#170), not
    # the gate's: read from griot so the two cannot drift apart.
    assert f"Error: {common.no_repository_sentence()}" in err, err
    assert "Traceback" not in err and out == ""
    assert embedded == []


def _ceiling_for(today, estimate):
    """The amount the refusal proposes, written out here rather than read
    from griot: what today's spend plus the estimate needs, up to the next
    half dollar, and half a dollar more when that is exact (an estimate)."""
    import math
    needed = today + estimate
    amount = math.ceil(needed * 2) / 2
    if amount == needed:
        amount += 0.5
    return f"{amount:.0f}" if amount == int(amount) else f"{amount:.2f}"


def _ceiling(monkeypatch, today, ceiling):
    monkeypatch.setattr(common, "get_spend_today", lambda: today)
    monkeypatch.setattr(common, "SPEND_CEILING_USD", ceiling)


@pytest.mark.parametrize("yes", [False, True])
def test_a_reached_spend_ceiling_is_said_before_asking(repo, embedded, monkeypatch, capsys, yes, tmp_path):
    """A yes could not change the outcome: the breaker refuses every paid
    call today. Cheap to read, so it comes before the question (and before
    a --yes run that would only reach the breaker)."""
    chars = _dry_chars(monkeypatch, tmp_path, "index", "code", "--repo", "repo")["code"]["to_embed_chars"]
    _paid(monkeypatch, threshold=0.0)
    estimate = common.estimated_embedding_cost_usd(chars)
    _ceiling(monkeypatch, today=3.0, ceiling=3.0)
    asked = _terminal(monkeypatch, "y")
    capsys.readouterr()
    assert cli.main(["index", "code", "--repo", "repo", *(["--yes"] if yes else [])]) == 1
    out, err = capsys.readouterr()
    assert asked == [] and embedded == [] and out == ""
    lines = err.rstrip().splitlines()
    assert lines[-1] == f"  griot config set spend-ceiling {_ceiling_for(3.0, estimate)}", err
    assert lines[-2].startswith("Error: today's estimated spend $3.00 reached the $3.00 daily ceiling"), err
    assert lines[-2].endswith(" would be refused. Nothing was changed."), lines[-2]
    assert "--yes" not in err and "<" not in err


def test_a_reached_spend_ceiling_refuses_a_run_whose_cost_is_unknown(repo, embedded, monkeypatch, capsys):
    """No estimate to add to today's spend: the ceiling already reached is
    reason enough, and the question (asked for an unknown cost) would be
    answered for nothing."""
    _paid(monkeypatch, threshold=1e12, price=None)
    _ceiling(monkeypatch, today=3.0, ceiling=3.0)
    asked = _terminal(monkeypatch, "y")
    capsys.readouterr()
    assert cli.main(["index", "code", "--repo", "repo"]) == 1
    err = capsys.readouterr().err
    assert asked == [] and embedded == []
    # No estimate to add: the next whole dollar above today's spend.
    assert err.rstrip().splitlines()[-1] == "  griot config set spend-ceiling 4", err


def test_a_run_that_would_pass_the_spend_ceiling_is_said_before_asking(repo, embedded, monkeypatch, capsys,
                                                                        tmp_path):
    chars = _dry_chars(monkeypatch, tmp_path, "index", "code", "--repo", "repo")["code"]["to_embed_chars"]
    _paid(monkeypatch, threshold=0.0)
    estimate = common.estimated_embedding_cost_usd(chars)
    _ceiling(monkeypatch, today=1.0, ceiling=1.0 + estimate * 0.99)
    asked = _terminal(monkeypatch, "y")
    capsys.readouterr()
    assert cli.main(["index", "code", "--repo", "repo"]) == 1
    err = capsys.readouterr().err
    assert asked == [] and embedded == []
    lines = err.rstrip().splitlines()
    assert lines[-1] == f"  griot config set spend-ceiling {_ceiling_for(1.0, estimate)}", err
    assert f"would cost {common.about_usd(estimate)} (estimated)" in lines[-2]
    assert lines[-2].endswith("which would stop the run partway. Nothing was changed."), lines[-2]
    assert "today's estimated spend $1.00 plus that passes the" in lines[-2], lines[-2]


def test_a_run_that_fits_under_the_spend_ceiling_asks(repo, embedded, monkeypatch, capsys, tmp_path):
    chars = _dry_chars(monkeypatch, tmp_path, "index", "code", "--repo", "repo")["code"]["to_embed_chars"]
    _paid(monkeypatch, threshold=0.0)
    estimate = common.estimated_embedding_cost_usd(chars)
    _ceiling(monkeypatch, today=1.0, ceiling=1.0 + estimate * 1.01)
    asked = _terminal(monkeypatch, "y")
    assert cli.main(["index", "code", "--repo", "repo"]) == 0
    assert len(asked) == 1 and embedded


def test_from_history_checks_the_spend_ceiling_before_asking(history, monkeypatch, capsys):
    _paid(monkeypatch, threshold=0.0)
    _ceiling(monkeypatch, today=5.0, ceiling=3.0)
    asked = _terminal(monkeypatch, "y")
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        quality_check.main(["--from-history", "--mode", "vector"])
    assert exc.value.code == 1
    assert asked == [] and history == []
    estimate = common.estimated_embedding_cost_usd(len("Add the parser"))
    assert capsys.readouterr().err.rstrip().splitlines()[-1] == \
        f"  griot config set spend-ceiling {_ceiling_for(5.0, estimate)}"


def test_the_refusal_keeps_quiet_when_it_was_typed(repo, embedded, monkeypatch, capsys):
    """The pasted line runs as quietly as the one that was typed."""
    monkeypatch.setenv("GRIOT_QUIET", "0")
    monkeypatch.setattr(cli.griot, "QUIET_FROM_COMMAND_LINE", False, raising=False)
    _paid(monkeypatch, threshold=0.0)
    _no_terminal(monkeypatch)
    capsys.readouterr()
    assert cli.main(["index", "all", "--sources", "code,commits", "--repo", "repo", "--quiet"]) == 2
    last = capsys.readouterr().err.rstrip().splitlines()[-1]
    assert last == "  griot index all --repo repo --sources code,commits --quiet --yes"


def test_the_quality_check_refusal_keeps_quiet_when_it_was_typed(history, monkeypatch, capsys):
    monkeypatch.setenv("GRIOT_QUIET", "0")
    monkeypatch.setattr(cli.griot, "QUIET_FROM_COMMAND_LINE", False, raising=False)
    _paid(monkeypatch, threshold=0.0)
    _no_terminal(monkeypatch)
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        cli.main(["quality-check", "--from-history", "--limit", "5", "-q"])
    assert exc.value.code == 2
    last = capsys.readouterr().err.rstrip().splitlines()[-1]
    assert last == "  griot quality-check --from-history --limit 5 --quiet --yes"


def test_a_credential_with_no_known_provider_ends_with_a_command_that_runs(repo, embedded, monkeypatch, capsys):
    _paid(monkeypatch, threshold=0.0)
    # A key no registered profile names: no `griot auth set` can store it.
    monkeypatch.setattr(common, "ACTIVE_PROFILE", {**common.ACTIVE_PROFILE, "api_key_env": "MY_CUSTOM_KEY"})
    monkeypatch.setattr(common, "credential", lambda env_var: None)
    _terminal(monkeypatch, "y")
    capsys.readouterr()
    assert cli.main(["index", "code", "--repo", "repo"]) == 1
    err = capsys.readouterr().err
    assert "needs MY_CUSTOM_KEY, and it is not set" in err
    assert err.rstrip().splitlines()[-1] == "  griot doctor", err
    assert "<provider>" not in err


def test_doctor_names_the_threshold_only_on_a_paid_profile(monkeypatch):
    monkeypatch.setattr(common, "CONFIRM_SPEND_USD", 0.50)
    monkeypatch.setattr(common, "get_spend_today", lambda: 0.12)
    monkeypatch.setattr(common, "SPEND_CEILING_USD", 3.0)
    assert not common.profile_is_paid()
    assert doctor.check_spend(common)["detail"] == "$0.12 today of a $3.00 daily ceiling"


@pytest.mark.anyio
@pytest.mark.parametrize("preview,paid", [({}, False), ({"estimated_cost_usd": 0.0, "to_embed": 0}, True),
                                          ({"estimated_cost_usd": 1.84, "to_embed": 3}, True),
                                          ({"ok": False, "reason": "timed out"}, True)])
async def test_every_index_question_ends_with_the_same_removal_clause(server_with_index, allowed, monkeypatch,
                                                                    preview, paid):
    """One fact, one wording; and nothing it cannot guarantee (a point whose
    source is gone comes back only if the source does)."""
    if paid:
        _paid(monkeypatch)
    _preview(monkeypatch, preview)
    mcp_server._index_questions.clear()
    async with Client(server_with_index.mcp) as client:
        out = await client.call_tool("griot_index_repo", {"path": allowed})
    text = out.structured_content["reason"]
    question = text.splitlines()[0]
    assert question.endswith("It also removes indexed points whose source is gone."), text
    assert "brings them back" not in text


# --- round 4: the credential first, a ceiling that can be pasted, one count -------------------


@pytest.mark.parametrize("amount,text", [(3.0, "$3.00"), (0.5, "$0.50"), (0.0001, "$0.0001"),
                                         (1234.5, "$1,234.50"), (0.125, "$0.125"), (0.0, "$0.00")])
def test_a_configured_limit_is_printed_as_configured(amount, text):
    """A limit is a value someone set, not an approximation: "under $0.001"
    or "$0.00" would misstate it."""
    assert common.format_limit(amount) == text


@pytest.mark.parametrize("today,estimate,amount", [(2.40, 0.84, "3.50"), (3.0, 0.5, "4"), (3.0, None, "4"),
                                                   (2.4, None, "4"), (0.004, 0.0001, "0.50"), (3.0, 1.0, "4.50"),
                                                   (10.2, 0.0042, "10.50")])
def test_the_ceiling_a_refusal_proposes(today, estimate, amount):
    """Today's spend plus the estimate, up to the next half dollar, and half
    a dollar more when that is exact (the estimate is approximate); with no
    estimate, the next whole dollar above today's spend. A plain number the
    config parser takes: `<usd>` is a redirect to a shell."""
    assert common.spend_ceiling_to_propose(today, estimate) == amount
    config.normalized(config.find("spend-ceiling"), amount)  # accepted as written


def test_the_ceiling_refusal_in_full(monkeypatch, capsys):
    monkeypatch.setattr(common, "ACTIVE_PROFILE_NAME", "openai-small")
    _ceiling(monkeypatch, today=2.40, ceiling=3.0)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("asked"))
    assert common.confirm_spend("412 chunks (code, commits) from my-app", 0.84, yes=False, command="griot x") == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err == ("Error: embedding 412 chunks (code, commits) from my-app with paid profile 'openai-small' would "
                   "cost about $0.84 (estimated), and today's estimated spend $2.40 plus that passes the $3.00 daily "
                   "ceiling, which would stop the run partway. Nothing was changed.\n"
                   "  griot config set spend-ceiling 3.50\n")


def test_the_reached_ceiling_refusal_in_full(monkeypatch, capsys):
    monkeypatch.setattr(common, "ACTIVE_PROFILE_NAME", "openai-small")
    _ceiling(monkeypatch, today=4.0, ceiling=0.0001)
    assert common.confirm_spend("2 chunks of code from my-app", None, yes=True, command="griot x") == 1
    assert capsys.readouterr().err == (
        "Error: today's estimated spend $4.00 reached the $0.0001 daily ceiling, so embedding 2 chunks of code from "
        "my-app with paid profile 'openai-small' would be refused. Nothing was changed.\n"
        "  griot config set spend-ceiling 5\n")


def test_the_threshold_in_a_refusal_is_printed_as_configured(monkeypatch, capsys):
    monkeypatch.setattr(common, "ACTIVE_PROFILE_NAME", "openai-small")
    monkeypatch.setattr(common, "CONFIRM_SPEND_USD", 0.0001)
    _ceiling(monkeypatch, today=0.0, ceiling=3.0)
    _no_terminal(monkeypatch)
    assert common.confirm_spend("2 chunks of code from my-app", 0.5, yes=False, command="griot x") == 2
    err = capsys.readouterr().err
    assert "at or above confirm-spend ($0.0001)" in err, err


def _no_estimate(monkeypatch):
    """The dry run behind the estimate: never reached when the run cannot start."""
    monkeypatch.setattr(common, "dry_run", lambda *a, **k: pytest.fail("estimated a run that cannot start"))


MISSING = ["Error: paid profile 'openai-small' needs GRIOT_OPENAI_API_KEY, and it is not set. Nothing was changed.",
           "  griot auth set openai"]


@pytest.mark.parametrize("argv,threshold", [
    (["index", "code", "--repo", "repo"], 1e12),                   # below the threshold
    (["index", "code", "--repo", "repo", "--yes"], 0.0),           # --yes
    (["index", "code", "--repo", "repo", "--yes", "--quiet"], 0.0),  # --quiet never hides an error
    (["index", "all", "--repo", "repo"], 1e12),
    (["index", "all", "--repo", "repo", "--yes"], 0.0),
])
def test_a_missing_credential_is_the_first_check_whatever_the_threshold(repo, embedded, monkeypatch, capsys, argv,
                                                                        threshold):
    """A run that cannot start never says it is estimating or spending:
    the credential is read before anything else, nothing is embedded, and
    the command that sets it is the last line."""
    monkeypatch.setenv("GRIOT_QUIET", "0")
    _paid(monkeypatch, threshold=threshold)
    _credential_unset(monkeypatch)
    _no_terminal(monkeypatch)
    _no_estimate(monkeypatch)
    capsys.readouterr()
    assert cli.main(argv) == 1
    out, err = capsys.readouterr()
    assert out == "" and embedded == []
    assert err.rstrip().splitlines() == MISSING, err
    assert _runs() == [], "a refusal is not a failed run"


def test_a_credential_that_is_set_lets_the_run_go_on(repo, embedded, monkeypatch, capsys):
    _paid(monkeypatch, threshold=1e12)
    monkeypatch.setattr(common, "ACTIVE_PROFILE", {**common.ACTIVE_PROFILE, "api_key_env": "GRIOT_OPENAI_API_KEY"})
    monkeypatch.setattr(common, "credential", lambda env_var: "sk-test" if env_var == "GRIOT_OPENAI_API_KEY" else None)
    _no_terminal(monkeypatch)
    capsys.readouterr()
    assert cli.main(["index", "code", "--repo", "repo"]) == 0
    assert embedded and "griot auth set" not in capsys.readouterr().err


def test_a_dry_run_needs_no_credential(repo, embedded, monkeypatch, capsys):
    _paid(monkeypatch, threshold=0.0)
    _credential_unset(monkeypatch)
    assert cli.main(["index", "code", "--repo", "repo", "--dry-run"]) == 0
    assert "griot auth set" not in capsys.readouterr().err


def test_a_custom_key_missing_ends_with_doctor_before_any_estimate(repo, embedded, monkeypatch, capsys):
    _paid(monkeypatch, threshold=1e12)
    monkeypatch.setattr(common, "ACTIVE_PROFILE", {**common.ACTIVE_PROFILE, "api_key_env": "MY_CUSTOM_KEY"})
    monkeypatch.setattr(common, "credential", lambda env_var: None)
    _no_terminal(monkeypatch)
    _no_estimate(monkeypatch)
    capsys.readouterr()
    assert cli.main(["index", "code", "--repo", "repo"]) == 1
    assert capsys.readouterr().err.rstrip().splitlines() == [
        "Error: paid profile 'openai-small' needs MY_CUSTOM_KEY, and it is not set. Nothing was changed.",
        "  griot doctor"]


@pytest.mark.parametrize("argv,threshold", [(["--from-history", "--mode", "vector"], 1e12),
                                            (["--from-history", "--mode", "vector", "--yes", "--json"], 0.0)])
def test_from_history_a_missing_credential_is_the_first_check(history, monkeypatch, capsys, argv, threshold):
    _paid(monkeypatch, threshold=threshold)
    _credential_unset(monkeypatch)
    _no_terminal(monkeypatch)
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        quality_check.main(argv)
    assert exc.value.code == 1
    out, err = capsys.readouterr()
    assert out == "" and history == []
    assert err.rstrip().splitlines() == MISSING, err


@pytest.mark.anyio
@pytest.mark.parametrize("confirm", [False, True])
async def test_the_index_tool_refuses_a_missing_credential_before_asking(server_with_index, allowed, monkeypatch,
                                                                         confirm):
    """The job runs with --yes: asked to approve a spend it cannot make,
    the person would say yes and the run would fail in its log."""
    from mcp_types import ElicitResult

    _paid(monkeypatch)
    _credential_unset(monkeypatch)
    calls = _preview(monkeypatch, {"estimated_cost_usd": 1.84, "to_embed": 3})
    monkeypatch.setattr(jobs, "start_index_job", lambda *a, **k: pytest.fail("started without a credential"))
    asked = []

    async def ask(ctx, params):
        asked.append(params.message)
        return ElicitResult(action="accept", content={})

    mcp_server._index_questions.clear()
    async with Client(server_with_index.mcp, elicitation_callback=ask) as client:
        out = await client.call_tool("griot_index_repo", {"path": allowed, "confirm": confirm})
    assert out.structured_content["started"] is False
    assert out.structured_content["reason"] == (
        "Paid profile 'openai-small' needs GRIOT_OPENAI_API_KEY, and it is not set. Nothing was started. "
        "A credential is set in a terminal:\n  griot auth set openai")
    assert asked == [] and calls == []


def _docs(names, text="def f():\n    return 1\n"):
    return [{"id": f"r:code:{n}:0", "content": f"{text}# {n}\n",
             "metadata": {"source_type": "code", "repo": "r", "file_path": n, "chunk_index": 0}} for n in names]


def test_the_run_says_how_many_it_embeds_and_how_many_were_current(embedded, capsys):
    name = common.ACTIVE_PROFILE_NAME
    common.index_documents(_docs(["a.py", "b.py", "c.py"]))
    first = capsys.readouterr()
    assert f"Embedding 3 of 3 chunks with profile '{name}'; 0 are already up to date." in first.err.splitlines()
    changed = _docs(["a.py", "b.py", "c.py"])
    changed[0]["content"] += "# changed\n"
    common.index_documents(changed)
    seen = capsys.readouterr()
    assert seen.out == ""
    assert f"Embedding 1 of 3 chunks with profile '{name}'; 2 are already up to date." in seen.err.splitlines()
    assert "Generating embeddings" not in seen.err
    common.index_documents(changed)
    assert "Nothing to embed: all 3 chunks are already up to date." in capsys.readouterr().err.splitlines()


def test_the_run_line_uses_the_source_unit_and_stays_a_notice(embedded, capsys, monkeypatch):
    name = common.ACTIVE_PROFILE_NAME
    common.index_documents(_docs(["a.py"]), unit="commits")
    assert f"Embedding 1 of 1 commit with profile '{name}'; 0 are already up to date." in \
        capsys.readouterr().err.splitlines()
    common.index_documents(_docs(["a.py", "b.py"]), unit="branches")
    assert f"Embedding 1 of 2 branches with profile '{name}'; 1 is already up to date." in \
        capsys.readouterr().err.splitlines()
    monkeypatch.setenv("GRIOT_QUIET", "1")
    common.index_documents(_docs(["c.py"]))
    assert "Embedding" not in capsys.readouterr().err


def test_the_gate_and_the_run_give_the_same_count(repo, embedded, monkeypatch, capsys):
    assert cli.main(["index", "code", "--repo", "repo"]) == 0  # indexed once, locally
    repo.commit("Add the lexer", filename="lexer.py", content="def lex(text):\n    return list(text)\n")
    embedded.clear()
    _paid(monkeypatch, threshold=1e12)
    _no_terminal(monkeypatch)
    capsys.readouterr()
    assert cli.main(["index", "code", "--repo", "repo"]) == 0
    out, err = capsys.readouterr()
    lines = err.splitlines()
    assert any(line.startswith("Embedding 1 chunk of code from repo with paid profile") for line in lines), err
    assert any(line.startswith("Embedding 1 of ") and line.endswith(" already up to date.") for line in lines), err
    total = next(line for line in out.splitlines() if line.startswith("Total:"))
    assert total.startswith("Total: 1 chunks indexed, ") and " already up to date, " in total, total
    assert "unchanged (skipped)" not in out


def test_doctor_proposes_a_ceiling_that_can_be_pasted(monkeypatch):
    monkeypatch.setattr(common, "get_spend_today", lambda: 3.2)
    monkeypatch.setattr(common, "SPEND_CEILING_USD", 3.0)
    check = doctor.check_spend(common)
    assert check["detail"] == ("today's estimated spend $3.20 reached the $3.00 daily ceiling: paid calls are "
                               "refused until tomorrow; raising the ceiling asks first")
    assert check["fix"] == "griot config set spend-ceiling 5"


def test_doctor_writes_amounts_as_the_refusal_does(monkeypatch):
    monkeypatch.setattr(common, "get_spend_today", lambda: 0.004)
    monkeypatch.setattr(common, "SPEND_CEILING_USD", 3.0001)
    assert doctor.check_spend(common)["detail"] == "$0.004 today of a $3.0001 daily ceiling"


def test_the_troubleshooting_skill_proposes_an_amount_not_a_placeholder():
    text = (Path(common.__file__).parent / "resources" / "skills" / "griot-troubleshooting" / "SKILL.md").read_text()
    assert "<usd>" not in text and "<USD>" not in text
    assert "`griot config set spend-ceiling 3.50`" in text


@pytest.mark.parametrize("source,unit", [("commits", "commit"), ("tags", "tag")])
def test_each_source_counts_in_its_own_unit(repo, embedded, capsys, source, unit):
    """The line agrees with the source's Total line, which counts commits
    or tags, not chunks (branches pass their unit the same way)."""
    repo.tag("v1") if source == "tags" else None
    assert cli.main(["index", source, "--repo", "repo"]) == 0
    out, err = capsys.readouterr()
    plan = next(line for line in err.splitlines() if line.startswith(("Embedding ", "Nothing to embed")))
    assert f" {unit}" in plan and "chunk" not in plan, plan
