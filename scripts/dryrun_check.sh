#!/usr/bin/env bash
# PatchPilot dry-run check (13:00 / 14:00) and pre-submit check.
#
# Repository: patch-pilot-duet only (local folder patch-pilot-duet,
# origin github.com/aiwhyclub/patch-pilot-duet). The script stops anywhere else.
#
# Usage (from the patch-pilot-duet root, with the proxy already running):
#   bash scripts/dryrun_check.sh             # environment + locked tests only
#   bash scripts/dryrun_check.sh --run       # also runs `patchpilot check` and checks out/
#   bash scripts/dryrun_check.sh --submit    # pre-submit checks (git, secrets, first commit time)
#
# Never prints secret values. ANTHROPIC_API_KEY is reported only as set / MISSING.

set -u
MODE="${1:-}"
BASE_URL="${WEATHER_BASE_URL:-http://127.0.0.1:8787}"
CONTEST_START="2026-11-09 10:00:00 +0900"
# Python: $PYTHON if set, else the active venv's python, else python3, else python.
if [ -n "${PYTHON:-}" ]; then
  PY="$PYTHON"
elif [ -n "${VIRTUAL_ENV:-}" ] && [ -x "$VIRTUAL_ENV/bin/python" ]; then
  PY="$VIRTUAL_ENV/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
elif command -v python >/dev/null 2>&1; then
  PY="python"
else
  echo "STOP  no python3 or python found"; exit 2
fi
FAILS=0
WARNS=0

pass() { printf '  PASS  %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; FAILS=$((FAILS + 1)); }
warn() { printf '  WARN  %s\n' "$1"; WARNS=$((WARNS + 1)); }

tests_hash() {
  # Same rule as frd-trd.md §7.2: tests/ + root conftest.py, pytest.ini, pyproject.toml,
  # sorted relative paths, path + NUL + content + NUL (same as the CLI), one SHA-256.
  # Caches are excluded.
  "$PY" - <<'EOF'
import hashlib, pathlib
root = pathlib.Path(".")
files = [p for p in (root / "tests").rglob("*") if p.is_file()]
files += [root / n for n in ("conftest.py", "pytest.ini", "pyproject.toml") if (root / n).is_file()]
skip = ("__pycache__", ".pytest_cache")
files = [p for p in files if not any(s in p.parts for s in skip) and p.suffix != ".pyc"]
h = hashlib.sha256()
for p in sorted(files, key=lambda p: p.as_posix()):
    h.update(p.as_posix().encode() + b"\0" + p.read_bytes() + b"\0")
print(h.hexdigest()[:12])
EOF
}

run_tests() {
  WEATHER_BASE_URL="$BASE_URL" PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 \
    "$PY" -m pytest -q -p no:cacheprovider tests 2>&1 | tail -1
}

# Repository guard: run only inside the patch-pilot-duet checkout, never any other repo.
ROOT_NAME="$(basename "$(pwd -P)")"
if [ "$ROOT_NAME" != "patch-pilot-duet" ]; then
  echo "STOP  run this from the patch-pilot-duet repository root (current folder: $ROOT_NAME)"
  exit 2
fi
ORIGIN="$(git remote get-url origin 2>/dev/null || true)"
case "$ORIGIN" in
  ""|https://github.com/aiwhyclub/patch-pilot-duet|https://github.com/aiwhyclub/patch-pilot-duet.git|git@github.com:aiwhyclub/patch-pilot-duet.git) ;;
  *) echo "STOP  origin is not aiwhyclub/patch-pilot-duet: $ORIGIN"; exit 2 ;;
esac

echo "== 1. Environment"
if "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  pass "python $("$PY" -c 'import platform; print(platform.python_version())')"
else
  fail "python 3.10+ required"
fi
for spec in anthropic==2.6.0 pytest==9.2.0 requests==2.34.2; do
  name="${spec%%==*}"; want="${spec##*==}"
  got="$("$PY" -c "import $name; print($name.__version__)" 2>/dev/null || echo none)"
  if [ "$got" = "$want" ]; then pass "$name $got"; else fail "$name want $want, got $got"; fi
done
if command -v git >/dev/null 2>&1; then pass "$(git --version)"; else fail "git not found"; fi
if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  pass "gh logged in (stretch)"
else
  warn "gh not available or not logged in (stretch only)"
fi
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then pass "ANTHROPIC_API_KEY set"; else fail "ANTHROPIC_API_KEY MISSING"; fi

