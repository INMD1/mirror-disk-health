#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dhctl.py — mirror-disk-health 제어 도구 (Python 3.6+, 표준 라이브러리만 사용)

  python3 dhctl.py -c config.env ping          InfluxDB 연결 확인
  python3 dhctl.py -c config.env influx-init   DB + 보존정책(1.x) / bucket + DBRP(2.x) 생성
  python3 dhctl.py -c config.env verify [--host mirror01] [--wait 120]
                                               InfluxDB 에 데이터가 들어오는지 확인
  python3 dhctl.py -c config.env query 'SHOW MEASUREMENTS'
                                               임의 InfluxQL 실행

config.env 는 install.sh 와 같은 파일을 사용한다.
"""

from __future__ import print_function

import argparse
import base64
import json
import os
import re
import ssl
import sys
import time

try:  # Python 3
    from urllib.error import HTTPError, URLError
    from urllib.parse import urlencode
    from urllib.request import Request, urlopen
except ImportError:  # pragma: no cover
    print("Python 3 가 필요합니다", file=sys.stderr)
    sys.exit(1)

HERE = os.path.dirname(os.path.abspath(__file__))


class CliError(Exception):
    pass


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #

def load_env(path):
    if not os.path.isfile(path):
        raise CliError("설정 파일이 없습니다: %s" % path)
    cfg = {}
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            if key.startswith("export "):
                key = key[7:].strip()
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
                continue
            cfg[key] = _shell_value(val.strip())
    return cfg


def _shell_value(val):
    r"""bash 대입문의 값 부분 해석: "..."(\\ \" \$ \` 이스케이프), '...'(그대로), 따옴표 없음(# 주석 제거)"""
    out = []
    i, n = 0, len(val)
    while i < n:
        ch = val[i]
        if ch == '"':
            i += 1
            while i < n and val[i] != '"':
                if val[i] == "\\" and i + 1 < n and val[i + 1] in '\\"$`':
                    out.append(val[i + 1])
                    i += 2
                else:
                    out.append(val[i])
                    i += 1
            i += 1
        elif ch == "'":
            end = val.find("'", i + 1)
            end = n if end < 0 else end
            out.append(val[i + 1:end])
            i = end + 1
        elif ch.isspace():
            break  # 값 뒤의 공백 + 주석
        elif ch == "\\" and i + 1 < n:
            out.append(val[i + 1])
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def truthy(v):
    return str(v or "").strip().lower() in ("1", "y", "yes", "true", "on")


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

class Http(object):
    def __init__(self, base, headers=None, insecure=False):
        self.base = base.rstrip("/")
        self.headers = headers or {}
        self.ctx = None
        if self.base.startswith("https") and insecure:
            self.ctx = ssl.create_default_context()
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def request(self, method, path, params=None, body=None, form=None, timeout=20):
        url = self.base + path
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params)
        headers = dict(self.headers)
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif form is not None:
            data = urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers.setdefault("Accept", "application/json")
        req = Request(url, data=data, headers=headers, method=method)
        try:
            resp = urlopen(req, timeout=timeout, context=self.ctx)
            status, raw = resp.status, resp.read()
        except HTTPError as exc:
            status, raw = exc.code, exc.read()
        except URLError as exc:
            raise CliError("%s %s 연결 실패: %s" % (method, url.split("?")[0], exc.reason))
        except OSError as exc:  # timeout 등
            raise CliError("%s %s 연결 실패: %s" % (method, url.split("?")[0], exc))
        text = raw.decode("utf-8", "replace") if raw else ""
        try:
            payload = json.loads(text) if text else None
        except ValueError:
            payload = text
        return status, payload


def basic(user, pw):
    return "Basic " + base64.b64encode(("%s:%s" % (user, pw)).encode("utf-8")).decode("ascii")


# --------------------------------------------------------------------------- #
# InfluxDB
# --------------------------------------------------------------------------- #

