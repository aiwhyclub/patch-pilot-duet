# PatchPilot 백엔드 런북: 첫 실제 Claude 호출 (Drone 로컬 맥)

대상 저장소: 로컬 `/Users/drone/Project/patch-pilot-duet`, GitHub `aiwhyclub/patch-pilot-duet`. 이 저장소 밖에서는 아무 명령도 실행하지 않습니다.
범위: `patchpilot/llm/`, `guard.py`, `runner.py`, `report.py`, `cli.py`. 아래 명령은 모두 이 저장소 루트에서 실행합니다.
비밀값: 환경변수는 **이름만** 씁니다. 키 값은 화면·파일·채팅 어디에도 붙여 넣지 않습니다.

---

## 0. 먼저 이 폴더로 이동

```bash
cd /Users/drone/Project/patch-pilot-duet
pwd    # 기대: /Users/drone/Project/patch-pilot-duet
```

## 1. 설치 (처음 한 번)

```bash
python3 --version                      # 3.10 이상 (권장 3.12)
python3 -m venv .venv
source .venv/bin/activate
python -m pip install "anthropic==2.6.0" "pytest==9.2.0" "requests==2.34.2"
python -c "import anthropic; print('anthropic', anthropic.__version__)"   # 기대: anthropic 2.6.0
git --version                          # git apply를 씀. 없으면 패치 검증 불가
```

## 2. 환경변수 (값은 직접 입력, 화면에 안 보임)

```bash
read -rs ANTHROPIC_API_KEY && export ANTHROPIC_API_KEY
python -c "import os; print('ANTHROPIC_API_KEY', 'set' if os.environ.get('ANTHROPIC_API_KEY') else 'MISSING')"
export PATCHPILOT_MODEL=claude-opus-5-5          # 비교 때만 claude-sonnet-5-5
export WEATHER_BASE_URL=http://127.0.0.1:8787
```

- `ANTHROPIC_API_KEY`가 없으면 PatchPilot은 API를 부르기 전에 `S07`로 끝납니다.
- `ANTHROPIC_AUTH_TOKEN`도 설정돼 있다면 `unset ANTHROPIC_AUTH_TOKEN`. 두 값이 함께 있으면 SDK는 API 키를 씁니다.
- 선택: `export PATCHPILOT_TIMEOUT_S=180`. 요청 1회 타임아웃(기본 90초, 30–600으로 제한). SDK가 타임아웃을 2번 재시도하므로 최대 대기는 타임아웃 × 3입니다.

## 3. 네트워크 없는 자체 테스트 (비용 0)

```bash
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s selftests
```

기대: 마지막 줄 `OK`. SDK·pytest·프록시는 가짜로 바꿔서 돌리므로 API 호출이 없습니다.

## 4. 모델별 스모크 테스트 (실제 호출 1회씩, 비용 아주 적음)

PatchPilot의 실제 호출 코드(`patchpilot/llm/client.py`)를 그대로 씁니다.

```bash
python -c "from patchpilot.llm import client as c; raw, stop = c.request_patch('Reply with exactly: OK', 'Reply with exactly: OK', 'low', model='claude-opus-5-5'); print(c.last_call['model'], stop, repr(raw.strip()), c.last_call['input_tokens'], c.last_call['output_tokens'], c.last_call['latency_s'])"
```

```bash
python -c "from patchpilot.llm import client as c; raw, stop = c.request_patch('Reply with exactly: OK', 'Reply with exactly: OK', 'low', model='claude-sonnet-5-5'); print(c.last_call['model'], stop, repr(raw.strip()), c.last_call['input_tokens'], c.last_call['output_tokens'], c.last_call['latency_s'])"
```

성공 모양: `claude-opus-5-5 end_turn 'OK' <입력토큰> <출력토큰> <초>` (Sonnet도 같은 모양).
실패하면 오류 줄이 `S07`/`S08`/`S12`로 시작합니다. 아래 7절 표를 보세요.

