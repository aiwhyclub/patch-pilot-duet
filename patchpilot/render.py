"""Terminal rendering for `patchpilot check`.

Layout follows hkt-design terminal-design.md v2:
  header line, rule, five step lines ([n/5]), rule, detail area (<= 14 lines),
  then the "Same tests, before and after" box that freezes for >= 3 seconds.

The pipeline calls only the functions in this module. Inputs are plain values,
dicts, or objects with matching attributes (models.py), so this module has no
dependency on the rest of the package.

ASCII mode (PATCHPILOT_ASCII=1): same screen, symbols replaced 1:1 (see ASCII_TABLE).

Plain mode (NO_COLOR set, stdout not a TTY, or PATCHPILOT_PLAIN=1): no colors,
no symbols, no live redraw. Each finished step prints its log line verbatim.
"""

from __future__ import annotations

import os
import sys
import re
import time
from typing import Any, Iterable

from rich.console import Console, Group
from rich.constrain import Constrain
from rich.live import Live
from rich.panel import Panel
from rich.rule import Rule
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from rich import box

MAX_WIDTH = 96          # design §0.5
DETAIL_MAX_LINES = 14   # design §5
PATCH_PREVIEW_MAX = 12  # design §3.4
USAGES_MAX = 8          # design §3.2
HASH_LEN = 12           # design §1
STEP_NAMES = {1: "Diff", 2: "Usages", 3: "Reproduce", 4: "Patch", 5: "Verify"}

# ASCII fallback (PATCHPILOT_ASCII=1) for terminals or fonts that do not show the symbols.
# One-to-one, same width, so layout and the 24-line budget do not change.
ASCII_TABLE = str.maketrans({
    "✓": "+", "✗": "x", "⊘": "!", "○": "-", "●": "*", "→": ">", "…": ".", "—": "-",
    "╭": "+", "╮": "+", "╰": "+", "╯": "+", "┌": "+", "┐": "+", "└": "+", "┘": "+",
    "├": "+", "┤": "+", "┬": "+", "┴": "+", "┼": "+", "│": "|", "─": "-", "·": "-",
})


def _ascii_mode() -> bool:
    return os.environ.get("PATCHPILOT_ASCII") == "1"


class _Ascii:
    """Renders the wrapped renderable with ASCII_TABLE applied to every segment."""

    def __init__(self, renderable: Any) -> None:
        self.renderable = renderable

    def __rich_console__(self, console: Console, options: Any):
        from rich.segment import Segment
        for seg in console.render(self.renderable, options):
            if seg.control or not seg.text:
                yield seg
            else:
                yield Segment(seg.text.translate(ASCII_TABLE), seg.style)


S_HASH = "bold magenta"
S_PASS = "bold green"
S_FAIL = "bold red"
S_REJECT = "bold yellow"


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        if key == "from_":
            return obj.get("from", default)
        return default
    if hasattr(obj, key):
        return getattr(obj, key)
    if key == "from_":
        return getattr(obj, "from", default)
    return default


def short_hash(value: str) -> str:
    """'sha256:abcdef...' or 'abcdef...' -> first 12 hex chars."""
    if not value:
        return "-" * HASH_LEN
    if ":" in value:
        value = value.split(":", 1)[1]
    return value[:HASH_LEN]


def _cut(text: str, width: int = MAX_WIDTH) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _plain_mode() -> bool:
    if os.environ.get("PATCHPILOT_PLAIN") == "1" or "NO_COLOR" in os.environ:
        return True
    return not sys.stdout.isatty()


