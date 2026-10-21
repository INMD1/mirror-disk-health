#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""grafana/disk-health.json 생성기.

    python3 grafana/build_dashboard.py          # disk-health.json 재생성

대시보드를 직접 수정하기보다 이 파일을 고치고 다시 생성하는 것을 권장한다.
모든 쿼리는 InfluxQL(raw) 이며 tests/influxql_check 로 문법 검증한다.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "disk-health.json")

DS = {"type": "influxdb", "uid": "${ds}"}
H = '"host" =~ /^$host$/'
D = '"device" =~ /^$device$/'
SMART_GROUP = 'GROUP BY "host", "device", "model", "serial", "kind"'

_id = [0]


def nid():
    _id[0] += 1
    return _id[0]


def target(q, ref="A", fmt="time_series", alias=None):
    t = {"refId": ref, "datasource": DS, "query": q, "rawQuery": True, "resultFormat": fmt}
    if alias:
        t["alias"] = alias
    return t


def grid(x, y, w, h):
    return {"x": x, "y": y, "w": w, "h": h}


def thresholds(*steps):
    """steps: (value or None, color)"""
    return {"mode": "absolute", "steps": [{"value": v, "color": c} for v, c in steps]}


STATUS_MAP = [{"type": "value", "options": {
    "0": {"text": "정상", "color": "green", "index": 0},
    "1": {"text": "경고", "color": "yellow", "index": 1},
    "2": {"text": "위험", "color": "red", "index": 2}}}]

SELFTEST_MAP = [{"type": "value", "options": {
    "1": {"text": "PASS", "color": "green", "index": 0},
    "0": {"text": "FAIL", "color": "red", "index": 1}}}]


def row(title, y, collapsed=False, panels=None):
    r = {"id": nid(), "type": "row", "title": title, "gridPos": grid(0, y, 24, 1), "collapsed": collapsed,
         "panels": panels or []}
    return r


def stat(title, q, pos, unit="none", th=None, mappings=None, desc="", color_mode="background", decimals=None):
    p = {
        "id": nid(), "type": "stat", "title": title, "description": desc, "datasource": DS, "gridPos": pos,
        "targets": [target(q)],
        "fieldConfig": {"defaults": {"unit": unit, "thresholds": th or thresholds((None, "green")),
                                     "mappings": mappings or [], "color": {"mode": "thresholds"},
                                     "noValue": "0"},
                        "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": color_mode, "graphMode": "none", "textMode": "value",
                    "justifyMode": "center", "orientation": "auto"},
    }
    if decimals is not None:
        p["fieldConfig"]["defaults"]["decimals"] = decimals
    return p


