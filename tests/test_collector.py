#!/usr/bin/env python3
"""수집기 단위 테스트 — 실제 smartctl 대신 tests/fixtures 의 JSON 을 사용한다.

    python3 -m unittest discover -s tests -v
"""
import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "collector"))
import disk_health_collector as c  # noqa: E402

FIX = os.path.join(HERE, "fixtures")

DEVICE_FIXTURE = {
    "/dev/sda": "sda.json", "/dev/sdb": "sdb.json", "/dev/sdc": "sdc.json", "/dev/sdd": "sdd.json",
    "/dev/sdf": "sdf.json", "/dev/nvme0": "nvme0.json", "/dev/bus/0": "megaraid1.json",
}


class FixtureRunner(c.Runner):
    def __init__(self, cfg, zfs=True):
        self.cfg = cfg
        self.smartctl = "/usr/sbin/smartctl"
        self.zpool = "/usr/sbin/zpool" if zfs else None
        self.sudo = False
        self.timeout = 5
        self.calls = []

    def smart(self, args):
        self.calls.append(list(args))
        if args == ["--scan-open"]:
            return 0, self._load("scan.json"), ""
        if args == ["--version"]:
            return 0, self._load("version.json"), ""
        data = self._load(DEVICE_FIXTURE[args[-1]])
        return data["smartctl"]["exit_status"], data, ""

    def zpool_cmd(self, args):
        name = "zpool_list.txt" if args[0] == "list" else "zpool_status.txt"
        with open(os.path.join(FIX, name)) as fh:
            return 0, fh.read(), ""

    @staticmethod
    def _load(name):
        with open(os.path.join(FIX, name)) as fh:
            return json.load(fh)


# --- 엄격한 line protocol 파서 (테스트용) ---------------------------------- #
_TOKEN = re.compile(r'(?:\\.|[^ ,="\\])+')


def parse_line(s):
    """InfluxDB line protocol 1줄을 (measurement, tags, fields) 로 파싱. 형식 오류 시 AssertionError."""
    i = 0

    def take_key(stop):
        nonlocal i
        j = i
        while i < len(s) and s[i] not in stop:
            if s[i] == "\\":
                i += 2
            else:
                i += 1
        return s[j:i]

    meas = take_key(", ")
    assert meas, "empty measurement: %r" % s
    tags = {}
    while i < len(s) and s[i] == ",":
        i += 1
        k = take_key("=")
        assert s[i] == "=", s
        i += 1
        v = take_key(", ")
        assert k and v, "empty tag key/value in %r" % s
        tags[k] = v
    assert s[i] == " ", "expected space after tags: %r" % s
    i += 1
    fields = {}
    while True:
        k = take_key("=")
        assert s[i] == "=", s
        i += 1
        if s[i] == '"':
            i += 1
            buf = []
            while s[i] != '"':
                if s[i] == "\\":
                    buf.append(s[i + 1])
                    i += 2
                else:
                    buf.append(s[i])
                    i += 1
            i += 1
            fields[k] = "".join(buf)
        else:
            j = i
            while i < len(s) and s[i] not in ", ":
                i += 1
            raw = s[j:i]
            if re.fullmatch(r"-?\d+i", raw):
                fields[k] = int(raw[:-1])
            elif raw in ("true", "false"):
                fields[k] = raw == "true"
            else:
                fields[k] = float(raw)
        if i >= len(s):
            break
        assert s[i] == ",", "unexpected %r at %d in %r" % (s[i], i, s)
        i += 1
    return meas, tags, fields


def unesc(v):
    return re.sub(r"\\(.)", r"\1", v)


