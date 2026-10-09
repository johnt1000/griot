"""`keyring` is a dependency of griot-rag, not an opt-in extra.

With it optional, `pipx install griot-rag` (the install command the README
gives) stored every credential in plaintext in <config>/.env, and the only
way out was knowing about an extra. Now the keychain is used wherever a
backend is reachable; where none is (Linux without a Secret Service, a
headless server, CI, keyring's `fail` backend), griot still falls back to the
file, and `griot auth set`, `griot auth list` and `griot doctor` say that the
key is in plaintext, why, and how to get a backend.

The real `keyring` is imported here only with PYTHON_KEYRING_BACKEND naming
its `fail` backend (conftest sets it for the whole suite), and each test
checks that this is the backend in use before anything asks it for a
credential: the real OS keychain is never reached."""

import importlib.metadata
import os
import sys
from pathlib import Path

import pytest
from packaging.requirements import Requirement

from griot import auth, common, doctor

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
FAIL_BACKEND = "keyring.backends.fail.Keyring"
SECRET = "sk-" + "k" * 40


# --- the package declares it ---------------------------------------------------------------------


def test_the_project_requires_keyring_unconditionally():
    names = {Requirement(text).name: Requirement(text) for text in PROJECT["dependencies"]}
    assert "keyring" in names and names["keyring"].marker is None


def test_the_installed_distribution_requires_keyring_without_an_extra():
    """What pip and pipx read when they install the wheel: a requirement
    with no `extra == ...` marker is installed for everyone."""
    requirements = [Requirement(text) for text in importlib.metadata.requires(PROJECT["name"]) or []]
    keyring = [r for r in requirements if r.name == "keyring"]
    assert keyring and any(r.marker is None for r in keyring)


def test_the_old_keychain_extra_still_resolves():
    """The README told people to install the `keychain` extra: it stays,
    empty, so a command naming it keeps installing without a warning."""
    assert PROJECT["optional-dependencies"].get("keychain") == []


def test_the_release_check_imports_keyring_from_the_built_wheel():
    """The CI job that installs the built wheel in a clean environment is the
    one place an undeclared dependency shows: griot itself imports keyring
    lazily and would fall back to plaintext without a word of failure."""
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert '/tmp/fresh/bin/python -c "import keyring"' in ci


# --- the fallback is said, with keyring's real fail backend ---------------------------------------


@pytest.fixture
def fail_backend(monkeypatch):
    """The real keyring module, on its `fail` backend (what keyring picks when
    nothing is reachable). Refuses to run on any other backend."""
    assert os.environ.get("PYTHON_KEYRING_BACKEND") == FAIL_BACKEND
    monkeypatch.delitem(sys.modules, "keyring", raising=False)
    import keyring

    backend = keyring.get_keyring()
    if f"{type(backend).__module__}.{type(backend).__name__}" != FAIL_BACKEND:
        pytest.fail(f"refusing to run against {type(backend)!r}: only the fail backend is safe here")
    monkeypatch.setattr(common, "EXPORTED_BEFORE_ENV_FILE", {})
    for env_var in common.credential_env_vars().values():
        monkeypatch.delenv(env_var, raising=False)
    common.secure_mkdir(common.CONFIG_DIR)
    return backend


def _says_plaintext_and_why(text):
    assert "plaintext" in text and str(common.ENV_PATH) in text
    assert "no OS keychain backend reachable" in text
    # Why: the variable that chose the fail backend is named.
    assert f"PYTHON_KEYRING_BACKEND={FAIL_BACKEND}" in text
    # How to get one, and never the old extra, which fixes nothing now.
    assert "Secret Service" in text
    assert "griot[keychain]" not in text and "griot-rag[keychain]" not in text
    assert SECRET not in text


def test_the_fail_backend_is_installed_but_unreachable(fail_backend):
    assert common.keychain_status() == {"available": False, "backend": None, "installed": True}


def test_set_says_the_key_went_to_plaintext_and_why(fail_backend, monkeypatch, capsys):
    monkeypatch.setattr(auth.getpass, "getpass", lambda prompt: SECRET)

    assert auth.cmd_set("openai") == 0

    _says_plaintext_and_why(capsys.readouterr().out)


def test_list_says_the_keys_are_in_plaintext_and_why(fail_backend, capsys):
    common.env_file_set("GRIOT_OPENAI_API_KEY", SECRET)

    assert auth.cmd_list() == 0

    out = capsys.readouterr().out
    _says_plaintext_and_why(out)
    openai = next(line for line in out.splitlines() if line.strip().startswith("openai"))
    assert "plaintext" in openai


def test_doctor_says_the_keys_are_in_plaintext_and_why(fail_backend):
    common.env_file_set("GRIOT_OPENAI_API_KEY", SECRET)

    check = next(c for c in doctor.run_checks() if c["check"] == "credentials")

    assert check["status"] == "ok"
    _says_plaintext_and_why(check["detail"])


def test_without_the_variable_the_why_does_not_name_it(fail_backend, monkeypatch):
    """The variable is named only when it is what chose the backend."""
    monkeypatch.delenv("PYTHON_KEYRING_BACKEND")

    phrase = auth.keychain_phrase({"available": False, "backend": None, "installed": True})

    assert "PYTHON_KEYRING_BACKEND" not in phrase and "Secret Service" in phrase


def test_a_missing_keyring_says_reinstall_griot_not_an_extra():
    """conftest's default: `import keyring` fails. keyring is a dependency
    now, so its absence is a broken install, and the extra is no fix."""
    phrase = auth.keychain_phrase(common.keychain_status())

    assert "no OS keychain backend reachable" in phrase and "plaintext" in phrase
    assert "reinstall griot-rag" in phrase
    assert "[keychain]" not in phrase