class Screen:
    """One run of `patchpilot check`. Create once, call methods in order."""

    def __init__(self, console: Console | None = None, plain: bool | None = None,
                 freeze_seconds: float | None = None) -> None:
        self.plain = _plain_mode() if plain is None else plain
        self.ascii = _ascii_mode()
        self._console = console or Console(
            width=min(100, Console().width) if not self.plain else 200,
            no_color=self.plain, highlight=False, emoji=False,
            force_terminal=None if not self.plain else False,
        )
        if freeze_seconds is None:
            freeze_seconds = float(os.environ.get("PATCHPILOT_FREEZE", "3.5"))
        self.freeze_seconds = freeze_seconds
        self.title = ""
        self.started = time.monotonic()
        self.frozen_elapsed: str | None = None
        # step n -> (state, result text or Text); state: wait | run | ok | fail
        self.steps: dict[int, tuple[str, Any]] = {n: ("wait", "") for n in STEP_NAMES}
        self.detail: list[Any] = []
        self.footer: list[Any] = []
        self.live: Live | None = None

    @property
    def console(self) -> Console:
        return self._console

    def _say(self, text: str) -> None:
        """Plain-mode line (verbatim log text)."""
        self._console.print(text.translate(ASCII_TABLE) if self.ascii else text, markup=False)

    # ---------- live frame ----------
    def _elapsed(self) -> str:
        if self.frozen_elapsed:
            return self.frozen_elapsed
        s = int(time.monotonic() - self.started)
        return f"{s // 60:02d}:{s % 60:02d}"

    def __rich__(self) -> Any:
        head = Table.grid(expand=True)
        head.add_column(ratio=1)
        head.add_column(justify="right")
        head.add_row(Text.from_markup(f" [bold]PatchPilot[/]  check  ·  {self.title}"),
                     Text(self._elapsed(), style="dim"))
        parts: list[Any] = [head, Rule(style="dim")]
        for n, name in STEP_NAMES.items():
            state, result = self.steps[n]
            label = f"[{n}/5] {name:<10}"
            if state == "run":
                row = Table.grid(padding=(0, 1))
                row.add_column(width=2, justify="right")
                row.add_column()
                row.add_row(Spinner("line" if self.ascii else "dots", style="bold"),
                            Text(label, style="bold") + _as_text(result))
                parts.append(row)
                continue
            sym, sty = {"wait": ("○", "dim"), "ok": ("✓", "green"),
                        "fail": ("✗", S_FAIL)}[state]
            line = Text(" ")
            line.append(sym, style=sty)
            line.append(" " + label, style="dim" if state == "wait" else "")
            line.append_text(_as_text(result))
            parts.append(line)
        if self.detail:
            parts.append(Rule(style="dim"))
            parts.extend(self.detail[:DETAIL_MAX_LINES])
        parts.extend(self.footer)
        frame = Constrain(Group(*parts), width=MAX_WIDTH)
        return _Ascii(frame) if self.ascii else frame

    def _refresh(self) -> None:
        if self.live:
            self.live.update(self, refresh=True)

    # ---------- 1. header ----------
    def header(self, project: str = "Open-Meteo", api: str = "v1 → v2 (staged)") -> None:
        """Start the screen. '(staged)' stays on screen (honesty rule)."""
        self.title = f"{project} {api}"
        self.started = time.monotonic()
        if self.plain:
            self._say(f"PatchPilot check - {project} {api}. v2 is a staged spec for this demo.")
            return
        self.console.show_cursor(False)
        self.live = Live(self, console=self.console, auto_refresh=True,
                         refresh_per_second=8, transient=False)
        self.live.start()

    # ---------- 2. step_start ----------
    def step_start(self, n: int, title: str | None = None, note: str = "") -> None:
        """Mark step n as running. Clears the detail area."""
        self.steps[n] = ("run", note)
        self.detail = []
        self._refresh()

    # ---------- 3. step_end ----------
    def step_end(self, n: int, ok: bool, line: str, seconds: float | None = None,
                 styled: Text | None = None) -> None:
        """Collapse step n into one line. `line` is the backend log text, verbatim."""
        if not self.plain:
            line, styled = _fit_step_line(line, styled, self.console.width - STEP_PREFIX)
        self.steps[n] = ("ok" if ok else "fail", styled if styled is not None else line)
        if self.plain:
            self._say(_cut(f"[{n}/5] {STEP_NAMES[n]:<10}{line}"))
        self._refresh()

    # ---------- 4. render_diff ----------
    def render_diff(self, changes: Iterable[Any], old_path: str = "specs/v1.json",
                    new_path: str = "specs/v2.json", shown_ids: tuple[str, ...] = ("C1", "C2")) -> None:
        """Table of C1, C2; other changes as one dim line (design §3.1)."""
        changes = list(changes)
        shown = [c for c in changes if _get(c, "id") in shown_ids]
        rest = [c for c in changes if _get(c, "id") not in shown_ids]
        if self.plain:
            for c in changes:
                self._say(f"  {_get(c,'id')}  {_get(c,'type')}  {_get(c,'from_')} -> {_get(c,'to')}")
            return
        top = Table.grid(expand=True)
        top.add_column(ratio=1)
        top.add_column(justify="right")
        top.add_row(Text(" API changes  v1 → v2", style="bold"),
                    Text(f"{old_path} → {new_path}", style="dim"))
        t = Table(box=box.SQUARE, show_edge=True, pad_edge=True, header_style="dim")
        for col in ("id", "type", "v1", "v2"):
            t.add_column(col, no_wrap=True, overflow="ellipsis")
        for c in shown:
            t.add_row(Text(_get(c, "id"), style="bold"), _get(c, "type"),
                      Text("- " + _strip(_get(c, "from_")), style="red"),
                      Text("+ " + _strip(_get(c, "to")), style="green"))
        self.detail = [top, t]
        if rest:
            ids = ", ".join(_get(c, "id") for c in rest)
            self.detail.append(Text(f" + {len(rest)} more in units ({ids})", style="dim"))
        self._refresh()

    # ---------- 5. render_usages ----------
    def render_usages(self, usages: Iterable[Any], old_names: Iterable[str] = ("current_weather", "temperature")) -> None:
        """`path:line: code`, old field names in red (design §3.2)."""
        usages = list(usages)
        if self.plain:
            for u in usages:
                self._say(f"  {_get(u,'path')}:{_get(u,'line')}: {_get(u,'code')}")
            return
        lines: list[Any] = [_two_col(Text(" Affected usages", style="bold"), Text("path:line: code", style="dim"))]
        show = usages if len(usages) <= USAGES_MAX else usages[: USAGES_MAX - 1]
        names = [f'"{n}"' for n in old_names]
        for u in show:
            t = Text("   ")
            t.append(f"{_get(u, 'path')}:", style="dim")
            t.append(str(_get(u, "line")), style="bold")
            t.append(": ")
            code = Text(str(_get(u, "code")))
            for nm in names:
                code.highlight_words([nm], style="red")
            t.append_text(code)
            t.truncate(MAX_WIDTH, overflow="ellipsis")
            lines.append(t)
        if len(usages) > len(show):
            lines.append(Text(f"   … and {len(usages) - len(show)} more", style="dim"))
        self.detail = lines
        self._refresh()

    # ---------- 6. render_test_run ----------
    def render_test_run(self, run: Any, tests_hash: str, phase: str = "reproduce") -> None:
        """Reproduce: hash first, FAILED lines with one-line cause (design §3.3)."""
        passed, failed = _get(run, "passed", 0), _get(run, "failed", 0)
        if self.plain:
            self._say(f"  pytest {failed} failed, {passed} passed | tests hash {short_hash(tests_hash)}")
            return
        lines: list[Any] = [
            _two_col(Text(" Fixed tests against v2", style="bold"), Text("pytest -q tests/", style="dim")),
            Text.assemble(" ", ("#", S_HASH), " tests hash  ", (short_hash(tests_hash), S_HASH), ("   (locked)", "dim")),
            Text(""),
        ]
        errors = _get(run, "first_error", {}) or {}
        for name in (_get(run, "failed_tests", []) or [])[:4]:
            lines.append(Text.assemble("   ", ("✗ FAILED", S_FAIL), "  ", _cut(str(name), 80)))
            if name in errors:
                lines.append(Text("             " + _cut(str(errors[name]), 80), style="dim"))
        lines.append(Text(""))
        lines.append(Text.assemble("   ", (f"{failed} failed", S_FAIL if failed else ""),
                                   ", ", (f"{passed} passed", "green")))
        self.detail = lines
        self._refresh()

    # ---------- 7. render_patch ----------
    def patch_begin(self) -> None:
        self.detail = [_two_col(Text(" Claude patch", style="bold"),
                                Text("scope: client/**   tests/ locked", style="dim"))]
        self._refresh()

    def render_attempt(self, n: int, max_n: int, ok: bool | None, text: str) -> None:
        """One attempt line. ok=None: thinking, True: diff OK, False: use render_rejected."""
        if self.plain and ok is not None:
            self._say(f"  attempt {n}/{max_n}  ... {text}")
        line = Text(" ")
        line.append(*("●", "bold") if ok is None or ok else ("⊘", S_REJECT))
        line.append(f" attempt {n}/{max_n}  ... ")
        line.append(text, style="dim" if ok is None else "")
        self._replace_attempt(n, line)

    def render_patch(self, diff_text: str, n: int, max_n: int, files: int, added: int, removed: int) -> None:
        """Successful attempt line + diff preview (max 12 lines)."""
        self.render_attempt(n, max_n, True, f"diff OK ({files} file{'s' if files != 1 else ''}, +{added} -{removed})")
        if self.plain:
            return
        preview = _diff_preview(diff_text)
        body: list[Any] = [Text("")]
        cut = preview[:PATCH_PREVIEW_MAX]
        body.extend(cut)
        if len(preview) > len(cut):
            body.append(Text(f"   … {len(preview) - len(cut)} more lines (out/patchpilot.patch)", style="dim"))
        room = DETAIL_MAX_LINES - len(self.detail)
        self.detail.extend(body[: max(room, 0)])
        self._refresh()

    # ---------- 8. render_rejected ----------
    def render_rejected(self, n: int, max_n: int, code: str, reason: str) -> None:
        """Yellow ⊘ line: rejected (E_*): reason."""
        if self.plain:
            self._say(_cut(f"  attempt {n}/{max_n}  ... rejected ({code}): {reason}"))
        line = Text(" ")
        line.append("⊘", style=S_REJECT)
        line.append(f" attempt {n}/{max_n}  ... rejected (")
        line.append(code, style="bold")
        line.append(f"): {reason}")
        line.truncate(MAX_WIDTH, overflow="ellipsis")
        self._replace_attempt(n, line)

    # ---------- 9. render_hash_check ----------
    def render_hash_check(self, before: Any, after: Any, hash_before: str, hash_after: str,
                          files_changed: str = "client/ only (1 file)") -> None:
        """The decisive box (design §4.1). Clears the detail area."""
        hb, ha = short_hash(hash_before), short_hash(hash_after)
        same = hb == ha and hash_before == hash_after
        if self.plain:
            self._say(f"  tests hash {hb} = {ha}" if same else f"  tests hash {hb} != {ha}")
            return
        g = Table.grid(padding=(0, 2))
        g.add_column(width=16, style="dim")
        g.add_column(width=22)
        g.add_column(width=26)
        g.add_row("", Text("before patch", style="dim"), Text("after patch", style="dim"))
        g.add_row("tests vs v2",
                  Text(f"✗ {_get(before,'failed',0)} failed, {_get(before,'passed',0)} passed", style=S_FAIL),
                  Text(f"✓ {_get(after,'passed',0)} passed", style=S_PASS) if not _get(after, "failed", 0)
                  else Text(f"✗ {_get(after,'failed')} failed", style=S_FAIL))
        g.add_row("tests hash", Text(hb, style=S_HASH),
                  Text.assemble((ha, S_HASH), "   ", ("identical", "green") if same else ("CHANGED", S_FAIL)))
        g.add_row("files changed", Text("—", style="dim"), Text(files_changed))
        body = Group(g, Text(""), Text("Tests were not changed. The client code was.", style="bold"))
        chip = Text(" PASS ", style="black on green") if same else Text(" FAIL ", style="white on red")
        title = Text.assemble(("Same tests, before and after", "bold"))
        self.detail = []
        self.footer = [Text(""), Panel(body, title=title, title_align="left",
                                       subtitle=Text.assemble("[", chip, "]"), subtitle_align="right",
                                       box=box.ROUNDED, width=MAX_WIDTH - 22, padding=(0, 2),
                                       border_style="green" if same else "red")]
        self._refresh()

    # ---------- 10. render_summary ----------
    def render_summary(self, paths: Iterable[str] = ("out/patchpilot.patch", "out/PR_BODY.md")) -> None:
        """DONE line, then freeze >= 3 s: clock fixed, no spinner, cursor hidden."""
        done = "DONE  " + ", ".join(paths)
        if self.plain:
            self._say(done)
            return
        self.footer.append(Text.assemble((" DONE", "bold green"), "  " + ", ".join(paths)))
        self.frozen_elapsed = self._elapsed()
        if self.live:
            # Live.stop() draws the final frame exactly once (in place). No extra refresh here.
            self.live.update(self, refresh=False)
            self.live.stop()  # note: rich shows the cursor here
            self.live = None
        self.console.show_cursor(False)  # keep the freeze frame clean
        time.sleep(self.freeze_seconds)
        self.console.show_cursor(True)

    # ---------- 11. render_error ----------
    def render_error(self, message: str, attempts: Iterable[tuple[int, str, str]] | None = None,
                     tests_hash: tuple[str, str] | None = None, max_n: int = 3) -> None:
        """No attempts: one red line (API / setup error). With attempts: FAIL box (design §4.2).
        Never pass secret values here."""
        if attempts is None:
            if self.plain:
                self._say(f"FAIL  {message}")
            else:
                self.footer = [Text.assemble(("✗ FAIL", S_FAIL), "  " + _cut(message, 88), style="red")]
            self.close()
            return
        lines = [Text.assemble(("FAIL", S_FAIL), f"  No verified patch after {max_n} attempts.")]
        for n, code, detail in attempts:
            lines.append(Text.assemble(f"      attempt {n}: ", (f"{code:<18}", "bold"), f"({detail})"))
        if tests_hash:
            lines.append(Text(f"      Tests unchanged: hash {short_hash(tests_hash[0])} = {short_hash(tests_hash[1])}"))
        lines.append(Text("      No patch file was written."))
        lines.append(Text("      Raw outputs: out/attempts/attempt_{1,2,3}.txt", style="dim"))
        if self.plain:
            for l in lines:
                self._say(l.plain)
            return
        chip = Text(" FAIL ", style="white on red")
        self.detail = []
        self.footer = [Text(""), Panel(Group(*lines), title=Text("No verified patch", style="bold"),
                                       title_align="left", subtitle=Text.assemble("[", chip, "]"),
                                       subtitle_align="right", box=box.ROUNDED, width=MAX_WIDTH - 22,
                                       border_style="red")]
        self.close()

    def close(self) -> None:
        """Safe to call more than once; draws nothing after the screen is stopped."""
        if self.live is None:
            return
        self.frozen_elapsed = self._elapsed()
        self.live.update(self, refresh=False)
        self.live.stop()
        self.live = None
        self.console.show_cursor(True)

    # ---------- helpers ----------
    def _replace_attempt(self, n: int, line: Text) -> None:
        if self.plain:
            return
        if not self.detail:
            self.patch_begin()
        idx = n  # detail[0] is the "Claude patch" header
        if idx < len(self.detail):
            self.detail[idx] = line
        else:
            self.detail.append(line)
        self._refresh()


