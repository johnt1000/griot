"""No griot module reaches another griot module's private names.

A private name is its module's to rename, split or drop without looking
elsewhere. One used across the module boundary breaks silently when it does
(an AttributeError at the first call, in whatever path the suite did not
run), or worse keeps working on a meaning its module no longer gives it.
What another module needs has a public name in the module that owns it.

Tests are held to the same rule with one difference: some genuinely need an
internal (to reset process state between tests, to inject a failure at a
seam no public call reaches, to shrink a delay, or to check a helper in
isolation). Those names are listed below, per module and each with why, so
that a new reach into a module from a test is a decision someone wrote down
rather than a habit. The list is checked both ways: a name used and not
listed fails, and a listed name no test uses any more fails too, so the list
shrinks when a need goes away.

Everything is read with `ast` from the source itself, so a module or a
private name added to the package later is covered without editing this
file."""

import ast
import pathlib
import re

import pytest

from griot import common

_PACKAGE = pathlib.Path(common.__file__).parent
_TESTS = pathlib.Path(__file__).parent

_STATE = "process state of the module, reset between tests or inspected after a call"
_SEAM = "a failure or a fake injected where no public call reaches"
_TUNABLE = "a delay, interval or size shrunk so the test runs in milliseconds"
_HELPER = "a helper checked on its own, where going through its callers would hide which case failed"
_SHARED = "a value of the module the expectation is built from, so retuning it does not break the test"