def timeseries(title, targets, pos, unit="none", desc="", th=None, min_=None, max_=None, interval=None,
               draw="line", time_from=None, stack=False):
    custom = {"drawStyle": draw, "lineWidth": 1, "fillOpacity": 10 if draw == "line" else 80,
              "showPoints": "never", "spanNulls": 3600000, "axisSoftMin": 0}
    if stack:
        custom["stacking"] = {"mode": "normal", "group": "A"}
    if th:
        custom["thresholdsStyle"] = {"mode": "line"}
    p = {
        "id": nid(), "type": "timeseries", "title": title, "description": desc, "datasource": DS, "gridPos": pos,
        "targets": targets,
        "fieldConfig": {"defaults": {"unit": unit, "custom": custom, "color": {"mode": "palette-classic"},
                                     "thresholds": th or thresholds((None, "green"))},
                        "overrides": []},
        "options": {"legend": {"displayMode": "table", "placement": "right", "calcs": ["lastNotNull", "max"],
                               "showLegend": True},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
    }
    if min_ is not None:
        p["fieldConfig"]["defaults"]["min"] = min_
    if max_ is not None:
        p["fieldConfig"]["defaults"]["max"] = max_
    if interval:
        p["interval"] = interval
    if time_from:
        p["timeFrom"] = time_from
    return p


def table(title, q, pos, overrides=None, desc="", hide=None, sort=None, rename=None, unit="none"):
    exclude = {"Time": True}
    for h in hide or []:
        exclude[h] = True
    p = {
        "id": nid(), "type": "table", "title": title, "description": desc, "datasource": DS, "gridPos": pos,
        "targets": [target(q, fmt="table")],
        "transformations": [
            {"id": "merge", "options": {}},
            {"id": "organize", "options": {"excludeByName": exclude, "renameByName": rename or {}}},
        ],
        "fieldConfig": {"defaults": {"unit": unit, "custom": {"align": "auto", "filterable": True},
                                     "thresholds": thresholds((None, "green"))},
                        "overrides": overrides or []},
        "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}},
    }
    if sort:
        p["options"]["sortBy"] = [{"displayName": sort[0], "desc": sort[1]}]
    return p


def ov(name, *props):
    return {"matcher": {"id": "byName", "options": name}, "properties": [{"id": k, "value": v} for k, v in props]}


def color_cell(name, mappings=None, th=None, unit=None):
    props = [("custom.cellOptions", {"type": "color-background", "mode": "basic"}),
             ("custom.displayMode", "color-background")]
    if mappings:
        props.append(("mappings", mappings))
    if th:
        props.append(("thresholds", th))
        props.append(("color", {"mode": "thresholds"}))
    if unit:
        props.append(("unit", unit))
    return ov(name, *props)


def count_where(field, cond):
    return ('SELECT count("last") FROM (SELECT last("%s") FROM "smart_device" WHERE %s AND $timeFilter '
            'GROUP BY "host", "device", "serial") WHERE %s' % (field, H, cond))


panels = []
y = 0

# ============================== 개요 ======================================= #
panels.append(row("개요 — 선택 기간 내 마지막 상태", y)); y += 1
panels += [
    stat("디스크", 'SELECT count("last") FROM (SELECT last("status_code") FROM "smart_device" WHERE %s AND '
                  '$timeFilter GROUP BY "host", "device", "serial")' % H,
         grid(0, y, 3, 4), th=thresholds((None, "blue")), desc="SMART 를 읽은 물리 디스크 수"),
    stat("위험", count_where("status_code", '"last" = 2'), grid(3, y, 3, 4),
         th=thresholds((None, "green"), (1, "red")),
         desc="SMART FAILED, 보류/복구불가 섹터, NVMe critical warning·미디어 에러, 셀프테스트 실패 등"),
    stat("경고", count_where("status_code", '"last" = 1'), grid(6, y, 3, 4),
         th=thresholds((None, "green"), (1, "yellow")),
         desc="재할당 섹터, CRC 에러, 고온, 수명 사용률 기준 초과 등"),
    stat("정상", count_where("status_code", '"last" = 0'), grid(9, y, 3, 4), th=thresholds((None, "green"))),
    stat("최고 온도", 'SELECT max("last") FROM (SELECT last("temperature_c") FROM "smart_device" WHERE %s AND '
                    '$timeFilter GROUP BY "host", "device", "serial")' % H,
         grid(12, y, 3, 4), unit="celsius", th=thresholds((None, "green"), (50, "yellow"), (60, "red")),
         color_mode="value"),
    stat("스핀다운", 'SELECT count("last") FROM (SELECT last("standby") FROM "disk_health_standby" WHERE %s AND '
                   'time > now() - 15m GROUP BY "host", "device")' % H,
         grid(15, y, 3, 4), th=thresholds((None, "blue")), color_mode="value",
         desc="최근 15분 동안 대기 상태라 SMART 조회를 건너뛴 디스크 (SKIP_STANDBY=yes)"),
    stat("수집 오류", 'SELECT count("last") FROM (SELECT last("exit_status") FROM "disk_health_error" WHERE %s AND '
                    'time > now() - 15m GROUP BY "host", "device")' % H,
         grid(18, y, 3, 4), th=thresholds((None, "green"), (1, "orange")),
         desc="최근 15분 동안 smartctl 이 열지 못한 장치 — 아래 '수집기' 행 참고"),
    stat("RAID/ZFS 이상", 'SELECT sum("last") FROM (SELECT last("vdevs_not_online") FROM "zfs_pool" WHERE %s AND '
                         'time > now() - 15m GROUP BY "host", "pool")' % H,
         grid(21, y, 3, 2), th=thresholds((None, "green"), (1, "red")), desc="ZFS: ONLINE 이 아닌 vdev 수"),
    stat("MD 실패 디스크", 'SELECT sum("last") FROM (SELECT last("DisksFailed") FROM "mdstat" WHERE %s AND '
                         'time > now() - 15m GROUP BY "host", "Name")' % H,
         grid(21, y + 2, 3, 2), th=thresholds((None, "green"), (1, "red")), desc="/proc/mdstat 의 실패 디스크 합계"),
]
y += 4

# ============================== 상태 표 ===================================== #
panels.append(row("디스크 상태", y)); y += 1
status_q = (
    'SELECT last("status_code") AS "상태", last("status_reason") AS "사유", last("temperature_c") AS "온도", '
    'last("power_on_hours") AS "가동시간", last("reallocated_sectors") AS "재할당", '
    'last("pending_sectors") AS "보류섹터", last("offline_uncorrectable") AS "복구불가", '
    'last("reported_uncorrect") AS "UNC", last("udma_crc_errors") AS "CRC", '
    'last("media_errors") AS "미디어에러", last("grown_defects") AS "결함(SAS)", '
    'last("life_used_percent") AS "수명사용", last("available_spare") AS "예비공간", '
    'last("last_selftest_passed") AS "셀프테스트", last("last_selftest_age_hours") AS "테스트경과", '
    'last("capacity_bytes") AS "용량", last("firmware") AS "펌웨어" '
    'FROM "smart_device" WHERE %s AND %s AND $timeFilter %s' % (H, D, SMART_GROUP))
panels.append(table(
    "디스크 상태 요약", status_q, grid(0, y, 24, 10),
    desc="상태: 0 정상 / 1 경고 / 2 위험. 각 값은 선택 기간 내 마지막 수집값.",
    sort=("상태", True),
    rename={"host": "호스트", "device": "장치", "model": "모델", "serial": "시리얼", "kind": "종류"},
    overrides=[
        color_cell("상태", mappings=STATUS_MAP),
        color_cell("셀프테스트", mappings=SELFTEST_MAP),
        color_cell("온도", th=thresholds((None, "green"), (50, "yellow"), (60, "red")), unit="celsius"),
        color_cell("보류섹터", th=thresholds((None, "green"), (1, "red"))),
        color_cell("복구불가", th=thresholds((None, "green"), (1, "red"))),
        color_cell("미디어에러", th=thresholds((None, "green"), (1, "red"))),
        color_cell("재할당", th=thresholds((None, "green"), (1, "yellow"), (100, "red"))),
        color_cell("UNC", th=thresholds((None, "green"), (1, "yellow"))),
        color_cell("CRC", th=thresholds((None, "green"), (1, "yellow"))),
        color_cell("결함(SAS)", th=thresholds((None, "green"), (1, "yellow"), (100, "red"))),
        color_cell("수명사용", th=thresholds((None, "green"), (80, "yellow"), (95, "red")), unit="percent"),
        color_cell("예비공간", th=thresholds((None, "red"), (10, "yellow"), (50, "green")), unit="percent"),
        ov("가동시간", ("unit", "h"), ("decimals", 0)),
        ov("테스트경과", ("unit", "h"), ("decimals", 0)),
        ov("용량", ("unit", "decbytes")),
        ov("사유", ("custom.width", 320)),
    ]))
y += 10

# ============================== 온도 ======================================== #
panels.append(row("온도", y)); y += 1
panels.append(timeseries(
    "디스크 온도",
    [target('SELECT mean("temperature_c") FROM "smart_device" WHERE %s AND %s AND $timeFilter '
            'GROUP BY time($__interval), "host", "device" fill(none)' % (H, D), alias="$tag_host/$tag_device")],
    grid(0, y, 24, 8), unit="celsius", interval="5m",
    th=thresholds((None, "green"), (50, "yellow"), (60, "red"))))
y += 8

# ============================== 에러 카운터 ================================== #
panels.append(row("에러 카운터 (증가하면 교체 검토)", y)); y += 1


def counter_ts(title, fields, pos, desc=""):
    sel = ", ".join('max("%s") AS "%s"' % (f, a) for f, a in fields)
    return timeseries(title, [target('SELECT %s FROM "smart_device" WHERE %s AND %s AND $timeFilter '
                                     'GROUP BY time($__interval), "host", "device" fill(none)' % (sel, H, D),
                                     alias="$tag_host/$tag_device $col")],
                      pos, interval="5m", desc=desc)


panels += [
    counter_ts("재할당 섹터 (ATA #5)", [("reallocated_sectors", "realloc")], grid(0, y, 8, 7),
               desc="불량으로 판정돼 예비 영역으로 옮겨진 섹터. 꾸준히 늘면 교체."),
    counter_ts("보류 / 복구불가 섹터 (ATA #197/#198)",
               [("pending_sectors", "pending"), ("offline_uncorrectable", "offline_unc")], grid(8, y, 8, 7),
               desc="읽기 실패 후 재할당 대기 중인 섹터. 0 이 아니면 데이터 손상 위험."),
    counter_ts("CRC / UNC (ATA #199/#187)",
               [("udma_crc_errors", "crc"), ("reported_uncorrect", "unc")], grid(16, y, 8, 7),
               desc="CRC 는 케이블/백플레인 문제, UNC 는 ECC 로 복구 못한 읽기 에러."),
]
y += 7
panels += [
    counter_ts("NVMe 미디어 에러 / Critical Warning",
               [("media_errors", "media_err"), ("critical_warning", "crit_warn")], grid(0, y, 8, 7)),
    counter_ts("SAS 결함 / 미정정 에러",
               [("grown_defects", "grown"), ("read_uncorrected", "read_unc"), ("write_uncorrected", "write_unc")],
               grid(8, y, 8, 7)),
    table("선택 기간 동안 증가한 카운터",
          'SELECT spread("reallocated_sectors") AS "재할당", spread("pending_sectors") AS "보류섹터", '
          'spread("udma_crc_errors") AS "CRC", spread("reported_uncorrect") AS "UNC", '
          'spread("media_errors") AS "미디어에러", spread("grown_defects") AS "결함(SAS)", '
          'spread("error_log_count") AS "에러로그" FROM "smart_device" WHERE %s AND %s AND $timeFilter '
          'GROUP BY "host", "device", "model"' % (H, D),
          grid(16, y, 8, 7), desc="spread = 기간 내 최대 - 최소. 0 이 아니면 해당 기간에 새로 발생한 것.",
          rename={"host": "호스트", "device": "장치", "model": "모델"},
          overrides=[color_cell(n, th=thresholds((None, "green"), (1, "red")))
                     for n in ("재할당", "보류섹터", "미디어에러", "결함(SAS)")]
          + [color_cell(n, th=thresholds((None, "green"), (1, "yellow"))) for n in ("CRC", "UNC", "에러로그")]),
]
y += 7

# ============================== SSD / NVMe 수명 ============================== #
panels.append(row("SSD / NVMe 수명", y)); y += 1
panels.append({
    "id": nid(), "type": "bargauge", "title": "수명 사용률", "datasource": DS, "gridPos": grid(0, y, 12, 8),
    "description": "NVMe Percentage Used / ATA Device Statistics / 벤더 SMART 속성 기준. 100% = 보증 TBW 도달.",
    "targets": [target('SELECT last("life_used_percent") FROM "smart_device" WHERE %s AND %s AND $timeFilter '
                       'GROUP BY "host", "device", "model"' % (H, D), alias="$tag_host/$tag_device ($tag_model)")],
    "fieldConfig": {"defaults": {"unit": "percent", "min": 0, "max": 100, "color": {"mode": "thresholds"},
                                 "thresholds": thresholds((None, "green"), (80, "yellow"), (95, "red"))},
                    "overrides": []},
    "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                "orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True,
                "valueMode": "color", "minVizHeight": 16, "maxVizHeight": 24},
})
panels.append(timeseries(
    "일별 기록량 (최근 30일)",
    [target('SELECT non_negative_difference(last("bytes_written")) FROM "smart_device" WHERE %s AND %s AND '
            '$timeFilter GROUP BY time(1d), "host", "device" fill(none)' % (H, D), alias="$tag_host/$tag_device")],
    grid(12, y, 12, 8), unit="decbytes", draw="bars", time_from="30d", stack=True,
    desc="NVMe data units written / ATA Logical Sectors Written / SAS 쓰기 처리량의 일별 증가분"))
