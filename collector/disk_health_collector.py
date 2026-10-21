#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
disk_health_collector.py
========================

smartmontools(smartctl --json) 결과를 InfluxDB line protocol로 변환해
stdout에 출력하는 수집기. Telegraf inputs.exec (data_format = "influx")에서 실행된다.

출력 measurement
  smart_device        디스크 1개당 1줄 (건강 상태 / 온도 / 에러 카운터 / 수명 / 셀프테스트)
  smart_attribute     ATA SMART 속성 원본 (id별 1줄)               COLLECT_ATTRIBUTES=yes
  zfs_pool            ZFS 풀 상태 / 용량 / 에러 합계                 COLLECT_ZFS=auto|yes
  disk_health_standby 대기(스핀다운) 상태라 조회를 건너뛴 디스크
  disk_health_error   smartctl 이 열지 못한 디스크
  disk_health_collector 수집기 자체 메타(장치 수, 소요 시간, smartctl 버전)

요구사항: Python 3.6+, smartmontools 7.0+ (JSON 출력 지원)
설정 파일: /etc/disk-health/collector.env (KEY=VALUE, 없으면 기본값)

항상 exit 0 으로 끝난다. (Telegraf exec 는 비정상 종료 시 출력 전체를 버리기 때문에,
개별 디스크 오류는 disk_health_error measurement 로 보고한다.)
"""

from __future__ import print_function

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

VERSION = "1.0.0"
DEFAULT_CONFIG = "/etc/disk-health/collector.env"

DEFAULTS = {
    "SMARTCTL": "",              # 비우면 PATH / /usr/sbin 에서 자동 탐색
    "ZPOOL": "",                 # 비우면 자동 탐색
    "USE_SUDO": "auto",          # auto: root 가 아니면 sudo -n 사용
    "SKIP_STANDBY": "yes",       # 스핀다운된 HDD 는 깨우지 않음 (-n standby)
    "EXCLUDE_REGEX": "",         # 장치 이름(/dev/...) 제외 정규식
    "EXTRA_DEVICES": "",         # 스캔으로 안 잡히는 장치. 공백 구분 "경로:타입"  예) /dev/bus/0:megaraid,1
    "SCAN": "yes",               # no 이면 EXTRA_DEVICES 만 조회
    "DEVICE_TIMEOUT": "60",      # 장치당 smartctl 타임아웃(초)
    "MAX_WORKERS": "4",          # 동시 조회 수
    "COLLECT_ATTRIBUTES": "yes", # ATA SMART 속성 원본 수집
    "COLLECT_ZFS": "auto",       # auto: zpool 명령이 있으면 수집
    "TEMP_WARN_HDD": "50",       # 경고 온도(°C)
    "TEMP_WARN_SSD": "65",
    "TEMP_WARN_NVME": "70",
    "WEAR_WARN_PERCENT": "80",   # 수명 사용률(%) 경고 기준
}

# --------------------------------------------------------------------------- #
# 설정
# --------------------------------------------------------------------------- #


def load_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                if key.startswith("export "):
                    key = key[len("export "):].strip()
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                cfg[key] = val
    # 환경변수가 설정 파일보다 우선 (테스트/디버깅 용)
    for key in DEFAULTS:
        env = os.environ.get("DISK_HEALTH_" + key)
        if env is not None:
            cfg[key] = env
    return cfg


def as_bool(val):
    return str(val).strip().lower() in ("1", "yes", "y", "true", "on")


def as_int(val, default):
    try:
        return int(str(val).strip())
    except (TypeError, ValueError):
        return default


def find_binary(explicit, name):
    if explicit:
        return explicit if os.path.exists(explicit) else None
    found = shutil.which(name)
    if found:
        return found
    for d in ("/usr/sbin", "/sbin", "/usr/local/sbin", "/usr/bin", "/usr/local/bin"):
        cand = os.path.join(d, name)
        if os.path.exists(cand):
            return cand
    return None


# --------------------------------------------------------------------------- #
# InfluxDB line protocol
# --------------------------------------------------------------------------- #

_NL = re.compile(r"[\r\n\t]+")


def _esc_key(s):
    s = _NL.sub(" ", str(s)).strip()
    return s.replace("\\", "\\\\").replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ")


def _esc_measurement(s):
    return str(s).replace(",", "\\,").replace(" ", "\\ ")


def _esc_str_field(s):
    s = _NL.sub(" ", str(s))
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def line(measurement, tags, fields):
    """tags: dict(str->str), fields: dict(str->int|float|str|bool). None 값은 제외."""
    flds = []
    for k in sorted(fields):
        v = fields[k]
        if v is None:
            continue
        if isinstance(v, bool):
            flds.append("%s=%s" % (_esc_key(k), "true" if v else "false"))
        elif isinstance(v, int):
            flds.append("%s=%di" % (_esc_key(k), v))
        elif isinstance(v, float):
            if v != v or v in (float("inf"), float("-inf")):
                continue
            flds.append("%s=%s" % (_esc_key(k), repr(v)))
        else:
            flds.append("%s=%s" % (_esc_key(k), _esc_str_field(v)))
    if not flds:
        return None
    tg = []
    for k in sorted(tags):
        v = tags[k]
        if v is None:
            continue
        v = _NL.sub(" ", str(v)).strip()
        if v == "":
            continue  # line protocol 은 빈 태그 값을 허용하지 않음
        tg.append("%s=%s" % (_esc_key(k), _esc_key(v)))
    head = _esc_measurement(measurement)
    if tg:
        head += "," + ",".join(tg)
    return head + " " + ",".join(flds)


# --------------------------------------------------------------------------- #
# smartctl 실행
# --------------------------------------------------------------------------- #


class Runner(object):
    """smartctl / zpool 실행기. 테스트에서는 FixtureRunner 로 대체된다."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.smartctl = find_binary(cfg.get("SMARTCTL"), "smartctl")
        self.zpool = find_binary(cfg.get("ZPOOL"), "zpool")
        mode = str(cfg.get("USE_SUDO", "auto")).lower()
        if mode == "auto":
            self.sudo = hasattr(os, "geteuid") and os.geteuid() != 0
        else:
            self.sudo = as_bool(mode)
        self.timeout = as_int(cfg.get("DEVICE_TIMEOUT"), 60)

    def _exec(self, argv, timeout, sudo=False):
        if sudo:
            argv = ["sudo", "-n"] + argv
        try:
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as exc:
            return 255, "", str(exc)
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return 254, "", "timeout after %ss" % timeout
        return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    def smart(self, args):
        """returns (exit_code, parsed_json_or_None, stderr)"""
        if not self.smartctl:
            return 255, None, "smartctl not found"
        # sudoers 는 "smartctl -j *" 형태만 허용하므로 -j 는 항상 첫 인자
        rc, out, err = self._exec([self.smartctl, "-j"] + args, self.timeout, sudo=self.sudo)
        data = None
        if out.strip():
            try:
                data = json.loads(out)
            except ValueError:
                data = None
        return rc, data, err

    def zpool_cmd(self, args):
        if not self.zpool:
            return 255, "", "zpool not found"
        # OpenZFS(Linux)는 일반 사용자도 zpool list/status 조회가 가능 → sudo 불필요
        return self._exec([self.zpool] + args, 30)