## 5. 프록시 띄우기 (다른 터미널, 같은 폴더)

```bash
cd /Users/drone/Project/patch-pilot-duet && source .venv/bin/activate
python proxy/server.py --stage v2              # 행사장 네트워크가 불안하면 --stage v2 --offline
```

확인 (원래 터미널):

```bash
curl -sS "$WEATHER_BASE_URL/health"            # 기대: {"stage": "v2", ...}
```

## 6. 전체 실행

콘솔 스크립트(`patchpilot`)는 pyproject.toml에 등록된 뒤에 쓸 수 있습니다. 그 전에는 모듈로 실행합니다.

```bash
python -m patchpilot.cli check; echo "exit=$?"
```

성공 모양 (숫자·해시는 실행마다 다름):

```text
PatchPilot check — v2 is a staged spec for this demo   proxy stage: v2
model=claude-opus-5-5 effort=medium/medium/high
[1/5] Diff   specs/v1.json (1.0.0) → specs/v2.json (2.0.0)
  C1 … C4
[2/5] Affected usages   (ast)
[3/5] Reproduce   … pytest 1 passed, 3 failed   KeyError: 'current_weather'
[4/5] Patch   attempt 1/3  ... diff OK (1 file, +2 -2)
[5/5] Verify  apply OK | pytest 4 passed | tests hash X = X
DONE  out/patchpilot.patch, out/PR_BODY.md
exit=0
```

산출물: `out/spec_diff.json`, `out/report.json`, `out/attempts/attempt_N.txt`, `out/runs.jsonl`, 그리고 성공 시에만 `out/patchpilot.patch`, `out/PR_BODY.md`.
원본 `client/`는 바뀌지 않습니다. 패치는 임시 복사본에서만 적용·검증합니다.

리허설(Claude 호출 없이 1–3단계만): `python -m patchpilot.cli check --no-llm` (테스트가 실패 중이면 종료 1).

## 7. 종료 코드와 오류 코드

| 종료 | 의미 | 흔한 원인 → 할 일 |
|---|---|---|
| 0 | 검증된 패치 생성, 또는 패치할 것 없음 (`S09`, `S10`) | `report.json`의 `result.status`가 `patched`인지 확인. `nothing_to_patch`이면 프록시가 `--stage v1`인지 확인 |
| 1 | 시도 상한까지 검증된 패치 없음, 또는 `E_REFUSAL` | `out/attempts/attempt_N.txt`의 거부 코드·원문 확인 |
| 2 | 설정 오류 | 아래 S 코드 |
| 3 | Claude API 일시 장애 (`S12`) | 잠시 뒤 재실행. 계속되면 노트북 네트워크와 Anthropic 상태 페이지 확인 (프록시 `--offline`과는 무관) |

| 코드 | 화면 문구 시작 | 할 일 |
|---|---|---|
| S01 / S02 / S03 | `spec file not found` / `spec is not valid JSON` / `no 200 response schema` | `specs/` 파일 확인 |
| S04 / S05 | `no Python files in` / `tests dir not found` | `client/`, `tests/` 위치 확인 |
| S06 | `cannot reach …` | 5절 프록시 실행 |
| S07 | `ANTHROPIC_API_KEY is missing or invalid.` | 2절 다시. 키 값은 출력하지 않음 |
| S08 | `Claude request rejected (<type>): …` | 400/402/403/404/413. `not_found_error`면 모델 ID·조직 권한, `permission_error`면 Console 권한 |
| S11 | `tests timed out after 60s` | 재현 단계 pytest가 60초 초과. 프록시 응답 확인 |
| S12 | `Claude API unavailable (<type>)` | 429/408/409/5xx/529/연결/타임아웃이 SDK 재시도 2회 뒤에도 실패. 종료 3 |
| S13 | `PATCHPILOT_MODEL must be one of …` | `claude-opus-5-5` 또는 `claude-sonnet-5-5`만 |
| S14 | `anthropic SDK is not installed` | 1절 설치 |
| S15 | `test setup error (...)` (pytest 없음, 수집 오류, 0개 수집) | `python -m pip install -e ".[dev]"` 후 다시 실행. Claude는 호출되지 않음 |