# The private names tests may reach, per module, and why.
TESTS_MAY_REACH = {
    "common": {
        "_client": _STATE,
        "_keychain_cache": _STATE,
        "_client_last_used_at": _STATE,
        "_embed_model": _STATE,
        "_echo_stream": _STATE,
        "_logger": _STATE,
        "_last_log_prune": _STATE,
        "_log_prune_lock": _STATE,
        "_progress": _STATE,
        "_progress_clock": _STATE,
        "_progress_chunks": _STATE,
        "_http_post": _SEAM,
        "_new_http_session": _SEAM,
        "_drop_http_session": _SEAM,
        "_openai_compatible_post_with_retry": _SEAM,
        "_gemini_post_with_retry": _SEAM,
        "_text_embedding_class": _SEAM,
        "_note_embedding_failure": _SEAM,
        "_lock_owner_is_alive": _SEAM,
        "_read_lock": _SEAM,
        "_today": _SEAM,
        "_load_shard": _SEAM,
        "_copy_into_keyword_collection": _SEAM,
        "_swap_in": _SEAM,
        "_write_stale_details": _SEAM,
        "_split_pending": _SEAM,
        "_fill_missing_keyword_vectors": _SEAM,
        "_restore_interrupted_keyword_swap": _SEAM,
        "_keyword_rebuild_paths": _SEAM,
        "_secure_collection_dir": _SEAM,
        "_check_env_file_permissions": _SEAM,
        "_ensure_log_handler": _SEAM,
        "_KEYCHAIN_SERVICE": _SEAM,
        "_LOCK_RETRY_DELAYS": _TUNABLE,
        "_PRUNE_GUARD_MIN_POINTS": _TUNABLE,
        "_FULL_CHECK_INTERVAL": _TUNABLE,
        "_STATUS_READ_PATIENCE": _TUNABLE,
        "_RACY_WINDOW_NS": _TUNABLE,
        "_PRUNE_PAGE": _TUNABLE,
        "_LOG_PRUNE_INTERVAL_SECONDS": _TUNABLE,
        "_KEYWORD_BATCH": _TUNABLE,
        "_project_of": _HELPER,
        "_optional_float_env": _HELPER,
        "_griot_role": _HELPER,
        "_is_the_project_s": _HELPER,
        "_amount_env": _HELPER,
        "_repair_mode": _HELPER,
        "_own_home": _HELPER,
        "_is_griot_command": _HELPER,
        "_ids_a_point_could_have": _HELPER,
        "_collection_config": _HELPER,
        "_clean_project": _HELPER,
        "_point": _HELPER,
        "_is_inside": _HELPER,
        "_estimated_spend_was_said": _HELPER,
        "_LEGACY_ENV_RENAMES": _HELPER,
        "_GIT_ENV_ALLOWED": _HELPER,
    },
    "auth": {
        "_ensure_env_file": _HELPER,
        "_mask": _HELPER,
        "_providers": _HELPER,
    },
    "cli": {
        "_main": _SEAM,
        "_run_index_source": _SEAM,
    },
    "config": {
        "_in_file": _SEAM,
        "_TRUE": _SHARED,
        "_FALSE": _SHARED,
    },
    "doctor": {
        "_settings_in_file": _HELPER,
    },
    "freshness": {
        "_commits_between": _SEAM,
    },
    "golden_set": {
        "_ask": _HELPER,
        "_show": _HELPER,
        "_must_include_entry": _HELPER,
        "_reject": _HELPER,
        "_save": _HELPER,
        "_unlike_note": _HELPER,
        "_unlike_the_check": _HELPER,
    },
    "harnesses": {
        "_griot_command": _SEAM,
        "_is_interactive": _SEAM,
        "_model_note": _SEAM,
        "_resources_root": _SEAM,
        "_run_harness_command": _SEAM,
        "_which": _SEAM,
        "_write_settings": _SEAM,
        "_summary": _HELPER,
        "_write_file": _HELPER,
    },
    "index_branches": {
        "_PER_CALL": _TUNABLE,
    },
    "index_platform": {
        "_remote_url": _SEAM,
        "_url_field": _SEAM,
    },
    "jobs": {
        "_loaded": _STATE,
        "_registry": _STATE,
        "_quality_check_lock": _STATE,
        "_run_dry_run": _SEAM,
        "_remove_progress_file": _HELPER,
    },
    "logdb": {
        "_connect": _STATE,
        "_ensure_tool_calls_project_column": _HELPER,
        "_tool_call_rows": _HELPER,
    },
    "mcp_server": {
        "_alone_queue": _STATE,
        "_inflight": _STATE,
        "_KEYWORD_NOTE_GIVEN_FOR": _STATE,
        "_tool_started": _STATE,
        "_tool_finished": _STATE,
        "_Ask": _SEAM,
        "_ask_assist_install": _SEAM,
        "_ask_golden_set_add": _SEAM,
        "_ask_index_repo": _SEAM,
        "_curated_cases_to_run": _SEAM,
        "_installation_removed": _SEAM,
        "_log_search": _SEAM,
        "_record_call": _SEAM,
        "_start_idle_reaper": _SEAM,
        "_WAIT_FOR_OTHER_CALLS_SECONDS": _TUNABLE,
        "_NO_CHANNEL": _SHARED,
        "_READ_ONLY_BUT_ASKED": _SHARED,
        "_assist_install_question": _HELPER,
        "_golden_set_add_question": _HELPER,
        "_golden_set_remove_question": _HELPER,
        "_index_repo_question": _HELPER,
        "_profiles_delete_question": _HELPER,
        "_repos_add_question": _HELPER,
        "_repos_remove_question": _HELPER,
        "_cli_command": _HELPER,
        "_confirmed": _HELPER,
        "_keep_stdout_for_the_protocol": _HELPER,
        "_progress_numbers": _HELPER,
        "_prompt": _HELPER,
        "_tool": _HELPER,
        "_reaper_interval": _HELPER,
        "_records_call": _HELPER,
        "_release_for_a_subprocess": _HELPER,
        "_release_if_idle": _HELPER,
        "_resolve_ask": _HELPER,
    },
    "platforms": {
        "_get_with_retry": _SEAM,
        "_gitea_matches": _HELPER,
    },
    "quality_check": {
        "_open_shard_for_sampling": _SEAM,
        "_matches": _HELPER,
    },
    "repos": {
        "_load": _STATE,
    },
    "retrieval_eval": {
        "_commits_with_files": _HELPER,
    },
    "stats": {
        "_BAR_EMPTY": _SHARED,
        "_BAR_FULL": _SHARED,
        "_EIGHTHS": _SHARED,
        "_INDENT": _SHARED,
        "_MAX_CHART_WIDTH": _SHARED,
        "_MIN_BAR_WIDTH": _SHARED,
        "_MIN_CHART_WIDTH": _SHARED,
        "_SPARK_LEVELS": _SHARED,
        "_ago": _HELPER,
        "_bar": _HELPER,
        "_chart_width": _HELPER,
        "_classify_source_label": _HELPER,
        "_fill_days": _HELPER,
        "_filter_by_days": _HELPER,
        "_sparkline": _HELPER,
    },
}


def _private_names_of(source: str) -> set[str]:
    """Every private name a module defines at its top level: functions,
    classes, assignments (tuple targets too), imports bound under a private
    name, and names a function makes global."""
    names = set()
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
    names.update(n for node in ast.walk(tree) if isinstance(node, ast.Global) for n in node.names)
    return {n for n in names if n.startswith("_") and not n.startswith("__")}


def _from_the_package(node: ast.ImportFrom) -> bool:
    """`from griot import x` or `from . import x`: x is a griot module."""
    return node.module == "griot" or (node.level > 0 and node.module is None)


