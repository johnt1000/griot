"""The suite never reaches the OS keychain, from any process it starts.

conftest.py makes `import keyring` fail inside the test process, which only
covers that process. A test that runs the real CLI in a subprocess
(`python -m griot.cli auth set ...`) imported the real `keyring` there:
on a machine with the `keychain` extra installed, the fake test credential
went into the user's login keychain under the service "griot" (it happened,
2026-10-06, through tests/test_env_symlink.py). The environment a
subprocess inherits now names keyring's `fail` backend, so a subprocess
started from it degrades to the file, as the test process does.
"""

import os
import subprocess
import sys

FAIL_BACKEND = "keyring.backends.fail.Keyring"
PROBE = ("import importlib.util\n"
         "if importlib.util.find_spec('keyring') is None:\n"
         "    print('absent')\n"
         "else:\n"
         "    import keyring\n"
         "    print(type(keyring.get_keyring()).__module__)\n")


def test_the_environment_subprocesses_inherit_names_the_fail_backend():
    assert os.environ.get("PYTHON_KEYRING_BACKEND") == FAIL_BACKEND


def test_a_subprocess_started_from_the_suite_cannot_reach_the_os_keychain():
    done = subprocess.run([sys.executable, "-c", PROBE], capture_output=True, text=True, env=dict(os.environ))

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() in {"absent", "keyring.backends.fail"}


def _functions_that_start_griot_with_a_hand_built_environment():
    """Functions in tests/ that start a Python process running griot with an
    environment written out by hand: those do not inherit the variable, and
    are where the next leak would come from (tests/test_config_dirs.py's
    helper read the real keychain that way, by importing griot.common)."""
    import ast
    from pathlib import Path

    def runs_griot(text):
        return any(marker in text for marker in ('"-m", "griot', "'-m', 'griot", "from griot", "import griot"))

    found = []
    for path in sorted(Path(__file__).parent.glob("test_*.py")):
        text = path.read_text()
        tree = ast.parse(text)
        # Code handed to `python -c` is often a module-level constant.
        griot_code = {target.id for node in tree.body if isinstance(node, ast.Assign)
                      and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
                      and runs_griot(node.value.value) for target in node.targets if isinstance(target, ast.Name)}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            source = ast.get_source_segment(text, node) or ""
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            starts_python = "sys.executable" in source and (runs_griot(source) or bool(names & griot_code))
            hand_built = any(isinstance(n, ast.Dict) and any(
                isinstance(k, ast.Constant) and k.value in ("PATH", "HOME") for k in n.keys) for n in ast.walk(node))
            inherits = "os.environ)" in source or "**os.environ" in source or "os.environ.items()" in source
            if starts_python and hand_built and not inherits and "PYTHON_KEYRING_BACKEND" not in source:
                found.append(f"{path.name}:{node.lineno} {node.name}")
    return found


def test_no_test_starts_griot_in_an_environment_that_reaches_the_keychain():
    assert _functions_that_start_griot_with_a_hand_built_environment() == []
