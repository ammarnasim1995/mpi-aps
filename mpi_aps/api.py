"""HTTP API (stdlib). Every route declares the permission it needs; plant scoping is enforced in the service."""
from __future__ import annotations

import json
import logging
import mimetypes
import re
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .auth import Authenticator
from .config import Settings, load, setup_logging
from .service import AppService, BadRequest, Forbidden, NotFound
from .store import PlanLocked, Store
from . import importer

log = logging.getLogger("mpi_aps")
STATIC = Path(__file__).parent / "static"
FILTERS = ("plant", "country", "process", "family", "customer", "sku", "order", "resource", "priority", "date_from", "date_to", "status")


def _need(b, *keys):
    miss = [k for k in keys if b.get(k) in (None, "")]
    if miss:
        raise BadRequest(f"Missing field(s): {', '.join(miss)}")


def make_routes(svc: AppService):
    f = lambda q: {k: q[k][0] for k in FILTERS if k in q and q[k][0]}
    pid = lambda q: q.get("plan_id", [None])[0]
    R = []

    def route(method, pattern, perm=None):
        def deco(fn):
            R.append((method, re.compile("^" + pattern + "$"), perm, fn))
            return fn
        return deco

    route("GET", "/api/meta", "view")(lambda pr, q, b, m: svc.meta(pr))
    route("GET", "/api/dashboard", "view")(lambda pr, q, b, m: svc.dashboard(pr, f(q), pid(q)))
    route("GET", "/api/orders", "view")(lambda pr, q, b, m: svc.orders(pr, f(q), pid(q)))
    route("GET", r"/api/orders/([^/]+)/explain", "view")(lambda pr, q, b, m: svc.explain(pr, m[0], pid(q)))
    route("GET", r"/api/planning/(knitting|dipping|packing)", "view")(lambda pr, q, b, m: svc.planning(pr, m[0], f(q), pid(q)))
    route("GET", "/api/gantt", "view")(lambda pr, q, b, m: svc.gantt(pr, f(q), pid(q)))
    route("GET", "/api/capacity", "view")(lambda pr, q, b, m: svc.capacity(pr, f(q), q.get("period", ["week"])[0], pid(q)))
    route("GET", "/api/exceptions", "view")(lambda pr, q, b, m: svc.exceptions(pr, pid(q)))
    route("GET", "/api/material", "view")(lambda pr, q, b, m: svc.material(pr, pid(q)))
    route("GET", "/api/validation", "view")(lambda pr, q, b, m: svc.validation(pr))
    route("GET", "/api/refresh", "view")(lambda pr, q, b, m: svc.refresh(pr))
    route("GET", "/api/settings", "view")(lambda pr, q, b, m: svc.settings_view(pr))
    route("GET", "/api/plans", "view")(lambda pr, q, b, m: svc.plans(pr))
    route("GET", r"/api/plans/(\d+)", "view")(lambda pr, q, b, m: svc.plan_detail(pr, m[0]))
    route("GET", "/api/scenarios", "view")(lambda pr, q, b, m: svc.scenarios(pr))
    route("GET", r"/api/scenarios/(\d+)", "view")(lambda pr, q, b, m: svc.scenario(pr, m[0]))
    route("GET", "/api/audit", "view_audit")(lambda pr, q, b, m: svc.audit(pr, int(q.get("limit", ["200"])[0]), q.get("entity", [None])[0]))
    route("GET", "/api/templates/orders.csv", "view")(lambda pr, q, b, m: ("text/csv", importer.TEMPLATE))

    @route("POST", "/api/plans/generate", "edit_schedule")
    def _gen(pr, q, b, m):
        return svc.generate_plan(pr, b.get("name"))

    @route("POST", r"/api/plans/(\d+)/clone", "edit_schedule")
    def _clone(pr, q, b, m):
        return svc.clone_plan(pr, m[0], b.get("reason"))

    @route("POST", r"/api/plans/(\d+)/undo", "edit_schedule")
    def _undo(pr, q, b, m):
        return svc.undo(pr, m[0])

    @route("POST", r"/api/plans/(\d+)/approve", "approve")
    def _appr(pr, q, b, m):
        return svc.approve(pr, m[0], b.get("reason", ""))

    @route("POST", r"/api/plans/(\d+)/publish", "publish")
    def _pub(pr, q, b, m):
        return svc.publish(pr, m[0], b.get("reason", ""))

    @route("POST", r"/api/plans/(\d+)/move", "edit_schedule")
    def _move(pr, q, b, m):
        _need(b, "op", "resource", "reason")
        return svc.move(pr, m[0], b["op"], b["resource"], b.get("not_before"), b["reason"], b.get("recipe"))

    @route("POST", "/api/orders/import", "import")
    def _imp(pr, q, b, m):
        _need(b, "csv", "file_name")
        return svc.import_orders(pr, b["csv"], b["file_name"], b.get("control_rows"), b.get("control_qty"), bool(b.get("replace_sample")))

    @route("POST", r"/api/orders/([^/]+)/priority", "edit_schedule")
    def _prio(pr, q, b, m):
        _need(b, "rank", "reason")
        return svc.set_override(pr, m[0], b["rank"], b["reason"])

    @route("DELETE", r"/api/orders/([^/]+)/priority", "edit_schedule")
    def _unprio(pr, q, b, m):
        return svc.clear_override(pr, m[0], b.get("reason"))

    @route("POST", "/api/scenarios/run", "run_scenarios")
    def _scn(pr, q, b, m):
        _need(b, "changes")
        return svc.run_scenario(pr, b.get("name"), b["changes"], b.get("mode", "replan"), b.get("base_plan_id"), b.get("save", True))

    @route("POST", "/api/scenarios/breakdown", "run_scenarios")
    def _bd(pr, q, b, m):
        _need(b, "resource", "start", "end")
        return svc.breakdown(pr, b["resource"], b["start"], b["end"])

    @route("POST", "/api/refresh/acknowledge", "ack_stale")
    def _ack(pr, q, b, m):
        _need(b, "source", "reason")
        return svc.acknowledge_stale(pr, b["source"], b["reason"])

    @route("POST", r"/api/settings/(efficiency|cure|freshness)", "settings")
    def _set(pr, q, b, m):
        _need(b, "payload", "reason")
        return svc.put_setting(pr, m[0], b["payload"], b["reason"])
    return R


