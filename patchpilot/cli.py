"""`patchpilot check` (frd-trd.md 2, 13.2, 14).

Order: check_proxy -> load/flatten/compare -> scan usages -> tests_hash ->
run_pytest (pass -> exit 0) -> loop up to --max-attempts [fresh_copy ->
prompt.build -> request_patch -> guard.check -> guard.apply -> run_pytest ->
tests_hash] -> report.write.

Exit codes: 0 patched / nothing to patch, 1 no verified patch (incl.
E_REFUSAL), 2 config error, 3 Claude API unavailable.
--pr (patchpilot/pr.py): S16/S17/S18 -> 2 before any Claude call; after a
verified patch, a failed git/gh step -> S19, exit 2 (patch file kept);
gh missing or not logged in -> branch still pushed, PR skipped, exit 0.
Screen text follows frd-trd.md 14 / backend-ai.md 4; styling is hkt-design's.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

from patchpilot import errors, guard, pr, report
from patchpilot.errors import (
    E_REFUSAL, E_TEST_HASH_CHANGED, E_TESTS_FAILED, PatchPilotError, screen_reason,
)
from patchpilot.llm import client as llm_client
from patchpilot.llm import prompt
from patchpilot.models import Attempt, SpecDiff, TestRun, Usage

DEFAULT_BASE_URL = "http://127.0.0.1:8787"


class _Screen:
    def __init__(self, quiet: bool):
        self.quiet = quiet
        self.rich = None    # [integration] render.Screen while the terminal view is live

    def __call__(self, text: str = "") -> None:
        if not self.quiet and self.rich is None:
            print(text, flush=True)


def _rich_screen(args):
    """[integration] hkt-frontend render.Screen for a real terminal.
    Off with --json, --plain, PATCHPILOT_PLAIN=1, NO_COLOR, or when stdout is not a TTY;
    then the frd-trd.md 14 plain text is printed instead."""
    if args.json or args.plain:
        return None
    try:
        from patchpilot import render
    except ImportError:          # rich not installed: plain text still works
        return None
    if render._plain_mode():
        return None
    return render.Screen(plain=False)


_KIND = {"structure_move": "move", "field_rename": "rename"}


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="patchpilot")
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check", help="diff specs, reproduce, patch, verify")
    c.add_argument("--old", default="specs/v1.json")
    c.add_argument("--new", default="specs/v2.json")
    c.add_argument("--client", default="client")
    c.add_argument("--tests", default="tests")
    c.add_argument("--out", default="out")
    c.add_argument("--max-attempts", type=int, default=3, choices=[1, 2, 3])
    c.add_argument("--no-llm", action="store_true", help="run steps 1-3 only")
    c.add_argument("--json", action="store_true", help="print report.json instead of the screen")
    c.add_argument("--pr", "--yes-pr", dest="pr", action="store_true",
                   help="after a verified patch: new branch, commit client/ only, push, gh pr create")
    c.add_argument("--base-url", default=None, help="override WEATHER_BASE_URL")
    c.add_argument("--plain", action="store_true", help="plain text output (no live terminal view)")
    return p


def _load_diff(repo: Path, old: str, new: str) -> SpecDiff:
    from patchpilot.diff import compare, flatten   # lazy: owned by another module
    old_spec = flatten.load_spec(repo / old)
    new_spec = flatten.load_spec(repo / new)
    old_map = flatten.flatten(flatten.response_schema(old_spec), old_spec.get("components", {}))
    new_map = flatten.flatten(flatten.response_schema(new_spec), new_spec.get("components", {}))
    old_ver = str((old_spec.get("info") or {}).get("version", ""))
    new_ver = str((new_spec.get("info") or {}).get("version", ""))
    return compare.compare(old_map, new_map, old_ver, new_ver)


def _scan(repo: Path, client_dir: str, diff: SpecDiff, say) -> tuple[list[Usage], str]:
    from patchpilot.usages import ast_scan
    try:
        return ast_scan.scan(repo / client_dir, diff), "ast"
    except SyntaxError as exc:
        say("WARN  " + errors.warn_message("W02", file=getattr(exc, "filename", client_dir)))
        from patchpilot.usages import text_scan
        return text_scan.scan(repo / client_dir, diff), "text"


def _short(h: Optional[str]) -> str:
    return report._short_hash(h)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    repo = Path.cwd()
    out_dir = repo / args.out
    out_rel = args.out
    say = _Screen(quiet=args.json)
    ui = None
    from patchpilot import runner

    state: dict = {"diff": None, "usages": [], "reproduce": None, "hash_before": None,
                   "attempts": [], "verify": None, "hash_after": None, "stage": None,
                   "model": None, "report": None}
    base_url = args.base_url or os.environ.get("WEATHER_BASE_URL") or DEFAULT_BASE_URL
    os.environ["WEATHER_BASE_URL"] = base_url   # [integration] frd-trd §19.1: tests read the same value

    def finish(status: str, patch_text: Optional[str] = None,
               err: Optional[PatchPilotError] = None, emit: bool = True) -> int:
        rep = report.build(
            status=status, out_dir=out_rel, base_url=base_url, proxy_stage=state["stage"],
            diff=state["diff"], usages=state["usages"], reproduce=state["reproduce"],
            tests_hash_before=state["hash_before"], attempts=state["attempts"],
            verify=state["verify"], tests_hash_after=state["hash_after"], model=state["model"],
            error_code=err.code if err else None, error_message=err.message if err else None,
        )
        report.write(rep, patch_text, out_dir, diff=state["diff"])
        state["report"] = rep
        if args.json and emit:
            print(json.dumps(rep, indent=2, ensure_ascii=False))
        return rep["result"]["exit_code"]

    def open_pr() -> int:
        """--pr after a verified patch. report.json is rewritten with the outcome."""
        rep = state["report"]
        res = pr.create(repo, patch_file=out_dir / report.PATCH_FILE,
                        body_file=out_dir / report.PR_BODY_FILE, client_dir=args.client,
                        title=report.PR_TITLE, out_rel=out_rel)
        if res.status == "created":
            say(f"PR  {res.url}")
            report.set_pr(rep, res.to_dict(), out_dir)
        elif res.status == "skipped_gh":
            say("PR skipped: gh not authenticated")
            say(f"      branch {res.branch} was pushed; back on {res.base}")
            report.set_pr(rep, res.to_dict(), out_dir)
        else:
            say(f"FAIL  {res.error.message}")
            report.set_pr(rep, res.to_dict(), out_dir, exit_code=res.error.exit_code,
                          error_code=res.error.code, error_message=res.error.message)
        if args.json:
            print(json.dumps(rep, indent=2, ensure_ascii=False))
        return rep["result"]["exit_code"]

    try:
        report.prepare_out(out_dir)
        if args.pr:
            pr.preflight(repo, out_rel)                              # S18/S16/S17: nothing changed
        if not args.no_llm:
            state["model"] = llm_client.resolve_model()             # S13 before anything else
        state["stage"] = runner.check_proxy(base_url)                # S06
        say(f"PatchPilot check — v2 is a staged spec for this demo   proxy stage: {state['stage']}")
        if state["model"]:
            say(f"model={state['model']} effort=medium/medium/high")
        if state["stage"] == "v1":
            say("WARN  " + errors.warn_message("W03"))
        ui = _rich_screen(args)
        if ui is not None:
            say.rich = ui
            ui.header()
            ui.step_start(1)

        # [1/5] Diff
        diff = _load_diff(repo, args.old, args.new)
        state["diff"] = diff
        report.write_spec_diff(diff, out_dir)
        say("")
        say(f"[1/5] Diff   {args.old} ({diff.from_version}) → {args.new} ({diff.to_version})")
        width = max((len(c.from_) for c in diff.changes), default=0)
        for c in diff.changes:
            say(f"  {c.id}  {c.type:<15} {c.from_:<{width}}  → {c.to}")
        for p in diff.removed:
            say(f"  removed  {p}")
        for p in diff.added:
            say(f"  added    {p}")
        if ui is not None:
            ui.render_diff(diff.changes, old_path=args.old, new_path=args.new)
            kinds = ", ".join(f"{c.id} {_KIND.get(c.type, c.type)}" for c in diff.changes)
            extra = len(diff.removed) + len(diff.added)
            ui.step_end(1, True, f"{len(diff.changes)} changes ({kinds})"
                        + (f", {extra} removed/added" if extra else ""))
        if not diff.changes and not diff.removed and not diff.added:
            say("DONE  " + errors.s_message("S09"))
            if ui is not None:
                ui.render_summary((errors.s_message("S09"),))
                say.rich = None
            return finish("nothing_to_patch")

        # [2/5] Affected usages
        usages, method = _scan(repo, args.client, diff, say)
        state["usages"] = usages
        say("")
        say(f"[2/5] Affected usages   ({method})")
        for u in usages:
            say(f"  {u.prompt_line()}   {' '.join(u.change_ids)}")
        if not usages:
            say("WARN  " + errors.warn_message("W01"))
        if ui is not None:
            ui.step_start(2)
            ui.render_usages(usages)
            files_hit = sorted({u.path for u in usages})
            ui.step_end(2, True, (f"{len(usages)} usages in {', '.join(files_hit)}" if usages
                                  else errors.warn_message("W01")) + f"  ({method})")

        # [3/5] Reproduce
        if not (repo / args.tests).is_dir():
            raise errors.fail("S05", path=args.tests)
        if ui is not None:
            ui.step_start(3, note="pytest running…")
        state["hash_before"] = runner.tests_hash(repo)
        runner.preflight_pytest(repo)                                # S15 before any Claude call
        repro = runner.run_pytest(repo)
        if ui is not None:
            ui.render_test_run(repro, state["hash_before"])
            ui.step_end(3, True, f"pytest {repro.failed} failed, {repro.passed} passed | "
                                 f"tests hash {_short(state['hash_before'])}")
        state["reproduce"] = repro
        say("")
        say(f"[3/5] Reproduce   WEATHER_BASE_URL={base_url}")
        err1 = next(iter(repro.first_error.values()), "")
        say(f"  pytest {repro.passed} passed, {repro.failed} failed   {err1}".rstrip())
        say(f"  tests hash {_short(state['hash_before'])}")
        if repro.failed == 0:
            say("DONE  " + errors.s_message("S10") + f" (proxy stage: {state['stage']})")
            if ui is not None:
                ui.render_summary((errors.s_message("S10") + f" (proxy stage: {state['stage']})",))
                say.rich = None
            return finish("nothing_to_patch")
        if args.no_llm:
            if ui is not None:
                ui.close()
                say.rich = None
            say("STOP  --no-llm: patch step skipped. No patch file was written.")
            return finish("no_verified_patch")

        # [4/5] Patch + [5/5] Verify
        allowed = guard.allowed_files(repo, args.client)
        if not allowed:
            raise errors.fail("S04", path=args.client)
        files = {p: (repo / p).read_text(encoding="utf-8") for p in sorted(allowed)}
        expected_total = repro.passed + repro.failed
        run_no = report.next_run_number(out_dir)
        patch_text: Optional[str] = None
        max_attempts = max(1, args.max_attempts)
        if ui is not None:
            ui.step_start(4)
            ui.patch_begin()

        for n in range(1, max_attempts + 1):
            effort = llm_client.effort_for_attempt(n)
            system, user = prompt.build(diff, usages, repro, files, state["attempts"])
            say(f"[4/5] Patch   attempt {n}/{max_attempts}  ... thinking")
            if ui is not None:
                ui.render_attempt(n, max_attempts, None, "thinking")
            raw, stop = llm_client.request_patch(system, user, effort, model=state["model"])
            meta = dict(llm_client.last_call)
            att = Attempt(n=n, effort=effort, result="rejected", raw_output=raw,
                          stop_reason=stop, model=meta.get("model"),
                          latency_s=meta.get("latency_s"),
                          input_tokens=meta.get("input_tokens"),
                          output_tokens=meta.get("output_tokens"))
            work = runner.fresh_copy(repo)
            verify: Optional[TestRun] = None
            hash_after: Optional[str] = None
            try:
                code, detail, cleaned = guard.check(raw, stop, allowed, work)
                if code == E_REFUSAL:
                    detail = meta.get("stop_category") or "unknown"
                if code is None:
                    att.files, att.added, att.removed = guard.diff_stats(cleaned, work)
                    guard.apply(cleaned, work)
                    try:
                        verify = runner.run_pytest(work)
                    except PatchPilotError as exc:
                        if exc.code != "S11":
                            raise
                        verify = TestRun(failed=1, output=getattr(exc, "output", "") or exc.message,
                                         timed_out=True)
                    if verify.failed or verify.passed < expected_total:
                        code = E_TESTS_FAILED
                        detail = ("timed out" if verify.timed_out
                                  else f"{verify.failed} failed, {verify.passed} passed")
                        att.test_log = verify.output
                    else:
                        hash_after = runner.tests_hash(work)
                        if hash_after != state["hash_before"]:
                            code = E_TEST_HASH_CHANGED
                            detail = f"{_short(state['hash_before'])} != {_short(hash_after)}"
            except guard.ApplyError as exc:
                code, detail = errors.E_APPLY_FAILED, str(exc)
            finally:
                runner.cleanup(work)

            if code is None:
                att.result = "diff_ok"
            else:
                att.code, att.detail = code, detail or ""
            state["attempts"].append(att)
            report.write_attempt(att, out_dir, max_attempts)
            report.append_run(out_dir, {
                "run": run_no, "attempt": n, "model": att.model, "effort": effort,
                "stop_reason": stop, "latency_s": att.latency_s,
                "input_tokens": att.input_tokens, "output_tokens": att.output_tokens,
                "verdict": code or "diff_ok",
                "tests": (f"{verify.passed}/{expected_total}" if verify else "not run"),
                "hash_ok": (hash_after == state["hash_before"]) if hash_after else None,
            })

            if ui is not None:
                if code is None:
                    ui.render_patch(cleaned, n, max_attempts, len(att.files), att.added, att.removed)
                else:
                    ui.render_rejected(n, max_attempts, code, screen_reason(code, att.detail))
            if code is None:
                say(f"[4/5] Patch   attempt {n}/{max_attempts}  ... diff OK "
                    f"({len(att.files)} file{'s' if len(att.files) != 1 else ''}, "
                    f"+{att.added} -{att.removed})")
                say(f"[5/5] Verify  apply OK | pytest {verify.passed} passed | tests hash "
                    f"{_short(state['hash_before'])} = {_short(hash_after)}")
                state["verify"], state["hash_after"] = verify, hash_after
                patch_text = cleaned
                break
            say(f"[4/5] Patch   attempt {n}/{max_attempts}  ... rejected ({code}): "
                f"{screen_reason(code, att.detail)}")
            if code == E_REFUSAL:
                break

        if patch_text is not None:
            say(f"DONE  {out_rel}/{report.PATCH_FILE}, {out_rel}/{report.PR_BODY_FILE}")
            if ui is not None:
                a = state["attempts"][-1]
                nf = len(a.files)
                hb, ha = _short(state["hash_before"]), _short(state["hash_after"])
                ui.step_end(4, True, f"attempt {a.n}/{max_attempts}  ... diff OK "
                            f"({nf} file{'s' if nf != 1 else ''}, +{a.added} -{a.removed})")
                ui.step_start(5)
                v = state["verify"]
                ui.step_end(5, True, f"apply OK | pytest {v.passed} passed | tests hash {hb} = {ha}")
                ui.render_hash_check(repro, v, state["hash_before"], state["hash_after"],
                                     files_changed=f"client/ only ({nf} file{'s' if nf != 1 else ''})")
                ui.render_summary((f"{out_rel}/{report.PATCH_FILE}", f"{out_rel}/{report.PR_BODY_FILE}"))
                say.rich = None        # PR lines (--pr) print below the frozen screen
            if not args.pr:
                return finish("patched", patch_text)
            finish("patched", patch_text, emit=False)
            try:
                return open_pr()
            except PatchPilotError as exc:          # S16/S17 re-check, or S19 from the return step
                say(f"FAIL  {exc.message}")
                report.set_pr(state["report"], {"status": "failed", "url": None,
                              "error": {"code": exc.code, "message": exc.message}}, out_dir,
                              exit_code=exc.exit_code, error_code=exc.code, error_message=exc.message)
                if args.json:
                    print(json.dumps(state["report"], indent=2, ensure_ascii=False))
                return exc.exit_code

        n_done = len(state["attempts"])
        if ui is not None:
            ui.step_end(4, False, f"no verified patch after {n_done} attempt{'s' if n_done != 1 else ''}")
            ui.render_error("No verified patch",
                            attempts=[(a.n, a.code or "", (a.detail or "").splitlines()[0] if a.detail else "")
                                      for a in state["attempts"]],
                            tests_hash=(state["hash_before"], runner.tests_hash(repo)), max_n=n_done)
            if args.pr:
                print("      --pr: no verified patch, so no branch or PR was created.", flush=True)
        say(f"FAIL  No verified patch after {n_done} attempt{'s' if n_done != 1 else ''}.")
        for a in state["attempts"]:
            say(f"      attempt {a.n}: {a.code:<17} ({(a.detail or '').splitlines()[0] if a.detail else ''})")
        say(f"      Tests unchanged: hash {_short(state['hash_before'])} = "
            f"{_short(runner.tests_hash(repo))}")
        say(f"      No patch file was written. Raw outputs: {out_rel}/{report.ATTEMPTS_DIR}/attempt_N.txt")
        if args.pr:
            say("      --pr: no verified patch, so no branch or PR was created.")
        return finish("no_verified_patch")

    except PatchPilotError as exc:
        if ui is not None and say.rich is not None:
            ui.render_error(exc.message)
            say.rich = None
        else:
            say(f"FAIL  {exc.message}")
        if exc.exit_code == errors.EXIT_OK:
            return finish("nothing_to_patch")
        status = report.EXIT_STATUS_ON_ERROR.get(exc.exit_code, "config_error")
        return finish(status, err=exc)


if __name__ == "__main__":
    sys.exit(main())