# --------------------------------------------------------------------------- #
# 파싱 헬퍼
# --------------------------------------------------------------------------- #


def dig(obj, *path, **kw):
    default = kw.get("default")
    cur = obj
    for p in path:
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        elif isinstance(cur, list) and isinstance(p, int) and -len(cur) <= p < len(cur):
            cur = cur[p]
        else:
            return default
    return cur


def to_int(v):
    if v is None or isinstance(v, bool):
        return None if v is None else int(v)
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    try:
        return int(str(v).strip().split()[0].replace(",", ""))
    except (ValueError, IndexError):
        return None


def device_tag(name, dtype):
    base = name[5:] if name.startswith("/dev/") else name
    if dtype and ("," in dtype or "+" in dtype):
        # megaraid,0 / sat+megaraid,3 / cciss,1 → 같은 경로 뒤의 여러 디스크를 구분
        base = base + "_" + re.sub(r"[^A-Za-z0-9]+", "", dtype)
    return base.replace("/", "_")


def message_text(data):
    msgs = dig(data, "smartctl", "messages", default=[]) or []
    return " | ".join(str(m.get("string", "")) for m in msgs if isinstance(m, dict))


EXIT_BITS = {
    "exit_cmdline_error": 0,
    "exit_open_failed": 1,
    "exit_cmd_failed": 2,
    "exit_failing_now": 3,       # SMART status: DISK FAILING
    "exit_prefail_now": 4,       # prefail 속성이 임계값 이하 (지금)
    "exit_attr_past_thresh": 5,  # 과거에 임계값 이하였던 속성 존재
    "exit_error_log": 6,         # 장치 에러 로그에 기록 존재
    "exit_selftest_errors": 7,   # 셀프테스트 로그에 실패 기록 존재
}