def _is_from(node: ast.ImportFrom, module: str) -> bool:
    """`from griot.<module> import ...` or `from .<module> import ...`, and
    not some other package's module of the same name."""
    return node.module == f"griot.{module}" or (node.level > 0 and node.module == module)


def _module_aliases(tree: ast.AST, module: str) -> set[str]:
    """The names a source binds to griot.<module>: `from griot import m`,
    `from . import m as c`, `import griot.m as c`."""
    aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and _from_the_package(node):
            aliases.update(a.asname or a.name for a in node.names if a.name == module)
        elif isinstance(node, ast.Import):
            aliases.update(a.asname for a in node.names if a.name == f"griot.{module}" and a.asname)
    return aliases


def _is_module(node: ast.AST, module: str, aliases: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in aliases
    # `import griot.<module>` then griot.<module>._x
    return (isinstance(node, ast.Attribute) and node.attr == module
            and isinstance(node.value, ast.Name) and node.value.id == "griot")


_DOTTED = re.compile(r"griot\.(\w+)\.(\w+)")


def reaches(source: str, module: str, private: set[str]) -> list[tuple[int, str]]:
    """(line, name) for each place `source` reaches one of `private` in
    griot.<module>: an attribute of the module, an import from it, a name
    given as a string to setattr/getattr/monkeypatch on the module, or a
    dotted string "griot.<module>._x" (mock.patch, monkeypatch.setattr)."""
    return _reaches_in_tree(ast.parse(source), module, private)


def _reaches_in_tree(tree: ast.AST, module: str, private: set[str]) -> list[tuple[int, str]]:
    aliases = _module_aliases(tree, module)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in private and _is_module(node.value, module, aliases):
            found.append((node.lineno, node.attr))
        elif isinstance(node, ast.ImportFrom) and _is_from(node, module):
            found.extend((node.lineno, a.name) for a in node.names if a.name in private)
        elif isinstance(node, ast.Call):
            args = node.args
            if (len(args) >= 2 and _is_module(args[0], module, aliases) and isinstance(args[1], ast.Constant)
                    and args[1].value in private):
                found.append((node.lineno, args[1].value))
            for arg in args[:1]:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    match = _DOTTED.fullmatch(arg.value)
                    if match and match.group(1) == module and match.group(2) in private:
                        found.append((node.lineno, match.group(2)))
    return found


def _modules(package: pathlib.Path = _PACKAGE) -> dict[str, pathlib.Path]:
    """{module: path} for every module of the package. A module in a
    subpackage would be reached as griot.<sub>.<module>, which `reaches`
    does not read: the assertion makes such a module a decision here."""
    paths = sorted(package.rglob("*.py"))
    assert all(p.parent == package for p in paths), "a subpackage needs the guard taught its dotted name"
    return {p.stem: p for p in paths if p.stem != "__init__"}


def _private_names() -> dict[str, set[str]]:
    """{module: its private names}."""
    return {module: _private_names_of(path.read_text(encoding="utf-8")) for module, path in _modules().items()}


def _reaches_in(paths) -> dict[tuple[str, str], list[str]]:
    """{(module, name): ["file:line", ...]} over `paths`. A module uses its
    own private names by their bare name, which `reaches` does not count."""
    private = _private_names()
    found: dict[tuple[str, str], list[str]] = {}
    for path in paths:
        # Parsed once and walked per module: parsing per module made the
        # scan of the suite take half a minute.
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for module, names in private.items():
            for line, name in _reaches_in_tree(tree, module, names):
                found.setdefault((module, name), []).append(f"{path.name}:{line}")
    return found


def test_the_private_names_are_read_from_every_module():
    """The guards below are only as wide as these sets: if reading a module
    stopped finding its private names, they would pass on nothing."""
    private = _private_names()
    assert {"common", "stats", "doctor", "mcp_server", "config", "harnesses"} <= set(private)
    assert {"_client", "_client_last_used_at", "_http_post", "_http_lock", "_echo_stream",
            "_LOCK_RETRY_DELAYS", "_KEYCHAIN_SERVICE"} <= private["common"]
    assert not {"get_client", "log_and_print", "__future__"} & private["common"]
    assert len([m for m, names in private.items() if names]) >= 15, "most modules have private names"


def test_a_module_in_a_subpackage_stops_the_scan(tmp_path):
    """Its private names would go unguarded, reached under a dotted name the
    scan does not read, so the scan refuses rather than pass on them."""
    (tmp_path / "top.py").write_text("")
    assert set(_modules(tmp_path)) == {"top"}
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "inner.py").write_text("")
    with pytest.raises(AssertionError, match="subpackage"):
        _modules(tmp_path)


