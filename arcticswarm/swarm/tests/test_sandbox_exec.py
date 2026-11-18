# Copyright 2026 Snowflake Inc.
# SPDX-License-Identifier: Apache-2.0
"""Tests for sandboxed bash / python_execute (tools.exec_backend: wsl).

Unit tests (always run) check settings resolution, tool schema/description
switching and package-spec validation. Integration tests run real commands in the
WSL sandbox distro and are skipped unless that distro exists (Windows + WSL2 with
``arcticswarm-sandbox`` configured by the folder-RAG harness).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from arcticswarm.tools.bash import BashTool
from arcticswarm.tools.python_execute import PythonExecuteTool
from arcticswarm.tools.sandbox_exec import ExecSettings, WslSandbox, settings_from_config

DISTRO = "arcticswarm-sandbox"


def _distro_available() -> bool:
    if sys.platform != "win32" or shutil.which("wsl.exe") is None:
        return False
    try:
        out = subprocess.run(["wsl.exe", "-l", "-q"], capture_output=True, timeout=30).stdout
    except Exception:
        return False
    return DISTRO in out.decode("utf-16-le", errors="ignore")


needs_wsl = pytest.mark.skipif(not _distro_available(), reason="WSL sandbox distro not available")


def test_settings_from_config_defaults_and_overrides():
    s = settings_from_config(SimpleNamespace())
    assert s.backend == "local" and not s.is_wsl
    s = settings_from_config(SimpleNamespace(exec_backend="wsl", exec_corpus_root="/corpus/x", exec_bash_timeout=7))
    assert s.is_wsl and s.corpus_root == "/corpus/x" and s.bash_timeout == 7


def test_tools_switch_description_and_schema():
    local_bash = BashTool()
    assert "Linux sandbox" not in local_bash.description
    wsl = ExecSettings(backend="wsl")
    assert "Linux sandbox" in BashTool(exec_settings=wsl).description
    py = PythonExecuteTool(exec_settings=wsl)
    assert "packages" in py.parameters_schema()["properties"]
    assert "packages" not in PythonExecuteTool().parameters_schema()["properties"]


def test_rejects_bad_package_specs():
    sb = WslSandbox(ExecSettings(backend="wsl"))
    rc, _, err, _ = sb.python("print(1)", timeout=5, packages=["ok-pkg", "evil; rm -rf /"])
    assert rc == 2 and "Rejected" in err


@needs_wsl
def test_wsl_bash_corpus_root_isolation_and_no_keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-should-not-leak")
    tool = BashTool(exec_settings=ExecSettings(backend="wsl", corpus_root="/corpus"))
    res = tool.execute(command='echo "root=$CORPUS_ROOT"; whoami; env | grep -ci anthropic; ls /mnt')
    out = res.output or ""
    assert "root=/corpus" in out and "root" in out
    assert "\n0\n" in out  # no ANTHROPIC_* variables inside the sandbox
    assert "c\n" not in out.split("\n0\n")[-1]  # no Windows drive mount listed


@needs_wsl
def test_wsl_bash_timeout_and_exit_code():
    tool = BashTool(exec_settings=ExecSettings(backend="wsl"))
    assert tool.execute(command="exit 3").is_error
    res = tool.execute(command="sleep 10", timeout=2)
    assert res.is_error and "timed out" in (res.error or "")


@needs_wsl
def test_wsl_python_runs_in_agent_venv():
    tool = PythonExecuteTool(exec_settings=ExecSettings(backend="wsl"))
    res = tool.execute(code="import sys, pandas, bm25s; print(sys.prefix)")
    assert not res.is_error, res.error
    assert "/opt/agent-venv" in res.output