y += 8
panels.append(timeseries(
    "NVMe 예비 공간 (Available Spare)",
    [target('SELECT min("available_spare") FROM "smart_device" WHERE %s AND %s AND "kind" = \'nvme\' AND $timeFilter '
            'GROUP BY time($__interval), "host", "device" fill(none)' % (H, D), alias="$tag_host/$tag_device")],
    grid(0, y, 12, 7), unit="percent", min_=0, max_=100, interval="5m",
    th=thresholds((None, "red"), (10, "yellow"), (50, "green"))))
panels.append(timeseries(
    "불안전 종료 / 전원 사이클",
    [target('SELECT max("unsafe_shutdowns") AS "unsafe", max("power_cycles") AS "cycles" FROM "smart_device" '
            'WHERE %s AND %s AND $timeFilter GROUP BY time($__interval), "host", "device" fill(none)' % (H, D),
            alias="$tag_host/$tag_device $col")],
    grid(12, y, 12, 7), interval="5m"))
y += 7

# ============================== I/O ========================================= #
panels.append(row("디스크 I/O (diskio)", y)); y += 1
IH = '"host" =~ /^$host$/ AND "name" =~ /^$iodev$/'
panels += [
    timeseries("IOPS", [target(
        'SELECT non_negative_derivative(max("reads"), 1s) AS "read", non_negative_derivative(max("writes"), 1s) '
        'AS "write" FROM "diskio" WHERE %s AND $timeFilter GROUP BY time($__interval), "host", "name" fill(none)'
        % IH, alias="$tag_name $col")], grid(0, y, 12, 8), unit="iops"),
    timeseries("처리량", [target(
        'SELECT non_negative_derivative(max("read_bytes"), 1s) AS "read", non_negative_derivative(max("write_bytes"),'
        ' 1s) AS "write" FROM "diskio" WHERE %s AND $timeFilter GROUP BY time($__interval), "host", "name" fill(none)'
        % IH, alias="$tag_name $col")], grid(12, y, 12, 8), unit="Bps"),
]
y += 8
panels += [
    timeseries("사용률 (%util)", [target(
        'SELECT non_negative_derivative(max("io_time"), 1s) / 10 FROM "diskio" WHERE %s AND $timeFilter '
        'GROUP BY time($__interval), "host", "name" fill(none)' % IH, alias="$tag_name")],
        grid(0, y, 8, 8), unit="percent", min_=0, max_=100,
        th=thresholds((None, "green"), (80, "yellow"), (95, "red")),
        desc="io_time(ms)/초 ÷ 10. iostat 의 %util 과 같은 의미 (NVMe/RAID 는 100% 여도 여유가 있을 수 있음)"),
    timeseries("평균 응답시간 (await)", [target(
        'SELECT ("rt" + "wt") / ("r" + "w") FROM (SELECT non_negative_derivative(max("read_time")) AS "rt", '
        'non_negative_derivative(max("write_time")) AS "wt", non_negative_derivative(max("reads")) AS "r", '
        'non_negative_derivative(max("writes")) AS "w" FROM "diskio" WHERE %s AND $timeFilter '
        'GROUP BY time($__interval), "host", "name") WHERE $timeFilter GROUP BY "host", "name"' % IH,
        alias="$tag_name")],
        grid(8, y, 8, 8), unit="ms", desc="(읽기+쓰기 대기시간 증가량) ÷ (요청 수 증가량)"),
    timeseries("평균 큐 길이", [target(
        'SELECT non_negative_derivative(max("weighted_io_time"), 1s) / 1000 FROM "diskio" WHERE %s AND $timeFilter '
        'GROUP BY time($__interval), "host", "name" fill(none)' % IH, alias="$tag_name")],
        grid(16, y, 8, 8), desc="weighted_io_time 기반 (iostat aqu-sz)"),
]
y += 8

