"""A test whose temporary directory is deleted from outside the run says so.

On 2026-10-06 test_the_tags_source_reads_every_tag failed once with
`assert set() == {'v1', 'v2'}`. The cause was not in griot: two agents had
chosen the same scratch path, and one ran `rm -rf` on it while the other's
full run used a directory below it as pytest's --basetemp. The test's
repository disappeared between creating its tags and listing them; git
failed, the tags source reads a failed listing as "no tags", and the
failure read as a flaky test. conftest now names that cause instead."""

from pathlib import Path

pytest_plugins = ("pytester",)

CONFTEST = Path(__file__).resolve().parent / "conftest.py"


def _run(pytester, body):
    pytester.makeconftest(CONFTEST.read_text())
    pytester.makepyfile(test_inner=body)
    return pytester.runpytest_subprocess("-q", "-p", "no:cacheprovider")


def test_a_temporary_directory_deleted_during_the_test_is_named_as_the_cause(pytester):
    result = _run(pytester, """
import shutil

def test_something_outside_deletes_it(tmp_path):
    (tmp_path / "repo").mkdir()
    shutil.rmtree(tmp_path)
""")
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*was deleted while the test ran*"])


def test_a_temporary_directory_left_in_place_changes_nothing(pytester):
    result = _run(pytester, """
def test_ordinary(tmp_path):
    (tmp_path / "repo").mkdir()
""")
    result.assert_outcomes(passed=1)