def q_ident(name):
    return '"' + str(name).replace("\\", "\\\\").replace('"', '\\"') + '"'


def q_str(val):
    return "'" + str(val).replace("\\", "\\\\").replace("'", "\\'") + "'"


class Influx(object):
    def __init__(self, cfg, admin=False):
        self.cfg = cfg
        self.version = str(cfg.get("INFLUX_VERSION", "1")).strip()
        self.db = cfg.get("INFLUX_DB") or "disk_health"
        headers = {}
        if self.version == "2":
            if not cfg.get("INFLUX_TOKEN"):
                raise CliError("INFLUX_VERSION=2 인데 INFLUX_TOKEN 이 비어 있습니다")
            headers["Authorization"] = "Token " + cfg["INFLUX_TOKEN"]
        else:
            user = cfg.get("INFLUX_USER", "")
            pw = cfg.get("INFLUX_PASSWORD", "")
            if admin and cfg.get("INFLUX_ADMIN_USER"):
                user, pw = cfg["INFLUX_ADMIN_USER"], cfg.get("INFLUX_ADMIN_PASSWORD", "")
            if user:
                headers["Authorization"] = basic(user, pw)
        if not cfg.get("INFLUX_URL"):
            raise CliError("INFLUX_URL 이 비어 있습니다")
        self.http = Http(cfg["INFLUX_URL"], headers, truthy(cfg.get("INFLUX_INSECURE_SKIP_VERIFY")))

    def ping(self):
        status, _ = self.http.request("GET", "/ping")
        if status not in (200, 204):
            raise CliError("InfluxDB /ping 응답 %s" % status)

    def query(self, q, db=None, write=False):
        """InfluxQL (1.x /query, 2.x 호환 /query) → results 리스트"""
        params = {"db": db} if db else {}
        if write:
            status, payload = self.http.request("POST", "/query", params=params, form={"q": q})
        else:
            params["q"] = q
            status, payload = self.http.request("GET", "/query", params=params)
        if status == 401 or status == 403:
            raise CliError("InfluxDB 인증 실패(%s): 계정/토큰 확인 — %s" % (status, short(payload)))
        if status >= 400:
            raise CliError("InfluxQL 실패(%s): %s\n  query: %s" % (status, short(payload), q))
        results = (payload or {}).get("results", []) if isinstance(payload, dict) else []
        for r in results:
            if "error" in r:
                raise CliError("InfluxQL 오류: %s\n  query: %s" % (r["error"], q))
        return results


def short(p, n=300):
    s = json.dumps(p, ensure_ascii=False) if not isinstance(p, str) else p
    return s if len(s) <= n else s[:n] + "…"


def duration_seconds(d):
    m = re.match(r"^(\d+)([hdw])$", d)
    if not m:
        raise CliError("INFLUX_RETENTION 형식 오류: %r (예: 400d, 52w, 720h, INF)" % d)
    n, unit = int(m.group(1)), m.group(2)
    return n * {"h": 3600, "d": 86400, "w": 604800}[unit]


def cmd_ping(cfg, args):
    ix = Influx(cfg)
    ix.ping()
    print("InfluxDB OK: %s (v%s)" % (cfg["INFLUX_URL"], ix.version))
    if ix.version == "1":
        try:
            ix.query("SHOW DATABASES")
            print("  인증/쿼리 OK")
        except CliError as exc:
            # 일반 사용자는 SHOW DATABASES 권한이 없을 수 있다
            print("  주의: %s" % exc)