def _as_text(value: Any) -> Text:
    if isinstance(value, Text):
        return value
    return Text(_cut(str(value), MAX_WIDTH - 18))


STEP_PREFIX = 19   # " ✓ [5/5] Verify     " = 19 cells
_HASH_EQ = re.compile(r"tests hash ([0-9a-f]+) = ([0-9a-f]+)")


def _fit_step_line(line: str, styled: Text | None, room: int) -> tuple[str, Text | None]:
    """Keep a collapsed step line on one row of a narrow terminal (e.g. 80 cols).
    At 100 cols nothing changes. If too long, 'tests hash X = X' becomes
    'tests unchanged (first 8)'; anything still too long is cut with '…'."""
    if len(line) <= room:
        return line, styled
    m = _HASH_EQ.search(line)
    if m and m.group(1) == m.group(2):
        short = m.group(1)[:8]
        head = line[: m.start()]
        line = f"{head}tests unchanged ({short})" + line[m.end():]
        styled = Text(head)
        styled.highlight_regex(r"\d+ failed", "red")
        styled.highlight_regex(r"\d+ passed", "green")
        styled.append("tests unchanged (")
        styled.append(short, style=S_HASH)
        styled.append(")" + line[len(head) + len("tests unchanged (") + len(short) + 1:])
    if len(line) > room:
        line = _cut(line, room)
        if styled is not None:
            styled.truncate(room, overflow="ellipsis")
    return line, styled


