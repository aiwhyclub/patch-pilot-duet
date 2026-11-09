"""Stdlib self-tests for llm/prompt, llm/client, guard, report, cli.

Run from the repository root (no pytest, no anthropic needed):
    PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s selftests -v

The anthropic SDK is replaced by a stub module, so these tests never call
the real API. The diff/usages modules and runner's pytest/proxy calls are
stubbed in the cli tests. git must be installed (guard uses git apply).
Kept outside tests/ on purpose: tests/ is the locked demo area.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from patchpilot import errors, guard, report  # noqa: E402
from patchpilot.llm import client as llm_client  # noqa: E402
from patchpilot.llm import prompt  # noqa: E402
from patchpilot.models import Attempt, Change, SpecDiff, TestRun, Usage  # noqa: E402

CLIENT_V1 = '''import os

import requests

BASE_URL = os.environ.get("WEATHER_BASE_URL", "http://127.0.0.1:8787")


def _fetch(lat, lon):
    r = requests.get(f"{BASE_URL}/forecast", params={"latitude": lat, "longitude": lon, "current_weather": "true"})
    r.raise_for_status()
    return r.json()


def get_current_temperature(lat, lon):
    data = _fetch(lat, lon)
    return data["current_weather"]["temperature"]


def get_current_windspeed(lat, lon):
    data = _fetch(lat, lon)
    return data["current_weather"]["windspeed"]
'''

GOOD_DIFF = '''diff --git a/client/weather_client.py b/client/weather_client.py
--- a/client/weather_client.py
+++ b/client/weather_client.py
@@ -14,9 +14,9 @@
 def get_current_temperature(lat, lon):
     data = _fetch(lat, lon)
-    return data["current_weather"]["temperature"]
+    return data["current"]["temperature_2m"]


 def get_current_windspeed(lat, lon):
     data = _fetch(lat, lon)
-    return data["current_weather"]["windspeed"]
+    return data["current"]["windspeed"]
'''

TEST_DIFF = '''diff --git a/tests/test_weather.py b/tests/test_weather.py
--- a/tests/test_weather.py
+++ b/tests/test_weather.py
@@ -1,1 +1,1 @@
-x = 1
+x = 2
'''

BAD_CONTEXT_DIFF = GOOD_DIFF.replace("     data = _fetch(lat, lon)\n-    return data[\"current_weather\"][\"temperature\"]",
                                     "     data = fetch_it(lat, lon)\n-    return data[\"current_weather\"][\"temperature\"]")


def demo_diff() -> SpecDiff:
    return SpecDiff("1.0.0", "2.0.0", [
        Change("C1", "structure_move", "$.current_weather", "$.current"),
        Change("C2", "field_rename", "$.current_weather.temperature", "$.current.temperature_2m", "number"),
        Change("C3", "structure_move", "$.current_weather_units", "$.current_units"),
        Change("C4", "field_rename", "$.current_weather_units.temperature", "$.current_units.temperature_2m", "string"),
    ])


def make_repo() -> Path:
    root = Path(tempfile.mkdtemp(prefix="pp-selftest-"))
    (root / "client").mkdir()
    (root / "client" / "weather_client.py").write_text(CLIENT_V1, encoding="utf-8")
    (root / "client" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_weather.py").write_text("x = 1\n", encoding="utf-8")
    return root


# --- fake anthropic SDK ---------------------------------------------------

def fake_sdk(response=None, raise_exc=None):
    m = types.ModuleType("anthropic")

    class AnthropicError(Exception):
        pass

    class APIError(AnthropicError):
        def __init__(self, message="", body=None):
            super().__init__(message)
            self.message = message
            self.body = body

    class APIStatusError(APIError):
        status_code = 0

        def __init__(self, message="", status_code=None, err_type=None):
            super().__init__(message, {"error": {"type": err_type}} if err_type else None)
            if status_code is not None:
                self.status_code = status_code

    class AuthenticationError(APIStatusError):
        status_code = 401

    class NotFoundError(APIStatusError):
        status_code = 404

    class BadRequestError(APIStatusError):
        status_code = 400

    class RateLimitError(APIStatusError):
        status_code = 429

    class InternalServerError(APIStatusError):
        status_code = 500

    class APIConnectionError(APIError):
        pass

    class APITimeoutError(APIConnectionError):
        pass

    for cls in (AnthropicError, APIError, APIStatusError, AuthenticationError, NotFoundError,
                BadRequestError, RateLimitError, InternalServerError, APIConnectionError,
                APITimeoutError):
        setattr(m, cls.__name__, cls)

    calls = []

    class Messages:
        def create(self, **kw):
            calls.append(kw)
            if raise_exc is not None:
                raise raise_exc(m)
            return response

    class Anthropic:
        def __init__(self, **kw):
            self.init_kw = kw
            self.messages = Messages()

    m.Anthropic = Anthropic
    m.calls = calls
    return m


def resp(text, stop="end_turn", model="claude-opus-5-5"):
    return types.SimpleNamespace(
        content=[types.SimpleNamespace(type="thinking", thinking="..."),
                 types.SimpleNamespace(type="text", text=text)],
        stop_reason=stop, model=model, stop_details=None,
        usage=types.SimpleNamespace(input_tokens=100, output_tokens=50))


class PromptTests(unittest.TestCase):
    def setUp(self):
        self.diff = demo_diff()
        self.usages = [Usage("client/weather_client.py", 16, 'return data["current_weather"]["temperature"]', ["C1", "C2"])]
        self.failing = TestRun(passed=1, failed=3, output="E KeyError: 'current_weather'\n")
        self.files = {"client/weather_client.py": CLIENT_V1}

    def test_first_attempt(self):
        system, user = prompt.build(self.diff, self.usages, self.failing, self.files, [])
        self.assertEqual(system, prompt.SYSTEM_PROMPT)
        self.assertTrue(user.endswith("Output the unified diff now."))
        self.assertIn('"from": "$.current_weather"', user)
        self.assertIn('client/weather_client.py:16: return data["current_weather"]["temperature"]', user)
        self.assertIn('<file path="client/weather_client.py">\nimport os', user)
        self.assertIn("Allowed files:\nclient/weather_client.py\n", user)
        self.assertNotIn("{{", user)

    def test_no_usages(self):
        _, user = prompt.build(self.diff, [], self.failing, self.files, [])
        self.assertIn("<affected_usages format=\"path:line: code\">\nnone found\n", user)

    def test_retry_blocks(self):
        prev = [
            Attempt(1, "medium", "rejected", "E_FORBIDDEN_PATH", "tests/test_weather.py", raw_output="RAW1 {{N}}"),
            Attempt(2, "medium", "rejected", "E_TESTS_FAILED", "2 failed, 2 passed", raw_output="RAW2",
                    test_log="NEWLOG"),
        ]
        _, user = prompt.build(self.diff, self.usages, self.failing, self.files, prev)
        self.assertTrue(user.endswith(prompt.FINAL_SENTENCE_RETRY))
        self.assertNotIn("Output the unified diff now.", user)
        self.assertIn('<previous_attempt number="1">', user)
        self.assertIn("RAW1 {{N}}", user)            # model text is not re-substituted
        self.assertIn("Your diff modified tests/test_weather.py, which is not allowed.", user)
        b1 = user.split('<previous_attempt number="1">')[1].split("</previous_attempt>")[0]
        b2 = user.split('<previous_attempt number="2">')[1].split("</previous_attempt>")[0]
        self.assertIn("<failing_test_log_after_attempt>\nnot run\n", b1)
        self.assertIn("<failing_test_log_after_attempt>\nNEWLOG\n", b2)
        self.assertLess(user.index("Allowed files:"), user.index("<previous_attempt"))

    def test_apply_failed_detail_first_5_lines(self):
        a = Attempt(1, "medium", "rejected", "E_APPLY_FAILED", "l1\nl2\nl3\nl4\nl5\nl6")
        d = prompt.rejection_detail(a)
        self.assertIn("git said: l1\nl2\nl3\nl4\nl5.", d)
        self.assertNotIn("l6", d)

    def test_truncate(self):
        log = "\n".join(f"line {i}" for i in range(500))
        t = prompt.truncate_log(log)
        self.assertTrue(t.startswith("[truncated: last 200 lines]\nline 300\n"))
        self.assertTrue(t.endswith("line 499"))
        big = "\n".join("x" * 100 for _ in range(150))
        t2 = prompt.truncate_log(big)
        self.assertTrue(t2.startswith("[truncated: last 8000 characters]\n"))
        self.assertLessEqual(len(t2.split("\n", 1)[1]), 8000)
        self.assertEqual(prompt.truncate_log("short"), "short")


class ClientTests(unittest.TestCase):
    def run_with(self, sdk, env=None, **kw):
        env = {"ANTHROPIC_API_KEY": "dummy-not-a-key"} if env is None else env
        with mock.patch.dict(sys.modules, {"anthropic": sdk}), mock.patch.dict(os.environ, env, clear=True):
            return llm_client.request_patch("SYS", "USER", kw.pop("effort", "medium"), **kw)

    def test_request_shape(self):
        sdk = fake_sdk(resp("diff --git a/x b/x"))
        raw, stop = self.run_with(sdk)
        self.assertEqual((raw, stop), ("diff --git a/x b/x", "end_turn"))
        call = sdk.calls[0]
        self.assertEqual(call, {"model": "claude-opus-5-5", "max_tokens": 16000,
                                "output_config": {"effort": "medium"}, "system": "SYS",
                                "messages": [{"role": "user", "content": "USER"}]})
        for banned in ("temperature", "top_p", "top_k", "thinking", "stream", "api_key"):
            self.assertNotIn(banned, call)
        self.assertEqual(llm_client.last_call["input_tokens"], 100)
        self.assertEqual(llm_client.last_call["stop_reason"], "end_turn")

    def test_client_construction_has_no_key_argument(self):
        sdk = fake_sdk(resp("ok"))
        with mock.patch.dict(sys.modules, {"anthropic": sdk}), \
                mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "dummy"}, clear=True):
            c = llm_client.make_client()
        self.assertEqual(c.init_kw, {"timeout": 90.0, "max_retries": 2})

    def test_model_env(self):
        self.assertEqual(llm_client.resolve_model({}), "claude-opus-5-5")
        self.assertEqual(llm_client.resolve_model({"PATCHPILOT_MODEL": "claude-sonnet-5-5"}), "claude-sonnet-5-5")
        with self.assertRaises(errors.PatchPilotError) as cm:
            llm_client.resolve_model({"PATCHPILOT_MODEL": "claude-other"})
        self.assertEqual((cm.exception.code, cm.exception.exit_code), ("S13", 2))
        sdk = fake_sdk(resp("x"))
        self.run_with(sdk, env={"ANTHROPIC_API_KEY": "d", "PATCHPILOT_MODEL": "claude-sonnet-5-5"},
                      effort="high")
        self.assertEqual(sdk.calls[0]["model"], "claude-sonnet-5-5")
        self.assertEqual(sdk.calls[0]["output_config"], {"effort": "high"})

    def test_missing_key(self):
        sdk = fake_sdk(resp("x"))
        with self.assertRaises(errors.PatchPilotError) as cm:
            self.run_with(sdk, env={})
        self.assertEqual((cm.exception.code, cm.exception.exit_code), ("S07", 2))
        self.assertEqual(sdk.calls, [])

    def test_sdk_missing(self):
        with mock.patch.dict(sys.modules, {"anthropic": None}), \
                mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "d"}, clear=True):
            with self.assertRaises(errors.PatchPilotError) as cm:
                llm_client.request_patch("S", "U", "medium")
        self.assertEqual(cm.exception.code, "S14")

    def test_error_mapping(self):
        cases = [
            (lambda m: m.AuthenticationError("bad key"), "S07", 2),
            (lambda m: m.NotFoundError("model not found\nmore", err_type="not_found_error"), "S08", 2),
            (lambda m: m.BadRequestError("bad request", err_type="invalid_request_error"), "S08", 2),
            (lambda m: m.RateLimitError("slow down", err_type="rate_limit_error"), "S12", 3),
            (lambda m: m.InternalServerError("overloaded", err_type="overloaded_error"), "S12", 3),
            (lambda m: m.APIConnectionError("conn"), "S12", 3),
            (lambda m: m.APITimeoutError("timeout"), "S12", 3),
        ]
        for make, code, exit_code in cases:
            with self.subTest(code=code, exc=make):
                with self.assertRaises(errors.PatchPilotError) as cm:
                    self.run_with(fake_sdk(raise_exc=make))
                self.assertEqual((cm.exception.code, cm.exception.exit_code), (code, exit_code))
        with self.assertRaises(errors.PatchPilotError) as cm:
            self.run_with(fake_sdk(raise_exc=lambda m: m.NotFoundError("model x\nline2", err_type="not_found_error")))
        self.assertEqual(cm.exception.message, "Claude request rejected (not_found_error): model x")

    def test_effort_schedule(self):
        self.assertEqual([llm_client.effort_for_attempt(n) for n in (1, 2, 3)], ["medium", "medium", "high"])


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.allowed = guard.allowed_files(self.repo)

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def check(self, raw, stop="end_turn"):
        return guard.check(raw, stop, self.allowed, self.repo)

    def test_allowed_files(self):
        self.assertEqual(self.allowed, {"client/weather_client.py", "client/__init__.py"})

    def test_stop_reasons_first(self):
        self.assertEqual(self.check(TEST_DIFF, "refusal")[0], "E_REFUSAL")
        self.assertEqual(self.check(GOOD_DIFF, "max_tokens")[0], "E_TRUNCATED")

    def test_no_patch(self):
        code, detail, _ = self.check("NO_PATCH: cannot do it safely.\n")
        self.assertEqual((code, detail), ("E_NO_PATCH", "cannot do it safely."))

    def test_no_diff(self):
        self.assertEqual(self.check("Here is the patch:\n" + GOOD_DIFF)[0], "E_NO_DIFF")
        self.assertEqual(self.check("```diff\n" + GOOD_DIFF + "```\nand more ```diff\nx\n```")[0], "E_NO_DIFF")

    def test_single_fence_removed(self):
        code, _, cleaned = self.check("```diff\n" + GOOD_DIFF + "```\n")
        self.assertIsNone(code)
        self.assertTrue(cleaned.startswith("diff --git a/client/weather_client.py"))

    def test_file_op(self):
        new_file = ("diff --git a/client/new.py b/client/new.py\nnew file mode 100644\n"
                    "--- /dev/null\n+++ b/client/new.py\n@@ -0,0 +1 @@\n+x = 1\n")
        code, detail, _ = self.check(new_file)
        self.assertEqual((code, detail), ("E_FILE_OP", "client/new.py"))

    def test_forbidden_path_before_apply(self):
        code, detail, _ = self.check(TEST_DIFF)
        self.assertEqual((code, detail), ("E_FORBIDDEN_PATH", "tests/test_weather.py"))
        cfg = TEST_DIFF.replace("tests/test_weather.py", "conftest.py")
        self.assertEqual(self.check(cfg)[0], "E_FORBIDDEN_PATH")
        dotdot = GOOD_DIFF.replace("a/client/weather_client.py b/client/weather_client.py",
                                   "a/client/../tests/x.py b/client/../tests/x.py")
        self.assertEqual(self.check(dotdot)[0], "E_FORBIDDEN_PATH")

    def test_apply_failed(self):
        code, detail, _ = self.check(BAD_CONTEXT_DIFF)
        self.assertEqual(code, "E_APPLY_FAILED")
        self.assertTrue(detail)
        self.assertLessEqual(len(detail.splitlines()), 5)

    def test_good_diff_applies_with_recount(self):
        wrong_counts = GOOD_DIFF.replace("@@ -14,9 +14,9 @@", "@@ -14,7 +14,7 @@")
        for d in (GOOD_DIFF, wrong_counts, GOOD_DIFF.replace("\n", "\r\n")):
            with self.subTest():
                work = Path(tempfile.mkdtemp()) / "repo"
                shutil.copytree(self.repo, work)
                code, detail, cleaned = guard.check(d, "end_turn", self.allowed, work)
                self.assertIsNone(code, detail)
                files, added, removed = guard.diff_stats(cleaned, work)
                self.assertEqual((files, added, removed), (["client/weather_client.py"], 2, 2))
                guard.apply(cleaned, work)
                text = (work / "client/weather_client.py").read_text()
                self.assertIn('data["current"]["temperature_2m"]', text)
                self.assertEqual((self.repo / "client/weather_client.py").read_text(), CLIENT_V1)
                shutil.rmtree(work.parent)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp()) / "out"

    def tearDown(self):
        shutil.rmtree(self.out.parent, ignore_errors=True)

    def sample(self, status):
        attempts = [Attempt(1, "medium", "rejected", "E_FORBIDDEN_PATH", "tests/test_weather.py", raw_output="RAW"),
                    Attempt(2, "medium", "diff_ok", files=["client/weather_client.py"], added=2, removed=2)]
        return report.build(
            status=status, out_dir="out", base_url="http://127.0.0.1:8787", proxy_stage="v2",
            diff=demo_diff(), usages=[Usage("client/weather_client.py", 16, "x", ["C1", "C2"])],
            reproduce=TestRun(1, 3, ["T1"], {"T1": "KeyError: 'current_weather'"}),
            tests_hash_before="sha256:abcdef0123456789", attempts=attempts,
            verify=TestRun(4, 0) if status == "patched" else None,
            tests_hash_after="sha256:abcdef0123456789" if status == "patched" else None,
            model="claude-opus-5-5"), attempts

    def test_patched(self):
        rep, attempts = self.sample("patched")
        self.assertEqual(rep["result"]["exit_code"], 0)
        self.assertEqual(rep["changes"], ["C1", "C2", "C3", "C4"])
        self.assertEqual(rep["attempts"][0]["raw_file"], "out/attempts/attempt_1.txt")
        self.assertTrue(rep["verify"]["unchanged"])
        paths = report.write(rep, GOOD_DIFF, self.out, attempts=attempts, diff=demo_diff())
        names = sorted(p.relative_to(self.out).as_posix() for p in paths)
        self.assertEqual(names, ["PR_BODY.md", "attempts/attempt_1.txt", "attempts/attempt_2.txt",
                                 "patchpilot.patch", "report.json"])
        body = (self.out / "PR_BODY.md").read_text()
        self.assertTrue(body.startswith("# fix(client): migrate to Weather API v2 (current_weather → current)\n\n"
                                        "Note: v2 is a staged spec served by a local proxy."))
        self.assertIn("| C2 | field_rename | `$.current_weather.temperature` | `$.current.temperature_2m` |", body)
        self.assertIn("- `client/weather_client.py:16` C1 C2", body)
        self.assertIn("abcdef012345 before, abcdef012345 after (unchanged)", body)
        self.assertIn("- model: claude-opus-5-5", body)
        self.assertTrue(body.rstrip().endswith("Generated by PatchPilot. Tests were not modified."))
        self.assertEqual(json.loads((self.out / "report.json").read_text())["schema_version"], 2)

    def test_failure_removes_stale_patch(self):
        self.out.mkdir(parents=True)
        (self.out / "patchpilot.patch").write_text("stale")
        (self.out / "PR_BODY.md").write_text("stale")
        rep, _ = self.sample("no_verified_patch")
        self.assertEqual(rep["result"]["exit_code"], 1)
        self.assertIsNone(rep["result"]["patch_file"])
        report.write(rep, GOOD_DIFF, self.out)
        self.assertFalse((self.out / "patchpilot.patch").exists())
        self.assertFalse((self.out / "PR_BODY.md").exists())
        self.assertTrue((self.out / "report.json").exists())

    def test_exit_codes(self):
        self.assertEqual({s: report.exit_code_for(s) for s in report.STATUS_EXIT},
                         {"patched": 0, "nothing_to_patch": 0, "no_verified_patch": 1,
                          "config_error": 2, "api_unavailable": 3})

    def test_runs_jsonl(self):
        self.assertEqual(report.next_run_number(self.out), 1)
        report.append_run(self.out, {"run": 1, "attempt": 1})
        report.append_run(self.out, {"run": 1, "attempt": 2})
        self.assertEqual(report.next_run_number(self.out), 2)


class CliTests(unittest.TestCase):
    """End-to-end loop with stubbed diff/usages/proxy/pytest and a fake SDK."""

    def setUp(self):
        self.repo = make_repo()
        self.cwd = os.getcwd()
        os.chdir(self.repo)
        diff = demo_diff()
        flatten = types.SimpleNamespace(
            load_spec=lambda p: {"info": {"version": "1.0.0" if "v1" in str(p) else "2.0.0"}, "components": {}},
            response_schema=lambda spec: {}, flatten=lambda schema, comps: {})
        compare = types.SimpleNamespace(compare=lambda a, b, v1, v2: diff)
        ast_scan = types.SimpleNamespace(scan=lambda d, df: [
            Usage("client/weather_client.py", 16, 'return data["current_weather"]["temperature"]', ["C1", "C2"])])
        pkg_diff = types.ModuleType("patchpilot.diff")
        pkg_diff.flatten, pkg_diff.compare = flatten, compare
        pkg_us = types.ModuleType("patchpilot.usages")
        pkg_us.ast_scan = ast_scan
        self.mods = mock.patch.dict(sys.modules, {"patchpilot.diff": pkg_diff, "patchpilot.usages": pkg_us})
        self.mods.start()
        from patchpilot import runner

        def fake_pytest(repo, timeout=60, python=None):
            text = (Path(repo) / "client/weather_client.py").read_text()
            if 'data["current"]["temperature_2m"]' in text:
                return TestRun(4, 0, output="4 passed")
            return TestRun(1, 3, ["T1", "T2", "T3"], {"T1": "KeyError: 'current_weather'"},
                           output="E   KeyError: 'current_weather'\n1 passed, 3 failed")

        self.patches = [
            mock.patch.object(runner, "check_proxy", lambda url, timeout=3.0: "v2"),
            mock.patch.object(runner, "run_pytest", fake_pytest),
            mock.patch.object(runner, "tests_hash", lambda root: "sha256:" + "a1b2c3d4e5f6" * 5),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.mods.stop()
        os.chdir(self.cwd)
        shutil.rmtree(self.repo, ignore_errors=True)

    def run_cli(self, responses, env=None, argv=("check",)):
        seq = list(responses)
        sdk = fake_sdk()
        calls = sdk.calls

        def create(**kw):
            calls.append(kw)
            item = seq.pop(0)
            if callable(item):
                raise item(sdk)
            return item

        sdk.Anthropic = lambda **kw: types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
        env = {"ANTHROPIC_API_KEY": "dummy", **(env or {})}
        from patchpilot import cli
        buf = io.StringIO()
        with mock.patch.dict(sys.modules, {"anthropic": sdk}), mock.patch.dict(os.environ, env, clear=True), \
                redirect_stdout(buf):
            code = cli.main(list(argv))
        out = self.repo / "out"
        rep = json.loads((out / "report.json").read_text())
        return code, buf.getvalue(), rep, calls, out

    def test_reject_then_success(self):
        code, screen, rep, calls, out = self.run_cli([resp(TEST_DIFF), resp(GOOD_DIFF)])
        self.assertEqual(code, 0, screen)
        self.assertIn("rejected (E_FORBIDDEN_PATH): diff touched tests/test_weather.py", screen)
        self.assertIn("diff OK (1 file, +2 -2)", screen)
        self.assertIn("tests hash a1b2c3d4e5f6 = a1b2c3d4e5f6", screen)
        self.assertEqual(rep["result"]["status"], "patched")
        self.assertEqual([a["result"] for a in rep["attempts"]], ["rejected", "diff_ok"])
        self.assertTrue((out / "patchpilot.patch").exists())
        self.assertTrue((out / "PR_BODY.md").exists())
        self.assertEqual(len(calls), 2)
        self.assertIn("<previous_attempt number=\"1\">", calls[1]["messages"][0]["content"])
        self.assertEqual((self.repo / "client/weather_client.py").read_text(), CLIENT_V1)  # original untouched
        runs = (out / "runs.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(r)["verdict"] for r in runs], ["E_FORBIDDEN_PATH", "diff_ok"])

    def test_three_failures(self):
        code, screen, rep, calls, out = self.run_cli([resp("prose"), resp(BAD_CONTEXT_DIFF), resp("NO_PATCH: no.")])
        self.assertEqual(code, 1, screen)
        self.assertEqual([c["output_config"]["effort"] for c in calls], ["medium", "medium", "high"])
        self.assertEqual([a["code"] for a in rep["attempts"]], ["E_NO_DIFF", "E_APPLY_FAILED", "E_NO_PATCH"])
        self.assertIn("FAIL  No verified patch after 3 attempts.", screen)
        self.assertFalse((out / "patchpilot.patch").exists())
        self.assertTrue((out / "attempts/attempt_3.txt").exists())

    def test_tests_failed_retry_gets_new_log(self):
        half = GOOD_DIFF.replace('+    return data["current"]["temperature_2m"]',
                                 '+    return data["current"]["temperature"]')
        code, screen, rep, calls, _ = self.run_cli([resp(half), resp(GOOD_DIFF)])
        self.assertEqual(code, 0, screen)
        self.assertEqual(rep["attempts"][0]["code"], "E_TESTS_FAILED")
        self.assertIn("<failing_test_log_after_attempt>\nE   KeyError", calls[1]["messages"][0]["content"])

    def test_refusal_stops(self):
        code, screen, rep, calls, _ = self.run_cli([resp("", stop="refusal"), resp(GOOD_DIFF)])
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(rep["attempts"][0]["code"], "E_REFUSAL")

    def test_api_unavailable(self):
        code, screen, rep, calls, _ = self.run_cli([lambda m: m.RateLimitError("x", err_type="rate_limit_error")])
        self.assertEqual(code, 3)
        self.assertEqual(rep["result"]["status"], "api_unavailable")
        self.assertEqual(rep["attempts"], [])
        self.assertIn("Claude API unavailable (rate_limit_error)", screen)

    def test_bad_model(self):
        code, screen, rep, calls, _ = self.run_cli([], env={"PATCHPILOT_MODEL": "gpt"})
        self.assertEqual((code, rep["result"]["error"]["code"]), (2, "S13"))
        self.assertEqual(calls, [])

    def test_key_value_never_written(self):
        secret = "sk-ant-" + "THIS-MUST-NOT-APPEAR"  # split so dryrun --submit key scan stays clean
        code, screen, rep, calls, out = self.run_cli([resp(GOOD_DIFF)], env={"ANTHROPIC_API_KEY": secret})
        self.assertEqual(code, 0)
        self.assertNotIn(secret, screen)
        for p in out.rglob("*"):
            if p.is_file():
                self.assertNotIn(secret, p.read_text(encoding="utf-8"), p)

    def test_no_llm(self):
        code, screen, rep, calls, out = self.run_cli([], argv=("check", "--no-llm"))
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])
        self.assertTrue((out / "spec_diff.json").exists())


if __name__ == "__main__":
    unittest.main()
