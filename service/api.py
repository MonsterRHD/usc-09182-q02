"""HTTP 接口层：JSON 路由、错误映射、幂等键透传（Idempotency-Key 头）。"""
from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .domain import DomainError, PermissionDenied, ValidationError
from .lineage import LineageService
from .store import EventStore


class _Ctx:
    """单次请求上下文：路径参数、JSON 体、查询串、幂等键。"""

    def __init__(self, handler, params):
        self.params = params
        self.body = handler.read_json()
        self.query = {k: v[0] for k, v in parse_qs(urlparse(handler.path).query).items()}
        self.headers = handler.headers
        self.idem = handler.headers.get("Idempotency-Key") or self.body.get("idempotency_key")


def _need(body: dict, key: str):
    val = body.get(key)
    if val is None or val == "":
        raise ValidationError(f"缺少字段: {key}")
    return val


def _mut(result):
    """写操作统一响应：结果内联 + replayed 标记。"""
    data, replayed = result
    return 200, {**data, "replayed": replayed}


def _routes(svc: LineageService):
    def health(ctx):
        return 200, {"status": "ok"}

    def register_workshop(ctx):
        return _mut(svc.register_workshop(name=_need(ctx.body, "name"),
                                          idempotency_key=ctx.idem))

    def register_person(ctx):
        return _mut(svc.register_person(
            name=_need(ctx.body, "name"), role=_need(ctx.body, "role"),
            workshop_id=_need(ctx.body, "workshop_id"),
            stage=ctx.body.get("stage", 0), idempotency_key=ctx.idem))

    def get_person(ctx):
        return 200, svc.person_view(ctx.params[0])

    def link_lineage(ctx):
        return _mut(svc.link_lineage(master_id=_need(ctx.body, "master_id"),
                                     apprentice_id=_need(ctx.body, "apprentice_id"),
                                     idempotency_key=ctx.idem))

    def register_shape(ctx):
        return _mut(svc.register_shape(name=_need(ctx.body, "name"),
                                       workshop_id=_need(ctx.body, "workshop_id"),
                                       idempotency_key=ctx.idem))

    def get_shape(ctx):
        return 200, svc.shape_view(ctx.params[0])

    def submit_version(ctx):
        b = ctx.body
        return _mut(svc.submit_version(
            shape_id=ctx.params[0], author_id=_need(b, "author_id"),
            photo_summary=_need(b, "photo_summary"), steps=_need(b, "steps"),
            materials=b.get("materials"), note=b.get("note", ""),
            base_version_id=b.get("base_version_id"),
            corrects_version_id=b.get("corrects_version_id"),
            idempotency_key=ctx.idem))

    def get_version(ctx):
        return 200, svc.version_view(ctx.params[0])

    def confirm_version(ctx):
        return _mut(svc.confirm_version(version_id=ctx.params[0],
                                        confirmer_id=_need(ctx.body, "confirmer_id"),
                                        idempotency_key=ctx.idem))

    def version_chain(ctx):
        return 200, svc.citation_chain(ctx.params[0])

    def public_version(ctx):
        return 200, svc.public_version_view(ctx.params[0])

    def explain(ctx):
        return 200, svc.explain(version_id=_need(ctx.query, "version_id"),
                                person_id=ctx.query.get("person_id"))

    def forbid_pattern(ctx):
        return _mut(svc.forbid_pattern(motif=_need(ctx.body, "motif"),
                                       reason=ctx.body.get("reason", ""),
                                       banned_by=_need(ctx.body, "banned_by"),
                                       idempotency_key=ctx.idem))

    def list_patterns(ctx):
        return 200, {"patterns": svc.list_patterns()}

    def promote(ctx):
        return _mut(svc.promote_apprentice(
            person_id=ctx.params[0], to_stage=_need(ctx.body, "to_stage"),
            approved_by=_need(ctx.body, "approved_by"),
            reason=ctx.body.get("reason", ""), idempotency_key=ctx.idem))

    def grant_license(ctx):
        return _mut(svc.grant_license(
            shape_id=_need(ctx.body, "shape_id"), scope=_need(ctx.body, "scope"),
            granted_by=_need(ctx.body, "granted_by"),
            version_id=ctx.body.get("version_id"), idempotency_key=ctx.idem))

    def list_licenses(ctx):
        return 200, {"licenses": svc.list_licenses(ctx.query.get("shape_id"))}

    def revoke_license(ctx):
        return _mut(svc.revoke_license(license_id=ctx.params[0],
                                       revoked_by=_need(ctx.body, "revoked_by"),
                                       reason=ctx.body.get("reason", ""),
                                       idempotency_key=ctx.idem))

    def cite(ctx):
        return _mut(svc.cite(version_id=_need(ctx.body, "version_id"),
                             workshop_id=_need(ctx.body, "workshop_id"),
                             purpose=ctx.body.get("purpose", ""),
                             cited_by=_need(ctx.body, "cited_by"),
                             idempotency_key=ctx.idem))

    def audit(ctx):
        requester = ctx.headers.get("X-Person-Id")
        if not requester:
            raise PermissionDenied("审计需要传承人身份（X-Person-Id 头）")
        return 200, {"audit": svc.audit_log(requester_id=requester)}

    def sync(ctx):
        return _mut(svc.sync(device_id=_need(ctx.body, "device_id"),
                             batch_id=_need(ctx.body, "batch_id"),
                             ops=_need(ctx.body, "ops")))

    return [
        ("GET", r"^/health$", health),
        ("POST", r"^/workshops$", register_workshop),
        ("POST", r"^/persons$", register_person),
        ("GET", r"^/persons/([^/]+)$", get_person),
        ("POST", r"^/persons/([^/]+)/promote$", promote),
        ("POST", r"^/lineage$", link_lineage),
        ("POST", r"^/shapes$", register_shape),
        ("GET", r"^/shapes/([^/]+)$", get_shape),
        ("POST", r"^/shapes/([^/]+)/versions$", submit_version),
        ("GET", r"^/versions/([^/]+)$", get_version),
        ("POST", r"^/versions/([^/]+)/confirm$", confirm_version),
        ("GET", r"^/versions/([^/]+)/chain$", version_chain),
        ("GET", r"^/public/versions/([^/]+)$", public_version),
        ("GET", r"^/permissions/explain$", explain),
        ("POST", r"^/patterns/forbid$", forbid_pattern),
        ("GET", r"^/patterns$", list_patterns),
        ("POST", r"^/licenses$", grant_license),
        ("GET", r"^/licenses$", list_licenses),
        ("POST", r"^/licenses/([^/]+)/revoke$", revoke_license),
        ("POST", r"^/citations$", cite),
        ("GET", r"^/audit$", audit),
        ("POST", r"^/sync$", sync),
    ]


