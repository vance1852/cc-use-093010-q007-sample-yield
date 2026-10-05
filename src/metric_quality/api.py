"""统计资料质量服务的 JSON HTTP 接口。

同时提供可在进程内直接调用的 :class:`JsonApplication`（便于单元测试）
和标准库 :mod:`http.server` 适配器（``python -m metric_quality.api``）。

所有业务错误返回统一形状::

    {"error": {"code": "...", "message": "..."}}

分析结果可由总体通过率逐层下钻：
``rates -> result.samples[].conclusion -> records[].record_id``。
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .errors import ServiceError
from .service import LEGACY_ALGORITHM_VERSION, MetricQualityService
from .storage import connect


class JsonApplication:
    def __init__(self, service: MetricQualityService) -> None:
        self.service = service

    @staticmethod
    def _token(headers: Mapping[str, str]) -> str:
        return headers.get("authorization", "").removeprefix("Bearer ").strip()

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return value

    def handle(
        self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> dict[str, Any] | list[Any]:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        parsed = urlparse(target)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        parts = [part for part in path.split("/") if part]
        token = self._token(normalized)
        payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
        service = self.service

        if method == "GET" and path == "/health":
            return 200, {"status": "ok", "service": "metric-quality"}
        if method == "POST" and path == "/login":
            return 200, {"token": service.auth.login(payload["user_id"], payload["password"])}
        if method == "POST" and path == "/users":
            actor = service.auth.require(token, "admin")
            user = service.auth.create_user(payload["user_id"], payload["password"], payload.get("role", "reporter"))
            return 201, {"user_id": user.user_id, "role": user.role, "created_by": actor.user_id}

        if method == "POST" and path == "/policies":
            return 201, service.publish_policy(token, payload)
        if method == "GET" and len(parts) == 3 and parts[0] == "policies" and parts[2] == "versions":
            return 200, service.list_policies(token, parts[1])
        if method == "GET" and len(parts) == 4 and parts[0] == "policies" and parts[2] == "versions":
            return 200, service.get_policy(token, parts[1], int(parts[3]))
        if method == "GET" and len(parts) == 2 and parts[0] == "policies":
            return 200, service.get_policy(token, parts[1])

        if (
            method == "POST" and len(parts) == 5
            and parts[0] == "policies" and parts[2] == "versions" and parts[4] == "records"
        ):
            result = service.import_records(
                token, parts[1], int(parts[3]), payload.get("records", []),
                normalized.get("idempotency-key", "").strip() or None,
            )
            return 200, result
        if method == "POST" and len(parts) == 4 and parts[0] == "records" and parts[2] == "revoke":
            policy_version = payload.get("policy_version")
            return 200, service.revoke_record(
                token, parts[1], payload["reason"],
                payload.get("policy_id"),
                None if policy_version is None else int(policy_version),
            )

        if (
            method == "POST" and len(parts) == 5
            and parts[0] == "policies" and parts[2] == "versions" and parts[4] == "analysis"
        ):
            return 200, service.run_analysis(token, parts[1], int(parts[3]))
        if method == "GET" and len(parts) == 2 and parts[0] == "analyses":
            return 200, service.get_analysis(token, int(parts[1]))
        if method == "GET" and len(parts) == 3 and parts[0] == "policies" and parts[2] == "analyses":
            return 200, service.list_analyses(token, parts[1])

        if method == "POST" and len(parts) == 3 and parts[0] == "analyses" and parts[2] == "decisions":
            return 201, service.decide(token, int(parts[1]), payload["decision"], payload["reason"])
        if method == "POST" and len(parts) == 3 and parts[0] == "analyses" and parts[2] == "revoke":
            return 200, service.revoke_decision(token, int(parts[1]), payload["reason"])

        if method == "POST" and path == "/legacy-analyses":
            return 201, service.register_legacy_analysis(
                token, payload["lot_id"], payload["input_summary"], payload["result"]
            )
        if method == "GET" and len(parts) == 2 and parts[0] == "legacy-analyses":
            return 200, service.get_legacy_analysis(token, int(parts[1]))
        if method == "GET" and len(parts) == 3 and parts[0] == "lots" and parts[2] == "legacy-analyses":
            return 200, service.list_legacy_analyses(token, parts[1])

        if method == "GET" and path == "/audit":
            return 200, service.audit(
                token, query.get("entity_type", [None])[0], query.get("entity_id", [None])[0]
            )

        return 404, {"error": {"code": "route_not_found", "message": f"接口不存在: {method} {path}"}}

    def handle_response(
        self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> tuple[int, Any]:
        try:
            return self.handle(method, target, headers, body)
        except ServiceError as exc:
            return exc.status, {"error": {"code": exc.code, "message": str(exc)}}
        except PermissionError as exc:
            return 403, {"error": {"code": "forbidden", "message": str(exc)}}
        except (KeyError, TypeError, ValueError) as exc:
            return 422, {"error": {"code": "invalid_request", "message": str(exc)}}


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "MetricQuality/2"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            status, payload = application.handle_response(self.command, self.path, dict(self.headers.items()), body)
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动统计资料质量 HTTP 服务")
    parser.add_argument("--database", default="metric-quality.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8082)
    args = parser.parse_args(argv)
    # ThreadingHTTPServer 会在工作线程中复用同一连接；所有写操作都经过
    # BEGIN IMMEDIATE 串行化，离线场景下安全共享。
    service = MetricQualityService(args.database, check_same_thread=False)
    service.bootstrap_admin()
    application = JsonApplication(service)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(application))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# 向后兼容旧的无状态冒烟入口（旧算法版本仍可被引用）。
__all__ = ["JsonApplication", "make_handler", "main", "LEGACY_ALGORITHM_VERSION"]