ATA_ATTR = {
    5: "reallocated_sectors",
    187: "reported_uncorrect",
    188: "command_timeout",
    196: "reallocated_events",
    197: "pending_sectors",
    198: "offline_uncorrectable",
    199: "udma_crc_errors",
    10: "spin_retry_count",
    184: "end_to_end_errors",
}

# 정규화 값(value)이 "남은 수명 %" 를 의미하는 SSD 벤더 속성 (우선순위 순)
ATA_LIFE_LEFT_NORMALIZED = (231, 233, 177, 202, 169, 173)


def ata_attr_table(data):
    return dig(data, "ata_smart_attributes", "table", default=[]) or []


def classify(data, protocol):
    proto = (protocol or dig(data, "device", "protocol") or "").upper()
    if proto == "NVME":
        return "nvme", "NVMe"
    rot = dig(data, "rotation_rate")
    if isinstance(rot, int):
        return ("ssd" if rot == 0 else "hdd"), proto or "ATA"
    # rotation_rate 정보가 없을 때: 이름으로 추정
    model = (dig(data, "model_name") or dig(data, "scsi_model_name") or "").upper()
    if "SSD" in model:
        return "ssd", proto or "ATA"
    return "unknown", proto or "ATA"


def devstat_value(data, page_num, name_contains):
    for page in dig(data, "ata_device_statistics", "pages", default=[]) or []:
        if page.get("number") != page_num:
            continue
        for tbl in page.get("table", []) or []:
            nm = str(tbl.get("name", ""))
            valid = dig(tbl, "flags", "valid", default=True)
            if name_contains.lower() in nm.lower() and valid and "value" in tbl:
                return to_int(tbl.get("value"))
    return None


def ata_selftest(data, poh):
    out = {}
    cur = dig(data, "ata_smart_data", "self_test", "status")
    if isinstance(cur, dict):
        val = to_int(cur.get("value"))
        if val is not None:
            out["selftest_in_progress"] = 1 if (val >> 4) == 15 else 0
            if (val >> 4) == 15:
                out["selftest_remaining_percent"] = to_int(cur.get("remaining_percent"))
    table = dig(data, "ata_smart_self_test_log", "standard", "table", default=None)
    if not table:
        table = dig(data, "ata_smart_self_test_log", "extended", "table", default=None)
    if table:
        last = table[0]
        st = last.get("status", {}) or {}
        out["last_selftest_type"] = str(dig(last, "type", "string", default="") or "")
        out["last_selftest_status"] = str(st.get("string", "") or "")
        passed = st.get("passed")
        if passed is None:
            code = to_int(st.get("value"))
            if code is not None:
                hi = code >> 4
                if hi == 0:
                    passed = True
                elif 3 <= hi <= 8:
                    passed = False
        if passed is not None:
            out["last_selftest_passed"] = 1 if passed else 0
        lh = to_int(last.get("lifetime_hours"))
        if lh is not None and poh is not None:
            # ATA 셀프테스트 로그의 lifetime_hours 는 16bit(최대 65535)라 감길 수 있다
            if poh < 65536:
                if poh >= lh:
                    out["last_selftest_age_hours"] = poh - lh
            else:
                out["last_selftest_age_hours"] = (poh % 65536 - lh) % 65536
    return out


NVME_SELFTEST_FAIL = (5, 6, 7)


def nvme_selftest(data, poh):
    out = {}
    log = dig(data, "nvme_self_test_log")
    if not isinstance(log, dict):
        return out
    cur = to_int(dig(log, "current_self_test_operation", "value"))
    if cur is not None:
        out["selftest_in_progress"] = 1 if cur != 0 else 0
        if cur != 0:
            comp = to_int(dig(log, "current_self_test_completion_percent"))
            if comp is not None:
                out["selftest_remaining_percent"] = max(0, 100 - comp)
    table = log.get("table") or []
    if table:
        last = table[0]
        res = to_int(dig(last, "self_test_result", "value"))
        out["last_selftest_type"] = str(dig(last, "self_test_code", "string", default="") or "")
        out["last_selftest_status"] = str(dig(last, "self_test_result", "string", default="") or "")
        if res == 0:
            out["last_selftest_passed"] = 1
        elif res in NVME_SELFTEST_FAIL:
            out["last_selftest_passed"] = 0
        lh = to_int(last.get("power_on_hours"))
        if lh is not None and poh is not None and poh >= lh:
            out["last_selftest_age_hours"] = poh - lh
    return out