class Handler(BaseHTTPRequestHandler):
    server_version = "MPI-APS/0.2"
    routes = []
    auth: Authenticator = None
    max_body = 5_000_000

    def log_message(self, *a):
        pass

    def _send(self, code, payload, ctype="application/json", extra=None):
        body = payload if isinstance(payload, bytes) else (payload.encode() if isinstance(payload, str) else json.dumps(payload, default=str).encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith(("text/", "application/json")) else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, name, msg):
        self._send(code, {"error": {"code": name, "message": msg}})

    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")
    def do_DELETE(self): self._handle("DELETE")

    def _handle(self, method):
        t0, rid = time.time(), uuid.uuid4().hex[:8]
        u = urlparse(self.path)
        user, status = None, 500
        try:
            if method == "GET" and not u.path.startswith("/api/"):
                status = self._static(u.path)
                return
            pr = self.auth.authenticate(self.headers)
            user = pr.user if pr else None
            if pr is None:
                status = 401
                return self._err(401, "unauthenticated", "Valid credentials required.")
            for m, rx, perm, fn in self.routes:
                mt = rx.match(u.path)
                if m == method and mt:
                    if perm and not pr.can(perm):
                        status = 403
                        return self._err(403, "forbidden", f"Role '{pr.role}' lacks permission '{perm}'.")
                    body = {}
                    if method in ("POST", "DELETE"):
                        n = int(self.headers.get("Content-Length") or 0)
                        if n > self.max_body:
                            status = 413
                            return self._err(413, "too_large", "Request body too large.")
                        raw = self.rfile.read(n) if n else b""
                        try:
                            body = json.loads(raw) if raw else {}
                        except json.JSONDecodeError:
                            status = 400
                            return self._err(400, "bad_json", "Body must be valid JSON.")
                        if not isinstance(body, dict):
                            status = 400
                            return self._err(400, "bad_json", "Body must be a JSON object.")
                    res = fn(pr, parse_qs(u.query), body, mt.groups())
                    status = 200
                    if isinstance(res, tuple):
                        return self._send(200, res[1], res[0])
                    return self._send(200, res)
            status = 404
            self._err(404, "not_found", "No such endpoint.")
        except BadRequest as e:
            status = 400
            self._err(400, "bad_request", str(e))
        except Forbidden as e:
            status = 403
            self._err(403, "forbidden", str(e))
        except NotFound as e:
            status = 404
            self._err(404, "not_found", str(e))
        except PlanLocked as e:
            status = 409
            self._err(409, "plan_locked", str(e))
        except Exception:
            log.exception("unhandled error", extra={"ctx": {"rid": rid, "path": u.path}})
            status = 500
            self._err(500, "internal", f"Internal error (reference {rid}).")
        finally:
            log.info("request", extra={"ctx": {"rid": rid, "method": method, "path": u.path, "status": status, "user": user,
                                               "ms": round((time.time() - t0) * 1000)}})

    def _static(self, path):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        f = (STATIC / rel).resolve()
        if STATIC.resolve() not in f.parents or not f.is_file():
            self._err(404, "not_found", "Not found.")
            return 404
        self._send(200, f.read_bytes(), mimetypes.guess_type(f.name)[0] or "application/octet-stream")
        return 200


def build_server(cfg: Settings) -> ThreadingHTTPServer:
    setup_logging(cfg.log_level)
    store = Store(cfg.db_path)
    svc = AppService(store, cfg.plan_date, cfg.four_eyes)
    Handler.routes = make_routes(svc)
    Handler.auth = Authenticator(cfg.users_file, cfg.dev_auth, cfg.host)
    if not cfg.dev_auth and not cfg.users_file:
        raise SystemExit("No authentication configured: set MPI_USERS_FILE (production) or MPI_DEV_AUTH=1 (local demo, loopback only).")
    Handler.max_body = cfg.max_body_bytes
    srv = ThreadingHTTPServer((cfg.host, cfg.port), Handler)
    srv.store = store
    return srv


def main() -> None:
    cfg = load()
    srv = build_server(cfg)
    log.info("listening", extra={"ctx": {"host": cfg.host, "port": cfg.port, "dev_auth": cfg.dev_auth, "plan_date": str(cfg.plan_date)}})
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
