"""Claude API call (backend-ai.md section 5.2, 4.4).

request_patch(system, user, effort) -> (raw_text, stop_reason)

- Model: env PATCHPILOT_MODEL, default claude-opus-5-5, alternative
  claude-sonnet-5-5. Anything else -> S13 (exit 2) before any API call.
- Key: the SDK reads ANTHROPIC_API_KEY from the environment by itself.
  This module only checks that the variable is set; it never reads,
  prints, logs, or stores the value.
- Settings: max_tokens=16000, output_config={"effort": ...}, no
  temperature / top_p / top_k / thinking, single user turn, no streaming,
  timeout 90 s, max_retries 2 (SDK backoff for 429 / 5xx / connection).
- Errors: 401 -> S07, 400 / 404 / other 4xx -> S08, 429 / 5xx /
  connection / timeout after SDK retries -> S12.
"""
from __future__ import annotations

import os
import time
from typing import Any, Optional

from patchpilot.errors import PatchPilotError, fail

DEFAULT_MODEL = "claude-opus-5-5"
ALTERNATIVE_MODEL = "claude-sonnet-5-5"
ALLOWED_MODELS = (DEFAULT_MODEL, ALTERNATIVE_MODEL)
MODEL_ENV = "PATCHPILOT_MODEL"
KEY_ENV = "ANTHROPIC_API_KEY"

MAX_TOKENS = 16000
TIMEOUT_S = 90.0            # backend-ai.md 5.2 default
TIMEOUT_ENV = "PATCHPILOT_TIMEOUT_S"   # optional override, 30..600 seconds
MAX_RETRIES = 2
RETRYABLE_4XX = (408, 409)
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

# Metadata of the most recent call, for out/runs.jsonl and the E_REFUSAL
# screen line. Never contains the key or request text.
last_call: dict[str, Any] = {}


def resolve_model(env: Optional[dict] = None) -> str:
    """Return the model ID from PATCHPILOT_MODEL, or raise S13."""
    env = os.environ if env is None else env
    value = env.get(MODEL_ENV) or DEFAULT_MODEL
    if value not in ALLOWED_MODELS:
        raise fail("S13", value=value)
    return value


def effort_for_attempt(n: int) -> str:
    """backend-ai.md 3: attempts 1-2 medium, attempt 3 high."""
    return "high" if n >= 3 else "medium"


def timeout_s(env: Optional[dict] = None) -> float:
    """Per-request timeout. PATCHPILOT_TIMEOUT_S overrides 90 s (clamped to 30..600).
    The SDK retries a timeout, so worst-case wait is timeout x (MAX_RETRIES + 1)."""
    env = os.environ if env is None else env
    raw = env.get(TIMEOUT_ENV)
    if not raw:
        return TIMEOUT_S
    try:
        return min(600.0, max(30.0, float(raw)))
    except ValueError:
        return TIMEOUT_S


def _import_sdk():
    try:
        import anthropic  # noqa: WPS433 (lazy: other modules work without the SDK)
    except ImportError:
        raise fail("S14") from None
    return anthropic


def make_client():
    """SDK client. The key comes from ANTHROPIC_API_KEY via the SDK default."""
    if not os.environ.get(KEY_ENV):
        raise fail("S07")
    anthropic = _import_sdk()
    try:
        return anthropic.Anthropic(timeout=timeout_s(), max_retries=MAX_RETRIES)
    except anthropic.AnthropicError:
        raise fail("S07") from None


def _error_type(exc: BaseException) -> str:
    t = getattr(exc, "type", None)
    if isinstance(t, str) and t:
        return t
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and isinstance(err.get("type"), str):
            return err["type"]
    return type(exc).__name__


def _first_line(exc: BaseException) -> str:
    msg = getattr(exc, "message", None) or str(exc) or type(exc).__name__
    return str(msg).strip().splitlines()[0] if str(msg).strip() else type(exc).__name__


def _map_sdk_error(anthropic, exc: BaseException) -> PatchPilotError:
    """Map SDK exceptions, most specific class first (backend-ai.md 4.4)."""
    if isinstance(exc, anthropic.AuthenticationError):
        return fail("S07")
    if isinstance(exc, anthropic.NotFoundError):
        return fail("S08", error_type=_error_type(exc), first_line=_first_line(exc))
    if isinstance(exc, anthropic.RateLimitError):
        return fail("S12", error_type=_error_type(exc))
    if isinstance(exc, anthropic.APIStatusError):
        status = getattr(exc, "status_code", 0) or 0
        # 408 / 409 / 5xx (incl. 529 overloaded) are the codes the SDK itself
        # retries; still failing after retries means "temporarily unavailable".
        if status >= 500 or status in RETRYABLE_4XX:
            return fail("S12", error_type=_error_type(exc))
        return fail("S08", error_type=_error_type(exc), first_line=_first_line(exc))
    if isinstance(exc, anthropic.APIConnectionError):   # APITimeoutError is a subclass
        return fail("S12", error_type=_error_type(exc))
    return fail("S12", error_type=_error_type(exc))


def _stop_category(resp) -> Optional[str]:
    details = getattr(resp, "stop_details", None)
    if details is None:
        return None
    if isinstance(details, dict):
        return details.get("category")
    return getattr(details, "category", None)


def request_patch(system: str, user: str, effort: str, *,
                  model: Optional[str] = None, client=None) -> tuple[str, str]:
    """One single-turn, non-streaming request. Returns (raw_text, stop_reason).

    model: defaults to resolve_model(). client: inject for tests; default
    is make_client(). Raises PatchPilotError (S07/S08/S12/S13/S14).
    """
    if effort not in EFFORT_LEVELS:
        raise ValueError(f"unknown effort: {effort!r}")
    model = model or resolve_model()
    if model not in ALLOWED_MODELS:
        raise fail("S13", value=model)
    anthropic = _import_sdk()
    client = client or make_client()

    last_call.clear()
    started = time.monotonic()
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            output_config={"effort": effort},
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.AnthropicError as exc:
        last_call.update({"model": model, "effort": effort,
                          "latency_s": round(time.monotonic() - started, 3),
                          "error_type": _error_type(exc)})
        raise _map_sdk_error(anthropic, exc) from None

    # Only `text` blocks carry the answer; `thinking` blocks (empty text by
    # default on these models) and any other block type are skipped.
    raw = "".join((getattr(b, "text", None) or "") for b in (getattr(resp, "content", None) or [])
                  if getattr(b, "type", None) == "text")
    stop_reason = getattr(resp, "stop_reason", None) or ""
    usage = getattr(resp, "usage", None)
    last_call.update({
        "model": getattr(resp, "model", None) or model,
        "effort": effort,
        "stop_reason": stop_reason,
        "stop_category": _stop_category(resp),
        "latency_s": round(time.monotonic() - started, 3),
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
    })
    return raw, stop_reason