# --------------------------------------------------------------------------- #
# 디스크 1개 → metrics
# --------------------------------------------------------------------------- #


def build_device_metrics(name, dtype, protocol, rc, data, cfg):
    """returns dict(kind=..., lines=[...], serial=..., wwn=...)"""
    dev = device_tag(name, dtype)
    msg = message_text(data) if data else ""

    # 대기(스핀다운) 상태
    if data is not None and re.search(r"\b(STANDBY|SLEEP)\b", msg, re.I) and (rc & 0x2):
        return {"kind": "standby", "lines": [line("disk_health_standby", {"device": dev}, {"standby": 1})]}

    # 장치 열기 실패 / JSON 없음
    if data is None or (rc & 0x3 and not dig(data, "smart_status") and not dig(data, "model_name")
                        and not dig(data, "scsi_model_name") and not dig(data, "serial_number")):
        return {"kind": "error", "lines": [line("disk_health_error", {"device": dev, "dev_type": dtype},
                                                 {"exit_status": rc,
                                                  "message": (msg or "no JSON output from smartctl")[:500]})]}

    kind, proto = classify(data, protocol)
    model = dig(data, "model_name") or " ".join(
        x for x in (dig(data, "scsi_vendor"), dig(data, "scsi_product")) if x) or dig(data, "scsi_model_name") or ""
    serial = str(dig(data, "serial_number") or "")
    wwn_obj = dig(data, "wwn")
    wwn = ""
    if isinstance(wwn_obj, dict) and "id" in wwn_obj:
        wwn = "%x-%06x-%09x" % (to_int(wwn_obj.get("naa")) or 0, to_int(wwn_obj.get("oui")) or 0,
                                to_int(wwn_obj.get("id")) or 0)

    tags = {"device": dev, "dev_type": dtype, "protocol": proto, "kind": kind,
            "model": model, "serial": serial}
    f = {}
    f["smartctl_exit"] = rc
    for key, bit in EXIT_BITS.items():
        if bit >= 2:
            f[key] = 1 if rc & (1 << bit) else 0
    f["firmware"] = str(dig(data, "firmware_version") or dig(data, "scsi_revision") or "")
    if wwn:
        f["wwn"] = wwn

    passed = dig(data, "smart_status", "passed")
    if isinstance(passed, bool):
        f["health_ok"] = 1 if passed else 0
    f["smart_enabled"] = None
    se = dig(data, "smart_support", "enabled")
    if isinstance(se, bool):
        f["smart_enabled"] = 1 if se else 0

    cap = to_int(dig(data, "user_capacity", "bytes")) or to_int(dig(data, "nvme_total_capacity"))
    f["capacity_bytes"] = cap
    f["temperature_c"] = to_int(dig(data, "temperature", "current"))
    poh = to_int(dig(data, "power_on_time", "hours"))
    f["power_on_hours"] = poh
    f["power_cycles"] = to_int(dig(data, "power_cycle_count"))
    rot = dig(data, "rotation_rate")
    if isinstance(rot, int):
        f["rotation_rpm"] = rot

    life_used = to_int(dig(data, "endurance_used", "current_percent"))
    attrs_lines = []
    sbytes = to_int(dig(data, "logical_block_size")) or 512

    pu = proto.upper()
    if pu in ("ATA", "SAT"):
        table = ata_attr_table(data)
        by_id = {}
        for a in table:
            aid = to_int(a.get("id"))
            if aid is None:
                continue
            by_id[aid] = a
            if as_bool(cfg.get("COLLECT_ATTRIBUTES", "yes")):
                flags = a.get("flags", {}) or {}
                attrs_lines.append(line("smart_attribute", {
                    "device": dev, "serial": serial, "model": model, "kind": kind,
                    "attr_id": str(aid), "attr_name": a.get("name", ""),
                }, {
                    "value": to_int(a.get("value")),
                    "worst": to_int(a.get("worst")),
                    "thresh": to_int(a.get("thresh")),
                    "raw": to_int(dig(a, "raw", "value")),
                    "prefailure": 1 if flags.get("prefailure") else 0,
                    "failing_now": 1 if str(a.get("when_failed", "")) == "now" else 0,
                    "failed_past": 1 if str(a.get("when_failed", "")) == "past" else 0,
                }))
        failing_now = [str(a.get("name") or a.get("id")) for a in table if str(a.get("when_failed", "")) == "now"]
        f["attrs_failing_now"] = len(failing_now)
        if failing_now:
            f["attrs_failing_names"] = ",".join(failing_now)[:200]
        for aid, fname in ATA_ATTR.items():
            if aid in by_id:
                raw = to_int(dig(by_id[aid], "raw", "value"))
                if raw is not None and aid == 188:
                    raw &= 0xFFFF  # Seagate 등은 상위 비트에 다른 카운터가 패킹됨
                f[fname] = raw
        if life_used is None:
            life_used = devstat_value(data, 7, "Percentage Used Endurance")
        if life_used is None and kind == "ssd":
            for aid in ATA_LIFE_LEFT_NORMALIZED:
                if aid in by_id:
                    v = to_int(by_id[aid].get("value"))
                    if v is not None and 0 <= v <= 100:
                        life_used = 100 - v
                        break
        lsw = devstat_value(data, 1, "Logical Sectors Written")
        lsr = devstat_value(data, 1, "Logical Sectors Read")
        if lsw is not None:
            f["bytes_written"] = lsw * sbytes
        if lsr is not None:
            f["bytes_read"] = lsr * sbytes
        f["error_log_count"] = to_int(dig(data, "ata_smart_error_log", "extended", "count")) \
            if dig(data, "ata_smart_error_log", "extended", "count") is not None \
            else to_int(dig(data, "ata_smart_error_log", "summary", "count"))
        f.update(ata_selftest(data, poh))

    elif pu == "NVME":
        h = dig(data, "nvme_smart_health_information_log", default={}) or {}
        f["critical_warning"] = to_int(h.get("critical_warning"))
        f["available_spare"] = to_int(h.get("available_spare"))
        f["available_spare_threshold"] = to_int(h.get("available_spare_threshold"))
        if life_used is None:
            life_used = to_int(h.get("percentage_used"))
        duw = to_int(h.get("data_units_written"))
        dur = to_int(h.get("data_units_read"))
        f["bytes_written"] = duw * 512000 if duw is not None else None   # 1 data unit = 1000 * 512B
        f["bytes_read"] = dur * 512000 if dur is not None else None
        f["media_errors"] = to_int(h.get("media_errors"))
        f["error_log_count"] = to_int(h.get("num_err_log_entries"))
        f["unsafe_shutdowns"] = to_int(h.get("unsafe_shutdowns"))
        f["warning_temp_minutes"] = to_int(h.get("warning_temp_time"))
        f["critical_temp_minutes"] = to_int(h.get("critical_comp_time"))
        f["controller_busy_minutes"] = to_int(h.get("controller_busy_time"))
        if f["temperature_c"] is None:
            f["temperature_c"] = to_int(h.get("temperature"))
        if f["power_on_hours"] is None:
            poh = to_int(h.get("power_on_hours"))
            f["power_on_hours"] = poh
        if f["power_cycles"] is None:
            f["power_cycles"] = to_int(h.get("power_cycles"))
        f.update(nvme_selftest(data, poh))

    elif pu == "SCSI":
        f["grown_defects"] = to_int(dig(data, "scsi_grown_defect_list"))
        rd = to_int(dig(data, "scsi_error_counter_log", "read", "total_uncorrected_errors"))
        wr = to_int(dig(data, "scsi_error_counter_log", "write", "total_uncorrected_errors"))
        vf = to_int(dig(data, "scsi_error_counter_log", "verify", "total_uncorrected_errors"))
        f["read_uncorrected"] = rd
        f["write_uncorrected"] = wr
        f["verify_uncorrected"] = vf
        gb = dig(data, "scsi_error_counter_log", "read", "gigabytes_processed")
        try:
            f["bytes_read"] = int(float(gb) * 1e9) if gb is not None else None
        except (TypeError, ValueError):
            pass
        gb = dig(data, "scsi_error_counter_log", "write", "gigabytes_processed")
        try:
            f["bytes_written"] = int(float(gb) * 1e9) if gb is not None else None
        except (TypeError, ValueError):
            pass
        if life_used is None:
            life_used = to_int(dig(data, "scsi_percentage_used_endurance_indicator"))
        if f["power_cycles"] is None:
            f["power_cycles"] = to_int(dig(data, "scsi_start_stop_cycle_counter", "accumulated_start_stop_cycles"))
        # SCSI 셀프테스트 결과: scsi_self_test_0
        st0 = dig(data, "scsi_self_test_0")
        if isinstance(st0, dict):
            code = to_int(dig(st0, "result", "value"))
            f["last_selftest_type"] = str(dig(st0, "code", "string", default="") or "")
            f["last_selftest_status"] = str(dig(st0, "result", "string", default="") or "")
            if code == 0:
                f["last_selftest_passed"] = 1
            elif code is not None and 3 <= code <= 7:
                f["last_selftest_passed"] = 0
            lh = to_int(dig(st0, "power_on_time", "hours"))
            if lh is not None and poh is not None and poh >= lh:
                f["last_selftest_age_hours"] = poh - lh

    if life_used is not None:
        life_used = max(0, min(life_used, 255))
        f["life_used_percent"] = life_used
        f["life_remaining_percent"] = max(0, 100 - life_used)

    status, reasons = evaluate(f, kind, cfg)
    f["status_code"] = status
    f["status_reason"] = ", ".join(reasons) if reasons else "OK"

    lines = [line("smart_device", tags, f)] + attrs_lines
    return {"kind": "ok", "lines": [l for l in lines if l], "serial": serial, "wwn": wwn,
            "status": status}