class CollectorTest(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(c.DEFAULTS)
        self.runner = FixtureRunner(self.cfg)
        self.lines = c.collect(self.runner, self.cfg)
        self.parsed = [parse_line(l) for l in self.lines]

    def devices(self):
        return {unesc(t["device"]): f for m, t, f in self.parsed if m == "smart_device"}

    def tags_of(self, dev):
        for m, t, f in self.parsed:
            if m == "smart_device" and unesc(t["device"]) == dev:
                return {k: unesc(v) for k, v in t.items()}

    def test_all_lines_parse(self):
        self.assertTrue(self.lines)
        for l in self.lines:
            self.assertNotIn("\n", l)

    def test_device_set(self):
        self.assertEqual(sorted(self.devices()), ["bus_0_satmegaraid1", "nvme0", "sda", "sdb", "sdc"])

    def test_hdd(self):
        f = self.devices()["sda"]
        self.assertEqual(f["health_ok"], 1)
        self.assertEqual(f["temperature_c"], 41)
        self.assertEqual(f["power_on_hours"], 30123)
        self.assertEqual(f["reallocated_sectors"], 8)
        self.assertEqual(f["pending_sectors"], 0)
        self.assertEqual(f["last_selftest_passed"], 1)
        self.assertEqual(f["last_selftest_age_hours"], 23)
        self.assertEqual(f["bytes_written"], 123456789000 * 512)
        self.assertEqual(f["status_code"], 1)  # reallocated > 0 → 경고
        self.assertIn("reallocated 8", f["status_reason"])
        t = self.tags_of("sda")
        self.assertEqual(t["kind"], "hdd")
        self.assertEqual(t["model"], "WDC WUH721818ALE6L4")
        self.assertNotIn("life_used_percent", f)

    def test_ssd_devstat_preferred_over_attribute(self):
        f = self.devices()["sdb"]
        self.assertEqual(self.tags_of("sdb")["kind"], "ssd")
        self.assertEqual(f["life_used_percent"], 4)          # devstat 우선 (177 기준이면 5)
        self.assertEqual(f["life_remaining_percent"], 96)
        self.assertEqual(f["selftest_in_progress"], 1)
        self.assertEqual(f["selftest_remaining_percent"], 90)
        self.assertEqual(f["udma_crc_errors"], 3)
        self.assertEqual(f["status_code"], 1)

    def test_ssd_attribute_fallback(self):
        data = FixtureRunner._load("sdb.json")
        del data["ata_device_statistics"]
        r = c.build_device_metrics("/dev/sdb", "sat", "ATA", 0, data, self.cfg)
        f = parse_line(r["lines"][0])[2]
        self.assertEqual(f["life_used_percent"], 5)   # 177 normalized 95 → 5% 사용

    def test_nvme(self):
        f = self.devices()["nvme0"]
        self.assertEqual(self.tags_of("nvme0")["kind"], "nvme")
        self.assertEqual(f["life_used_percent"], 3)
        self.assertEqual(f["available_spare"], 100)
        self.assertEqual(f["bytes_written"], 987654321 * 512000)
        self.assertEqual(f["error_log_count"], 77)
        self.assertEqual(f["last_selftest_passed"], 1)
        self.assertEqual(f["last_selftest_age_hours"], 10)
        self.assertEqual(f["status_code"], 0)  # num_err_log_entries 는 상태에 반영하지 않음
        self.assertEqual(f["status_reason"], "OK")

    def test_sas(self):
        f = self.devices()["sdc"]
        t = self.tags_of("sdc")
        self.assertEqual(t["protocol"], "SCSI")
        self.assertEqual(t["model"], "SEAGATE ST4000NM0023")
        self.assertEqual(f["grown_defects"], 2)
        self.assertEqual(f["power_on_hours"], 51000)
        self.assertEqual(f["bytes_read"], 123456789000000)  # 123456.789 GB
        self.assertEqual(f["last_selftest_passed"], 1)
        self.assertEqual(f["status_code"], 1)

    def test_failing_megaraid(self):
        f = self.devices()["bus_0_satmegaraid1"]
        t = self.tags_of("bus_0_satmegaraid1")
        self.assertEqual(t["dev_type"], "sat+megaraid,1")
        self.assertEqual(f["health_ok"], 0)
        self.assertEqual(f["exit_failing_now"], 1)
        self.assertEqual(f["exit_error_log"], 1)
        self.assertEqual(f["exit_selftest_errors"], 1)
        self.assertEqual(f["pending_sectors"], 16)
        self.assertEqual(f["command_timeout"], 4295032833 & 0xFFFF)
        self.assertEqual(f["last_selftest_passed"], 0)
        self.assertEqual(f["error_log_count"], 12)
        self.assertEqual(f["status_code"], 2)
        self.assertTrue(f["status_reason"].startswith("SMART FAILED"))
        self.assertEqual(f["attrs_failing_now"], 1)
        self.assertIn("attribute failing now: Reallocated_Sector_Ct", f["status_reason"])
        self.assertEqual(self.devices()["sda"]["attrs_failing_now"], 0)
        attrs = [(t2, f2) for m, t2, f2 in self.parsed if m == "smart_attribute"
                 and unesc(t2["device"]) == "bus_0_satmegaraid1" and t2["attr_id"] == "5"]
        self.assertEqual(len(attrs), 1)
        self.assertEqual(attrs[0][1]["failing_now"], 1)

    def test_standby_and_errors(self):
        sb = [unesc(t["device"]) for m, t, f in self.parsed if m == "disk_health_standby"]
        self.assertEqual(sb, ["sdd"])
        errs = sorted(unesc(t["device"]) for m, t, f in self.parsed if m == "disk_health_error")
        self.assertEqual(errs, ["sde", "sdf"])

    def test_collector_meta(self):
        meta = [f for m, t, f in self.parsed if m == "disk_health_collector"]
        self.assertEqual(len(meta), 1)
        m = meta[0]
        self.assertEqual(m["devices_ok"], 5)
        self.assertEqual(m["devices_standby"], 1)
        self.assertEqual(m["devices_error"], 2)
        self.assertEqual(m["devices_critical"], 1)
        self.assertEqual(m["devices_warning"], 3)
        self.assertEqual(m["smartctl_version"], "7.4")

    def test_zfs(self):
        pools = {unesc(t["pool"]): f for m, t, f in self.parsed if m == "zfs_pool"}
        self.assertEqual(set(pools), {"tank", "rpool"})
        self.assertEqual(pools["rpool"]["status_code"], 0)
        self.assertEqual(pools["rpool"]["capacity_percent"], 20)
        tank = pools["tank"]
        self.assertEqual(tank["health_ok"], 0)
        self.assertEqual(tank["status_code"], 2)
        self.assertEqual(tank["read_errors"], 3)
        self.assertEqual(tank["write_errors"], 12)
        self.assertEqual(tank["checksum_errors"], 5)
        self.assertEqual(tank["vdevs_not_online"], 2)  # raidz2-0 DEGRADED + FAULTED 디스크
        self.assertEqual(tank["resilver_in_progress"], 1)

    def test_command_args(self):
        sda = [a for a in self.runner.calls if a and a[-1] == "/dev/sda"][0]
        self.assertEqual(sda, ["-a", "-l", "devstat", "-n", "standby", "-d", "sat", "/dev/sda"])
        nv = [a for a in self.runner.calls if a and a[-1] == "/dev/nvme0"][0]
        self.assertNotIn("devstat", nv)

    def test_exclude_and_extra(self):
        cfg = dict(c.DEFAULTS, EXCLUDE_REGEX=r"^/dev/(sd[c-f]|bus)", EXTRA_DEVICES="/dev/sda:sat /dev/nvme0:nvme")
        lines = c.collect(FixtureRunner(cfg, zfs=False), cfg)
        devs = sorted(unesc(parse_line(l)[1]["device"]) for l in lines if l.startswith("smart_device,"))
        self.assertEqual(devs, ["nvme0", "sda", "sdb"])  # 중복 장치는 제거

    def test_field_types_stable(self):
        # 같은 필드가 디스크마다 다른 타입(int/str)이면 InfluxDB 가 쓰기를 거부한다
        types = {}
        for m, t, f in self.parsed:
            for k, v in f.items():
                key = (m, k)
                types.setdefault(key, set()).add(type(v).__name__)
        bad = {k: v for k, v in types.items() if len(v) > 1}
        self.assertEqual(bad, {})

    def test_escaping(self):
        l = c.line("m", {"model": "A B,C=D"}, {"s": 'x "y" \\z', "n": 1})
        meas, tags, fields = parse_line(l)
        self.assertEqual(unesc(tags["model"]), "A B,C=D")
        self.assertEqual(fields["s"], 'x "y" \\z')

    def test_main_never_fails(self):
        cfg = dict(c.DEFAULTS, SMARTCTL="/nonexistent/smartctl", ZPOOL="/nonexistent/zpool")
        lines = c.collect(c.Runner(cfg), cfg)
        meas = sorted(parse_line(l)[0] for l in lines)
        self.assertIn("disk_health_collector", meas)
        self.assertIn("disk_health_error", meas)


if __name__ == "__main__":
    unittest.main()
