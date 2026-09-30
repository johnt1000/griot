"""What `griot index code` reads from a repository.

In a git work tree: what git tracks plus new files it would track, that is,
everything the repository does not ignore. A file the repository ignores is
ignored here too, which is where local settings and notes with secrets usually
live; on a paid profile they would be sent to the embedding API. Work that is
not added yet is kept: in real repositories it is a large share of the files.
Outside git the tree is walked as before.

In both cases a symlinked file is never followed (its target can be anywhere
on the machine), files over a size ceiling are skipped and reported, and
generated noise (lockfiles, minified bundles, tool caches) stays out."""

import os
import subprocess

import pytest

from griot import index_code


def _rel(found, root):
    return sorted(str(p.relative_to(root)) for p in found)


@pytest.fixture
def repo(git_repo):
    git_repo.commit("first", filename="tracked.py")
    return git_repo.path


# --- git work tree: what git tracks ---------------------------------------------


def test_a_file_the_repository_ignores_is_not_indexed(repo):
    (repo / ".gitignore").write_text("local_settings.py\nnotes/\n")
    (repo / "local_settings.py").write_text("SECRET = 'x'\n")
    (repo / "notes").mkdir()
    (repo / "notes" / "private.md").write_text("# private\n")
    assert _rel(index_code.discover_files(repo), repo) == ["tracked.py"]


def test_a_new_file_that_is_not_ignored_is_indexed_before_it_is_added(repo):
    """Work in progress is most of some repositories. Only ignoring a file
    keeps it out; not having run `git add` yet does not."""
    (repo / "draft.py").write_text("x = 1\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "adr-001.md").write_text("# decision\n")
    assert _rel(index_code.discover_files(repo), repo) == ["docs/adr-001.md", "draft.py", "tracked.py"]


def test_a_file_listed_twice_by_git_is_read_once(repo):
    (repo / "draft.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "-N", "draft.py"], check=True)
    found = index_code.discover_files(repo)
    assert len(found) == len(set(found)) == 2


def test_files_ignored_by_the_local_exclude_file_are_left_out_too(repo):
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "exclude").write_text("private-notes.md\n")
    (repo / "private-notes.md").write_text("# private\n")
    assert _rel(index_code.discover_files(repo), repo) == ["tracked.py"]


def test_a_tracked_file_in_a_generated_directory_is_still_left_out(repo):
    (repo / "dist").mkdir()
    (repo / "dist" / "bundle.js").write_text("//bundle\n")
    subprocess.run(["git", "-C", str(repo), "add", "-f", "dist/bundle.js"], check=True)
    assert _rel(index_code.discover_files(repo), repo) == ["tracked.py"]


def test_a_subdirectory_of_a_repository_lists_only_its_own_files(repo):
    (repo / ".gitignore").write_text("secret.py\n")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "mod.py").write_text("y = 2\n")
    (repo / "pkg" / "scratch.py").write_text("z = 3\n")
    (repo / "pkg" / "secret.py").write_text("KEY = 'x'\n")
    subprocess.run(["git", "-C", str(repo), "add", "pkg/mod.py"], check=True)
    assert _rel(index_code.discover_files(repo / "pkg"), repo / "pkg") == ["mod.py", "scratch.py"]


def test_a_tracked_file_deleted_from_disk_is_skipped_not_an_error(repo):
    (repo / "tracked.py").unlink()
    assert index_code.discover_files(repo) == []


def test_names_with_spaces_newlines_and_unicode_survive(repo):
    names = ["with space.py", "naïve.py", "new\nline.py"]
    for name in names:
        (repo / name).write_text("x = 1\n")
        subprocess.run(["git", "-C", str(repo), "add", "--", name], check=True)
    assert _rel(index_code.discover_files(repo), repo) == sorted(names + ["tracked.py"])


# --- outside git: the walk is kept -------------------------------------------------


