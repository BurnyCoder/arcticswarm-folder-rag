# Copyright 2026 Snowflake Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Sandboxed code execution for ``bash`` / ``python_execute`` (``tools.exec_backend``).

Global context
--------------
For retrieval-augmented generation over a local folder, agents must be able to run
*arbitrary* code over the files (grep/rg/find, parsers, pandas, …) and install any
tool or library they need. Running that on the host is unsafe and, on Windows, the
original ``shell=True`` path is cmd.exe. With ``exec_backend: wsl`` every command runs
as root inside a dedicated WSL2 distro (default ``arcticswarm-sandbox``) that the
harness configures with ``/etc/wsl.conf``: ``[automount] enabled=false`` (no Windows
drives visible) and ``[interop] enabled=false`` (no Windows executables). Windows
environment variables — including API keys — are not forwarded (no ``WSLENV``).

Local design
------------
* The command / code is **base64-encoded into a small bash wrapper** that is piped to
  ``wsl.exe -d <distro> -u root -- bash -s`` over stdin *as bytes*. This sidesteps
  Windows command-line quoting and text-mode ``\\n`` -> ``\\r\\n`` translation.
* The wrapper creates a per-agent scratch dir ``/work/<id>``, exports ``CORPUS_ROOT``,
  puts ``/opt/agent-venv/bin`` first on ``PATH`` (so ``python``/``pip``/``uv pip`` hit
  the agent venv), sets an optional soft proxy guard (``http(s)_proxy`` pointing at a
  dead port, ``no_proxy`` = package hosts) and runs the payload under coreutils
  ``timeout`` so runaway processes inside the distro are killed, not orphaned.
* ``timeout`` exits 124 on expiry; that is reported as a timeout to the agent.
"""

from __future__ import annotations

import base64
import logging
import re
import subprocess
import uuid
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Package names/specs accepted by python_execute(packages=[...]) — PEP 508-ish subset.
_PKG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-\[\],<>=!~]*$")
_AGENT_VENV = "/opt/agent-venv"


@dataclass
class ExecSettings:
    """Resolved execution settings (from ``ArcticswarmConfig.exec_*``)."""

    backend: str = "local"
    distro: str = "arcticswarm-sandbox"
    corpus_root: str = ""
    python: str = ""
    bash_timeout: int = 60
    python_timeout: int = 300
    proxy: str = ""
    no_proxy: str = ""

    @property
    def is_wsl(self) -> bool:
        return self.backend.strip().lower() == "wsl"


def settings_from_config(config: Any) -> ExecSettings:
    """Build :class:`ExecSettings` from an agent config (missing fields -> defaults)."""
    d = ExecSettings()
    return ExecSettings(
        backend=getattr(config, "exec_backend", d.backend) or d.backend,
        distro=getattr(config, "exec_wsl_distro", d.distro) or d.distro,
        corpus_root=getattr(config, "exec_corpus_root", "") or "",
        python=getattr(config, "exec_python", "") or "",
        bash_timeout=int(getattr(config, "exec_bash_timeout", d.bash_timeout) or d.bash_timeout),
        python_timeout=int(getattr(config, "exec_python_timeout", d.python_timeout) or d.python_timeout),
        proxy=getattr(config, "exec_proxy", "") or "",
        no_proxy=getattr(config, "exec_no_proxy", "") or "",
    )


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _decode(raw: bytes) -> str:
    """Decode sandbox output; wsl.exe's *own* errors are UTF-16LE, Linux output is UTF-8."""
    if raw.count(b"\x00") > len(raw) // 4 and raw:
        try:
            return raw.decode("utf-16-le", errors="replace")
        except Exception:
            pass
    return raw.decode("utf-8", errors="replace")


class WslSandbox:
    """Runs payloads inside the sandbox distro for one agent (one scratch dir)."""

    def __init__(self, settings: ExecSettings) -> None:
        self.settings = settings
        self.work_dir = f"/work/{uuid.uuid4().hex[:12]}"

    def _prologue(self, cwd: str | None) -> str:
        s = self.settings
        lines = [
            "set +e",
            f"mkdir -p {self.work_dir}",
            "export HOME=/root DEBIAN_FRONTEND=noninteractive PIP_DISABLE_PIP_VERSION_CHECK=1",
            f"export VIRTUAL_ENV={_AGENT_VENV} PATH={_AGENT_VENV}/bin:/root/.local/bin:$PATH",
            "export MPLBACKEND=Agg PYTHONIOENCODING=utf-8 LANG=C.UTF-8",
        ]
        if s.corpus_root:
            lines.append(f"export CORPUS_ROOT='{s.corpus_root}'")
        if s.proxy:
            # Soft no-web guard: most HTTP clients honour these variables.
            lines.append(
                f"export http_proxy='{s.proxy}' https_proxy='{s.proxy}' "
                f"HTTP_PROXY='{s.proxy}' HTTPS_PROXY='{s.proxy}' "
                f"no_proxy='{s.no_proxy}' NO_PROXY='{s.no_proxy}'"
            )
        target = cwd or self.work_dir
        lines.append(f"cd '{target}' 2>/dev/null || cd {self.work_dir}")
        return "\n".join(lines) + "\n"

    def run(self, payload: str, *, timeout: int, cwd: str | None = None,
            interpreter: str = "bash", suffix: str = ".sh") -> tuple[int, str, str, bool]:
        """Run ``payload`` (bash script or python code). Returns (rc, stdout, stderr, timed_out)."""
        name = f"{self.work_dir}/.payload_{uuid.uuid4().hex[:8]}{suffix}"
        script = (
            self._prologue(cwd)
            + f"echo {_b64(payload)} | base64 -d > {name}\n"
            + f"timeout --kill-after=5 {int(timeout)} {interpreter} {name}\n"
            + "rc=$?\n"
            + f"rm -f {name}\n"
            + "exit $rc\n"
        )
        argv = ["wsl.exe", "-d", self.settings.distro, "-u", "root", "--", "bash", "-s"]
        try:
            proc = subprocess.run(
                argv, input=script.encode("utf-8"), capture_output=True,
                timeout=timeout + 30,  # outer guard in case wsl.exe itself hangs
            )
        except subprocess.TimeoutExpired:
            return 124, "", f"sandbox call exceeded {timeout + 30}s", True
        except FileNotFoundError:
            return 127, "", "wsl.exe not found: tools.exec_backend=wsl needs WSL2 on Windows", False
        out, err = _decode(proc.stdout), _decode(proc.stderr)
        return proc.returncode, out, err, proc.returncode == 124

    def python(self, code: str, *, timeout: int, packages: list[str] | None = None) -> tuple[int, str, str, bool]:
        """Install ``packages`` (serialised with flock) then run ``code`` with the agent venv."""
        interp = self.settings.python or f"{_AGENT_VENV}/bin/python"
        install_log = ""
        if packages:
            bad = [p for p in packages if not _PKG_RE.match(p)]
            if bad:
                return 2, "", f"Rejected package spec(s): {bad}", False
            pkgs = " ".join(packages)
            rc, out, err, to = self.run(
                f"flock /tmp/agent-pip.lock uv pip install --python {interp} {pkgs}",
                timeout=max(timeout, 300),
            )
            install_log = f"[pip install {pkgs} -> exit {rc}]\n{(out + err).strip()[-3000:]}\n"
            if rc != 0:
                return rc, install_log, err, to
        rc, out, err, to = self.run(code, timeout=timeout, interpreter=interp, suffix=".py")
        return rc, install_log + out, err, to