def cmd_influx_init(cfg, args):
    ret = (cfg.get("INFLUX_RETENTION") or "400d").strip()
    if ret.upper() == "INF":
        ret = "INF"
    else:
        duration_seconds(ret)
    ix = Influx(cfg, admin=True)
    ix.ping()
    if ix.version == "1":
        db = ix.db
        ix.query("CREATE DATABASE %s" % q_ident(db), write=True)
        rps = ix.query("SHOW RETENTION POLICIES ON %s" % q_ident(db))
        names = []
        for s in (rps[0].get("series") or []) if rps else []:
            cols = s.get("columns", [])
            for row in s.get("values", []):
                names.append(row[cols.index("name")])
        rp = "disk_health"
        if rp in names:
            ix.query("ALTER RETENTION POLICY %s ON %s DURATION %s DEFAULT" % (q_ident(rp), q_ident(db), ret),
                     write=True)
            print("보존정책 갱신: %s.%s (%s, DEFAULT)" % (db, rp, ret))
        else:
            ix.query("CREATE RETENTION POLICY %s ON %s DURATION %s REPLICATION 1 DEFAULT"
                     % (q_ident(rp), q_ident(db), ret), write=True)
            print("보존정책 생성: %s.%s (%s, DEFAULT)" % (db, rp, ret))
        print("데이터베이스 준비 완료: %s" % db)
        return

    # ---- InfluxDB 2.x ----
    org_name = cfg.get("INFLUX_ORG") or ""
    bucket = cfg.get("INFLUX_BUCKET") or "disk_health"
    st, p = ix.http.request("GET", "/api/v2/orgs", params={"org": org_name})
    if st != 200 or not p.get("orgs"):
        raise CliError("조직(org) 조회 실패 %s: %s" % (st, short(p)))
    org_id = p["orgs"][0]["id"]
    st, p = ix.http.request("GET", "/api/v2/buckets", params={"name": bucket, "orgID": org_id})
    if st != 200:
        raise CliError("버킷 조회 실패 %s: %s" % (st, short(p)))
    rules = [] if ret == "INF" else [{"type": "expire", "everySeconds": duration_seconds(ret)}]
    if p.get("buckets"):
        bucket_id = p["buckets"][0]["id"]
        st, p = ix.http.request("PATCH", "/api/v2/buckets/" + bucket_id, body={"retentionRules": rules})
        if st >= 300:
            raise CliError("버킷 보존기간 갱신 실패 %s: %s" % (st, short(p)))
        print("버킷 존재: %s (보존 %s 로 갱신)" % (bucket, ret))
    else:
        st, p = ix.http.request("POST", "/api/v2/buckets",
                                body={"orgID": org_id, "name": bucket, "retentionRules": rules})
        if st >= 300:
            raise CliError("버킷 생성 실패 %s: %s" % (st, short(p)))
        bucket_id = p["id"]
        print("버킷 생성: %s (보존 %s)" % (bucket, ret))
    st, p = ix.http.request("GET", "/api/v2/dbrps", params={"orgID": org_id, "bucketID": bucket_id})
    if st != 200:
        raise CliError("DBRP 조회 실패 %s: %s" % (st, short(p)))
    existing = [d for d in (p.get("content") or []) if d.get("database") == ix.db]
    if existing:
        print("DBRP 매핑 존재: %s → %s" % (ix.db, bucket))
    else:
        st, p = ix.http.request("POST", "/api/v2/dbrps", body={
            "orgID": org_id, "bucketID": bucket_id, "database": ix.db,
            "retention_policy": "autogen", "default": True})
        if st >= 300:
            raise CliError("DBRP 생성 실패 %s: %s" % (st, short(p)))
        print("DBRP 매핑 생성: %s/autogen → %s  (Grafana InfluxQL 조회용)" % (ix.db, bucket))


def cmd_query(cfg, args):
    ix = Influx(cfg)
    res = ix.query(args.influxql, db=ix.db, write=args.write)
    print_results(res)


def print_results(results):
    for r in results:
        for s in r.get("series", []) or []:
            title = s.get("name", "")
            if s.get("tags"):
                title += " " + ",".join("%s=%s" % kv for kv in sorted(s["tags"].items()))
            print("## " + title)
            cols = s.get("columns", [])
            rows = s.get("values", []) or []
            widths = [max([len(str(c))] + [len(str(row[i])) for row in rows]) for i, c in enumerate(cols)]
            print("  ".join(str(c).ljust(w) for c, w in zip(cols, widths)))
            for row in rows:
                print("  ".join(str(v).ljust(w) for v, w in zip(row, widths)))


