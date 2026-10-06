"""<config_dir>/.env as a symbolic link (debt 12): people keep it in a
dotfiles checkout and link it into place. Every write griot makes must keep
the link a link and change the file it points to, with that file 0600."""

import os
import stat
import subprocess
import sys

import pytest
from dotenv import dotenv_values

from griot import ConfigurationError, common, doctor


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture
def dotfiles(tmp_path):
    where = tmp_path / "dotfiles"
    where.mkdir()
    common.secure_mkdir(common.CONFIG_DIR)
    return where


def _linked(dotfiles, text="GRIOT_LOG_LEVEL=INFO\n", mode=0o600):
    real = dotfiles / "griot.env"
    real.write_text(text)
    real.chmod(mode)
    common.ENV_PATH.symlink_to(real)
    return real


def test_setting_a_value_keeps_the_link_and_writes_the_file_it_points_to(dotfiles):
    real = _linked(dotfiles)

    common.env_file_set("GEMINI_TOKEN", "gm-fake-1234")

    assert common.ENV_PATH.is_symlink()
    assert os.readlink(common.ENV_PATH) == str(real)
    assert dotenv_values(real) == {"GRIOT_LOG_LEVEL": "INFO", "GEMINI_TOKEN": "gm-fake-1234"}
    assert _mode(real) == 0o600


def test_the_file_behind_the_link_is_closed_to_0600_by_a_write(dotfiles):
    real = _linked(dotfiles, mode=0o644)

    common.env_file_set("GEMINI_TOKEN", "gm-fake-1234")

    assert common.ENV_PATH.is_symlink() and _mode(real) == 0o600


def test_unsetting_a_value_keeps_the_link(dotfiles):
    real = _linked(dotfiles, "GRIOT_LOG_LEVEL=INFO\nGEMINI_TOKEN=gm-old\n")

    common.env_file_unset("GEMINI_TOKEN")

    assert common.ENV_PATH.is_symlink()
    assert dotenv_values(real) == {"GRIOT_LOG_LEVEL": "INFO"}
    assert _mode(real) == 0o600


def test_a_link_to_a_relative_target_is_followed_from_where_the_link_is(dotfiles):
    real = dotfiles / "griot.env"
    real.write_text("")
    real.chmod(0o600)
    relative = os.path.relpath(real, common.ENV_PATH.parent)
    common.ENV_PATH.symlink_to(relative)

    common.env_file_set("GEMINI_TOKEN", "gm-fake-1234")

    assert common.ENV_PATH.is_symlink()
    assert os.readlink(common.ENV_PATH) == relative, "the link is left exactly as the user made it"
    assert dotenv_values(real) == {"GEMINI_TOKEN": "gm-fake-1234"}


def test_a_chain_of_links_keeps_every_link_and_writes_the_last_file(dotfiles):
    real = dotfiles / "griot.env"
    real.write_text("")
    middle = dotfiles / "current.env"
    middle.symlink_to(real)
    common.ENV_PATH.symlink_to(middle)

    common.env_file_set("GEMINI_TOKEN", "gm-fake-1234")
    common.env_file_unset("GEMINI_TOKEN")
    common.env_file_set("GRIOT_LOG_LEVEL", "DEBUG")

    assert common.ENV_PATH.is_symlink() and middle.is_symlink()
    assert dotenv_values(real) == {"GRIOT_LOG_LEVEL": "DEBUG"}
    assert _mode(real) == 0o600


def test_a_dangling_link_gets_the_template_written_where_it_points(dotfiles):
    real = dotfiles / "griot.env"
    common.ENV_PATH.symlink_to(real)

    common.ensure_env_template()

    assert common.ENV_PATH.is_symlink()
    assert "GRIOT_EMBED_PROFILE" in dotenv_values(real)
    assert _mode(real) == 0o600


def test_a_dangling_link_into_a_missing_directory_is_left_alone_by_the_template(dotfiles):
    """The template runs on every command: a link into a directory that is
    gone must not stop them all, and griot does not make directories in
    someone else's tree to satisfy it."""
    missing = dotfiles / "gone" / "griot.env"
    common.ENV_PATH.symlink_to(missing)

    common.ensure_env_template()

    assert common.ENV_PATH.is_symlink() and not missing.parent.exists()


