"""No module outside common.py reaches one of common.py's private names.

A private name is common.py's to rename, split or drop without looking
elsewhere. One used across the module boundary breaks silently when it does
(an AttributeError at the first call, in whatever path the suite did not
run), or worse keeps working on a meaning common.py no longer gives it.
What other modules need has a public name in common.py.

Tests are held to the same rule with one difference: some genuinely need an
internal (to reset process state between tests, to inject a failure at a
seam no public call reaches, to shrink a delay, or to check a helper in
isolation). Those names are listed below, each with why, so that a new
reach into common.py from a test is a decision someone wrote down rather
than a habit. The list is checked both ways: a name used and not listed
fails, and a listed name no test uses any more fails too, so the list
shrinks when a need goes away.

Everything is read with `ast` from the source itself, so a private name
added to common.py later is covered without editing this file."""

import ast
import pathlib
import re

from griot import common

_PACKAGE = pathlib.Path(common.__file__).parent
_TESTS = pathlib.Path(__file__).parent

_STATE = "process state of common.py, reset between tests or inspected after a call"
_SEAM = "a failure or a fake injected where no public call reaches"
_TUNABLE = "a delay, interval or size shrunk so the test runs in milliseconds"
_HELPER = "a helper checked on its own, where going through its callers would hide which case failed"