def evaluate(f, kind, cfg):
    """0 = 정상, 1 = 경고, 2 = 위험"""
    crit, warn = [], []

    def gt0(key):
        v = f.get(key)
        return v is not None and v > 0

    if f.get("health_ok") == 0:
        crit.append("SMART FAILED")
    if f.get("exit_failing_now"):
        crit.append("disk failing")
    if f.get("exit_prefail_now"):
        crit.append("prefail attribute below threshold")
    if gt0("attrs_failing_now"):
        crit.append("attribute failing now: %s" % f.get("attrs_failing_names", ""))
    if gt0("pending_sectors"):
        crit.append("pending sectors %d" % f["pending_sectors"])
    if gt0("offline_uncorrectable"):
        crit.append("offline uncorrectable %d" % f["offline_uncorrectable"])
    if gt0("critical_warning"):
        crit.append("NVMe critical warning 0x%x" % f["critical_warning"])
    if gt0("media_errors"):
        crit.append("NVMe media errors %d" % f["media_errors"])
    sp, spt = f.get("available_spare"), f.get("available_spare_threshold")
    if sp is not None and spt is not None and sp <= spt:
        crit.append("available spare %d%% <= %d%%" % (sp, spt))
    if f.get("last_selftest_passed") == 0:
        crit.append("last self-test failed")
    lu = f.get("life_used_percent")
    if lu is not None and lu >= 100:
        crit.append("endurance exhausted %d%%" % lu)

    if gt0("reallocated_sectors"):
        warn.append("reallocated %d" % f["reallocated_sectors"])
    if gt0("reported_uncorrect"):
        warn.append("reported uncorrect %d" % f["reported_uncorrect"])
    if gt0("udma_crc_errors"):
        warn.append("CRC errors %d (cable/backplane)" % f["udma_crc_errors"])
    if gt0("grown_defects"):
        warn.append("grown defects %d" % f["grown_defects"])
    for k in ("read_uncorrected", "write_uncorrected", "verify_uncorrected"):
        if gt0(k):
            warn.append("%s %d" % (k.replace("_", " "), f[k]))
    if gt0("end_to_end_errors"):
        warn.append("end-to-end errors %d" % f["end_to_end_errors"])
    if f.get("exit_attr_past_thresh"):
        warn.append("attribute was below threshold in the past")
    if lu is not None and as_int(cfg.get("WEAR_WARN_PERCENT"), 80) <= lu < 100:
        warn.append("endurance used %d%%" % lu)
    t = f.get("temperature_c")
    if t is not None:
        limit = {"hdd": "TEMP_WARN_HDD", "ssd": "TEMP_WARN_SSD", "nvme": "TEMP_WARN_NVME"}.get(kind, "TEMP_WARN_HDD")
        if t >= as_int(cfg.get(limit), 55):
            warn.append("temperature %d°C" % t)

    if crit:
        return 2, crit + warn
    if warn:
        return 1, warn
    return 0, []


