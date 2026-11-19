"""A row of `griot doctor` that carries more than one command prints each on
its own line, so every one of them can be pasted.

The ledger appends a condition's action to a row's own fix. Joined on one
line, a fix that ends in a shell comment (`griot index all   # and read what
it says`) swallowed the command after it: pasted, `griot index keywords`
never ran."""

from types import SimpleNamespace

from griot import doctor


def _fixes(report: str) -> list[str]:
    return [line.split("→", 1)[1].strip() for line in report.splitlines() if "→" in line]


def test_each_command_of_a_row_is_on_its_own_line():
    row = doctor._check("index", doctor.WARN, "the last indexing run died", "griot index all")
    keyword = SimpleNamespace(id="keyword_vectors_missing", severity=doctor.WARN, sentence="keyword search is not built",
                              action="griot index keywords")

    fixes = _fixes(doctor.format_report([doctor._with_condition(row, keyword)]))

    assert "griot index keywords" in fixes
    assert "griot index all" in fixes


def test_no_command_follows_a_comment_on_the_same_line():
    first = SimpleNamespace(id="one", severity=doctor.WARN, sentence="one", action="griot index all   # first")
    second = SimpleNamespace(id="two", severity=doctor.WARN, sentence="two", action="griot index keywords")

    for fix in _fixes(doctor.format_report([doctor._from_ledger("index", [first, second])])):
        if "#" in fix:
            assert "griot " not in fix.split("#", 1)[1], fix