def test_a_link_loop_is_left_alone_by_the_template_and_refused_by_a_write(dotfiles):
    loop = dotfiles / "loop.env"
    loop.symlink_to(common.ENV_PATH)
    common.ENV_PATH.symlink_to(loop)

    common.ensure_env_template()
    with pytest.raises(ConfigurationError):
        common.env_file_set("GEMINI_TOKEN", "gm-fake-1234")

    assert common.ENV_PATH.is_symlink() and loop.is_symlink()


def test_writing_through_a_dangling_link_into_a_missing_directory_says_where_it_leads(dotfiles):
    missing = dotfiles / "gone" / "griot.env"
    common.ENV_PATH.symlink_to(missing)

    with pytest.raises(ConfigurationError) as raised:
        common.env_file_set("GEMINI_TOKEN", "gm-fake-1234")

    assert str(common.ENV_PATH) in str(raised.value) and str(missing.parent) in str(raised.value)
    assert common.ENV_PATH.is_symlink() and not missing.parent.exists()


def test_the_permission_repair_closes_the_file_behind_the_link(dotfiles, caplog):
    real = _linked(dotfiles, mode=0o644)

    common._check_env_file_permissions(common.ENV_PATH)

    assert common.ENV_PATH.is_symlink() and _mode(real) == 0o600


def test_doctor_says_a_dangling_link_is_not_a_file_waiting_to_be_written(dotfiles):
    missing = dotfiles / "gone" / "griot.env"
    common.ENV_PATH.symlink_to(missing)

    check = doctor.check_settings(common.ENV_PATH)

    assert check["status"] == doctor.WARN
    assert str(missing) in check["detail"] and "does not exist yet" not in check["detail"]


def test_the_real_cli_keeps_the_link(tmp_path):
    """Through the commands a person runs, in a fresh process: `griot config
    set`, `griot config unset`, `griot profiles use` and `griot auth set`."""
    config = tmp_path / "config" / "griot"
    config.mkdir(mode=0o700, parents=True)
    real = tmp_path / "dotfiles" / "griot.env"
    real.parent.mkdir()
    real.write_text("")
    real.chmod(0o600)
    (config / ".env").symlink_to(real)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GRIOT_", "RAG_"))}
    env.update(GRIOT_CONFIG_DIR=str(config.parent), GRIOT_DATA_DIR=str(tmp_path / "data"))

    def griot(*argv, stdin=None):
        done = subprocess.run([sys.executable, "-m", "griot.cli", *argv], env=env, capture_output=True, text=True,
                              timeout=120, input=stdin)
        assert done.returncode == 0, (argv, done.stdout, done.stderr)

    griot("config", "set", "chat-model", "some-model")
    griot("config", "unset", "chat-model")
    griot("profiles", "use", "bge-small")
    griot("auth", "set", "gemini", stdin="gm-fake-1234\n")

    assert (config / ".env").is_symlink()
    values = dotenv_values(real)
    assert values["GRIOT_EMBED_PROFILE"] == "bge-small" and values["GEMINI_TOKEN"] == "gm-fake-1234"
    assert "GRIOT_CHAT_MODEL" not in values
    assert _mode(real) == 0o600


def test_the_cli_says_in_one_line_that_a_link_leads_nowhere(tmp_path):
    config = tmp_path / "config" / "griot"
    config.mkdir(mode=0o700, parents=True)
    missing = tmp_path / "gone" / "griot.env"
    (config / ".env").symlink_to(missing)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GRIOT_", "RAG_"))}
    env.update(GRIOT_CONFIG_DIR=str(config.parent), GRIOT_DATA_DIR=str(tmp_path / "data"))

    done = subprocess.run([sys.executable, "-m", "griot.cli", "config", "set", "chat-model", "some-model"], env=env,
                          capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)

    assert done.returncode == 2 and "Traceback" not in done.stderr, done.stderr
    assert done.stderr.startswith("Error: ") and missing.parent.name in done.stderr
    assert (config / ".env").is_symlink() and not missing.parent.exists()