def _two_col(left: Text, right: Text) -> Table:
    t = Table.grid(expand=True)
    t.add_column(ratio=1)
    t.add_column(justify="right")
    t.add_row(left, right)
    return t


def _strip(path: str) -> str:
    return path[2:] if path and path.startswith("$.") else path


def _diff_preview(diff_text: str) -> list[Text]:
    """'-'/'+' lines with new-file line numbers, file name header."""
    out: list[Text] = []
    old_no = new_no = 0
    for raw in diff_text.splitlines():
        if raw.startswith("diff --git "):
            out.append(Text(" " + raw.split(" b/", 1)[-1], style="bold"))
        elif raw.startswith("@@"):
            try:
                seg = raw.split(" ")
                old_no = int(seg[1].split(",")[0][1:])
                new_no = int(seg[2].split(",")[0][1:])
            except (IndexError, ValueError):
                pass
        elif raw.startswith(("---", "+++", "index ")):
            continue
        elif raw.startswith("-"):
            out.append(Text(_cut(f"   {old_no:>3} - {raw[1:].strip()}"), style="red"))
            old_no += 1
        elif raw.startswith("+"):
            out.append(Text(_cut(f"   {new_no:>3} + {raw[1:].strip()}"), style="green"))
            new_no += 1
        else:
            old_no += 1
            new_no += 1
    return out


