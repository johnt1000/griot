"""Where credential-looking values are replaced, and that nothing gets around it.

Two moments. Writing: everything that is about to be embedded and stored goes
through the detectors first, before it is cut into chunks (a value cut in two
would match nothing) and again as each document is taken up (for what was
never chunked). Reading: what an older version already stored is replaced on
the way out, so it does not reach an agent or a chat model while it waits to
be indexed again.

The values are assembled from pieces for the reason given in test_redaction.py."""

import re
from pathlib import Path

import pytest
import qdrant_edge as qe

from griot import ask, common, index_code, index_commits, index_platform, logdb, redaction, repos


def T(*parts: str) -> str:
    return "".join(parts)


TOKEN = T("ghp_", "A1b2C3d4E5f6G7h8I9j0", "K1l2M3n4O5p6Q7r8")
JWT = T("eyJhbGciOiJIUzI1NiJ9", ".", "eyJzdWIiOiIxMjM0NTY3ODkwIn0", ".", "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U")
SRC = Path(common.__file__).parent


@pytest.fixture(autouse=True)
def _fake_embed(monkeypatch):
    sent = []

    def fake(texts, **kw):
        sent.extend(texts)
        return [[0.1] * common.EMBED_DIM for _ in texts]

    monkeypatch.setattr(common, "embed_texts", fake)
    common.take_redactions()
    return sent


def _stored_contents():
    client, out, offset = common.get_client(), [], None
    while True:
        points, offset = client.scroll(qe.ScrollRequest(limit=200, offset=offset, with_payload=True, with_vector=False))
        out += [p.payload["content"] for p in points]
        if offset is None:
            return out


# --- writing -------------------------------------------------------------------


def test_a_value_is_replaced_before_the_text_is_cut_so_no_piece_of_it_survives():
    """Placed across the 1500-character boundary: cut first, each half would
    look like nothing and both would be stored."""
    text = "x" * 1490 + " " + JWT + " " + "y" * 400
    chunks = common.chunk_text(text, where="repo/file.ts")
    joined = "".join(chunks)
    assert "[REDACTED:jwt]" in joined
    for piece in (JWT[:30], JWT[-30:], JWT[40:70]):
        assert piece not in joined


def test_chunking_records_what_was_replaced_and_where():
    common.chunk_text("token " + TOKEN, where="repo/config.yml")
    assert common.take_redactions() == [("repo/config.yml", "github-token")]
    assert common.take_redactions() == [], "taking them clears the record"


def test_text_without_credentials_is_chunked_exactly_as_before():
    text = "line\n" * 900
    assert "".join(c[:1300] for c in common.chunk_text(text)) and common.chunk_text(text) == common.chunk_text(text, where="x")
    assert common.take_redactions() == []


def test_a_document_that_was_never_chunked_is_still_covered(_fake_embed):
    doc = {"id": "repo:tag:v1", "content": "release signed with " + TOKEN,
           "metadata": {"source_type": "tag", "repo": "repo", "tag_name": "v1"}}
    common.index_documents([doc])
    assert all(TOKEN not in text for text in _fake_embed), "the value was sent to the embedding model"
    assert all(TOKEN not in content for content in _stored_contents())
    assert ("repo:tag:v1", "github-token") in common.take_redactions()


def test_a_dry_run_reports_without_embedding(_fake_embed):
    doc = {"id": "repo:tag:v1", "content": "signed with " + TOKEN,
           "metadata": {"source_type": "tag", "repo": "repo", "tag_name": "v1"}}
    common.count_pending([doc])
    assert _fake_embed == []
    assert common.take_redactions() == [("repo:tag:v1", "github-token")]


def test_indexing_the_same_redacted_document_twice_embeds_it_once(_fake_embed):
    def doc():
        return {"id": "repo:tag:v1", "content": "signed with " + TOKEN,
                "metadata": {"source_type": "tag", "repo": "repo", "tag_name": "v1"}}
    assert common.index_documents([doc()])[:2] == (1, 0)
    assert common.index_documents([doc()])[:2] == (0, 1)