def cmd_verify(cfg, args):
    ix = Influx(cfg)
    ix.ping()
    host_cond = (' AND "host" = %s' % q_str(args.host)) if args.host else ""
    deadline = time.time() + max(0, args.wait)
    want = args.measurement
    probe = {
        "diskio": 'SELECT count("reads") FROM "diskio" WHERE time > now() - 10m%s' % host_cond,
        "smart_device": 'SELECT count("smartctl_exit") FROM "smart_device" WHERE time > now() - 1h%s' % host_cond,
        "disk_health_collector": 'SELECT count("duration_ms") FROM "disk_health_collector" WHERE time > now() - 1h%s'
                                 % host_cond,
    }[want]
    while True:
        res = ix.query(probe, db=ix.db)
        n = 0
        for s in (res[0].get("series") or []) if res else []:
            for row in s.get("values", []):
                n += int(row[1] or 0)
        if n > 0:
            print("OK: %s 포인트 %d개 수신 확인%s" % (want, n, (" (host=%s)" % args.host) if args.host else ""))
            break
        if time.time() >= deadline:
            print("아직 %s 데이터 없음%s" % (want, (" (host=%s)" % args.host) if args.host else ""),
                  file=sys.stderr)
            return 2
        time.sleep(10)

    res = ix.query('SELECT last("status_code") AS status, last("status_reason") AS reason, '
                   'last("temperature_c") AS temp_c, last("power_on_hours") AS poh '
                   'FROM "smart_device" WHERE time > now() - 1h%s GROUP BY "host", "device", "model"'
                   % host_cond, db=ix.db)
    if res and res[0].get("series"):
        print("\n최근 1시간 SMART 상태 (status: 0 정상 / 1 경고 / 2 위험)")
        for s in res[0]["series"]:
            t = s.get("tags", {})
            row = dict(zip(s["columns"], s["values"][0]))
            print("  %-14s %-22s %-34s status=%s temp=%s°C poh=%s  %s" % (
                t.get("host", ""), t.get("device", ""), t.get("model", "")[:34], row.get("status"),
                row.get("temp_c"), row.get("poh"), row.get("reason") or ""))
    else:
        print("\nSMART 데이터는 아직 없습니다 (SMART_INTERVAL 경과 후 다시 확인)")
    return 0


# --------------------------------------------------------------------------- #

def main(argv=None):
    ap = argparse.ArgumentParser(description="mirror-disk-health 제어 도구")
    ap.add_argument("-c", "--config", default=os.path.join(HERE, "config.env"))
    sub = ap.add_subparsers(dest="cmd")
    sub.required = True
    sub.add_parser("ping", help="InfluxDB 연결 확인")
    sub.add_parser("influx-init", help="DB/보존정책(1.x) 또는 bucket/DBRP(2.x) 생성")
    vp = sub.add_parser("verify", help="데이터 수신 확인")
    vp.add_argument("--host", default="")
    vp.add_argument("--wait", type=int, default=0, help="데이터가 보일 때까지 기다릴 최대 초")
    vp.add_argument("--measurement", default="smart_device",
                    choices=["smart_device", "diskio", "disk_health_collector"])
    qp = sub.add_parser("query", help="InfluxQL 실행")
    qp.add_argument("influxql")
    qp.add_argument("--write", action="store_true", help="POST 로 실행 (CREATE/DROP 등)")
    args = ap.parse_args(argv)
    cfg = load_env(args.config)
    handler = {"ping": cmd_ping, "influx-init": cmd_influx_init,
               "verify": cmd_verify, "query": cmd_query}[args.cmd]
    try:
        rc = handler(cfg, args)
    except CliError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1
    return rc or 0


if __name__ == "__main__":
    sys.exit(main())
