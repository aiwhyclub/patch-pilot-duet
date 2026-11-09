"""Test running, test-file locking and per-attempt workspaces.

frd-trd.md section 13.2 (runner.py) and section 7.2 / 7.3,
backend-ai.md section 3 (standard pytest command, hash rules).

    tests_hash(root)        -> "sha256:<hex>"   (raises RunnerError S05)
    check_proxy(base_url)   -> "v1" | "v2"      (raises RunnerError S06)
    run_pytest(repo)        -> TestRun          (raises RunnerError S11 on timeout)
    fresh_copy(repo)        -> Path to a new temporary copy of the repo
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from patchpilot.errors import PatchPilotError, S_CODES, s_message
from patchpilot.models import TestRun


class RunnerError(PatchPilotError):
    """S05 / S06 / S11 from errors.py. `output` keeps extra detail (pytest
    output so far, or why the proxy could not be reached)."""

    def __init__(self, code: str, message: str, output: str = "") -> None:
        super().__init__(code, message, S_CODES[code][1])
        self.output = output


# --- test-file lock -------------------------------------------------------

TESTS_DIR = "tests"
ROOT_LOCKED_FILES = ("conftest.py", "pytest.ini", "pyproject.toml")
# Cache files are never part of the hash (backend-ai.md section 3).
CACHE_DIR_NAMES = {"__pycache__", ".pytest_cache"}
CACHE_FILE_PATTERNS = ("*.pyc",)


def _is_cache(rel: Path) -> bool:
    if any(part in CACHE_DIR_NAMES for part in rel.parts):
        return True
    return any(fnmatch.fnmatch(rel.name, pat) for pat in CACHE_FILE_PATTERNS)


def locked_files(root: Path) -> list[Path]:
    """Files covered by the test lock, as paths relative to root, sorted."""
    root = Path(root)
    tests = root / TESTS_DIR
    if not tests.is_dir():
        raise RunnerError("S05", s_message("S05", path=tests))
    files: list[Path] = []
    for path in tests.rglob("*"):
        if path.is_file():
            rel = path.relative_to(root)
            if not _is_cache(rel):
                files.append(rel)
    for name in ROOT_LOCKED_FILES:
        if (root / name).is_file():
            files.append(Path(name))
    return sorted(files, key=lambda p: p.as_posix())


def tests_hash(root: Path) -> str:
    """SHA-256 over (relative path + NUL + content) of every locked file."""
    root = Path(root)
    digest = hashlib.sha256()
    for rel in locked_files(root):
        digest.update(rel.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update((root / rel).read_bytes())
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def short_hash(value: str) -> str:
    """First 12 hex characters, as shown on screen."""
    return value.split(":", 1)[-1][:12]


# --- proxy check ------------------------------------------------------------

STAGE_HEADER = "X-PatchPilot-Stage"
_PROBE_QUERY = "/forecast?latitude=37.57&longitude=126.98&current_weather=true"


def check_proxy(base_url: str, timeout: float = 3.0) -> str:
    """Return the proxy stage from the X-PatchPilot-Stage header.

    Tries GET /health first (no upstream call), then GET /forecast.
    """
    base = base_url.rstrip("/")
    last_error = "no response"
    for path in ("/health", _PROBE_QUERY):
        try:
            with urllib.request.urlopen(base + path, timeout=timeout) as resp:
                stage = resp.headers.get(STAGE_HEADER)
        except urllib.error.HTTPError as exc:
            stage = exc.headers.get(STAGE_HEADER)
            last_error = f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = str(getattr(exc, "reason", exc))
            break  # not reachable at all; trying another path won't help
        if stage:
            return stage
    raise RunnerError("S06", s_message("S06", base_url=base_url), output=last_error)


# --- pytest -------------------------------------------------------------------

# Standard command (backend-ai.md section 3). Do not add flags here.
PYTEST_ARGS = ["-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/"]

_COUNT_RE = re.compile(r"(\d+) (passed|failed|errors?)\b")
_SUMMARY_LINE_RE = re.compile(r"^(FAILED|ERROR) (\S+)(?: - (.*))?$")


def pytest_command(python: str | None = None) -> list[str]:
    return [python or sys.executable, *PYTEST_ARGS]


def _parse_pytest(output: str) -> tuple[int, int, list[str], dict[str, str]]:
    passed = failed = 0
    lines = output.splitlines()
    # Final summary line, e.g. "1 passed, 3 failed in 0.12s".
    for line in reversed(lines):
        counts = _COUNT_RE.findall(line)
        if counts and (" in " in line or line.startswith("=")):
            for num, word in counts:
                if word == "passed":
                    passed = int(num)
                else:  # failed, error, errors
                    failed += int(num)
            break
    failed_tests: list[str] = []
    first_error: dict[str, str] = {}
    for line in lines:
        m = _SUMMARY_LINE_RE.match(line.strip())
        if m:
            name = m.group(2)
            if name not in failed_tests:
                failed_tests.append(name)
                first_error[name] = (m.group(3) or "").strip()
    return passed, failed, failed_tests, first_error


def run_pytest(repo: Path, timeout: int = 60, python: str | None = None) -> TestRun:
    """Run the standard pytest command in repo and parse the result.

    Environment: PYTHONDONTWRITEBYTECODE=1, repo first on PYTHONPATH,
    WEATHER_BASE_URL inherited from the caller. pytest must be importable
    by `python` (default: the interpreter running PatchPilot).
    """
    repo = Path(repo)
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.setdefault("COLUMNS", "200")  # [integration] keep pytest's one-line errors uncut
    env["PYTHONPATH"] = os.pathsep.join(
        [str(repo)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    start = time.monotonic()
    try:
        proc = subprocess.run(
            pytest_command(python),
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or "") if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
        raise RunnerError("S11", s_message("S11", n=timeout), output=partial) from None
    duration = time.monotonic() - start
    output = proc.stdout + (("\n" + proc.stderr) if proc.stderr else "")
    passed, failed, failed_tests, first_error = _parse_pytest(output)
    if proc.returncode not in (0, 5) and passed == 0 and failed == 0:
        # pytest itself failed (usage error, import error before collection, missing pytest).
        failed = 1
        first_error.setdefault("pytest", (output.strip().splitlines() or ["pytest failed"])[-1])
    return TestRun(
        passed=passed,
        failed=failed,
        failed_tests=failed_tests,
        first_error=first_error,
        output=output,
        duration_s=round(duration, 3),
        timed_out=False,
    )


# --- per-attempt workspace ----------------------------------------------------

COPY_IGNORE = shutil.ignore_patterns(
    "__pycache__", ".pytest_cache", "*.pyc", ".git", "out", ".venv", "venv", "*.egg-info"
)


def fresh_copy(repo: Path) -> Path:
    """Copy repo into a new temporary directory and return the copy's path.

    Cache files, .git, out/ and virtualenvs are not copied. The caller owns
    the copy and can remove it with cleanup().
    """
    repo = Path(repo).resolve()
    parent = Path(tempfile.mkdtemp(prefix="patchpilot-"))
    dest = parent / "repo"
    shutil.copytree(repo, dest, ignore=COPY_IGNORE, symlinks=True)
    return dest


def cleanup(copy: Path) -> None:
    """Remove a directory made by fresh_copy() (its temporary parent too)."""
    copy = Path(copy)
    target = copy.parent if copy.name == "repo" and copy.parent.name.startswith("patchpilot-") else copy
    shutil.rmtree(target, ignore_errors=True)