# --------------------------------------------------------------------------- #
# ZFS
# --------------------------------------------------------------------------- #

ZFS_STATE = {"ONLINE": 0, "DEGRADED": 1, "FAULTED": 2, "OFFLINE": 3, "UNAVAIL": 4, "REMOVED": 5, "SUSPENDED": 6}
_ZVDEV = re.compile(r"^\s+(\S+)\s+(ONLINE|DEGRADED|FAULTED|OFFLINE|UNAVAIL|REMOVED|AVAIL|INUSE)\s+(\d+)\s+(\d+)\s+(\d+)")


def parse_zpool_status(text):
    """zpool status -p 출력 → {pool: {read, write, cksum, faulted_vdevs, scan}}"""
    res = {}
    pool = None
    in_config = False
    for raw in text.splitlines():
        m = re.match(r"^\s*pool:\s*(\S+)", raw)
        if m:
            pool = m.group(1)
            res[pool] = {"read_errors": 0, "write_errors": 0, "checksum_errors": 0, "vdevs_not_online": 0,
                         "scrub_in_progress": 0, "resilver_in_progress": 0}
            in_config = False
            continue
        if pool is None:
            continue
        if re.match(r"^\s*scan:", raw):
            low = raw.lower()
            if "scrub in progress" in low:
                res[pool]["scrub_in_progress"] = 1
            if "resilver in progress" in low:
                res[pool]["resilver_in_progress"] = 1
            if "scrub repaired" in low:
                res[pool]["last_scrub"] = raw.split(":", 1)[1].strip()
        if re.match(r"^\s*config:", raw):
            in_config = True
            continue
        if re.match(r"^\s*errors:", raw):
            in_config = False
            continue
        if in_config:
            m = _ZVDEV.match(raw)
            if m and m.group(1) not in ("NAME",):
                name, state = m.group(1), m.group(2)
                rd, wr, ck = int(m.group(3)), int(m.group(4)), int(m.group(5))
                if name == pool:
                    # 풀 루트 라인은 하위 vdev 합계와 중복이므로 별도 저장만
                    continue
                res[pool]["read_errors"] += rd
                res[pool]["write_errors"] += wr
                res[pool]["checksum_errors"] += ck
                if state not in ("ONLINE", "AVAIL", "INUSE"):
                    res[pool]["vdevs_not_online"] += 1
    return res