def test_what_an_older_version_stored_raw_is_rewritten_by_the_next_run(_fake_embed):
    """Its stored hash is the hash of the raw text, so the redacted document
    is seen as changed, embedded again and written over it."""
    import hashlib
    raw = "signed with " + TOKEN
    client = common.get_client()
    client.update(qe.UpdateOperation.upsert_points([qe.Point(
        id=common.stable_id("repo:tag:v1"), vector={"dense": [0.1] * common.EMBED_DIM},
        payload={"source_type": "tag", "repo": "repo", "tag_name": "v1", "content": raw,
                 "content_hash": hashlib.md5(raw.encode()).hexdigest()})]))
    client.flush()
    assert any(TOKEN in c for c in _stored_contents())
    common.index_documents([{"id": "repo:tag:v1", "content": raw,
                             "metadata": {"source_type": "tag", "repo": "repo", "tag_name": "v1"}}])
    assert all(TOKEN not in c for c in _stored_contents())


def test_no_source_cuts_text_without_saying_where_it_came_from():
    """`where` is what lets a run tell the person WHICH file to look at."""
    offenders = []
    for path in sorted(SRC.glob("index_*.py")):
        code = "\n".join(line for line in path.read_text().splitlines() if not line.lstrip().startswith("#"))
        for match in re.finditer(r"common\.chunk_text\(([^)]*)\)", code):
            if "where=" not in match.group(1):
                offenders.append(f"{path.name}: chunk_text({match.group(1)})")
    assert offenders == []


# --- through the real indexers ------------------------------------------------------


def _register(tmp_path):
    path = tmp_path / "a" / "proj"
    path.mkdir(parents=True)
    repos.add_repo(str(path))
    return path


def test_index_code_says_which_file_and_stores_no_value(tmp_path, capsys, _fake_embed):
    path = _register(tmp_path)
    (path / "config.yml").write_text("github:\n  token: " + TOKEN + "\n")
    (path / "clean.py").write_text("x = 1\n")
    index_code.main(["--repo", "proj"])
    out = capsys.readouterr().out
    assert "credential-looking" in out and "proj/config.yml" in out and "github-token" in out
    assert TOKEN not in out
    assert all(TOKEN not in c for c in _stored_contents()) and all(TOKEN not in t for t in _fake_embed)
    assert logdb.read_since(common.LOG_DIR, "runs", days=1)[-1]["redacted"] == 1


def test_index_code_dry_run_says_it_too(tmp_path, capsys):
    path = _register(tmp_path)
    (path / "config.yml").write_text("token: " + TOKEN + "\n")
    index_code.main(["--repo", "proj", "--dry-run"])
    out = capsys.readouterr().out
    assert "credential-looking" in out and "proj/config.yml" in out


def test_a_clean_run_says_nothing_about_credentials(tmp_path, capsys):
    path = _register(tmp_path)
    (path / "clean.py").write_text("x = 1\n")
    index_code.main(["--repo", "proj"])
    assert "credential" not in capsys.readouterr().out
    assert logdb.read_since(common.LOG_DIR, "runs", days=1)[-1]["redacted"] == 0


def test_a_commit_message_is_covered(git_repo, capsys, _fake_embed):
    sha = git_repo.commit("rotate key", body="old one was " + TOKEN, filename="a.py")
    repos.add_repo(str(git_repo.path))
    index_commits.main(["--repo", git_repo.path.name])
    assert all(TOKEN not in c for c in _stored_contents()) and all(TOKEN not in t for t in _fake_embed)
    assert sha[:12] in capsys.readouterr().out


def test_platform_text_is_covered():
    docs = index_platform.build_chunks("PR body with " + TOKEN, "repo:merge_request:7", {"source_type": "merge_request", "repo": "repo"})
    assert all(TOKEN not in d["content"] for d in docs)
    assert common.take_redactions() == [("repo:merge_request:7", "github-token")]


