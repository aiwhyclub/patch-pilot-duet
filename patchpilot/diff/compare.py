"""Compare two flattened response schemas (frd-trd.md section 4.2).

Rules:
  1. Walk both trees from "$" in parallel. Same-named children are matched.
  2. Under one parent, a removed object X and an added object Y are a
     `structure_move` when their child-key sets are equal except for at most
     one renamed key. Ambiguous pairs are broken by how many children match
     by name and type, then by name similarity.
  3. Inside a matched pair, exactly one removed and one added leaf of the
     same type is a `field_rename`.
  4. Anything left is reported in `removed` / `added` (top-most path only).
  5. IDs in order: a move, then the renames inside it, then the next move.
     For the demo specs: C1 move, C2 rename, C3 move, C4 rename.
"""
from __future__ import annotations

from difflib import SequenceMatcher

from patchpilot.models import Change, FieldMap, SpecDiff


def _child(path: str, key: str) -> str:
    return f"{path}.{key}"


def _is_move_pair(old: FieldMap, new: FieldMap, op: str, np_: str) -> bool:
    a, b = old[op], new[np_]
    if a.type != "object" or b.type != "object":
        return False
    oc, nc = set(a.children), set(b.children)
    return len(oc - nc) <= 1 and len(nc - oc) <= 1 and len(oc) == len(nc)


def _pair_score(old: FieldMap, new: FieldMap, op: str, np_: str) -> tuple[int, float]:
    a, b = old[op], new[np_]
    same_typed = sum(
        1 for k in set(a.children) & set(b.children)
        if old[_child(op, k)].type == new[_child(np_, k)].type
    )
    name_ratio = SequenceMatcher(None, op.rsplit(".", 1)[-1], np_.rsplit(".", 1)[-1]).ratio()
    return same_typed, name_ratio


def compare(old: FieldMap, new: FieldMap, old_ver: str, new_ver: str) -> SpecDiff:
    changes: list[Change] = []
    removed: list[str] = []
    added: list[str] = []

    def next_id() -> str:
        return f"C{len(changes) + 1}"

    def walk(op: str, np_: str) -> None:
        oc = old[op].children
        nc = new[np_].children
        common = [k for k in oc if k in nc]
        old_only = [k for k in oc if k not in nc]
        new_only = [k for k in nc if k not in oc]

        # Rule 3: one removed + one added leaf of the same type -> rename.
        old_leaves = [k for k in old_only if old[_child(op, k)].type != "object"]
        new_leaves = [k for k in new_only if new[_child(np_, k)].type != "object"]
        if len(old_leaves) == 1 and len(new_leaves) == 1:
            ok, nk = old_leaves[0], new_leaves[0]
            if old[_child(op, ok)].type == new[_child(np_, nk)].type:
                changes.append(Change(id=next_id(), type="field_rename",
                                      from_=_child(op, ok), to=_child(np_, nk),
                                      value_type=old[_child(op, ok)].type))
                old_only.remove(ok)
                new_only.remove(nk)

        # Rule 2: pair removed/added objects as moves, best score first.
        old_objs = [k for k in old_only if old[_child(op, k)].type == "object"]
        new_objs = [k for k in new_only if new[_child(np_, k)].type == "object"]
        candidates = []
        for ok in old_objs:
            for nk in new_objs:
                if _is_move_pair(old, new, _child(op, ok), _child(np_, nk)):
                    candidates.append((_pair_score(old, new, _child(op, ok), _child(np_, nk)), ok, nk))
        candidates.sort(key=lambda c: c[0], reverse=True)
        pairs: dict[str, str] = {}
        used_new: set[str] = set()
        for _score, ok, nk in candidates:
            if ok not in pairs and nk not in used_new:
                pairs[ok] = nk
                used_new.add(nk)

        # Same-named children: recurse into objects; a type change is remove + add.
        for k in common:
            ot, nt = old[_child(op, k)].type, new[_child(np_, k)].type
            if ot == "object" and nt == "object":
                walk(_child(op, k), _child(np_, k))
            elif ot != nt:
                removed.append(_child(op, k))
                added.append(_child(np_, k))

        # Moves in old key order, each followed by what changed inside it.
        for ok in old_objs:
            if ok in pairs:
                nk = pairs[ok]
                changes.append(Change(id=next_id(), type="structure_move",
                                      from_=_child(op, ok), to=_child(np_, nk)))
                walk(_child(op, ok), _child(np_, nk))

        removed.extend(_child(op, k) for k in old_only if k not in pairs)
        added.extend(_child(np_, k) for k in new_only if k not in used_new)

    if "$" in old and "$" in new:
        walk("$", "$")
    return SpecDiff(from_version=old_ver, to_version=new_ver,
                    changes=changes, removed=removed, added=added)