# ============================== 파일시스템 ==================================== #
panels.append(row("파일시스템", y)); y += 1
panels.append({
    "id": nid(), "type": "bargauge", "title": "사용률", "datasource": DS, "gridPos": grid(0, y, 12, 8),
    "targets": [target('SELECT last("used_percent") FROM "disk" WHERE %s AND $timeFilter GROUP BY "host", "path"'
                       % H, alias="$tag_host:$tag_path")],
    "fieldConfig": {"defaults": {"unit": "percent", "min": 0, "max": 100, "color": {"mode": "thresholds"},
                                 "thresholds": thresholds((None, "green"), (80, "yellow"), (90, "red"))},
                    "overrides": []},
    "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                "orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True, "valueMode": "color"},
})
panels.append(timeseries(
    "사용량 추이", [target('SELECT mean("used") FROM "disk" WHERE %s AND $timeFilter GROUP BY time($__interval), '
                         '"host", "path" fill(none)' % H, alias="$tag_host:$tag_path")],
    grid(12, y, 12, 8), unit="decbytes"))
y += 8

# ============================== RAID / ZFS ================================== #
panels.append(row("RAID / ZFS", y)); y += 1
panels.append(table(
    "MD RAID (/proc/mdstat)",
    'SELECT last("DisksTotal") AS "전체", last("DisksActive") AS "활성", last("DisksFailed") AS "실패", '
    'last("DisksDown") AS "다운", last("DisksSpare") AS "스페어", last("BlocksSyncedPct") AS "동기화진행" '
    'FROM "mdstat" WHERE %s AND $timeFilter GROUP BY "host", "Name", "ActivityState", "Devices"' % H,
    grid(0, y, 12, 6), rename={"host": "호스트", "Name": "어레이", "ActivityState": "상태", "Devices": "구성"},
    overrides=[color_cell("실패", th=thresholds((None, "green"), (1, "red"))),
               color_cell("다운", th=thresholds((None, "green"), (1, "red"))),
               ov("동기화진행", ("unit", "percent"))]))