def collect_zfs(runner):
    lines = []
    rc, out, err = runner.zpool_cmd(["list", "-H", "-p", "-o", "name,size,alloc,free,frag,cap,health"])
    if rc != 0:
        return lines
    pools = {}
    for raw in out.splitlines():
        parts = raw.split("\t")
        if len(parts) < 7:
            continue
        name, size, alloc, free, frag, cap, health = parts[:7]
        pools[name] = {"size": to_int(size), "alloc": to_int(alloc), "free": to_int(free),
                       "frag": to_int(frag.rstrip("%")) if frag not in ("-", "") else None,
                       "cap": to_int(cap.rstrip("%")), "health": health.strip()}
    if not pools:
        return lines
    rc2, out2, _ = runner.zpool_cmd(["status", "-p"])
    status = parse_zpool_status(out2) if rc2 == 0 else {}
    for name, p in sorted(pools.items()):
        st = status.get(name, {})
        state = ZFS_STATE.get(p["health"], 9)
        errs = st.get("read_errors", 0) + st.get("write_errors", 0) + st.get("checksum_errors", 0)
        status_code = 2 if state != 0 else (1 if errs > 0 else 0)
        fields = {
            "size_bytes": p["size"], "alloc_bytes": p["alloc"], "free_bytes": p["free"],
            "fragmentation_percent": p["frag"], "capacity_percent": p["cap"],
            "health_ok": 1 if state == 0 else 0, "state_code": state, "health": p["health"],
            "status_code": status_code,
        }
        for k in ("read_errors", "write_errors", "checksum_errors", "vdevs_not_online",
                  "scrub_in_progress", "resilver_in_progress"):
            if k in st:
                fields[k] = st[k]
        if st.get("last_scrub"):
            fields["last_scrub"] = st["last_scrub"]
        lines.append(line("zfs_pool", {"pool": name}, fields))
    return [l for l in lines if l]


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def discover(runner, cfg):
    devices = []
    errors = []
    if as_bool(cfg.get("SCAN", "yes")):
        rc, data, err = runner.smart(["--scan-open"])
        for d in dig(data, "devices", default=[]) or []:
            if d.get("open_error"):
                errors.append((d.get("name", "?"), d.get("type", ""), str(d.get("open_error"))))
                continue
            devices.append((d.get("name"), d.get("type", "auto"), d.get("protocol", "")))
        if data is None:
            errors.append(("scan", "", (err or "smartctl --scan-open failed").strip()[:300]))
    for item in str(cfg.get("EXTRA_DEVICES", "")).split():
        if ":" in item:
            name, dtype = item.split(":", 1)
        else:
            name, dtype = item, "auto"
        if not any(n == name and t == dtype for n, t, _ in devices):
            devices.append((name, dtype, ""))
    rx = cfg.get("EXCLUDE_REGEX", "")
    if rx:
        pat = re.compile(rx)
        devices = [d for d in devices if not pat.search(d[0])]
    return devices, errors