# ---------- self-check (fake data, design examples) ----------
def _demo(freeze: float = 0.0, delay: float = 0.0) -> Screen:
    s = Screen(freeze_seconds=freeze)
    s.header()
    changes = [
        {"id": "C1", "type": "structure_move", "from": "$.current_weather", "to": "$.current"},
        {"id": "C2", "type": "field_rename", "from": "$.current_weather.temperature", "to": "$.current.temperature_2m"},
        {"id": "C3", "type": "structure_move", "from": "$.current_weather_units", "to": "$.current_units"},
        {"id": "C4", "type": "field_rename", "from": "$.current_weather_units.temperature", "to": "$.current_units.temperature_2m"},
    ]
    s.step_start(1); s.render_diff(changes); time.sleep(delay)
    s.step_end(1, True, "2 breaking changes (C1 move, C2 rename)")
    usages = [
        {"path": "client/weather_client.py", "line": 42, "code": 'temp = data["current_weather"]["temperature"]'},
        {"path": "client/weather_client.py", "line": 47, "code": 'wind = data["current_weather"]["windspeed"]'},
    ]
    s.step_start(2); s.render_usages(usages); time.sleep(delay)
    s.step_end(2, True, "2 usages in client/weather_client.py")
    h = "sha256:a1b2c3d4e5f6" + "0" * 52
    before = {"passed": 1, "failed": 3, "failed_tests": ["tests/test_weather.py::test_current_temperature"],
              "first_error": {"tests/test_weather.py::test_current_temperature": "KeyError: 'current_weather'"}}
    s.step_start(3); s.render_test_run(before, h); time.sleep(delay)
    s.step_end(3, True, "pytest 3 failed, 1 passed | tests hash a1b2c3d4e5f6",
               styled=Text.assemble("pytest ", ("3 failed", "red"), ", 1 passed | tests hash ", ("a1b2c3d4e5f6", S_HASH)))
    s.step_start(4); s.patch_begin()
    s.render_attempt(1, 3, None, "thinking"); time.sleep(delay)
    s.render_rejected(1, 3, "E_FORBIDDEN_PATH", "diff touched tests/test_weather.py")
    diff = ('diff --git a/client/weather_client.py b/client/weather_client.py\n'
            '--- a/client/weather_client.py\n+++ b/client/weather_client.py\n@@ -42,6 +42,6 @@\n'
            '-    temp = data["current_weather"]["temperature"]\n+    temp = data["current"]["temperature_2m"]\n'
            '     x = 1\n     y = 2\n     z = 3\n     w = 4\n'
            '-    wind = data["current_weather"]["windspeed"]\n+    wind = data["current"]["windspeed"]\n')
    s.render_patch(diff, 2, 3, 1, 2, 2); time.sleep(delay)
    s.step_end(4, True, "attempt 2/3  ... diff OK (1 file, +2 -2)")
    s.step_start(5, note="apply OK | pytest running…"); time.sleep(delay)
    s.step_end(5, True, "apply OK | pytest 4 passed | tests hash a1b2c3d4e5f6 = a1b2c3d4e5f6",
               styled=Text.assemble("apply OK | pytest ", ("4 passed", "green"), " | tests hash ",
                                    ("a1b2c3d4e5f6", S_HASH), " = ", ("a1b2c3d4e5f6", S_HASH)))
    s.render_hash_check(before, {"passed": 4, "failed": 0}, h, h)
    s.render_summary()
    return s


if __name__ == "__main__":
    _demo(freeze=float(os.environ.get("PATCHPILOT_FREEZE", "3.5")), delay=0.6)
