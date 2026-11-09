"""Claude patch prompt (backend-ai.md section 2, final text, verbatim).

build(diff, usages, failing, files, previous) -> (system, user)

Placeholders are filled in one pass over the template only, so text that
comes from client files, test logs, or model output is never re-scanned
for `{{...}}`.
"""
from __future__ import annotations

import json
import re
from typing import Iterable

from patchpilot.models import Attempt, SpecDiff, TestRun, Usage

# --- backend-ai.md 2.1 System prompt (verbatim) ---------------------------
SYSTEM_PROMPT = """You are the patch engine of PatchPilot. An upstream HTTP API changed from v1 to v2. A Python client in this repository still expects v1, so fixed tests now fail against v2. Your job is to change the client so that the same tests pass against v2.

Hard rules:
1. Output exactly one unified diff in `git diff` format and nothing else. No prose, no explanation, no Markdown code fences.
2. Modify only files that already exist and are listed under "Allowed files". Do not create, delete, rename, or change the mode of any file.
3. Never modify anything under `tests/`, any `conftest.py`, or any pytest, packaging, or CI configuration file (`pytest.ini`, `pyproject.toml`, `setup.cfg`, `tox.ini`, `.github/`). A patch that touches these is rejected automatically, and the test files are hash-checked before and after.
4. Do not make tests pass by any means other than correct client behavior. Do not skip tests, swallow exceptions, hard-code response values, read test files, detect pytest, or change how the client reaches the API.
5. Change only what the spec diff requires. Keep public function names, signatures, and return types unchanged. Do not refactor, reformat, or touch unrelated lines.
6. Target v2 only. Do not keep a v1 code path or add v1 fallback logic.
7. Format each file section as: `diff --git a/<path> b/<path>`, then `--- a/<path>`, then `+++ b/<path>`, then hunks with `@@` headers and 3 lines of unchanged context. Paths are relative to the repository root.
8. If you cannot produce a safe patch under these rules, output exactly one line: `NO_PATCH: <one-sentence reason>`."""

# --- backend-ai.md 2.2 User prompt template (verbatim) --------------------
USER_TEMPLATE_HEAD = """The API changed from v1 to v2. Patch the client so the fixed tests pass against v2.

<spec_diff format="json">
{{SPEC_DIFF_JSON}}
</spec_diff>

<affected_usages format="path:line: code">
{{AFFECTED_USAGES}}
</affected_usages>

<failing_test_log>
{{FAILING_TEST_LOG}}
</failing_test_log>

<client_source>
{{CLIENT_FILES}}
</client_source>

Allowed files:
{{ALLOWED_PATHS}}
"""

FINAL_SENTENCE_FIRST = "Output the unified diff now."

# --- backend-ai.md 2.3 Retry blocks (verbatim) ----------------------------
PREVIOUS_ATTEMPT_TEMPLATE = """<previous_attempt number="{{N}}">
<previous_output>
{{PREVIOUS_RAW_OUTPUT}}
</previous_output>
<rejection code="{{REJECTION_CODE}}">
{{REJECTION_DETAIL}}
</rejection>
<failing_test_log_after_attempt>
{{NEW_FAILING_TEST_LOG}}
</failing_test_log_after_attempt>
</previous_attempt>"""

FINAL_SENTENCE_RETRY = (
    "Your previous attempt was rejected for the reason in <rejection>. Write a new patch "
    "against the ORIGINAL code in <client_source>, not on top of any previous patch. Fix the "
    "stated cause, follow every rule in the system prompt, and output only the new unified diff."
)

REJECTION_DETAIL: dict[str, str] = {
    "E_NO_DIFF": 'Your output was not a unified diff. Output only the diff, starting with "diff --git".',
    "E_FORBIDDEN_PATH": "Your diff modified {{PATHS}}, which is not allowed. Modify only the allowed files.",
    "E_FILE_OP": "Your diff created, deleted, renamed, or changed the mode of {{PATHS}}. Only edit existing allowed files in place.",
    "E_APPLY_FAILED": "Your diff could not be applied to the original files. git said: {{GIT_ERROR_FIRST_5_LINES}}. Copy context lines exactly from <client_source>.",
    "E_TESTS_FAILED": "Your diff applied, but the same tests still fail. See <failing_test_log_after_attempt>.",
    "E_TEST_HASH_CHANGED": "The test files changed during verification. Do not change test files or anything they read.",
    "E_TRUNCATED": "Your output was cut off before the diff ended. Make the smallest patch that fixes the failure.",
    "E_NO_PATCH": "You returned NO_PATCH: {{REASON}}. Try again using only the allowed files; if it is truly impossible, return NO_PATCH again.",
}