def query_device(runner, cfg, name, dtype, protocol):
    is_ata = (protocol or "").upper() == "ATA"
    args = ["-a"]
    if is_ata:
        # Device Statistics: 수명 사용률 / 누적 읽기·쓰기량 (ATA 전용)
        args += ["-l", "devstat"]
    if as_bool(cfg.get("SKIP_STANDBY", "yes")):
        args += ["-n", "standby"]
    if dtype and dtype != "auto":
        args += ["-d", dtype]
    args.append(name)
    rc, data, err = runner.smart(args)
    if data is None and is_ata and rc != 0:
        # -l devstat 을 이해하지 못하는 오래된 smartmontools 대비 재시도
        args = [a for a in args if a not in ("-l", "devstat")]
        rc, data, err = runner.smart(args)
    return build_device_metrics(name, dtype, protocol, rc, data, cfg)


def collect(runner, cfg):
    t0 = time.time()
    out = []
    devices, scan_errors = discover(runner, cfg)
    for name, dtype, msg in scan_errors:
        out.append(line("disk_health_error", {"device": device_tag(name, dtype), "dev_type": dtype},
                        {"exit_status": 2, "message": msg[:500]}))

    workers = max(1, as_int(cfg.get("MAX_WORKERS"), 4))
    results = []
    if devices:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(query_device, runner, cfg, n, t, p) for n, t, p in devices]
            for fut in futs:
                try:
                    results.append(fut.result())
                except Exception as exc:  # 수집기 전체가 죽지 않도록
                    results.append({"kind": "error", "lines": [line("disk_health_error", {"device": "unknown"},
                                                                     {"exit_status": 255, "message": str(exc)[:500]})]})

    seen = set()
    counts = {"ok": 0, "standby": 0, "error": len(scan_errors), "warn": 0, "crit": 0, "dup": 0}
    for r in results:
        if r["kind"] == "ok":
            ident = (r.get("serial") or "", r.get("wwn") or "")
            if ident != ("", "") and ident in seen:
                counts["dup"] += 1
                continue
            seen.add(ident)
            counts["ok"] += 1
            if r.get("status") == 1:
                counts["warn"] += 1
            elif r.get("status") == 2:
                counts["crit"] += 1
        else:
            counts[r["kind"]] += 1
        out.extend(r["lines"])

    zfs_mode = str(cfg.get("COLLECT_ZFS", "auto")).lower()
    if zfs_mode == "yes" or (zfs_mode == "auto" and runner.zpool):
        try:
            out.extend(collect_zfs(runner))
        except Exception as exc:
            out.append(line("disk_health_error", {"device": "zfs"}, {"exit_status": 255, "message": str(exc)[:500]}))

    rcv, vdata, _ = runner.smart(["--version"])
    sver = ""
    if isinstance(vdata, dict):
        ver = dig(vdata, "smartctl", "version")
        if isinstance(ver, list):
            sver = ".".join(str(x) for x in ver)
    out.append(line("disk_health_collector", {}, {
        "devices_ok": counts["ok"], "devices_standby": counts["standby"], "devices_error": counts["error"],
        "devices_warning": counts["warn"], "devices_critical": counts["crit"],
        "duration_ms": int((time.time() - t0) * 1000),
        "smartctl_version": sver, "collector_version": VERSION,
    }))
    return [l for l in out if l]


def main(argv=None):
    ap = argparse.ArgumentParser(description="SMART → InfluxDB line protocol collector")
    ap.add_argument("-c", "--config", default=os.environ.get("DISK_HEALTH_CONFIG", DEFAULT_CONFIG))
    ap.add_argument("--version", action="version", version=VERSION)
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    runner = Runner(cfg)
    try:
        lines = collect(runner, cfg)
    except Exception as exc:  # 최후의 안전망: 항상 0 으로 종료
        lines = [line("disk_health_error", {"device": "collector"}, {"exit_status": 255, "message": str(exc)[:500]})]
    sys.stdout.write("\n".join(lines) + ("\n" if lines else ""))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
