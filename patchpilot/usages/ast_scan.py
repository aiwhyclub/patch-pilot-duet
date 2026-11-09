"""Find client lines that read changed response paths (frd-trd.md section 5).

    scan(client_dir, diff) -> list[Usage]        S04 if no .py files
    key_chains(tree) -> list[(line, [keys])]

A key chain is a run of string-key lookups such as
    data["current_weather"]["temperature"]
    data.get("current_weather", {}).get("temperature")
    data.get("current_weather")["temperature"]
It becomes "$.current_weather.temperature" and matches a change when it
equals the change's `from` path or starts with `from` + ".". Aliases
(cw = data["current_weather"]; cw["temperature"]) are not followed.

A SyntaxError propagates with .filename set; cli falls back to text_scan.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Optional

from patchpilot import errors
from patchpilot.models import SpecDiff, Usage

_SKIP_PARTS = {"__pycache__", ".pytest_cache"}


def _link_key(node: ast.AST) -> Optional[tuple[str, ast.AST]]:
    """If node is one string-key lookup, return (key, inner expression)."""
    if isinstance(node, ast.Subscript):
        sl = node.slice
        if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
            return sl.value, node.value
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get" and node.args
            and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
        return node.args[0].value, node.func.value
    return None


def key_chains(tree: ast.AST) -> list[tuple[int, list[str]]]:
    """Every maximal string-key chain in tree, as (line, keys from the root lookup outward)."""
    inner_links: set[int] = set()
    for node in ast.walk(tree):
        link = _link_key(node)
        if link is not None and _link_key(link[1]) is not None:
            inner_links.add(id(link[1]))
    chains: list[tuple[int, list[str]]] = []
    for node in ast.walk(tree):
        if id(node) in inner_links or _link_key(node) is None:
            continue
        keys: list[str] = []
        cur: ast.AST = node
        while True:
            link = _link_key(cur)
            if link is None:
                break
            keys.append(link[0])
            cur = link[1]
        keys.reverse()
        chains.append((node.lineno, keys))
    chains.sort(key=lambda c: (c[0], c[1]))
    return chains


def match_ids(path: str, diff: SpecDiff) -> list[str]:
    """IDs of changes whose `from` equals path or is a prefix of it."""
    return [c.id for c in diff.changes if path == c.from_ or path.startswith(c.from_ + ".")]


def python_files(client_dir: Path) -> list[Path]:
    client_dir = Path(client_dir)
    files = [p for p in client_dir.rglob("*.py")
             if p.is_file() and not (_SKIP_PARTS & set(p.relative_to(client_dir).parts))]
    if not files:
        raise errors.fail("S04", path=client_dir)
    return sorted(files)


def display_path(path: Path, client_dir: Path) -> str:
    """Repo-relative POSIX path (relative to the working directory when possible)."""
    path = Path(path).resolve()
    for base in (Path.cwd().resolve(), Path(client_dir).resolve().parent):
        try:
            return path.relative_to(base).as_posix()
        except ValueError:
            continue
    return path.as_posix()


def scan(client_dir: Path, diff: SpecDiff) -> list[Usage]:
    usages: list[Usage] = []
    for file in python_files(client_dir):
        source = file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(file))  # SyntaxError -> caller falls back
        lines = source.splitlines()
        by_line: dict[int, set[str]] = {}
        for line, keys in key_chains(tree):
            ids = match_ids("$." + ".".join(keys), diff)
            if ids:
                by_line.setdefault(line, set()).update(ids)
        rel = display_path(file, client_dir)
        for line in sorted(by_line):
            code = lines[line - 1].strip() if 0 < line <= len(lines) else ""
            usages.append(Usage(path=rel, line=line, code=code,
                                change_ids=sorted(by_line[line], key=lambda i: int(i[1:])),
                                method="ast"))
    return usages


# [integration] frd-trd.md §19 names (hkt-spec) as aliases.
match_chain = lambda chain, diff: match_ids("$." + ".".join(chain), diff)  # noqa: E731
rel = lambda path, root: Path(path).resolve().relative_to(Path(root).resolve()).as_posix()  # noqa: E731
