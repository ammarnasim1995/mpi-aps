"""SQLite persistence with versioned migrations (see docs/MIGRATIONS.md).

Production target is PostgreSQL; the SQL here is kept portable (no SQLite-only features beyond AUTOINCREMENT).
An approved or published plan is immutable: edits require cloning to a new draft.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone

MIGRATIONS = [
    (1, """
    CREATE TABLE plans(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'plan',
        status TEXT NOT NULL DEFAULT 'draft', base_plan_id INTEGER, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
        decisions TEXT NOT NULL, snapshot TEXT NOT NULL, meta TEXT NOT NULL DEFAULT '{}',
        approved_by TEXT, approved_at TEXT, published_by TEXT, published_at TEXT);
    CREATE TABLE audit(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, user TEXT NOT NULL, action TEXT NOT NULL,
        entity TEXT NOT NULL, previous TEXT, new TEXT, reason TEXT, approval_status TEXT);
    CREATE TABLE orders(so TEXT NOT NULL, line TEXT NOT NULL, payload TEXT NOT NULL, origin TEXT NOT NULL,
        import_id INTEGER, PRIMARY KEY(so, line));
    CREATE TABLE refresh_log(id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, origin TEXT NOT NULL,
        attempted_at TEXT NOT NULL, success INTEGER NOT NULL, file_name TEXT, rows INTEGER, accepted INTEGER,
        rejected INTEGER, report TEXT, errors TEXT, user TEXT);
    CREATE TABLE overrides(order_key TEXT PRIMARY KEY, rank INTEGER NOT NULL, reason TEXT NOT NULL, user TEXT NOT NULL, ts TEXT NOT NULL);
    """),
    (2, """
    CREATE TABLE acknowledgments(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, user TEXT NOT NULL, source TEXT NOT NULL, reason TEXT NOT NULL);
    CREATE INDEX idx_audit_entity ON audit(entity);
    CREATE INDEX idx_plans_status ON plans(status);
    """),
    (3, """
    CREATE TABLE kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """),
    (4, """
    CREATE TABLE master_rows(entity TEXT NOT NULL, row_no INTEGER NOT NULL, payload TEXT NOT NULL, import_id INTEGER, PRIMARY KEY(entity, row_no));
    """),
]


class PlanLocked(Exception):
    pass


class ConflictError(Exception):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.migrate()

    def close(self) -> None:
        self.conn.close()

    def migrate(self) -> None:
        with self.lock, self.conn:
            self.conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
            done = {r[0] for r in self.conn.execute("SELECT version FROM schema_migrations")}
            for v, sql in MIGRATIONS:
                if v not in done:
                    for stmt in [s for s in sql.split(";") if s.strip()]:
                        self.conn.execute(stmt)
                    self.conn.execute("INSERT INTO schema_migrations VALUES(?,?)", (v, now()))

    def schema_version(self) -> int:
        return self.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]

    # ---- audit
    def audit(self, user, action, entity, previous=None, new=None, reason="", approval_status=None) -> None:
        with self.lock, self.conn:
            self.conn.execute("INSERT INTO audit(ts,user,action,entity,previous,new,reason,approval_status) VALUES(?,?,?,?,?,?,?,?)",
                              (now(), user, action, entity, json.dumps(previous, default=str), json.dumps(new, default=str), reason, approval_status))

    def audit_list(self, limit=200, entity=None):
        q, a = "SELECT * FROM audit", []
        if entity:
            q += " WHERE entity LIKE ?"
            a.append(f"%{entity}%")
        q += " ORDER BY id DESC LIMIT ?"
        return [dict(r) for r in self.conn.execute(q, a + [limit])]

    # ---- plans
    def save_plan(self, name, kind, decisions, snapshot, user, base_plan_id=None, meta=None, status="draft") -> int:
        with self.lock, self.conn:
            c = self.conn.execute(
                "INSERT INTO plans(name,kind,status,base_plan_id,created_by,created_at,decisions,snapshot,meta) VALUES(?,?,?,?,?,?,?,?,?)",
                (name, kind, status, base_plan_id, user, now(), json.dumps(decisions), json.dumps(snapshot), json.dumps(meta or {})))
            return c.lastrowid

    def get_plan(self, pid: int):
        r = self.conn.execute("SELECT * FROM plans WHERE id=?", (pid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        for k in ("decisions", "snapshot", "meta"):
            d[k] = json.loads(d[k])
        return d

    def list_plans(self, kind=None):
        q = "SELECT id,name,kind,status,base_plan_id,created_by,created_at,approved_by,approved_at,published_by,published_at,meta FROM plans"
        a = []
        if kind:
            q += " WHERE kind=?"
            a.append(kind)
        return [dict(r) for r in self.conn.execute(q + " ORDER BY id DESC", a)]

    def update_draft(self, pid, decisions, snapshot) -> None:
        with self.lock, self.conn:
            st = self.conn.execute("SELECT status FROM plans WHERE id=?", (pid,)).fetchone()
            if not st:
                raise KeyError(pid)
            if st[0] != "draft":
                raise PlanLocked(f"Plan {pid} is {st[0]}; approved/published plans are immutable. Clone it to a new draft to change it.")
            self.conn.execute("UPDATE plans SET decisions=?, snapshot=? WHERE id=?", (json.dumps(decisions), json.dumps(snapshot), pid))

    def transition(self, pid, to, user, four_eyes=True) -> None:
        with self.lock, self.conn:
            r = self.conn.execute("SELECT status, created_by, kind FROM plans WHERE id=?", (pid,)).fetchone()
            if not r:
                raise KeyError(pid)
            if r["kind"] != "plan":
                raise ConflictError("Only plans (not scenarios) can be approved/published; promote a scenario to a draft plan first.")
            if to == "approved":
                if r["status"] != "draft":
                    raise ConflictError(f"Only a draft can be approved (status is {r['status']}).")
                if four_eyes and r["created_by"] == user:
                    raise ConflictError("Four-eyes rule: the approver must differ from the plan author.")
                self.conn.execute("UPDATE plans SET status='approved', approved_by=?, approved_at=? WHERE id=?", (user, now(), pid))
            elif to == "published":
                if r["status"] != "approved":
                    raise ConflictError(f"Only an approved plan can be published (status is {r['status']}).")
                self.conn.execute("UPDATE plans SET status='superseded' WHERE status='published' AND kind='plan'")
                self.conn.execute("UPDATE plans SET status='published', published_by=?, published_at=? WHERE id=?", (user, now(), pid))
            else:
                raise ValueError(to)

    def baseline_plan(self):
        for st in ("published", "approved"):
            r = self.conn.execute("SELECT id FROM plans WHERE status=? AND kind='plan' ORDER BY id DESC LIMIT 1", (st,)).fetchone()
            if r:
                return self.get_plan(r[0])
        return None

    # ---- orders / refresh / overrides
    def save_orders(self, orders, import_id=None) -> None:
        with self.lock, self.conn:
            for o in orders:
                self.conn.execute("INSERT OR IGNORE INTO orders(so,line,payload,origin,import_id) VALUES(?,?,?,?,?)",
                                  (o.so, o.line, json.dumps(o.__dict__, default=str), o.origin, import_id))

    def stored_orders(self):
        return [json.loads(r["payload"]) for r in self.conn.execute("SELECT payload FROM orders")]

    def log_refresh(self, source, origin, success, file_name=None, rows=None, accepted=None, rejected=None, report=None, errors=None, user=None) -> int:
        with self.lock, self.conn:
            return self.conn.execute(
                "INSERT INTO refresh_log(source,origin,attempted_at,success,file_name,rows,accepted,rejected,report,errors,user) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (source, origin, now(), int(success), file_name, rows, accepted, rejected, json.dumps(report), json.dumps(errors or []), user)).lastrowid

    def refresh_list(self, limit=100):
        out = []
        for r in self.conn.execute("SELECT * FROM refresh_log ORDER BY id DESC LIMIT ?", (limit,)):
            d = dict(r)
            d["report"], d["errors"] = json.loads(d["report"] or "null"), json.loads(d["errors"] or "[]")
            out.append(d)
        return out

    def set_override(self, key, rank, reason, user) -> None:
        with self.lock, self.conn:
            self.conn.execute("INSERT OR REPLACE INTO overrides VALUES(?,?,?,?,?)", (key, rank, reason, user, now()))

    def clear_override(self, key) -> None:
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM overrides WHERE order_key=?", (key,))

    def overrides(self) -> dict:
        return {r["order_key"]: (r["rank"], r["reason"], r["user"]) for r in self.conn.execute("SELECT * FROM overrides")}

    def master_replace(self, entity, rows, import_id=None) -> None:
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM master_rows WHERE entity=?", (entity,))
            self.conn.executemany("INSERT INTO master_rows VALUES(?,?,?,?)", [(entity, i, json.dumps(r), import_id) for i, r in enumerate(rows)])

    def master_rows(self, entity):
        return [json.loads(r["payload"]) for r in self.conn.execute("SELECT payload FROM master_rows WHERE entity=? ORDER BY row_no", (entity,))]

    def master_delete(self, entity) -> None:
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM master_rows WHERE entity=?", (entity,))

    def master_entities(self) -> dict:
        return {r[0]: r[1] for r in self.conn.execute("SELECT entity, COUNT(*) FROM master_rows GROUP BY entity")}

    def kv_get(self, key, default=None):
        r = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def kv_set(self, key, value) -> None:
        with self.lock, self.conn:
            self.conn.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (key, json.dumps(value, default=str)))

    def add_ack(self, user, source, reason) -> None:
        with self.lock, self.conn:
            self.conn.execute("INSERT INTO acknowledgments(ts,user,source,reason) VALUES(?,?,?,?)", (now(), user, source, reason))
