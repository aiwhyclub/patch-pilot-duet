"""`patchpilot check --pr`: put a verified patch on a new branch and open a PR.

Only runs after a verified patch (status "patched"). Steps:

    preflight(repo)        origin + clean-tree checks (S16/S17/S18). Called at
                           the start of the run (before any Claude call) and
                           again right before any git change.
    create(repo, ...)      new branch patchpilot/openmeteo-v2-<timestamp>
                           -> git apply --recount --index out/patchpilot.patch
                           -> commit (only the patch's client/ files)
                           -> git push -u origin <branch>
                           -> gh pr create --body-file out/PR_BODY.md
                           -> always back on the original branch (finally).

Safety rules (Drone): the only allowed repository is aiwhyclub/patch-pilot-duet.
Both the fetch URL and the push URL of `origin` must match it exactly, or
nothing is done. The original branch is never committed to, and its files are
never changed. No URL with credentials is ever printed (userinfo is stripped).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from patchpilot import errors

ALLOWED_REPO = "aiwhyclub/patch-pilot-duet"
BRANCH_PREFIX = "patchpilot/openmeteo-v2-"
GIT_TIMEOUT_S = 30
PUSH_TIMEOUT_S = 120

# Exact repo match. `patch-pilot-duet` must be the whole name, so any other
# repository (including ones whose name is a prefix of it) is refused.
_ORIGIN_RE = re.compile(
    r"(?:https://(?:[^@/\s]+@)?github\.com/"
    r"|git@github\.com:"
    r"|ssh://git@github\.com/)"
    + re.escape(ALLOWED_REPO)
    + r"(?:\.git)?/?",
    re.IGNORECASE,
)


@dataclass
class PrResult:
    status: str                      # "created" | "skipped_gh" | "failed"
    branch: Optional[str] = None
    base: Optional[str] = None
    commit: Optional[str] = None
    pushed: bool = False
    url: Optional[str] = None
    files: list[str] = field(default_factory=list)
    error: Optional[errors.PatchPilotError] = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "branch": self.branch,
            "base": self.base,
            "commit": self.commit,
            "pushed": self.pushed,
            "url": self.url,
            "files": list(self.files),
            "error": ({"code": self.error.code, "message": self.error.message}
                      if self.error else None),
        }


class GitError(Exception):
    def __init__(self, cmd: list[str], detail: str):
        super().__init__(detail)
        self.cmd = cmd
        self.detail = detail


def safe_url(url: str) -> str:
    """Remove `user[:token]@` from every http(s) URL in the text before showing it."""
    return re.sub(r"(https?://)[^@/\s]+@", r"\1", (url or "").strip())


def origin_ok(url: str) -> bool:
    return bool(_ORIGIN_RE.fullmatch((url or "").strip()))


def _env() -> dict:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"      # fail instead of hanging on a password prompt
    env.setdefault("GH_PROMPT_DISABLED", "1")
    return env


def _run(cmd: list[str], repo: Path, timeout: int = GIT_TIMEOUT_S,
         check: bool = True) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(cmd, cwd=repo, env=_env(), capture_output=True, text=True,
                              timeout=timeout)
    except FileNotFoundError:
        raise GitError(cmd, f"{cmd[0]} not found")
    except subprocess.TimeoutExpired:
        raise GitError(cmd, f"timed out after {timeout}s")
    if check and proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise GitError(cmd, safe_url(msg[-1]) if msg else f"exit {proc.returncode}")
    return proc


def _git(repo: Path, *args: str, timeout: int = GIT_TIMEOUT_S, check: bool = True) -> str:
    return _run(["git", *args], repo, timeout=timeout, check=check).stdout.strip()


def dirty_files(repo: Path, out_rel: str) -> list[str]:
    """Tracked changes (staged or unstaged), ignoring the out/ folder.

    Untracked files (`??`) are not counted: the commit adds only the patch's
    files, so untracked files such as __pycache__/ from the pytest run can
    never end up in it, and counting them would block every repeat demo run.
    """
    # Not _git(): .strip() would eat the leading space of " M path".
    out = _run(["git", "status", "--porcelain", "--untracked-files=no"], repo).stdout
    prefix = out_rel.strip("/").rstrip("/") + "/"
    files = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].split(" -> ")[-1].strip('"')
        if path == out_rel.strip("/") or path.startswith(prefix):
            continue
        files.append(path)
    return files


def preflight(repo: Path, out_rel: str = "out") -> str:
    """Raise S18/S16/S17 unless it is safe to make git changes. Returns origin URL."""
    repo = Path(repo).resolve()
    if shutil.which("git") is None:
        raise errors.fail("S18", detail="git not found")
    try:
        top = _git(repo, "rev-parse", "--show-toplevel")
    except GitError as exc:
        raise errors.fail("S18", detail=exc.detail)
    if Path(top).resolve() != repo:
        raise errors.fail("S18", detail=f"run from the repository root ({top})")
    urls = []
    for args in (("remote", "get-url", "origin"), ("remote", "get-url", "--push", "origin")):
        try:
            urls.append(_git(repo, *args))
        except GitError:
            raise errors.fail("S16", url="(no origin remote)")
    for url in urls:
        for one in url.splitlines():
            if not origin_ok(one):
                raise errors.fail("S16", url=safe_url(one))
    dirty = dirty_files(repo, out_rel)
    if dirty:
        shown = ", ".join(dirty[:3]) + (f" +{len(dirty) - 3} more" if len(dirty) > 3 else "")
        raise errors.fail("S17", files=shown)
    return safe_url(urls[0])


def gh_ready(repo: Path) -> bool:
    if shutil.which("gh") is None:
        return False
    try:
        _run(["gh", "auth", "status", "--hostname", "github.com"], repo, timeout=GIT_TIMEOUT_S)
    except GitError:
        return False
    return True


def branch_name(now: Optional[float] = None) -> str:
    return BRANCH_PREFIX + time.strftime("%Y%m%d-%H%M%S", time.localtime(now))


def _current_ref(repo: Path) -> tuple[str, str]:
    """(name to check out again, sha). Detached HEAD -> (sha, sha)."""
    sha = _git(repo, "rev-parse", "HEAD")
    name = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return (sha if name == "HEAD" else name), sha


def create(repo: Path, *, patch_file: Path, body_file: Path, client_dir: str,
           title: str, out_rel: str = "out", now: Optional[float] = None) -> PrResult:
    """Run the branch/commit/push/PR steps. Never raises for git failures:
    returns PrResult(status="failed", error=S19) after going back to the
    original branch. Preflight errors (S16/S17/S18) are raised before any change."""
    repo = Path(repo).resolve()
    preflight(repo, out_rel)
    use_gh = gh_ready(repo)
    original, original_sha = _current_ref(repo)
    branch = branch_name(now)
    res = PrResult(status="failed", branch=branch, base=original)
    client_prefix = client_dir.strip("/") + "/"
    created = False
    committed = False
    step = "create branch"
    try:
        if _git(repo, "branch", "--list", branch):
            raise GitError(["git", "branch"], f"branch {branch} already exists")
        _git(repo, "checkout", "-b", branch)
        created = True

        step = "git apply"
        # Same flags as guard.apply (the patch was verified with --recount).
        _git(repo, "apply", "--check", "--recount", "--index", str(patch_file))
        _git(repo, "apply", "--recount", "--index", str(patch_file))
        staged = [p for p in _git(repo, "diff", "--cached", "--name-only").splitlines() if p]
        outside = [p for p in staged if not p.startswith(client_prefix)]
        if outside:
            raise GitError(["git", "diff"], f"patch touches files outside {client_prefix}: "
                           + ", ".join(outside))
        if not staged:
            raise GitError(["git", "diff"], "patch changed nothing")
        res.files = staged

        step = "git commit"
        _git(repo, "commit", "-m", title, "-m",
             "Generated by PatchPilot. Tests were not modified.", "--", *staged)
        res.commit = _git(repo, "rev-parse", "HEAD")
        committed = True

        step = "git push"
        _git(repo, "push", "-u", "origin", branch, timeout=PUSH_TIMEOUT_S)
        res.pushed = True

        if not use_gh:
            res.status = "skipped_gh"
            return res

        step = "gh pr create"
        out = _run(["gh", "pr", "create", "--repo", ALLOWED_REPO, "--base", original,
                    "--head", branch, "--title", title, "--body-file", str(body_file)],
                   repo, timeout=PUSH_TIMEOUT_S).stdout
        urls = re.findall(r"https://github\.com/\S+/pull/\d+", out or "")
        lines = (out or "").strip().splitlines()
        res.url = urls[-1] if urls else (lines[-1] if lines else None)
        res.status = "created"
        return res
    except GitError as exc:
        hint = ""
        if committed and not res.pushed:
            hint = f" Local branch kept: git push -u origin {branch}"
        elif res.pushed:
            hint = f" Branch {branch} was pushed; open the PR on GitHub."
        res.error = errors.fail("S19", step=step, detail=exc.detail,
                                patch=f"{out_rel}/{patch_file.name}", branch=original)
        if hint:
            res.error.message += hint
        return res
    finally:
        _return_to(repo, original, original_sha, branch, created, committed)


def _return_to(repo: Path, original: str, original_sha: str, branch: str,
               created: bool, committed: bool) -> None:
    """Back to the original branch, whatever happened. Leftover staged or
    unstaged changes exist only on our own new branch (the tree was clean at
    start), so they are discarded there before switching back."""
    if not created:
        return
    try:
        if _git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False) == branch:
            if _git(repo, "status", "--porcelain", "--untracked-files=no", check=False):
                _git(repo, "reset", "--hard", "-q", "HEAD", check=False)
        _git(repo, "checkout", "-q", original, check=False)
        if not committed:
            _git(repo, "branch", "-D", branch, check=False)
        now_sha = _git(repo, "rev-parse", original, check=False)
    except GitError:
        return
    if now_sha and now_sha != original_sha:     # should never happen; say so loudly
        raise errors.fail("S19", step="return to original branch",
                          detail=f"{original} moved from {original_sha[:12]} to {now_sha[:12]}",
                          patch="out/patchpilot.patch", branch=original)