# The private names of common.py tests may reach, and why.
TESTS_MAY_REACH = {
    "_client": _STATE,
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
    "_inject_keychain_credentials": _SEAM,
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


def _common_aliases(tree: ast.AST) -> set[str]:
    """The names a module binds to common.py: `from griot import common`,
    `from . import common as c`, `import griot.common as c`."""
    aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            aliases.update(a.asname or a.name for a in node.names if a.name == "common")
        elif isinstance(node, ast.Import):
            aliases.update(a.asname for a in node.names if a.name.endswith(".common") and a.asname)
    return aliases


def _is_common(node: ast.AST, aliases: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in aliases
    # `import griot.common` then griot.common._x
    return (isinstance(node, ast.Attribute) and node.attr == "common"
            and isinstance(node.value, ast.Name) and node.value.id == "griot")


_DOTTED = re.compile(r"griot\.common\.(\w+)")


def reaches(source: str, private: set[str]) -> list[tuple[int, str]]:
    """(line, name) for each place `source` reaches one of `private` in
    common.py: an attribute of the module, an import from it, a name given
    as a string to setattr/getattr/monkeypatch on the module, or a dotted
    string "griot.common._x" (mock.patch, monkeypatch.setattr)."""
    tree = ast.parse(source)
    aliases = _common_aliases(tree)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in private and _is_common(node.value, aliases):
            found.append((node.lineno, node.attr))
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[-1] == "common":
            found.extend((node.lineno, a.name) for a in node.names if a.name in private)
        elif isinstance(node, ast.Call):
            args = node.args
            if (len(args) >= 2 and _is_common(args[0], aliases) and isinstance(args[1], ast.Constant)
                    and args[1].value in private):
                found.append((node.lineno, args[1].value))
            for arg in args[:1]:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    match = _DOTTED.fullmatch(arg.value)
                    if match and match.group(1) in private:
                        found.append((node.lineno, match.group(1)))
    return found


def _private_names_of_common() -> set[str]:
    return _private_names_of(pathlib.Path(common.__file__).read_text(encoding="utf-8"))


def _reaches_in(paths) -> dict[str, list[str]]:
    """{name: ["file:line", ...]} over `paths`."""
    private = _private_names_of_common()
    found: dict[str, list[str]] = {}
    for path in paths:
        for line, name in reaches(path.read_text(encoding="utf-8"), private):
            found.setdefault(name, []).append(f"{path.name}:{line}")
    return found


def test_the_private_names_are_read_from_common():
    """The guards below are only as wide as this set: if reading common.py
    stopped finding its private names, they would pass on nothing."""
    assert {"_client", "_client_last_used_at", "_http_post", "_http_lock", "_echo_stream",
            "_LOCK_RETRY_DELAYS", "_KEYCHAIN_SERVICE"} <= _private_names_of_common()
    assert not {"get_client", "log_and_print", "__future__"} & _private_names_of_common()


def test_private_names_cover_every_kind_of_definition():
    source = ("import os as _os\nfrom x import y as _y\n_a, (_b, _c) = 1, (2, 3)\n_d: int = 1\n"
              "_e += 1\ndef _f(): global _g\nasync def _h(): pass\nclass _I: pass\npublic = 1\n__all__ = []\n")
    assert _private_names_of(source) == {"_os", "_y", "_a", "_b", "_c", "_d", "_e", "_f", "_g", "_h", "_I"}


def test_reaches_finds_every_way_into_the_module():
    private = {"_x"}
    cases = {
        "from griot import common\ncommon._x()": 2,
        "from griot import common as c\nc._x": 2,
        "from . import common\ncommon._x": 2,
        "import griot.common as c\nc._x": 2,
        "import griot.common\ngriot.common._x": 2,
        "from griot.common import _x": 1,
        "from .common import a, _x": 1,
        "from griot import common\nmonkeypatch.setattr(common, '_x', 1)": 2,
        "from griot import common\ngetattr(common, '_x')": 2,
        "monkeypatch.setattr('griot.common._x', 1)": 1,
        "mock.patch('griot.common._x')": 1,
    }
    for source, line in cases.items():
        assert reaches(source, private) == [(line, "_x")], source


def test_reaches_leaves_other_objects_and_public_names_alone():
    private = {"_x"}
    for source in ("self._x", "other._x", "from griot import common\ncommon.x",
                   "from griot import other\nother._x", "from griot.other import _x",
                   "from griot import common\nsetattr(other, '_x', 1)",
                   "mock.patch('griot.other._x')", "x = 'griot.common._x'"):
        assert reaches(source, private) == [], source


def test_no_module_outside_common_reaches_a_private_name_of_it():
    """What another module of the package needs from common.py has a public
    name there; a private one is common.py's alone."""
    modules = [p for p in sorted(_PACKAGE.rglob("*.py")) if p.name != "common.py"]
    # A scan that reads nothing passes on nothing.
    assert {"mcp_server.py", "auth.py", "quality_check.py", "redaction.py", "config.py"} <= {p.name for p in modules}
    assert _reaches_in(modules) == {}


def unlisted(found: dict, allowed) -> dict:
    """The reaches of `found` ({name: places}) whose name is not allowed."""
    return {name: places for name, places in found.items() if name not in allowed}


def stale(found: dict, allowed, private: set[str]) -> list[str]:
    """Allowed names that nothing reaches any more, or that are no longer a
    private name of common.py."""
    return sorted(name for name in allowed if name not in found or name not in private)


def test_the_list_is_checked_both_ways():
    found = {"_a": ["test_x.py:3"], "_b": ["test_y.py:9"]}
    assert unlisted(found, {"_a"}) == {"_b": ["test_y.py:9"]}
    assert unlisted(found, {"_a", "_b"}) == {}
    assert stale(found, {"_a", "_gone"}, {"_a", "_b", "_gone"}) == ["_gone"]
    assert stale(found, {"_a"}, {"_b"}) == ["_a"], "a name common.py no longer has comes off too"
    assert stale(found, {"_a", "_b"}, {"_a", "_b"}) == []


def _tests_found() -> dict:
    tests = [p for p in sorted(_TESTS.rglob("*.py")) if p.name != pathlib.Path(__file__).name]
    assert {"conftest.py", "test_mcp_server.py"} <= {p.name for p in tests}
    return _reaches_in(tests)


def test_tests_reach_only_the_private_names_listed():
    """A test that reaches a private name not listed above either wants the
    public name that already exists, or adds the name to the list with why."""
    assert unlisted(_tests_found(), TESTS_MAY_REACH) == {}


def test_every_listed_name_is_still_needed():
    """A listed name no test reaches any more, or one common.py no longer has
    as a private name, comes off the list: otherwise it grows forever and
    stops saying what the tests need."""
    assert stale(_tests_found(), TESTS_MAY_REACH, _private_names_of_common()) == []