# --- reading: what is already stored ----------------------------------------------------


class _Hit:
    def __init__(self, content):
        self.score = 0.9
        self.id = "00000000-0000-0000-0000-000000000001"
        self.payload = {"repo": "repo", "source_type": "code", "file_path": "config.yml", "content": content}


def test_stored_text_replaces_a_value_and_leaves_clean_text_alone():
    assert common.stored_text({"content": "token " + TOKEN}) == "token [REDACTED:github-token]"
    assert common.stored_text({"content": "x = 1"}) == "x = 1"
    assert common.stored_text({}) == "" and common.stored_text({"content": None}) == ""


def test_search_over_mcp_never_returns_a_stored_value(monkeypatch):
    from griot import mcp_server
    monkeypatch.setattr(common, "search", lambda *a, **k: [_Hit("token: " + TOKEN)])
    result = mcp_server.griot_search("where is the token")
    assert TOKEN not in str(result) and "[REDACTED:github-token]" in result["results"][0]["content"]


def test_search_in_the_cli_never_prints_a_stored_value(monkeypatch, capsys):
    from griot import cli
    monkeypatch.setattr(common, "search", lambda *a, **k: [_Hit("token: " + TOKEN)])
    cli.main(["search", "where is the token"])
    out = capsys.readouterr().out
    assert TOKEN not in out and "[REDACTED:github-token]" in out


def test_the_context_sent_to_a_chat_model_never_carries_a_stored_value():
    assert TOKEN not in ask.build_context([_Hit("token: " + TOKEN)])


def test_quality_check_does_not_send_a_stored_value_back_to_the_embedding_model(monkeypatch):
    from griot import quality_check
    queries = []
    point = _Hit("token: " + TOKEN)
    class _Shard:
        def close(self):
            pass

    monkeypatch.setattr(quality_check, "_open_shard_for_sampling", lambda collection: _Shard())
    monkeypatch.setattr(quality_check, "sample_points", lambda client, n: [point])
    monkeypatch.setattr(common, "search", lambda q, limit=5: queries.append(q) or [point])
    quality_check.run_self_check(common.COLLECTION_NAME, sample_size=1)
    assert queries and all(TOKEN not in q for q in queries)


def _reads_of_stored_content(tree):
    """Every place a module takes `"content"` out of something: `x.get("content")`
    and `x["content"]` (loaded, not assigned). By syntax, not by line: a read
    split across lines or through an intermediate variable is still one."""
    import ast
    found = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == "content"):
            found.append(node.lineno)
        if (isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load)
                and isinstance(node.slice, ast.Constant) and node.slice.value == "content"):
            found.append(node.lineno)
    return found


# Reads of a key named "content" that are NOT the text of a stored point:
# responses of provider and platform APIs, and documents still being built.
NOT_STORED_POINTS = {
    "common.py",       # stored_text() itself, the chat API responses, documents on their way in
    "redaction.py",    # the audit, which exists to look at the raw text
    "platforms.py",    # fields of platform API responses
}


def test_nothing_reads_stored_content_except_through_the_one_function():
    """The rule, not today's four call sites: any new reader of a stored
    point's text would bypass the replacement."""
    import ast
    offenders = []
    for path in sorted(SRC.glob("*.py")):
        if path.name in NOT_STORED_POINTS:
            continue
        offenders += [f"{path.name}:{line}" for line in _reads_of_stored_content(ast.parse(path.read_text()))]
    assert offenders == []


@pytest.mark.parametrize("code", [
    'def f(r):\n    p = r.payload\n    return p.get("content")\n',
    'def f(r):\n    return getattr(r, "payload")["content"]\n',
    'def f(r):\n    return r.payload.get(\n        "content")\n',
    'def f(r):\n    return (r.payload or {}).get("content", "")\n',
])
def test_the_rule_sees_a_read_however_it_is_written(code):
    import ast
    assert _reads_of_stored_content(ast.parse(code))