NOT_RUN = "not run"
NONE_FOUND = "none found"
LOG_MAX_LINES = 200
LOG_MAX_CHARS = 8000

_PLACEHOLDER = re.compile(r"\{\{([A-Z0-9_]+)\}\}")


def _fill(template: str, values: dict[str, str]) -> str:
    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key not in values:
            raise KeyError(f"missing prompt value for {{{{{key}}}}}")
        return values[key]
    return _PLACEHOLDER.sub(sub, template)


def truncate_log(text: str) -> str:
    """Keep the last 200 lines and at most 8,000 characters (backend-ai.md 2.2).

    Marker when cut by lines: `[truncated: last 200 lines]` (doc text).
    Marker when the 8,000-character cap also cut it: `[truncated: last 8000 characters]`.
    """
    text = (text or "").rstrip("\n")
    lines = text.split("\n")
    marker = None
    if len(lines) > LOG_MAX_LINES:
        lines = lines[-LOG_MAX_LINES:]
        marker = f"[truncated: last {LOG_MAX_LINES} lines]"
    body = "\n".join(lines)
    if len(body) > LOG_MAX_CHARS:
        body = body[-LOG_MAX_CHARS:]
        nl = body.find("\n")
        if 0 <= nl < len(body) - 1:
            body = body[nl + 1:]          # start on a whole line
        marker = f"[truncated: last {LOG_MAX_CHARS} characters]"
    return f"{marker}\n{body}" if marker else body


def format_usages(usages: Iterable[Usage]) -> str:
    lines = [u.prompt_line() for u in usages]
    return "\n".join(lines) if lines else NONE_FOUND


def format_client_files(files: dict[str, str]) -> str:
    parts = []
    for path in sorted(files):
        content = files[path]
        if not content.endswith("\n"):
            content += "\n"
        parts.append(f'<file path="{path}">\n{content}</file>')
    return "\n".join(parts)


def rejection_detail(attempt: Attempt) -> str:
    code = attempt.code or ""
    template = REJECTION_DETAIL.get(code)
    if template is None:
        return attempt.detail or code
    detail = attempt.detail or ""
    if code == "E_APPLY_FAILED":
        detail = "\n".join(detail.splitlines()[:5])
    return _fill(template, {
        "PATHS": detail,
        "GIT_ERROR_FIRST_5_LINES": detail,
        "REASON": detail,
    })


def previous_attempt_block(attempt: Attempt) -> str:
    if attempt.code == "E_TESTS_FAILED" and attempt.test_log:
        new_log = truncate_log(attempt.test_log)
    else:
        new_log = NOT_RUN
    return _fill(PREVIOUS_ATTEMPT_TEMPLATE, {
        "N": str(attempt.n),
        "PREVIOUS_RAW_OUTPUT": attempt.raw_output or "",
        "REJECTION_CODE": attempt.code or "",
        "REJECTION_DETAIL": rejection_detail(attempt),
        "NEW_FAILING_TEST_LOG": new_log,
    })


def build(diff: SpecDiff, usages: list[Usage], failing: TestRun,
          files: dict[str, str], previous: list[Attempt]) -> tuple[str, str]:
    """Return (system, user) for one single-turn request.

    files: allowed client files, repo-relative path -> original content.
    previous: earlier rejected attempts of this run, oldest first.
    """
    head = _fill(USER_TEMPLATE_HEAD, {
        "SPEC_DIFF_JSON": json.dumps(diff.to_dict(), indent=2, ensure_ascii=False),
        "AFFECTED_USAGES": format_usages(usages),
        "FAILING_TEST_LOG": truncate_log(failing.output),
        "CLIENT_FILES": format_client_files(files),
        "ALLOWED_PATHS": "\n".join(sorted(files)),
    })
    rejected = [a for a in previous if a.result == "rejected"]
    if not rejected:
        return SYSTEM_PROMPT, f"{head}\n{FINAL_SENTENCE_FIRST}"
    blocks = "\n\n".join(previous_attempt_block(a) for a in rejected)
    return SYSTEM_PROMPT, f"{head}\n{blocks}\n\n{FINAL_SENTENCE_RETRY}"