def test_a_directory_that_is_not_a_repository_is_walked(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.md").write_text("# b\n")
    assert _rel(index_code.discover_files(tmp_path), tmp_path) == ["a.py", "sub/b.md"]


@pytest.mark.parametrize("cache", [".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".turbo", ".cache"])
def test_tool_caches_are_never_walked(tmp_path, cache):
    (tmp_path / cache).mkdir()
    (tmp_path / cache / "README.md").write_text("# cache\n")
    (tmp_path / "a.py").write_text("x = 1\n")
    assert _rel(index_code.discover_files(tmp_path), tmp_path) == ["a.py"]


# --- both: symlinks, size, noise -----------------------------------------------------


@pytest.fixture(params=["git", "plain"])
def tree(request, tmp_path, git_repo):
    """The same rules hold in a work tree and in a plain directory. `add`
    makes a path visible to discovery in either."""
    if request.param == "plain":
        root = tmp_path / "plain"
        root.mkdir()
        return root, lambda *paths: None
    git_repo.commit("first", filename="tracked.py")
    (git_repo.path / "tracked.py").unlink()
    subprocess.run(["git", "-C", str(git_repo.path), "rm", "-q", "--cached", "tracked.py"], check=True)

    def add(*paths):
        subprocess.run(["git", "-C", str(git_repo.path), "add", "-f", "--", *paths], check=True)

    return git_repo.path, add


def test_a_symlinked_file_is_never_followed(tree, tmp_path):
    root, add = tree
    outside = tmp_path / "outside-secret.md"
    outside.write_text("aws_secret_access_key = not-real\n")
    os.symlink(outside, root / "notes.md")
    (root / "real.md").write_text("# real\n")
    add("notes.md", "real.md")
    assert _rel(index_code.discover_files(root), root) == ["real.md"]


def test_a_symlink_to_a_file_inside_the_repository_is_skipped_too(tree):
    """The target is already indexed under its own name; following the link
    would index it twice, and telling inside from outside is one resolve()
    away from being wrong."""
    root, add = tree
    (root / "real.md").write_text("# real\n")
    os.symlink(root / "real.md", root / "alias.md")
    add("real.md", "alias.md")
    assert _rel(index_code.discover_files(root), root) == ["real.md"]


def test_a_file_over_the_size_ceiling_is_skipped_and_reported(tree, capsys):
    root, add = tree
    (root / "small.sql").write_text("select 1;\n")
    (root / "dump.sql").write_text("x" * (index_code.MAX_FILE_BYTES + 1))
    add("small.sql", "dump.sql")
    assert _rel(index_code.discover_files(root), root) == ["small.sql"]
    out = capsys.readouterr().out
    assert "dump.sql" in out and "larger than" in out


def test_a_file_exactly_at_the_ceiling_is_kept(tree):
    root, add = tree
    (root / "edge.py").write_text("x" * index_code.MAX_FILE_BYTES)
    add("edge.py")
    assert _rel(index_code.discover_files(root), root) == ["edge.py"]


@pytest.mark.parametrize("name", ["pnpm-lock.yaml", "app.min.js", "styles.min.css", "vendor.MIN.js"])
def test_lockfiles_and_minified_bundles_are_left_out(tree, name):
    root, add = tree
    (root / name).write_text("generated\n")
    (root / "a.py").write_text("x = 1\n")
    add(name, "a.py")
    assert _rel(index_code.discover_files(root), root) == ["a.py"]


NEW_EXTENSIONS = [".tsx", ".jsx", ".sql", ".yml", ".yaml", ".mjs", ".cjs", ".tf", ".toml", ".mdx"]


@pytest.mark.parametrize("ext", NEW_EXTENSIONS)
def test_the_extensions_real_repositories_use_are_indexed(tree, ext):
    root, add = tree
    (root / f"file{ext}").write_text("content\n")
    add(f"file{ext}")
    assert _rel(index_code.discover_files(root), root) == [f"file{ext}"]


def test_the_result_is_sorted_so_two_runs_agree(tree):
    root, add = tree
    for name in ("b.py", "a.py", "c.py"):
        (root / name).write_text("x = 1\n")
    add("b.py", "a.py", "c.py")
    found = index_code.discover_files(root)
    assert found == sorted(found)


# --- end to end: the content that must not leave ----------------------------------------


def test_the_content_of_a_symlink_target_never_becomes_a_document(tree, tmp_path):
    root, add = tree
    outside = tmp_path / "credentials"
    outside.write_text("TOP-SECRET-MARKER\n")
    os.symlink(outside, root / "notes.md")
    (root / "real.md").write_text("# real\n")
    add("notes.md", "real.md")
    documents = index_code.process_repository(root)
    assert "TOP-SECRET-MARKER" not in "".join(d["content"] for d in documents)
    assert {d["metadata"]["file_path"] for d in documents} == {"real.md"}


def test_the_bundled_skill_names_every_extension_that_is_indexed():
    """The list lives in prose for people and agents to read, so it is
    checked against the code rather than trusted to stay in step. Whole
    tokens: `.ts` must not pass because `.tsx` is there."""
    import re
    from griot import harnesses
    skill = (harnesses._resources_root() / "skills" / "griot-indexing" / "SKILL.md").read_text()
    named = set(re.findall(r"(?<![\w*])\.[a-z]+\b", skill))
    assert sorted(index_code.SUPPORTED_EXTENSIONS - named) == []


# --- fail closed: a git work tree that git cannot list --------------------------------


def _break_git(repo):
    (repo / ".git" / "HEAD").write_text("not a ref\n")


def test_a_work_tree_git_cannot_list_is_not_walked_instead(repo, capsys):
    """Walking would index exactly what the rule keeps out: the ignored
    files. A repository whose listing fails is read not at all."""
    (repo / ".gitignore").write_text("local_settings.py\n")
    (repo / "local_settings.py").write_text("SECRET = 'x'\n")
    _break_git(repo)
    assert index_code.discover_files(repo) == []
    out = capsys.readouterr().out
    assert "could not list" in out and "nothing is read" in out


def test_a_subdirectory_of_a_work_tree_git_cannot_list_is_not_walked_either(repo):
    (repo / "pkg").mkdir()
    (repo / "pkg" / "scratch.py").write_text("z = 3\n")
    _break_git(repo)
    assert index_code.discover_files(repo / "pkg") == []


def test_a_directory_whose_files_are_all_ignored_says_so(repo, capsys):
    (repo / ".gitignore").write_text("scratch/\n")
    (repo / "scratch").mkdir()
    (repo / "scratch" / "a.py").write_text("x = 1\n")
    assert index_code.discover_files(repo / "scratch") == []
    assert "git lists no files under" in capsys.readouterr().out


# --- symlinks anywhere on the way ---------------------------------------------------


def test_a_directory_swapped_for_a_symlink_does_not_lead_out_of_the_repository(repo, tmp_path):
    """git lists `d/a.md` by name. If `d` has since become a link to
    somewhere else, the name now reaches a file outside the repository."""
    (repo / "d").mkdir()
    (repo / "d" / "a.md").write_text("# inside\n")
    subprocess.run(["git", "-C", str(repo), "add", "d/a.md"], check=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.md").write_text("OUTSIDE-MARKER\n")
    (repo / "d").rename(repo / "d.real")
    os.symlink(outside, repo / "d")
    assert "d/a.md" not in _rel(index_code.discover_files(repo), repo)
    documents = index_code.process_repository(repo)
    assert "OUTSIDE-MARKER" not in "".join(d["content"] for d in documents)


def test_a_file_swapped_for_a_symlink_after_discovery_is_not_read(tree, tmp_path, monkeypatch):
    """Discovery and reading are two moments. What is opened is checked
    again, by the open itself."""
    root, add = tree
    outside = tmp_path / "credentials"
    outside.write_text("TOP-SECRET-MARKER\n")
    (root / "a.md").write_text("# fine\n")
    add("a.md")
    found = index_code.discover_files(root)
    (root / "a.md").unlink()
    os.symlink(outside, root / "a.md")
    monkeypatch.setattr(index_code, "discover_files", lambda repo_path: found)
    documents = index_code.process_repository(root)
    assert "TOP-SECRET-MARKER" not in "".join(d["content"] for d in documents)


def test_a_file_that_grew_past_the_ceiling_after_discovery_is_not_read(tree, monkeypatch):
    root, add = tree
    (root / "a.md").write_text("# fine\n")
    add("a.md")
    found = index_code.discover_files(root)
    (root / "a.md").write_text("x" * (index_code.MAX_FILE_BYTES + 1))
    monkeypatch.setattr(index_code, "discover_files", lambda repo_path: found)
    assert index_code.process_repository(root) == []


def test_the_size_note_speaks_in_the_same_unit_as_the_docs(tree, capsys):
    root, add = tree
    (root / "dump.sql").write_text("x" * (index_code.MAX_FILE_BYTES + 1))
    add("dump.sql")
    index_code.discover_files(root)
    assert "larger than 1 MB" in capsys.readouterr().out