# --- what is SHOWN about a point: names come from the repository too ----------------------


def test_a_token_in_a_branch_name_is_not_printed_by_the_run_report(capsys):
    name = "fix/" + TOKEN
    common._split_pending([{"id": f"repo:branch:{name}", "content": f"Branch: {name}",
                            "metadata": {"source_type": "branch", "repo": "repo", "branch_name": name}}], common.get_client())
    common.report_redactions()
    out = capsys.readouterr().out
    assert TOKEN not in out and "repo:branch:fix/[REDACTED:github-token]" in out


def test_control_characters_in_a_name_do_not_reach_the_terminal_through_the_report(capsys):
    common.chunk_text("token " + TOKEN, where="repo/a\x1b[2J\x07\nNothing found.py")
    common.report_redactions()
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\x07" not in out
    assert "\nNothing found" not in out


@pytest.mark.parametrize("payload", [
    {"source_type": "branch", "repo": "r", "branch_name": "fix/" + TOKEN},
    {"source_type": "tag", "repo": "r", "tag_name": "v1-" + TOKEN},
    {"source_type": "code", "repo": "r", "file_path": "deploy/" + TOKEN + ".sh"},
])
def test_a_label_never_carries_a_credential_shaped_name(payload):
    assert TOKEN not in ask.source_label(payload)


def test_audit_does_not_let_a_stored_name_write_to_the_terminal(capsys):
    _store_raw("r:code:x:0", "token: " + TOKEN, source_type="code", repo="r\nFAKE-LINE: clean",
               file_path="a\x1b[2J\x07\nFound nothing", chunk_index=0)
    assert redaction.main([]) == 1
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\x07" not in out
    assert not any(line.startswith(("FAKE-LINE", "Found nothing")) for line in out.splitlines())


def test_search_in_the_cli_does_not_let_stored_text_write_to_the_terminal(monkeypatch, capsys):
    from griot import cli

    class Hostile(_Hit):
        def __init__(self):
            super().__init__("\x1b]0;owned\x07first line")
            self.payload["file_path"] = "a\x1b[2J.py"

    monkeypatch.setattr(common, "search", lambda *a, **k: [Hostile()])
    cli.main(["search", "anything"])
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\x07" not in out and "first line" in out