def test_private_names_cover_every_kind_of_definition():
    source = ("import os as _os\nfrom x import y as _y\n_a, (_b, _c) = 1, (2, 3)\n_d: int = 1\n"
              "_e += 1\ndef _f(): global _g\nasync def _h(): pass\nclass _I: pass\npublic = 1\n__all__ = []\n")
    assert _private_names_of(source) == {"_os", "_y", "_a", "_b", "_c", "_d", "_e", "_f", "_g", "_h", "_I"}


def test_reaches_finds_every_way_into_the_module():
    private = {"_x"}
    cases = {
        "from griot import stats\nstats._x()": 2,
        "from griot import stats as c\nc._x": 2,
        "from . import stats\nstats._x": 2,
        "from . import common, stats\nstats._x": 2,
        "import griot.stats as c\nc._x": 2,
        "import griot.stats\ngriot.stats._x": 2,
        "from griot.stats import _x": 1,
        "from .stats import a, _x": 1,
        "from griot import stats\nmonkeypatch.setattr(stats, '_x', 1)": 2,
        "from griot import stats\ngetattr(stats, '_x')": 2,
        "monkeypatch.setattr('griot.stats._x', 1)": 1,
        "mock.patch('griot.stats._x')": 1,
    }
    for source, line in cases.items():
        assert reaches(source, "stats", private) == [(line, "_x")], source


def test_reaches_leaves_other_objects_and_public_names_alone():
    private = {"_x"}
    for source in ("self._x", "other._x", "from griot import stats\nstats.x",
                   "from griot import other\nother._x", "from griot.other import _x",
                   "from griot import stats\nsetattr(other, '_x', 1)",
                   "from griot import stats\nmonkeypatch.setattr(stats, 'x', 1)",
                   "mock.patch('griot.other._x')", "x = 'griot.stats._x'",
                   # Another package's module of the same name is not griot's.
                   "from elsewhere import stats\nstats._x", "from elsewhere.stats import _x",
                   "import elsewhere.stats as stats\nstats._x", "mock.patch('elsewhere.stats._x')"):
        assert reaches(source, "stats", private) == [], source


def test_no_module_reaches_a_private_name_of_another():
    """What a module of the package needs from another has a public name
    there; a private one is its own module's alone."""
    modules = sorted(_PACKAGE.rglob("*.py"))
    # A scan that reads nothing passes on nothing.
    assert {"doctor.py", "stats.py", "mcp_server.py", "cli.py", "common.py"} <= {p.name for p in modules}
    assert _reaches_in(modules) == {}


def unlisted(found: dict, allowed) -> dict:
    """The reaches of `found` ({key: places}) whose key is not allowed."""
    return {key: places for key, places in found.items() if key not in allowed}


def stale(found: dict, allowed, private) -> list:
    """Allowed keys that nothing reaches any more, or that are no longer a
    private name of their module."""
    return sorted(key for key in allowed if key not in found or key not in private)


def test_the_list_is_checked_both_ways():
    found = {"_a": ["test_x.py:3"], "_b": ["test_y.py:9"]}
    assert unlisted(found, {"_a"}) == {"_b": ["test_y.py:9"]}
    assert unlisted(found, {"_a", "_b"}) == {}
    assert stale(found, {"_a", "_gone"}, {"_a", "_b", "_gone"}) == ["_gone"]
    assert stale(found, {"_a"}, {"_b"}) == ["_a"], "a name the module no longer has comes off too"
    assert stale(found, {"_a", "_b"}, {"_a", "_b"}) == []


def _allowed() -> set[tuple[str, str]]:
    return {(module, name) for module, names in TESTS_MAY_REACH.items() for name in names}


def _tests_found() -> dict:
    tests = [p for p in sorted(_TESTS.rglob("*.py")) if p.name != pathlib.Path(__file__).name]
    assert {"conftest.py", "test_mcp_server.py", "test_doctor.py"} <= {p.name for p in tests}
    return _reaches_in(tests)


def test_tests_reach_only_the_private_names_listed():
    """A test that reaches a private name not listed above either wants the
    public name that already exists, or adds the name to the list with why."""
    assert unlisted(_tests_found(), _allowed()) == {}


def test_every_listed_name_is_still_needed():
    """A listed name no test reaches any more, or one its module no longer
    has as a private name, comes off the list: otherwise it grows forever and
    stops saying what the tests need."""
    private = {(module, name) for module, names in _private_names().items() for name in names}
    assert stale(_tests_found(), _allowed(), private) == []
