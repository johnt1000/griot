import pytest

from griot import index_code


def _make_tree(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print(1)")
    (tmp_path / "src" / "README.md").write_text("# doc")
    (tmp_path / "src" / "image.png").write_text("not code")  # unsupported extension

    ignored = tmp_path / "node_modules" / "pacote"
    ignored.mkdir(parents=True)
    (ignored / "index.js").write_text("module.exports = 1;")  # should never be read

    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "bundle.js").write_text("//bundle")

    # A `.git` directory somewhere below, not at the root: a `.git` at the
    # root would make this a git work tree, which is listed by git and not
    # walked (tests/test_discovery_rules.py covers that side).
    nested = tmp_path / "third_party" / ".git"
    nested.mkdir(parents=True)
    (nested / "config.py").write_text("# not real code")
    return tmp_path


def test_discover_files_finds_supported_extensions(tmp_path):
    _make_tree(tmp_path)
    found = index_code.discover_files(tmp_path)
    rel = {str(p.relative_to(tmp_path)) for p in found}
    assert "src/app.py" in rel
    assert "src/README.md" in rel


def test_discover_files_ignores_unsupported_extensions(tmp_path):
    _make_tree(tmp_path)
    found = index_code.discover_files(tmp_path)
    rel = {str(p.relative_to(tmp_path)) for p in found}
    assert "src/image.png" not in rel


def test_discover_files_prunes_ignored_dirs_entirely(tmp_path):
    """The entire node_modules/dist/.git tree must never be descended into — not
    just filtered afterward. Regression of the node_modules bug where ~99 thousand
    files were scanned by mistake in a monorepo with a giant node_modules (real case)."""
    _make_tree(tmp_path)
    found = index_code.discover_files(tmp_path)
    rel = {str(p.relative_to(tmp_path)) for p in found}
    assert not any(set(f.split("/")) & index_code.IGNORE_DIRS for f in rel)
    assert "node_modules/pacote/index.js" not in rel
    assert "dist/bundle.js" not in rel
    assert "third_party/.git/config.py" not in rel


def test_discover_files_empty_directory_returns_empty_list(tmp_path):
    assert index_code.discover_files(tmp_path) == []


# --- --path (mutually exclusive with --repo, skips repos.json) -------------

def test_main_with_path_indexes_without_reading_repos_json(tmp_path, monkeypatch):
    _make_tree(tmp_path)
    calls = {}

    def fake_index_documents(documents, desc="Indexing"):
        calls["documents"] = documents
        return (len(documents), 0, 0)

    def fail_load_repos():
        raise FileNotFoundError("repos.json should not be read when --path is used")

    monkeypatch.setattr(index_code.common, "index_documents", fake_index_documents)
    monkeypatch.setattr(index_code.common, "load_repos", fail_load_repos)

    index_code.main(["--path", str(tmp_path)])

    assert calls["documents"]
    assert calls["documents"][0]["metadata"]["repo"] == tmp_path.name


def test_path_and_repo_together_is_argparse_error(tmp_path):
    with pytest.raises(SystemExit):
        index_code.main(["--path", str(tmp_path), "--repo", "some-repo"])


def test_path_disambiguates_ids_for_same_basename_different_location(tmp_path, monkeypatch):
    """Natural-id collision guard: same rel_path ('a.py') in two repos with the same final
    basename ('repo'), indexed via --path -> without the disambiguated key the
    ids would collide literally, and upserting one would overwrite the other."""
    repo1 = tmp_path / "loc1" / "repo"
    repo2 = tmp_path / "loc2" / "repo"
    repo1.mkdir(parents=True)
    repo2.mkdir(parents=True)
    (repo1 / "a.py").write_text("print('repo1')")
    (repo2 / "a.py").write_text("print('repo2')")

    calls = []

    def fake_index_documents(documents, desc="Indexing"):
        calls.append(documents)
        return (len(documents), 0, 0)

    monkeypatch.setattr(index_code.common, "index_documents", fake_index_documents)

    index_code.main(["--path", str(repo1)])
    index_code.main(["--path", str(repo2)])

    id1 = calls[0][0]["id"]
    id2 = calls[1][0]["id"]
    assert id1 != id2
    assert calls[0][0]["metadata"]["repo"] == "repo"
    assert calls[1][0]["metadata"]["repo"] == "repo"
