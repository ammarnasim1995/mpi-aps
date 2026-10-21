import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from mpi_aps.api import build_server
from mpi_aps.config import Settings

from .fixtures import PD


class Http(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cfg = Settings("127.0.0.1", 0, os.path.join(cls.tmp, "t.sqlite"), None, True, PD, "CRITICAL", True)
        cls.srv = build_server(cfg)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.srv.store.close()

    def call(self, path, user=None, method="GET", body=None, raw=None):
        h = {"Content-Type": "application/json"}
        if user:
            h["X-Demo-User"] = user
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        try:
            r = urllib.request.urlopen(urllib.request.Request(self.base + path, data=data, headers=h, method=method))
            return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_auth_and_permissions(self):
        self.assertEqual(self.call("/api/meta")[0], 401)
        self.assertEqual(self.call("/api/meta", "demo.nobody")[0], 401)
        self.assertEqual(self.call("/api/meta", "demo.management")[0], 200)
        self.assertEqual(self.call("/api/plans/generate", "demo.management", "POST", {})[0], 403)
        self.assertEqual(self.call("/api/audit", "demo.pk_supervisor")[0], 403)
        self.assertEqual(self.call("/api/audit", "demo.admin")[0], 200)

    def test_input_validation_and_errors(self):
        self.assertEqual(self.call("/api/orders/import", "demo.admin", "POST", {})[0], 400)           # missing fields
        self.assertEqual(self.call("/api/orders/import", "demo.admin", "POST", raw=b"{not json")[0], 400)
        self.assertEqual(self.call("/api/scenarios/run", "demo.admin", "POST", {"changes": [{"type": "nope"}]})[0], 400)
        self.assertEqual(self.call("/api/nothing", "demo.admin")[0], 404)
        s, b = self.call("/api/orders/NOPE/explain", "demo.admin")
        self.assertEqual(s, 404)
        self.assertEqual(json.loads(b)["error"]["code"], "not_found")

    def test_static_path_traversal_blocked_and_headers(self):
        self.assertEqual(self.call("/../../etc/passwd")[0], 404)
        self.assertEqual(self.call("/%2e%2e/%2e%2e/etc/passwd")[0], 404)
        self.assertEqual(self.call("/")[0], 200)

    def test_end_to_end_flow_over_http(self):
        s, b = self.call("/api/plans/generate", "demo.central", "POST", {"name": "http plan"})
        pid = json.loads(b)["plan_id"]
        self.assertEqual(self.call(f"/api/plans/{pid}/approve", "demo.central", "POST", {})[0], 400)          # four-eyes
        self.assertEqual(self.call(f"/api/plans/{pid}/approve", "demo.central2", "POST", {"reason": "ok"})[0], 200)
        g = json.loads(self.call(f"/api/gantt?plan_id={pid}", "demo.central")[1])
        self.assertFalse(g["editable"])
        op = next(o for o in g["ops"] if o["start"])
        s, b = self.call(f"/api/plans/{pid}/move", "demo.central", "POST", {"op": op["id"], "resource": op["resource"], "reason": "x"})
        self.assertEqual(s, 403)


if __name__ == "__main__":
    unittest.main()