panels.append(table(
    "ZFS 풀",
    'SELECT last("status_code") AS "상태", last("health") AS "health", last("capacity_percent") AS "사용률", '
    'last("fragmentation_percent") AS "단편화", last("read_errors") AS "READ", last("write_errors") AS "WRITE", '
    'last("checksum_errors") AS "CKSUM", last("vdevs_not_online") AS "비정상vdev", '
    'last("resilver_in_progress") AS "리실버", last("last_scrub") AS "마지막 스크럽" '
    'FROM "zfs_pool" WHERE %s AND $timeFilter GROUP BY "host", "pool"' % H,
    grid(12, y, 12, 6), rename={"host": "호스트", "pool": "풀"},
    overrides=[color_cell("상태", mappings=STATUS_MAP),
               color_cell("사용률", th=thresholds((None, "green"), (80, "yellow"), (90, "red")), unit="percent"),
               ov("단편화", ("unit", "percent"))]
    + [color_cell(n, th=thresholds((None, "green"), (1, "red"))) for n in ("READ", "WRITE", "CKSUM", "비정상vdev")]))
y += 6

# ============================== 원본 속성 / 수집기 (접힘) ===================== #
attr_panel = table(
    "ATA SMART 속성 원본",
    'SELECT last("value") AS "value", last("worst") AS "worst", last("thresh") AS "thresh", last("raw") AS "raw", '
    'last("prefailure") AS "prefail", last("failing_now") AS "failing" FROM "smart_attribute" '
    'WHERE %s AND %s AND $timeFilter GROUP BY "host", "device", "model", "attr_id", "attr_name"' % (H, D),
    grid(0, y + 1, 24, 12), desc="value/worst 가 thresh 이하로 내려가면 해당 속성 실패",
    overrides=[color_cell("failing", th=thresholds((None, "green"), (1, "red")))])