echo "== 2. Proxy"
STAGE="$("$PY" - "$BASE_URL" <<'EOF'
import sys, urllib.request
url = sys.argv[1].rstrip("/") + "/forecast?latitude=37.57&longitude=126.98&current_weather=true"
try:
    with urllib.request.urlopen(url, timeout=5) as r:
        print(r.headers.get("X-PatchPilot-Stage") or "no-header")
except Exception as e:
    print("unreachable")
EOF
)"
case "$STAGE" in
  v1|v2) pass "proxy at $BASE_URL, stage $STAGE" ;;
  *) fail "proxy at $BASE_URL: $STAGE (start: python proxy/server.py --stage v2 [--offline])" ;;
esac

echo "== 3. Locked tests"
HASH_BEFORE="$(tests_hash)"
pass "tests hash $HASH_BEFORE"
if [ "$STAGE" = "v1" ] || [ "$STAGE" = "v2" ]; then
  RESULT="$(run_tests)"
  if [ "$STAGE" = "v1" ]; then WANT="4 passed"; else WANT="3 failed, 1 passed"; fi
  case "$RESULT" in
    *"$WANT"*) pass "stage $STAGE: $RESULT" ;;
    *) fail "stage $STAGE: want '$WANT', got '$RESULT' (if the client is already patched, v2 gives 4 passed)" ;;
  esac
fi
HASH_AFTER="$(tests_hash)"
if [ "$HASH_BEFORE" = "$HASH_AFTER" ]; then pass "tests hash unchanged after run"; else fail "tests hash changed: $HASH_BEFORE -> $HASH_AFTER"; fi

if [ "$MODE" = "--run" ]; then
  echo "== 4. patchpilot check"
  if ! command -v patchpilot >/dev/null 2>&1; then
    fail "patchpilot command not found (pip install -e .)"
  else
    START=$(date +%s)
    patchpilot check; CODE=$?
    echo "  exit code $CODE, $(( $(date +%s) - START ))s"
    if [ "$CODE" -eq 0 ]; then pass "exit 0"; else fail "exit $CODE (1 no verified patch, 2 config, 3 API down)"; fi
    for f in out/spec_diff.json out/report.json out/patchpilot.patch out/PR_BODY.md; do
      if [ -s "$f" ]; then pass "$f"; else fail "$f missing"; fi
    done
    if [ -s out/PR_BODY.md ] && grep -q "Tests were not modified." out/PR_BODY.md; then
      pass "PR body has 'Tests were not modified.'"
    else
      fail "PR body lacks 'Tests were not modified.'"
    fi
    if [ "$(tests_hash)" = "$HASH_BEFORE" ]; then pass "tests hash unchanged after patchpilot"; else fail "tests hash changed after patchpilot"; fi
    if [ -n "$(git status --porcelain -- client tests 2>/dev/null)" ]; then
      fail "client/ or tests/ changed in the working tree (patch must go to out/ only)"
    else
      pass "client/ and tests/ untouched"
    fi
  fi
fi

if [ "$MODE" = "--submit" ]; then
  echo "== 4. Pre-submit"
  FIRST="$(git log --reverse --format='%ci' 2>/dev/null | head -1)"
  if [ -z "$FIRST" ]; then
    fail "no commits yet"
  elif [ "$("$PY" -c "from datetime import datetime as d; f='%Y-%m-%d %H:%M:%S %z'; print(int(d.strptime('$FIRST', f) >= d.strptime('$CONTEST_START', f)))")" = "1" ]; then
    pass "first commit $FIRST (after 10:00 KST)"
  else
    fail "first commit $FIRST is before 10:00 KST"
  fi
  if [ -n "$(git status --porcelain 2>/dev/null)" ]; then warn "uncommitted changes"; else pass "working tree clean"; fi
  if git rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
    if [ "$(git rev-parse HEAD)" = "$(git rev-parse '@{u}')" ]; then pass "HEAD pushed to upstream"; else fail "HEAD not pushed"; fi
  else
    warn "no upstream branch set"
  fi
  if git ls-files | grep -qE '(^|/)\.env$|(^|/)out/'; then fail ".env or out/ is tracked"; else pass ".env and out/ not tracked"; fi
  if git grep -qE 'sk-ant-[A-Za-z0-9_-]{8,}' -- . 2>/dev/null; then fail "key-like string in tracked files"; else pass "no key-like string in tracked files"; fi
  if git log -p 2>/dev/null | grep -qE 'sk-ant-[A-Za-z0-9_-]{8,}'; then fail "key-like string in commit history"; else pass "no key-like string in commit history"; fi
  echo "  Manual: open the GitHub URL and the video link in a logged-out window; video <= 90s, English."
fi

echo "== Summary: $FAILS failed, $WARNS warnings"
[ "$FAILS" -eq 0 ]
