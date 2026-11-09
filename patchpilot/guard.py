"""Patch guard (frd-trd.md 7.1, backend-ai.md 3).

check(raw, stop_reason, allowed, workdir) -> (code | None, detail | None, diff)
apply(diff_text, workdir) -> None

check() runs checks 0-5 in order and stops at the first failure:
  0 E_REFUSAL / E_TRUNCATED   (stop_reason)
  1 E_NO_PATCH                (output starts with NO_PATCH:)
  2 E_NO_DIFF                 (no `diff --git ` start, even after removing one ```diff fence)
  3 E_FILE_OP                 (new/deleted file mode, rename from, old mode, Binary files, /dev/null)
  4 E_FORBIDDEN_PATH          (diff --git a/X b/Y, ---/+++ paths, git apply --numstat paths)
  5 E_APPLY_FAILED            (git apply --check --recount in workdir)
Checks 6 (E_TESTS_FAILED) and 7 (E_TEST_HASH_CHANGED) need pytest and the
tests hash, so cli.py runs them after apply().

detail format (prompt.py fills REJECTION_DETAIL from it):
  E_FORBIDDEN_PATH, E_FILE_OP: comma-separated paths
  E_APPLY_FAILED: first 5 lines of git stderr
  E_NO_PATCH: the model's reason
  E_REFUSAL: "" (cli adds the stop category from llm.client.last_call)
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Optional

from patchpilot.errors import (
    E_APPLY_FAILED, E_FILE_OP, E_FORBIDDEN_PATH, E_NO_DIFF, E_NO_PATCH,
    E_REFUSAL, E_TRUNCATED,
)

DIFF_START = "diff --git "
FILE_OP_PREFIXES = ("new file mode", "deleted file mode", "rename from", "old mode", "Binary files")
_HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)$")
_SKIP_PARTS = {"__pycache__", ".pytest_cache"}


class ApplyError(RuntimeError):
    """git apply failed in apply(). Message is git's stderr (first 5 lines)."""


def allowed_files(repo: Path, client_dir: str = "client") -> set[str]:
    """Existing .py files under client/, as repo-relative POSIX paths."""
    root = Path(repo) / client_dir
    if not root.is_dir():
        return set()
    out = set()
    for p in root.rglob("*.py"):
        rel = p.relative_to(repo)
        if p.is_file() and not p.is_symlink() and not (_SKIP_PARTS & set(rel.parts)):
            out.add(rel.as_posix())
    return out


def _normalize(raw: str) -> str:
    return (raw or "").replace("\r\n", "\n").replace("\r", "\n")


def strip_single_fence(text: str) -> Optional[str]:
    """If the whole output is exactly one ```diff fence, return its inside."""
    s = text.strip()
    lines = s.split("\n")
    if len(lines) < 3 or lines[0].strip() != "```diff" or lines[-1].strip() != "```":
        return None
    inner = lines[1:-1]
    if any(l.lstrip().startswith("```") for l in inner):
        return None
    return "\n".join(inner)


def _sections(diff_text: str) -> list[list[str]]:
    sections: list[list[str]] = []
    for line in diff_text.split("\n"):
        if line.startswith(DIFF_START):
            sections.append([line])
        elif sections:
            sections[-1].append(line)
    return sections


def _header_lines(section: list[str]) -> list[str]:
    """Lines before the first hunk (@@). Hunk lines never count as headers."""
    out = []
    for line in section:
        if line.startswith("@@"):
            break
        out.append(line)
    return out


def _git_env(workdir: Path) -> dict:
    env = dict(os.environ)
    # Never let git climb out of the temp copy into an enclosing repository.
    env["GIT_CEILING_DIRECTORIES"] = str(Path(workdir).resolve().parent)
    return env


def _git_apply(args: list[str], diff_text: str, workdir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "apply", *args, "-"], input=diff_text, text=True,
        cwd=str(workdir), env=_git_env(workdir), capture_output=True, timeout=30,
    )


def numstat(diff_text: str, workdir: Path) -> Optional[list[tuple[int, int, str]]]:
    """`git apply --numstat --recount`: [(added, removed, path)], None if git rejects the diff."""
    proc = _git_apply(["--numstat", "--recount"], diff_text, workdir)
    if proc.returncode != 0:
        return None
    rows = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        a, r, path = parts
        rows.append((int(a) if a.isdigit() else 0, int(r) if r.isdigit() else 0, path))
    return rows


