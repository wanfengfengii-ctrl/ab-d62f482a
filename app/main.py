"""HTTP entrypoint (standard library only).

Exposes:
  POST /api/cold-chain/exposure  -- adjudicate thermal exposure
  GET  /health                   -- readiness/liveness probe
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .exposure import NonFinite, ValidationError, evaluate


class _Raw:
    """Wrapper for a JSON scalar that must be emitted verbatim."""

    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text


def _encode(value: object) -> str:
    """JSON encoder which emits Decimal exactly (never via float)."""
    if value is None or isinstance(value, bool):
        return json.dumps(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, _Raw):
        return value.text
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, float)):
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_encode(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(
            f"{json.dumps(str(k), ensure_ascii=False)}:{_encode(v)}"
            for k, v in value.items()
        ) + "}"
    raise TypeError(f"不可序列化的类型: {type(value)!r}")


def _loads_constant(token: str) -> NonFinite:
    return NonFinite(token)


def parse_json(body: bytes) -> object:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"请求体必须是 UTF-8 编码的 JSON: {exc}") from exc
    try:
        return json.loads(text, parse_constant=_loads_constant)
    except json.JSONDecodeError as exc:
        raise ValueError(f"非法 JSON（第 {exc.lineno} 行第 {exc.colno} 列）: {exc.msg}") from exc


class Handler(BaseHTTPRequestHandler):
    server_version = "ColdChainExposure/1.0"

    def _send_json(self, status: int, payload: object) -> None:
        data = _encode(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path.split("?", 1)[0] in ("/health", "/healthz", "/"):
            self._send_json(HTTPStatus.OK, {"status": "ok"})
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"errors": [
                {"loc": "path", "message": "路径不存在"}
            ]})

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        path = self.path.split("?", 1)[0]
        if path != "/api/cold-chain/exposure":
            self._send_json(HTTPStatus.NOT_FOUND, {"errors": [
                {"loc": "path", "message": "路径不存在"}
            ]})
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = parse_json(raw)
        except ValueError as exc:
            self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {
                "errors": [{"loc": "body", "message": str(exc)}]
            })
            return

        try:
            result = evaluate(payload)
        except ValidationError as exc:
            self._send_json(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                {"errors": [{"loc": e.loc, "message": e.message} for e in exc.errors]},
            )
            return
        self._send_json(HTTPStatus.OK, result)

    def log_message(self, fmt: str, *args: object) -> None:
        if os.environ.get("ACCESS_LOG", "1") == "1":
            super().log_message(fmt, *args)


def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"cold-chain exposure service listening on {host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