거부 코드 순서: `E_REFUSAL` → `E_TRUNCATED` → `E_NO_PATCH` → `E_NO_DIFF` → `E_FILE_OP` → `E_FORBIDDEN_PATH` → `E_APPLY_FAILED` → `E_TESTS_FAILED` → `E_TEST_HASH_CHANGED`. `E_REFUSAL`만 재시도하지 않고 종료 1.

## 8. 13:20 모델 비교 (모델당 1회, Opus → Sonnet 순서, 같은 조건)

프록시는 두 실행 모두 같은 fixture로: 다른 터미널에서 `python proxy/server.py --stage v2 --offline`.

```bash
cd /Users/drone/Project/patch-pilot-duet
for m in claude-opus-5-5 claude-sonnet-5-5; do
  echo "=== $m $(date +%H:%M:%S)"
  start=$(date +%s)
  PATCHPILOT_MODEL=$m python -m patchpilot.cli check --out "out/compare-$m"
  echo "exit=$? total_s=$(( $(date +%s) - start ))"
done
```

표 한 줄로 요약 (5.6 기록표 칸 순서, 비용은 5.4 가격 기준 추정):

```bash
python - <<'EOF'
import json, pathlib
price = {"claude-opus-5-5": (4, 20), "claude-sonnet-5-5": (2, 10)}
for m in price:
    out = pathlib.Path(f"out/compare-{m}")
    rep = json.loads((out / "report.json").read_text())
    rows = [json.loads(l) for l in (out / "runs.jsonl").read_text().splitlines()]
    last = max(r["run"] for r in rows)
    rows = [r for r in rows if r["run"] == last]
    tin = sum(r.get("input_tokens") or 0 for r in rows)
    tout = sum(r.get("output_tokens") or 0 for r in rows)
    wait = sum(r.get("latency_s") or 0 for r in rows)
    pi, po = price[m]
    v = rep.get("verify") or {}
    print(f"| {m} | {rep['result']['status']} (exit {rep['result']['exit_code']}) | attempts {len(rows)} | "
          f"{', '.join(r['verdict'] for r in rows)} | claude_wait_s {wait:.1f} | in {tin} | out {tout} | "
          f"${(tin*pi + tout*po)/1e6:.3f} | tests {rows[-1]['tests']} | hash_ok {v.get('unchanged')} |")
EOF
```

결정 규칙은 backend-ai.md 5.7을 그대로 씁니다. 총 소요 시간은 위 루프의 `total_s`. 모델당 1회 결과이므로 발표 자료에는 "1회 실행 결과"라고 표시합니다.

## 9. 알아 둘 점

- 요청 설정: `max_tokens=16000`, `output_config={"effort": "medium"}` (3회차 `"high"`), `temperature`·`thinking` 보내지 않음, 비스트리밍, `max_retries=2`.
- 응답 텍스트는 `type == "text"` 블록만 이어 붙입니다. `thinking` 블록(기본 표시 방식에서 빈 텍스트)은 무시합니다.
- 서버측 refusal fallback(`fallbacks`)은 설계 문서 5.3 결정대로 쓰지 않습니다. 거절되면 `E_REFUSAL`, 종료 1.
- 타임아웃이 나면 SDK가 같은 요청을 다시 보내므로 비용이 다시 들 수 있습니다.
- push와 PR 생성은 `--pr`을 붙였을 때만 합니다(10절). `--pr` 없이 돌리면 git은 건드리지 않습니다.

## 10. PR 만들기 (`--pr`)

검증된 패치가 나왔을 때만 동작합니다(종료 코드 0, status `patched`). main의 client 코드는 v1 그대로 남으므로 데모를 반복할 수 있습니다.

