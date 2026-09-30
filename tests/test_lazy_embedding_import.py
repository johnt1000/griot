"""fastembed (and onnxruntime under it) is loaded when a local model is first
used, not when griot starts. Every `griot` command and every MCP server start
imported it, a few hundred milliseconds each, including the ones that never
embed locally: `griot stats`, `griot repos list`, any run on an API profile."""

import os
import subprocess
import sys

import pytest

from griot import common


def _run(code: str, tmp_path, **extra) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GRIOT_", "RAG_"))}
    env.update(GRIOT_CONFIG_DIR=str(tmp_path / "config"), GRIOT_DATA_DIR=str(tmp_path / "data"), **extra)
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize("module", [
    "griot.common", "griot.mcp_server", "griot.stats", "griot.index_code", "griot.index_commits", "griot.cli",
    "griot.ask", "griot.quality_check", "griot.jobs", "griot.harnesses", "griot.golden_set", "griot.retrieval_eval",
])
def test_importing_griot_does_not_import_the_local_embedding_library(module, tmp_path):
    done = _run(f"import sys, {module}; heavy = sorted(m for m in sys.modules if m.split('.')[0] in "
                f"('fastembed', 'onnxruntime')); print(heavy); sys.exit(1 if heavy else 0)", tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_model_class_is_the_real_one_when_asked_for():
    from fastembed import TextEmbedding
    assert common._text_embedding_class() is TextEmbedding


def test_the_model_is_built_from_that_class_once(monkeypatch, tmp_path):
    built = []

    class Fake:
        def __init__(self, **kwargs):
            built.append(kwargs)

    monkeypatch.setattr(common, "_embed_model", None)
    monkeypatch.setattr(common, "DATA_DIR", tmp_path)
    monkeypatch.setattr(common, "_text_embedding_class", lambda: Fake)

    first, second = common.get_embed_model(), common.get_embed_model()

    assert first is second and len(built) == 1
    assert built[0]["model_name"] == common.ACTIVE_PROFILE["model"]