def _first_lines(text: str, n: int = 5) -> str:
    lines = [l for l in (text or "").strip().splitlines() if l.strip()]
    return "\n".join(lines[:n]) if lines else "git apply failed"


def check(raw: str, stop_reason: str, allowed: set[str],
          workdir: Path) -> tuple[Optional[str], Optional[str], str]:
    """Return (E_* code or None, detail or None, cleaned diff text)."""
    text = _normalize(raw)

    # 0: stop reason
    if stop_reason == "refusal":
        return E_REFUSAL, "", text
    if stop_reason == "max_tokens":
        return E_TRUNCATED, "", text

    # 1: NO_PATCH
    stripped = text.strip()
    if stripped.startswith("NO_PATCH:"):
        reason = stripped[len("NO_PATCH:"):].strip().split("\n")[0].strip()
        return E_NO_PATCH, reason, text

    # 2: diff format (one ```diff fence may be removed)
    candidate = text.lstrip()
    if not candidate.startswith(DIFF_START):
        inner = strip_single_fence(text)
        if inner is None or not inner.lstrip().startswith(DIFF_START):
            return E_NO_DIFF, "", text
        candidate = inner.lstrip()
    diff_text = candidate.rstrip("\n") + "\n"
    sections = _sections(diff_text)

    # 3: file operations
    file_op_paths: list[str] = []
    for sec in sections:
        m = _HEADER.match(sec[0])
        label = m.group(2) if m else sec[0][len(DIFF_START):].strip()
        for line in _header_lines(sec)[1:]:
            if line.startswith(FILE_OP_PREFIXES) or line in ("--- /dev/null", "+++ /dev/null"):
                file_op_paths.append(label)
                break
    if file_op_paths:
        return E_FILE_OP, ", ".join(sorted(set(file_op_paths))), diff_text

    # 4: paths
    touched: set[str] = set()
    for sec in sections:
        m = _HEADER.match(sec[0])
        if not m:
            touched.add(sec[0][len(DIFF_START):].strip() or "<unparsed header>")
            continue
        touched.update(m.groups())
        for line in _header_lines(sec)[1:]:
            if line.startswith("--- ") or line.startswith("+++ "):
                p = line[4:].split("\t")[0].strip()
                if p[:2] in ("a/", "b/"):
                    p = p[2:]
                touched.add(p)
    rows = numstat(diff_text, workdir)
    if rows:
        touched.update(path for _, _, path in rows)
    forbidden = sorted(p for p in touched if p not in allowed)
    if forbidden:
        return E_FORBIDDEN_PATH, ", ".join(forbidden), diff_text

    # 5: apply check on the fresh copy
    proc = _git_apply(["--check", "--recount"], diff_text, workdir)
    if proc.returncode != 0:
        return E_APPLY_FAILED, _first_lines(proc.stderr or proc.stdout), diff_text

    return None, None, diff_text


def apply(diff_text: str, workdir: Path) -> None:
    """Apply a diff that passed check() to the fresh copy. Raises ApplyError."""
    proc = _git_apply(["--recount"], diff_text, workdir)
    if proc.returncode != 0:
        raise ApplyError(_first_lines(proc.stderr or proc.stdout))


def diff_stats(diff_text: str, workdir: Path) -> tuple[list[str], int, int]:
    """(files, added, removed) for the screen and report. Falls back to counting lines."""
    rows = numstat(diff_text, workdir)
    if rows is not None:
        return sorted({p for _, _, p in rows}), sum(a for a, _, _ in rows), sum(r for _, r, _ in rows)
    files, added, removed = set(), 0, 0
    for sec in _sections(diff_text):
        m = _HEADER.match(sec[0])
        if m:
            files.add(m.group(2))
        in_hunk = False
        for line in sec[1:]:
            if line.startswith("@@"):
                in_hunk = True
            elif in_hunk and line.startswith("+"):
                added += 1
            elif in_hunk and line.startswith("-"):
                removed += 1
    return sorted(files), added, removed