### 10.1 미리 확인 (처음 한 번)

```bash
cd /Users/drone/Project/patch-pilot-duet
git remote get-url origin          # 기대: https://github.com/aiwhyclub/patch-pilot-duet(.git) 또는 git@github.com:aiwhyclub/patch-pilot-duet(.git)
git status --short --untracked-files=no   # 기대: 아무것도 안 나옴 (out/ 변경은 무시)
gh auth status                     # 기대: Logged in to github.com
gh auth setup-git                  # https push가 비밀번호를 묻지 않게 함 (한 번만)
git config user.name && git config user.email   # 커밋 작성자. 비어 있으면 먼저 설정
```

### 10.2 실행

```bash
export PATCHPILOT_MODEL=claude-sonnet-5-5    # 영상 촬영용. 코드 기본값은 Opus 5.5 (바꾸지 않음)
python proxy/server.py --stage v2 &          # 이미 떠 있으면 생략
python -m patchpilot.cli check --pr
git branch --show-current                    # 기대: main
```

화면 끝 기대 모양:

```
[5/5] Verify  apply OK | pytest <N> passed | tests hash <해시> = <해시>
DONE  out/patchpilot.patch, out/PR_BODY.md
PR  https://github.com/aiwhyclub/patch-pilot-duet/pull/<번호>
```

### 10.3 하는 일 (순서)

1. Claude를 부르기 전에 확인: origin(fetch·push URL 둘 다)이 정확히 `aiwhyclub/patch-pilot-duet`인가, 추적 중인 파일에 커밋 안 된 변경이 없는가(`out/`과 추적 안 되는 파일은 무시). 아니면 아무것도 바꾸지 않고 종료 2.
2. 패치 검증 후 같은 확인을 한 번 더 함.
3. 새 브랜치 `patchpilot/openmeteo-v2-<YYYYmmdd-HHMMSS>` 생성 → `git apply --recount --index out/patchpilot.patch` → `client/` 파일만 커밋(그 밖의 파일이 섞이면 중단).
4. `git push -u origin <브랜치>` → `gh pr create --repo aiwhyclub/patch-pilot-duet --base main --body-file out/PR_BODY.md`.
5. 성공·실패와 상관없이 원래 브랜치(main)로 돌아감. main에는 커밋하지 않음.
6. 결과는 `out/report.json`의 `result.pr`(status·branch·commit·pushed·url)와 `result.pr_url`에 기록.

### 10.4 종료 코드와 화면

| 상황 | 화면 | 종료 |
|---|---|---|
| PR 생성 성공 | `PR  <url>` | 0 |
| gh 없음 또는 로그인 안 됨 | `PR skipped: gh not authenticated` (브랜치는 push됨) | 0 |
| origin이 다른 저장소 | `FAIL  --pr: origin is not aiwhyclub/patch-pilot-duet ...` (S16) | 2, 아무것도 안 바뀜 |
| 커밋 안 된 변경 있음 | `FAIL  --pr: working tree has uncommitted changes ...` (S17) | 2, 아무것도 안 바뀜 |
| 저장소 루트가 아님 / git 없음 | `FAIL  --pr: not usable as a git repository ...` (S18) | 2 |
| apply·commit·push·gh 실패 | `FAIL  --pr: <단계> failed: ... Patch kept in out/patchpilot.patch; back on main.` (S19) | 2, 패치 파일 유지 |
| 검증된 패치 없음 | `--pr: no verified patch, so no branch or PR was created.` | 1 |

- push 실패 시 로컬 브랜치는 남겨 둡니다. 화면에 나온 `git push -u origin <브랜치>`로 다시 올릴 수 있습니다.
- 데모를 다시 하려면 GitHub에서 PR을 닫고(또는 그대로 두고) 다시 실행하면 됩니다. 브랜치 이름에 초 단위 시간이 들어가 겹치지 않습니다.

