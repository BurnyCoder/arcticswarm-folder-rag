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

"""Python execution tool — run Python code directly.

A dedicated tool for the coding profile that executes Python code
without shell quoting issues.  Writes code to a temp file, runs it
with ``python3``, and returns stdout/stderr.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from typing import Any

from arcticswarm.tools.base import BaseTool, ToolResult

_DEFAULT_TIMEOUT = 120  # seconds
_MAX_OUTPUT = 100_000  # characters

# Prepended to every executed script to suppress GUI pop-ups from PIL.
# PIL.Image.show() launches macOS Preview via `open`, which is not
# controlled by MPLBACKEND.  The try/except is a no-op if PIL is absent.
_PREAMBLE = """\
try:
    import PIL.Image as _PIL_Image
    _PIL_Image.Image.show = lambda self, *a, **kw: None
except ImportError:
    pass
"""


def _truncate(text: str, max_chars: int = _MAX_OUTPUT) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... (truncated, {len(text)} total chars)"


class PythonExecuteTool(BaseTool):
    """Execute Python code directly and return stdout/stderr."""

    name = "python_execute"
    description = (
        "Execute Python code directly. Returns stdout and stderr. "
        "Use for data analysis, computation, file processing, and scripting tasks. "
        "The code runs in a subprocess with full access to installed packages."
    )

    SANDBOX_DESCRIPTION = (
        "Run Python 3 code in an isolated Linux sandbox (agent virtualenv; you are "
        "root). The folder being researched is at os.environ['CORPUS_ROOT']. "
        "Preinstalled: pandas, numpy, pypdf, markitdown, bm25s, scikit-learn. You may "
        "install ANY PyPI library via the `packages` argument (e.g. "
        "[\"rapidfuzz\", \"networkx\"]) before the code runs. Print only what you "
        "need; output is truncated. No internet except package installs."
    )

    def __init__(self, exec_settings: Any = None) -> None:
        # Optional sandbox backend (tools/sandbox_exec.py, exec_backend=wsl): code
        # runs in the WSL sandbox's agent venv and may pip-install packages first.
        self._sandbox = None
        self._default_timeout = _DEFAULT_TIMEOUT
        if exec_settings is not None and getattr(exec_settings, "is_wsl", False):
            from arcticswarm.tools.sandbox_exec import WslSandbox
            self._sandbox = WslSandbox(exec_settings)
            self._default_timeout = exec_settings.python_timeout
            self.description = self.SANDBOX_DESCRIPTION

    def parameters_schema(self) -> dict[str, Any]:
        props: dict[str, Any] = {
            "code": {
                "type": "string",
                "description": "Python code to execute.",
            },
            "timeout": {
                "type": "integer",
                "description": f"Timeout in seconds. Default: {self._default_timeout}.",
            },
        }
        if self._sandbox is not None:
            props["packages"] = {
                "type": "array",
                "items": {"type": "string"},
                "description": "PyPI packages to pip-install before running (any library you need).",
            }
        return {"type": "object", "properties": props, "required": ["code"]}

    def _execute_sandbox(self, code: str, timeout: int, packages: list[str] | None) -> ToolResult:
        rc, out, err, timed_out = self._sandbox.python(code, timeout=timeout, packages=packages)
        if timed_out:
            return ToolResult(error=f"Python execution timed out after {timeout}s.", is_error=True)
        parts: list[str] = []
        if out:
            parts.append(_truncate(out))
        if err:
            parts.append(f"STDERR:\n{_truncate(err)}")
        output = "\n".join(parts) if parts else "(no output)"
        if rc != 0:
            return ToolResult(output=output, error=f"Exit code {rc}\n{output}", is_error=True)
        return ToolResult(output=output)

    def execute(
        self,
        *,
        code: str,
        timeout: int | None = None,
        packages: list[str] | None = None,
        **_: Any,
    ) -> ToolResult:
        if not code.strip():
            return ToolResult(error="Empty code.", is_error=True)
        timeout = int(timeout or self._default_timeout)
        if self._sandbox is not None:
            return self._execute_sandbox(code, timeout, packages)

        # Run in a scratch directory so generated files (images, data) don't
        # pollute the repo working directory.
        work_dir = tempfile.mkdtemp(prefix="arcticswarm_py_")
        fd, path = tempfile.mkstemp(suffix=".py", prefix="arcticswarm_", dir=work_dir)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(_PREAMBLE + code)

            env = os.environ.copy()
            env.pop("MallocStackLogging", None)
            env.pop("MallocStackLoggingNoCompact", None)
            env.setdefault("MPLBACKEND", "Agg")
            result = subprocess.run(
                # sys.executable instead of "python3": a Windows venv has no
                # python3.exe (the name resolves to the Microsoft Store stub).
                [sys.executable, path],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
                cwd=work_dir,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                error=f"Python execution timed out after {timeout}s.",
                is_error=True,
            )
        except Exception as exc:
            return ToolResult(error=f"Python execution failed: {exc}", is_error=True)
        finally:
            import shutil
            shutil.rmtree(work_dir, ignore_errors=True)

        output_parts: list[str] = []
        if result.stdout:
            output_parts.append(_truncate(result.stdout))
        if result.stderr:
            output_parts.append(f"STDERR:\n{_truncate(result.stderr)}")

        output = "\n".join(output_parts) if output_parts else "(no output)"

        if result.returncode != 0:
            return ToolResult(
                output=output,
                error=f"Exit code {result.returncode}\n{output}",
                is_error=True,
            )

        return ToolResult(output=output)