def make_handler(svc: LineageService):
    routes = [(m, re.compile(p), fn) for m, p, fn in _routes(svc)]

    class Handler(BaseHTTPRequestHandler):
        server_version = "NiguguLineage/0.1"

        def read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise ValidationError("请求体不是合法 JSON")
            if not isinstance(data, dict):
                raise ValidationError("请求体须为 JSON 对象")
            return data

        def _send(self, status, obj):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _dispatch(self, method):
            path = urlparse(self.path).path
            for m, pattern, fn in routes:
                if m != method:
                    continue
                match = pattern.match(path)
                if not match:
                    continue
                try:
                    ctx = _Ctx(self, list(match.groups()))
                    status, obj = fn(ctx)
                except DomainError as e:
                    status, obj = e.status, {"error": {"code": e.code, "message": e.message}}
                except Exception as e:  # noqa: BLE001 - 兜底，避免连接悬挂
                    status, obj = 500, {"error": {"code": "internal", "message": str(e)}}
                return self._send(status, obj)
            self._send(404, {"error": {"code": "not_found", "message": "接口不存在"}})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def log_message(self, *_):
            pass

    return Handler


def run():
    db_path = os.getenv("LINEAGE_DB") or os.path.join(
        os.getenv("DATA_DIR", "data"), "lineage.jsonl")
    svc = LineageService(EventStore(db_path))
    port = int(os.getenv("PORT", "8000"))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), make_handler(svc))
    print(f"泥咕咕技艺谱系服务已启动: http://0.0.0.0:{port}（数据文件 {db_path}）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