panels.append(row("ATA SMART 속성 원본", y, collapsed=True, panels=[attr_panel])); y += 1

coll_panels = [
    timeseries("수집 소요 시간", [target(
        'SELECT max("duration_ms") FROM "disk_health_collector" WHERE %s AND $timeFilter '
        'GROUP BY time($__interval), "host" fill(none)' % H, alias="$tag_host")],
        grid(0, y + 1, 12, 7), unit="ms", interval="5m"),
    table("수집 오류 (최근 1시간)",
          'SELECT last("exit_status") AS "exit", last("message") AS "message" FROM "disk_health_error" '
          'WHERE %s AND time > now() - 1h GROUP BY "host", "device"' % H,
          grid(12, y + 1, 12, 7), desc="스캔은 되지만 열리지 않는 장치. RAID 뒤 디스크면 EXTRA_DEVICES 설정"),
    table("수집기 정보",
          'SELECT last("smartctl_version") AS "smartctl", last("collector_version") AS "collector", '
          'last("devices_ok") AS "정상조회", last("devices_standby") AS "대기", last("devices_error") AS "오류" '
          'FROM "disk_health_collector" WHERE %s AND $timeFilter GROUP BY "host"' % H,
          grid(0, y + 8, 24, 5)),
]
panels.append(row("수집기", y, collapsed=True, panels=coll_panels)); y += 1


