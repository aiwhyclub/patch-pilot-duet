"""Text fallback for usage detection (frd-trd.md section 5, scope cut 3).

    scan(client_dir, diff) -> list[Usage]        S04 if no .py files

Per line, the quoted identifier-like strings ("current_weather",
'temperature', ...) are read in order. A change matches when its `from`
path's keys appear as a contiguous run in that list. Comment-only lines
are skipped. Same output shape as ast_scan, with method="text".
"""
from __future__ import annotations

import re
from pathlib import Path

from patchpilot.models import SpecDiff, Usage
from patchpilot.usages.ast_scan import display_path, python_files

_QUOTED = re.compile(r"""(?<![\w])(['"])([A-Za-z_][A-Za-z0-9_]*)\1""")


def _keys(path: str) -> list[str]:
    return [k for k in path.split(".")[1:] if k]  # "$.a.b" -> ["a", "b"]


def _contains_run(tokens: list[str], run: list[str]) -> bool:
    n = len(run)
    return n > 0 and any(tokens[i:i + n] == run for i in range(len(tokens) - n + 1))


def scan(client_dir: Path, diff: SpecDiff) -> list[Usage]:
    runs = [(c.id, _keys(c.from_)) for c in diff.changes]
    usages: list[Usage] = []
    for file in python_files(client_dir):
        rel = display_path(file, client_dir)
        for lineno, line in enumerate(file.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            tokens = [m.group(2) for m in _QUOTED.finditer(line)]
            ids = [cid for cid, run in runs if _contains_run(tokens, run)]
            if ids:
                usages.append(Usage(path=rel, line=lineno, code=stripped,
                                    change_ids=sorted(ids, key=lambda i: int(i[1:])),
                                    method="text"))
    return usages
