"""Shared data types for PatchPilot (frd-trd.md section 13.1).

Field names follow frd-trd.md 13.1. Additions beyond 13.1 are marked
"[added]" and have defaults, so code written against 13.1 still works.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal, Optional

ChangeType = Literal["structure_move", "field_rename"]
AttemptResult = Literal["diff_ok", "rejected"]


@dataclass
class FieldEntry:
    path: str                      # "$.current_weather.temperature"
    type: str                      # object | number | integer | string | boolean | array
    parent: Optional[str] = None   # "$.current_weather"
    children: list[str] = field(default_factory=list)  # direct child keys, sorted


FieldMap = dict[str, FieldEntry]


@dataclass
class Change:
    id: str                        # "C1".."C4"
    type: ChangeType
    from_: str                     # JSON key "from"
    to: str
    value_type: Optional[str] = None  # only for field_rename

    def to_dict(self) -> dict:
        d = {"id": self.id, "type": self.type, "from": self.from_, "to": self.to}
        if self.value_type is not None:
            d["value_type"] = self.value_type
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Change":
        return cls(id=d["id"], type=d["type"], from_=d["from"], to=d["to"],
                   value_type=d.get("value_type"))


@dataclass
class SpecDiff:
    from_version: str
    to_version: str
    changes: list[Change] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Shape of out/spec_diff.json (frd-trd.md 4.3). removed/added always present."""
        return {
            "from_version": self.from_version,
            "to_version": self.to_version,
            "changes": [c.to_dict() for c in self.changes],
            "removed": list(self.removed),
            "added": list(self.added),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SpecDiff":
        return cls(
            from_version=d["from_version"],
            to_version=d["to_version"],
            changes=[Change.from_dict(c) for c in d.get("changes", [])],
            removed=list(d.get("removed", [])),
            added=list(d.get("added", [])),
        )


@dataclass
class Usage:
    path: str                      # "client/weather_client.py"
    line: int
    code: str                      # stripped source line
    change_ids: list[str] = field(default_factory=list)
    method: Literal["ast", "text"] = "ast"

    def prompt_line(self) -> str:
        """backend-ai.md 2.2: `client/weather_client.py:42: temp = ...`"""
        return f"{self.path}:{self.line}: {self.code}"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TestRun:
    passed: int = 0
    failed: int = 0
    failed_tests: list[str] = field(default_factory=list)
    first_error: dict[str, str] = field(default_factory=dict)  # test name -> one-line error
    output: str = ""
    duration_s: float = 0.0
    timed_out: bool = False

    __test__ = False  # keep pytest from collecting this class


@dataclass
class Attempt:
    n: int
    effort: str                    # "medium" | "high"
    result: AttemptResult
    code: Optional[str] = None     # E_* code when rejected
    detail: str = ""               # paths, git error, reason ... (see guard.check)
    raw_output: str = ""           # Claude text output, unmodified
    files: list[str] = field(default_factory=list)
    added: int = 0
    removed: int = 0
    # [added] fields used by prompt retry blocks and out/runs.jsonl
    test_log: Optional[str] = None     # pytest output after this attempt (E_TESTS_FAILED only)
    stop_reason: Optional[str] = None
    model: Optional[str] = None
    latency_s: Optional[float] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


# [integration] frd-trd.md §19 names (hkt-spec) as aliases of the working names.
Change.to_json = Change.to_dict
SpecDiff.to_json = SpecDiff.to_dict
Usage.to_json = Usage.to_dict