def test_the_remote_url_warning_never_prints_the_credentials_in_it(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(index_platform, "_remote_url",
                        lambda repo_path: "https://ci:" + TOKEN + "@git.internal.example/team/repo.git")
    assert index_platform.build_documents(tmp_path) == []
    out = capsys.readouterr().out
    assert TOKEN not in out and "ci:" not in out and "git.internal.example" in out


# --- wiring of the sources that the two tests above do not reach -----------------------------


def _last_run():
    return logdb.read_since(common.LOG_DIR, "runs", days=1)[-1]


def test_index_tags_reports_and_records(git_repo, capsys):
    from griot import index_tags
    git_repo.commit("one")
    git_repo.tag("v1", "signed with " + TOKEN)
    repos.add_repo(str(git_repo.path))
    name = git_repo.path.name
    index_tags.main(["--repo", name, "--dry-run"])
    assert "credential-looking" in capsys.readouterr().out
    index_tags.main(["--repo", name])
    assert "credential-looking" in capsys.readouterr().out and _last_run()["redacted"] == 1
    assert all(TOKEN not in c for c in _stored_contents())


def test_index_branches_reports_and_records(git_repo, capsys):
    from griot import index_branches
    git_repo.commit("one")
    git_repo.branch("feature")
    import subprocess
    subprocess.run(["git", "-C", str(git_repo.path), "checkout", "-q", "feature"], check=True)
    git_repo.commit("use " + TOKEN, filename="b.py")
    subprocess.run(["git", "-C", str(git_repo.path), "checkout", "-q", "main"], check=True)
    git_repo.set_remote_head("main")
    repos.add_repo(str(git_repo.path))
    name = git_repo.path.name
    index_branches.main(["--repo", name, "--dry-run"])
    assert "credential-looking" in capsys.readouterr().out
    index_branches.main(["--repo", name])
    assert "credential-looking" in capsys.readouterr().out and _last_run()["redacted"] >= 1
    assert all(TOKEN not in c for c in _stored_contents())


def test_index_platform_reports_and_records(tmp_path, monkeypatch, capsys):
    path = _register(tmp_path)
    monkeypatch.setattr(index_platform, "build_documents", lambda repo_path, repo_key=None, fetches=None: index_platform.build_chunks(
        "PR body with " + TOKEN, "proj:merge_request:7", {"source_type": "merge_request", "repo": "proj", "mr_iid": 7}))
    index_platform.main(["--repo", "proj", "--dry-run"])
    assert "credential-looking" in capsys.readouterr().out
    index_platform.main(["--repo", "proj"])
    assert "credential-looking" in capsys.readouterr().out and _last_run()["redacted"] == 1
    assert all(TOKEN not in c for c in _stored_contents())


# --- auditing what is already indexed ------------------------------------------------------


def _store_raw(doc_id, content, **metadata):
    import hashlib
    client = common.get_client()
    client.update(qe.UpdateOperation.upsert_points([qe.Point(
        id=common.stable_id(doc_id), vector={"dense": [0.1] * common.EMBED_DIM},
        payload={**metadata, "content": content, "content_hash": hashlib.md5(content.encode()).hexdigest()})]))
    client.flush()


def test_audit_lists_where_stored_values_are_and_never_the_values(capsys):
    _store_raw("repo:code:config.yml:0", "token: " + TOKEN, source_type="code", repo="repo", file_path="config.yml", chunk_index=0)
    _store_raw("repo:code:clean.py:0", "x = 1", source_type="code", repo="repo", file_path="clean.py", chunk_index=0)
    assert redaction.main([]) == 1
    out = capsys.readouterr().out
    assert "repo/config.yml" in out and "github-token" in out and "clean.py" not in out
    assert TOKEN not in out


def test_audit_of_a_clean_index_exits_zero(capsys):
    _store_raw("repo:code:clean.py:0", "x = 1", source_type="code", repo="repo", file_path="clean.py", chunk_index=0)
    assert redaction.main([]) == 0
    assert "nothing" in capsys.readouterr().out.lower()


def test_audit_is_reachable_from_the_cli(capsys):
    from griot import cli
    _store_raw("repo:code:clean.py:0", "x = 1", source_type="code", repo="repo", file_path="clean.py", chunk_index=0)
    assert cli.main(["audit"]) == 0


@pytest.mark.parametrize("where", ["ask --show-sources", "golden-set add", "the too-large list"])
def test_every_place_the_cli_prints_a_name_goes_through_the_same_cleaning(where, monkeypatch, capsys, tmp_path):
    """A name reaches the terminal in more places than search: the rule is
    one function, `common.shown`, and these are its other callers."""
    hostile = "a\x1b[2J\x07b"
    if where == "ask --show-sources":
        hit = _Hit("text")
        hit.payload["file_path"] = hostile + ".py"
        monkeypatch.setattr(ask, "ask", lambda question, model, limit: ("answer", [hit]))
        ask.main(["anything", "--show-sources"])
    elif where == "golden-set add":
        from griot import golden_set
        hit = _Hit("text")
        hit.payload["file_path"] = hostile + ".py"
        monkeypatch.setattr(common, "search", lambda *a, **k: [hit])
        monkeypatch.setattr("builtins.input", lambda prompt="": "")
        golden_set.cmd_add("anything")
    else:
        (tmp_path / (hostile + ".sql")).write_text("x" * (index_code.MAX_FILE_BYTES + 1))
        index_code.discover_files(tmp_path)
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\x07" not in out