def var_query(name, label, q, multi=True, regex=""):
    return {"name": name, "label": label, "type": "query", "datasource": DS, "query": q, "definition": q,
            "refresh": 2, "multi": multi, "includeAll": True, "allValue": ".*",
            "current": {"selected": True, "text": ["All"], "value": ["$__all"]},
            "sort": 1, "regex": regex, "hide": 0, "options": []}


dashboard = {
    "uid": "mirror-disk-health",
    "title": "Disk Health (SMART)",
    "description": "smartmontools 기반 디스크 건강 상태 — github: mirror-disk-health",
    "tags": ["disk", "smart", "storage", "mirror"],
    "timezone": "browser",
    "editable": True,
    "graphTooltip": 1,
    "refresh": "5m",
    "schemaVersion": 39,
    "version": 1,
    "time": {"from": "now-24h", "to": "now"},
    "timepicker": {"refresh_intervals": ["1m", "5m", "15m", "1h"]},
    "fiscalYearStartMonth": 0,
    "liveNow": False,
    "annotations": {"list": [{"builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                              "enable": True, "hide": True, "iconColor": "rgba(0, 211, 255, 1)",
                              "name": "Annotations & Alerts", "type": "dashboard"}]},
    "links": [],
    "templating": {"list": [
        {"name": "ds", "label": "InfluxDB", "type": "datasource", "query": "influxdb", "regex": "",
         "current": {}, "hide": 0, "includeAll": False, "multi": False, "options": [], "refresh": 1},
        var_query("host", "호스트", 'SHOW TAG VALUES WITH KEY = "host"'),
        var_query("device", "SMART 장치",
                  'SHOW TAG VALUES FROM "smart_device" WITH KEY = "device" WHERE "host" =~ /^$host$/'),
        var_query("iodev", "I/O 장치",
                  'SHOW TAG VALUES FROM "diskio" WITH KEY = "name" WHERE "host" =~ /^$host$/'),
    ]},
    "panels": panels,
}

if __name__ == "__main__":
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(dashboard, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    n = sum(1 + len(p.get("panels", [])) for p in panels)
    print("wrote %s (%d panels)" % (OUT, n))
