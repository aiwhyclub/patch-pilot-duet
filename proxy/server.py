"""PatchPilot weather proxy.

Serves Open-Meteo forecast responses at GET /forecast and, with --stage v2,
rewrites them into the staged v2 shape (backend-ai.md section 6, frd-trd.md
section 3):

    $.current_weather                     -> $.current
    $.current_weather.temperature         -> $.current.temperature_2m
    $.current_weather_units               -> $.current_units
    $.current_weather_units.temperature   -> $.current_units.temperature_2m

Every other key and every value stays as it is. Request URL and query
parameters are the same in v1 and v2. Standard library only.

Usage:
    python proxy/server.py --stage v1
    python proxy/server.py --stage v2 --offline
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURE = REPO_ROOT / "fixtures" / "open_meteo_v1.json"
DEFAULT_UPSTREAM = "https://api.open-meteo.com/v1/forecast"
DEFAULT_PORT = 8787
UPSTREAM_TIMEOUT_S = 10
STAGE_HEADER = "X-PatchPilot-Stage"
SOURCE_HEADER = "X-PatchPilot-Source"
FIXTURE_KEY_PREFIX = "_patchpilot"


def _rename_key(obj: dict, old: str, new: str) -> dict:
    """Return a copy of obj with key old renamed to new, keeping key order."""
    return {(new if key == old else key): value for key, value in obj.items()}


def to_v2(body: dict) -> dict:
    """Rewrite a v1 (current_weather) response into the staged v2 shape.

    Responses without current_weather (for example Open-Meteo error bodies)
    are returned unchanged.
    """
    if not isinstance(body, dict) or "current_weather" not in body:
        return body
    out = dict(body)
    if isinstance(out["current_weather"], dict):
        out["current_weather"] = _rename_key(out["current_weather"], "temperature", "temperature_2m")
    out = _rename_key(out, "current_weather", "current")
    if isinstance(out.get("current_weather_units"), dict):
        out["current_weather_units"] = _rename_key(
            out["current_weather_units"], "temperature", "temperature_2m"
        )
    out = _rename_key(out, "current_weather_units", "current_units")
    return out


def strip_fixture_notes(body: dict) -> dict:
    """Drop top-level keys such as _patchpilot_fixture that only document the fixture."""
    if not isinstance(body, dict):
        return body
    return {k: v for k, v in body.items() if not k.startswith(FIXTURE_KEY_PREFIX)}


def load_fixture(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return strip_fixture_notes(json.load(f))


class ProxyHandler(BaseHTTPRequestHandler):
    server_version = "PatchPilotProxy/1.0"
    # Filled in by make_server().
    stage = "v1"
    offline = False
    fixture_path = DEFAULT_FIXTURE
    upstream = DEFAULT_UPSTREAM

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/forecast":
            self._forecast(parsed.query)
        elif parsed.path == "/health":
            self._send_json(200, {"stage": self.stage, "offline": self.offline}, source="proxy")
        else:
            self._send_json(404, {"error": True, "reason": f"unknown path {parsed.path}; use GET /forecast"}, source="proxy")

    def _forecast(self, query: str) -> None:
        if self.offline:
            try:
                body = load_fixture(self.fixture_path)
            except (OSError, json.JSONDecodeError) as exc:
                self._send_json(500, {"error": True, "reason": f"cannot read fixture {self.fixture_path}: {exc}"}, source="proxy")
                return
            self._send_json(200, self._stage_body(body), source="fixture")
            return

        url = self.upstream + ("?" + query if query else "")
        request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "PatchPilotProxy/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=UPSTREAM_TIMEOUT_S) as resp:
                raw = resp.read()
                status = resp.status
                content_type = resp.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as exc:
            # Upstream error: pass it through unchanged, in both stages.
            self._send_raw(exc.code, exc.read(), exc.headers.get("Content-Type", "application/json"), source="upstream")
            return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            self._send_json(502, {"error": True, "reason": f"upstream unreachable: {reason}. Try --offline."}, source="proxy")
            return

        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            # Not JSON: pass through unchanged.
            self._send_raw(status, raw, content_type, source="upstream")
            return
        self._send_json(status, self._stage_body(body), source="upstream")

    def _stage_body(self, body: dict) -> dict:
        return to_v2(body) if self.stage == "v2" else body

    def _send_json(self, status: int, body: dict, source: str) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self._send_raw(status, data, "application/json; charset=utf-8", source=source)

    def _send_raw(self, status: int, data: bytes, content_type: str, source: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header(STAGE_HEADER, self.stage)
        self.send_header(SOURCE_HEADER, source)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(f"[proxy {self.stage}] {self.address_string()} {fmt % args}\n")


def make_server(stage: str, offline: bool, host: str, port: int,
                fixture: Path = DEFAULT_FIXTURE, upstream: str = DEFAULT_UPSTREAM) -> ThreadingHTTPServer:
    handler = type("ConfiguredProxyHandler", (ProxyHandler,), {
        "stage": stage,
        "offline": offline,
        "fixture_path": fixture,
        "upstream": upstream,
    })
    return ThreadingHTTPServer((host, port), handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PatchPilot weather proxy (staged v1/v2).")
    parser.add_argument("--stage", choices=["v1", "v2"], required=True, help="response shape to serve")
    parser.add_argument("--offline", action="store_true", help=f"serve {DEFAULT_FIXTURE.relative_to(REPO_ROOT)} instead of calling Open-Meteo")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE, help="fixture file for --offline")
    parser.add_argument("--upstream", default=DEFAULT_UPSTREAM, help="upstream forecast URL (for testing)")
    args = parser.parse_args(argv)

    if args.offline and not args.fixture.is_file():
        print(f"fixture not found: {args.fixture}", file=sys.stderr)
        return 2

    server = make_server(args.stage, args.offline, args.host, args.port, args.fixture, args.upstream)
    source = f"fixture {args.fixture}" if args.offline else f"upstream {args.upstream}"
    print(f"PatchPilot proxy  stage={args.stage}  http://{args.host}:{args.port}/forecast  ({source})", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
