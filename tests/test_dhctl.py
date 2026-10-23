#!/usr/bin/env python3
"""dhctl.py 테스트 — InfluxDB 1.x / 2.x HTTP API 를 흉내 내는 목 서버 사용."""
import base64
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import dhctl  # noqa: E402


class State(object):
    def __init__(self):
        self.reset()

    def reset(self):
        self.queries = []
        self.dbs = {}             # db -> {rp: duration}
        self.auth = None          # 기대하는 Authorization 헤더 (None = 인증 없음)
        self.buckets = {}
        self.dbrps = []
        self.requests = []


S = State()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj=None):
        body = b"" if obj is None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if self.headers.get("Content-Type", "").startswith("application/json"):
            return json.loads(raw or b"null")
        return parse_qs(raw.decode())

    def _route(self, method):
        u = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        S.requests.append((method, u.path, self.headers.get("Authorization")))
        if S.auth is not None and self.headers.get("Authorization") != S.auth and u.path != "/ping":
            return self._send(401, {"error": "unauthorized"})
        body = self._body() if method in ("POST", "PUT", "PATCH") else None
        p = u.path
        # ---------------- InfluxDB ----------------
        if p == "/ping":
            return self._send(204)
        if p == "/query":
            q = qs.get("q") or (body or {}).get("q", [""])[0]
            S.queries.append(q)
            return self._send(200, {"results": [self._influxql(q)]})
        if p == "/api/v2/orgs":
            return self._send(200, {"orgs": [{"id": "org1", "name": qs.get("org")}]})
        if p == "/api/v2/buckets" and method == "GET":
            b = S.buckets.get(qs.get("name"))
            return self._send(200, {"buckets": [b] if b else []})
        if p == "/api/v2/buckets" and method == "POST":
            b = dict(body, id="bkt1")
            S.buckets[body["name"]] = b
            return self._send(201, b)
        if p.startswith("/api/v2/buckets/") and method == "PATCH":
            for b in S.buckets.values():
                b.update(body)
            return self._send(200, {})
        if p == "/api/v2/dbrps" and method == "GET":
            return self._send(200, {"content": S.dbrps})
        if p == "/api/v2/dbrps" and method == "POST":
            S.dbrps.append(body)
            return self._send(201, body)
        return self._send(404, {"message": "no route %s %s" % (method, p)})

    def _influxql(self, q):
        qu = q.upper()
        if qu.startswith("CREATE DATABASE"):
            S.dbs.setdefault(q.split('"')[1], {"autogen": "0s"})
            return {}
        if qu.startswith("SHOW RETENTION POLICIES"):
            db = q.split('"')[1]
            vals = [[n, d, "168h0m0s", 1, False] for n, d in S.dbs.get(db, {}).items()]
            return {"series": [{"columns": ["name", "duration", "shardGroupDuration", "replicaN", "default"],
                                "values": vals}]}
        if qu.startswith("CREATE RETENTION POLICY") or qu.startswith("ALTER RETENTION POLICY"):
            parts = q.split('"')
            S.dbs[parts[3]][parts[1]] = q.split("DURATION ")[1].split()[0]
            return {}
        if qu.startswith("SHOW DATABASES"):
            return {"series": [{"columns": ["name"], "values": [[d] for d in S.dbs]}]}
        if "COUNT(" in qu:
            return {"series": [{"name": "x", "columns": ["time", "count"], "values": [[0, 42]]}]}
        if "LAST(\"STATUS_CODE\")" in qu:
            return {"series": [{"name": "smart_device", "tags": {"host": "mirror01", "device": "sda",
                                                                  "model": "WDC"},
                                "columns": ["time", "status", "reason", "temp_c", "poh"],
                                "values": [[0, 1, "reallocated 8", 41, 30123]]}]}
        return {}

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def do_PATCH(self):
        self._route("PATCH")


class DhctlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Handler)
        cls.url = "http://127.0.0.1:%d" % cls.srv.server_port
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        S.reset()
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def run_cli(self, env, *argv):
        self.tmp.seek(0)
        self.tmp.truncate()
        for k, v in env.items():
            self.tmp.write('%s="%s"\n' % (k, v))
        self.tmp.flush()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = dhctl.main(["-c", self.tmp.name] + list(argv))
        return rc, out.getvalue(), err.getvalue()

    def v1(self, **kw):
        env = {"INFLUX_VERSION": "1", "INFLUX_URL": self.url, "INFLUX_DB": "disk_health",
               "INFLUX_RETENTION": "400d"}
        env.update(kw)
        return env

    def test_influx_init_v1_create_then_alter(self):
        rc, out, err = self.run_cli(self.v1(), "influx-init")
        self.assertEqual(rc, 0, err)
        self.assertEqual(S.dbs["disk_health"]["disk_health"], "400d")
        self.assertIn('CREATE RETENTION POLICY "disk_health" ON "disk_health" DURATION 400d REPLICATION 1 DEFAULT',
                      S.queries)
        rc, out, err = self.run_cli(self.v1(INFLUX_RETENTION="52w"), "influx-init")
        self.assertEqual(rc, 0, err)
        self.assertEqual(S.dbs["disk_health"]["disk_health"], "52w")
        self.assertIn("갱신", out)

    def test_influx_init_bad_retention(self):
        rc, out, err = self.run_cli(self.v1(INFLUX_RETENTION="5m"), "influx-init")
        self.assertEqual(rc, 1)
        self.assertIn("INFLUX_RETENTION", err)

    def test_auth_admin_preferred(self):
        S.auth = "Basic " + base64.b64encode(b"admin:secret").decode()
        rc, _, err = self.run_cli(self.v1(INFLUX_USER="writer", INFLUX_PASSWORD="x",
                                          INFLUX_ADMIN_USER="admin", INFLUX_ADMIN_PASSWORD="secret"), "influx-init")
        self.assertEqual(rc, 0, err)
        rc, _, err = self.run_cli(self.v1(INFLUX_USER="writer", INFLUX_PASSWORD="x"), "influx-init")
        self.assertEqual(rc, 1)
        self.assertIn("인증 실패", err)

    def test_influx_init_v2(self):
        env = {"INFLUX_VERSION": "2", "INFLUX_URL": self.url, "INFLUX_TOKEN": "tok", "INFLUX_ORG": "dcloud",
               "INFLUX_BUCKET": "disk_health", "INFLUX_DB": "disk_health", "INFLUX_RETENTION": "30d"}
        S.auth = "Token tok"
        rc, out, err = self.run_cli(env, "influx-init")
        self.assertEqual(rc, 0, err)
        self.assertEqual(S.buckets["disk_health"]["retentionRules"], [{"type": "expire", "everySeconds": 2592000}])
        self.assertEqual(S.dbrps[0]["database"], "disk_health")
        self.assertTrue(S.dbrps[0]["default"])
        rc, out, err = self.run_cli(env, "influx-init")   # 두 번째 실행: 중복 생성 없음
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(S.dbrps), 1)

    def test_verify(self):
        rc, out, err = self.run_cli(self.v1(), "verify", "--host", "mirror01", "--measurement", "diskio")
        self.assertEqual(rc, 0, err)
        self.assertIn("포인트 42개", out)
        self.assertIn("reallocated 8", out)
        self.assertTrue(any('"host" = \'mirror01\'' in q for q in S.queries))

    def test_connection_error(self):
        rc, _, err = self.run_cli(self.v1(INFLUX_URL="http://127.0.0.1:1"), "ping")
        self.assertEqual(rc, 1)
        self.assertIn("연결 실패", err)


if __name__ == "__main__":
    unittest.main()
