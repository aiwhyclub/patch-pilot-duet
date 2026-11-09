"""Error codes and messages (frd-trd.md section 15 and section 7.1).

- S01..S13: setup / run errors. Messages are English (frd-trd.md 15).
- W01..W04: warnings, never change the exit code.
- E_*: patch rejection codes, in the check order of frd-trd.md 7.1.

No message here ever contains a secret. Callers must not pass key values in.
"""
from __future__ import annotations

# Exit codes (frd-trd.md 2.2, backend-ai.md 4.5)
EXIT_OK = 0            # verified patch, or nothing to patch
EXIT_NO_PATCH = 1      # no verified patch after the attempt limit, or E_REFUSAL
EXIT_CONFIG = 2        # configuration / input error
EXIT_API = 3           # Claude API temporarily unavailable

# code -> (message template, exit code)
S_CODES: dict[str, tuple[str, int]] = {
    "S01": ("spec file not found: {path}", EXIT_CONFIG),
    "S02": ("spec is not valid JSON: {path}", EXIT_CONFIG),
    "S03": ("no 200 response schema at GET /forecast in {path}", EXIT_CONFIG),
    "S04": ("no Python files in {path}", EXIT_CONFIG),
    "S05": ("tests dir not found: {path}", EXIT_CONFIG),
    "S06": ("cannot reach {base_url} — start: python proxy/server.py --stage v2", EXIT_CONFIG),
    "S07": ("ANTHROPIC_API_KEY is missing or invalid.", EXIT_CONFIG),
    "S08": ("Claude request rejected ({error_type}): {first_line}", EXIT_CONFIG),
    "S09": ("no changes between specs — nothing to patch", EXIT_OK),
    "S10": ("nothing to patch — tests already pass", EXIT_OK),
    "S11": ("tests timed out after {n}s", EXIT_CONFIG),
    "S12": ("Claude API unavailable ({error_type}). No patch attempted this round.", EXIT_API),
    "S13": ("PATCHPILOT_MODEL must be one of the two supported model IDs (got {value})", EXIT_CONFIG),
    # [proposed] not in frd-trd.md 15: the SDK package is missing in this Python.
    "S14": ("anthropic SDK is not installed: python -m pip install \"anthropic==2.6.0\"", EXIT_CONFIG),
    # [proposed] not in frd-trd.md 15: pytest missing or tests not collectable.
    # Checked before step 3 so Claude is never called on a broken test setup.
    "S15": ("test setup error ({detail}). Fix: python -m pip install -e \".[dev]\"", EXIT_CONFIG),
    # [proposed] --pr (patchpilot/pr.py). S16-S18 are checked before any Claude
    # call and again before any git change; nothing is changed when they fire.
    "S16": ("--pr: origin is not aiwhyclub/patch-pilot-duet (got {url}). Nothing was changed.", EXIT_CONFIG),
    "S17": ("--pr: working tree has uncommitted changes ({files}). Commit or stash them first.", EXIT_CONFIG),
    "S18": ("--pr: not usable as a git repository ({detail})", EXIT_CONFIG),
    # After a verified patch: the patch file stays, and we are back on the original branch.
    "S19": ("--pr: {step} failed: {detail}. Patch kept in {patch}; back on {branch}.", EXIT_CONFIG),
}

W_CODES: dict[str, str] = {
    "W01": "no usages found; continuing with all client files",
    "W02": "ast parse failed for {file}; using text search",
    "W03": "proxy stage is v1; reproduce expects v2",
    "W04": "gh not available or not logged in; PR not created (patch saved)",
}

# Rejection codes in check order (frd-trd.md 7.1). Earlier failure stops later checks.
E_REFUSAL = "E_REFUSAL"
E_TRUNCATED = "E_TRUNCATED"
E_NO_PATCH = "E_NO_PATCH"
E_NO_DIFF = "E_NO_DIFF"
E_FILE_OP = "E_FILE_OP"
E_FORBIDDEN_PATH = "E_FORBIDDEN_PATH"
E_APPLY_FAILED = "E_APPLY_FAILED"
E_TESTS_FAILED = "E_TESTS_FAILED"
E_TEST_HASH_CHANGED = "E_TEST_HASH_CHANGED"

E_ORDER: tuple[str, ...] = (
    E_REFUSAL, E_TRUNCATED, E_NO_PATCH, E_NO_DIFF, E_FILE_OP,
    E_FORBIDDEN_PATH, E_APPLY_FAILED, E_TESTS_FAILED, E_TEST_HASH_CHANGED,
)

# Short screen text after "rejected (E_*): " (backend-ai.md 4.1, 4.2).
# {detail} is the detail string returned by guard.check / cli.
E_SCREEN: dict[str, str] = {
    E_REFUSAL: "model declined (category: {detail})",
    E_TRUNCATED: "output truncated",
    E_NO_PATCH: "model returned NO_PATCH: {detail}",
    E_NO_DIFF: "output was not a unified diff",
    E_FILE_OP: "diff created, deleted, renamed, or changed mode of {detail}",
    E_FORBIDDEN_PATH: "diff touched {detail}",
    E_APPLY_FAILED: "{detail}",
    E_TESTS_FAILED: "pytest {detail}",
    E_TEST_HASH_CHANGED: "tests hash changed ({detail})",
}


class PatchPilotError(Exception):
    """A setup/run error with an S* code. cli.main turns it into an exit code."""

    def __init__(self, code: str, message: str, exit_code: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def s_message(code: str, **kw) -> str:
    template, _ = S_CODES[code]
    return template.format(**kw)


def fail(code: str, **kw) -> PatchPilotError:
    """Build (not raise) the error for an S* code: `raise fail("S01", path=p)`."""
    template, exit_code = S_CODES[code]
    return PatchPilotError(code, template.format(**kw), exit_code)


def warn_message(code: str, **kw) -> str:
    return W_CODES[code].format(**kw)


def screen_reason(code: str, detail: str) -> str:
    """One-line text for `rejected (CODE): ...`."""
    first = (detail or "").splitlines()[0] if detail else ""
    return E_SCREEN.get(code, "{detail}").format(detail=first)


# [integration] frd-trd.md §19 names (hkt-spec) as aliases.
EXIT_NO_VERIFIED_PATCH = EXIT_NO_PATCH
EXIT_API_UNAVAILABLE = EXIT_API


def warning_text(code: str, **kw) -> str:
    return f"warning {code}: {warn_message(code, **kw)}"
