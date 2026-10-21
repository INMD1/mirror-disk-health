#!/usr/bin/env bash
# =============================================================================
# mirror-disk-health 설치 스크립트 (미러 서버에서 root 로 실행)
#
#   sudo ./install.sh                     # ./config.env 사용
#   sudo ./install.sh -c /path/config.env
#   ./install.sh --render-only ./out      # 시스템 변경 없이 생성될 설정만 출력 (root 불필요)
#
# 설치 내용
#   - smartmontools(smartctl/smartd), nvme-cli, Telegraf(InfluxData 공식 저장소)
#   - /opt/disk-health/collector/disk_health_collector.py   SMART 수집기
#   - /etc/disk-health/{collector.env,telegraf.conf,telegraf.d/}  설정
#   - telegraf-disk-health.service  (기존 telegraf.service 와 독립된 전용 인스턴스)
#   - /etc/sudoers.d/telegraf-disk-health   (telegraf → smartctl -j 만 허용)
#   - smartd 정기 셀프테스트 스케줄 (MANAGE_SMARTD=yes)
# =============================================================================
set -Eeuo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$REPO_DIR/config.env"
RENDER_ONLY=""
SKIP_VERIFY="no"

PREFIX="/opt/disk-health"
ETC="/etc/disk-health"
UNIT="telegraf-disk-health.service"
SUDOERS="/etc/sudoers.d/telegraf-disk-health"
STATE="/var/lib/disk-health"
MARK="# managed by mirror-disk-health"

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -c|--config) CONFIG="$2"; shift 2 ;;
    --render-only) RENDER_ONLY="$2"; shift 2 ;;
    --skip-verify) SKIP_VERIFY="yes"; shift ;;
    -h|--help) usage 0 ;;
    *) echo "알 수 없는 옵션: $1" >&2; usage 1 ;;
  esac
done

c_info=$'\e[1;34m'; c_ok=$'\e[1;32m'; c_warn=$'\e[1;33m'; c_err=$'\e[1;31m'; c_off=$'\e[0m'
[[ -t 1 ]] || { c_info=""; c_ok=""; c_warn=""; c_err=""; c_off=""; }
log()  { printf '%s==>%s %s\n' "$c_info" "$c_off" "$*"; }
ok()   { printf '%s ok%s %s\n' "$c_ok" "$c_off" "$*"; }
warn() { printf '%s !!%s %s\n' "$c_warn" "$c_off" "$*" >&2; }
die()  { printf '%sERR%s %s\n' "$c_err" "$c_off" "$*" >&2; exit 1; }
trap 'die "실패: ${BASH_COMMAND} (line ${LINENO})"' ERR

# --------------------------------------------------------------------------- #
# 설정 로드
# --------------------------------------------------------------------------- #
[[ -f "$CONFIG" ]] || die "설정 파일이 없습니다: $CONFIG  (cp config.env.example config.env 후 수정)"
set -f   # DISKIO_DEVICES 등의 glob 문자가 확장되지 않도록
# shellcheck disable=SC1090
source "$CONFIG"
set +f

: "${HOST_TAG:=}" "${ENV_TAG:=}" "${ROLE_TAG:=mirror}"
: "${INFLUX_VERSION:=1}" "${INFLUX_URL:?INFLUX_URL 을 설정하세요}" "${INFLUX_INSECURE_SKIP_VERIFY:=no}"
: "${INFLUX_DB:=disk_health}" "${INFLUX_RETENTION:=400d}" "${INFLUX_USER:=}" "${INFLUX_PASSWORD:=}"
: "${INFLUX_ORG:=}" "${INFLUX_BUCKET:=disk_health}" "${INFLUX_TOKEN:=}" "${INFLUX_INIT:=yes}"
: "${SMART_INTERVAL:=5m}" "${SMART_TIMEOUT:=240s}" "${IO_INTERVAL:=30s}"
: "${DISKIO_DEVICES:=sd[a-z] sd[a-z][a-z] nvme[0-9]*n[0-9] vd[a-z] xvd[a-z] hd[a-z] md*}"
: "${SKIP_STANDBY:=yes}" "${EXCLUDE_REGEX:=}" "${EXTRA_DEVICES:=}" "${MAX_WORKERS:=4}" "${DEVICE_TIMEOUT:=60}"
: "${COLLECT_ATTRIBUTES:=yes}" "${COLLECT_ZFS:=auto}"
: "${TEMP_WARN_HDD:=50}" "${TEMP_WARN_SSD:=65}" "${TEMP_WARN_NVME:=70}" "${WEAR_WARN_PERCENT:=80}"
: "${MANAGE_SMARTD:=yes}" "${SMARTD_SCHEDULE:=(S/../.././02|L/../../7/03)}" "${SMARTD_TEMP:=4,50,60}"

[[ -n "$HOST_TAG" ]] || HOST_TAG="$(hostname -s 2>/dev/null || hostname)"
case "$INFLUX_VERSION" in
  1) ;;
  2) [[ -n "$INFLUX_TOKEN" && -n "$INFLUX_ORG" ]] || die "INFLUX_VERSION=2 이면 INFLUX_TOKEN, INFLUX_ORG 가 필요합니다" ;;
  *) die "INFLUX_VERSION 은 1 또는 2 여야 합니다 (현재: $INFLUX_VERSION)" ;;
esac

for _v in INFLUX_PASSWORD INFLUX_TOKEN INFLUX_USER; do
  # Telegraf 는 설정 파일의 ${VAR} 를 환경변수로 치환하므로 '$' 가 들어간 비밀값은 깨질 수 있다
  [[ "${!_v}" != *'$'* ]] || warn "$_v 에 '\$' 문자가 있습니다. Telegraf 환경변수 치환과 충돌할 수 있으니 바꾸는 것을 권장합니다"
done

dur_to_sec() {  # 30s / 5m / 1h → 초
  local v="$1"
  [[ "$v" =~ ^([0-9]+)(s|m|h)$ ]] || die "기간 형식 오류: '$v' (예: 30s, 5m, 1h)"
  case "${BASH_REMATCH[2]}" in s) echo "${BASH_REMATCH[1]}" ;; m) echo $(( BASH_REMATCH[1]*60 )) ;; h) echo $(( BASH_REMATCH[1]*3600 )) ;; esac
}
(( $(dur_to_sec "$SMART_TIMEOUT") < $(dur_to_sec "$SMART_INTERVAL") )) \
  || die "SMART_TIMEOUT($SMART_TIMEOUT) 은 SMART_INTERVAL($SMART_INTERVAL) 보다 작아야 합니다"
(( $(dur_to_sec "$SMART_INTERVAL") >= 60 )) || die "SMART_INTERVAL 은 1m 이상으로 설정하세요"
dur_to_sec "$IO_INTERVAL" >/dev/null

yes_no() { [[ "${1,,}" =~ ^(1|y|yes|true|on)$ ]]; }

# TOML 기본 문자열 이스케이프
toml() { local s="$1"; s="${s//\\/\\\\}"; s="${s//\"/\\\"}"; printf '"%s"' "$s"; }

PY="$(command -v python3 || true)"
[[ -n "$PY" ]] || PY="/usr/bin/python3"
SMARTCTL_BIN="$(command -v smartctl 2>/dev/null || echo /usr/sbin/smartctl)"
ZPOOL_BIN="$(command -v zpool 2>/dev/null || true)"

# --------------------------------------------------------------------------- #
# 설정 파일 렌더링
# --------------------------------------------------------------------------- #
render_collector_env() {
  cat <<EOF
$MARK — install.sh 가 config.env 로부터 생성 (직접 수정 시 재설치하면 덮어써짐)
SMARTCTL=$SMARTCTL_BIN
ZPOOL=$ZPOOL_BIN
USE_SUDO=auto
SKIP_STANDBY=$SKIP_STANDBY
EXCLUDE_REGEX=$EXCLUDE_REGEX
EXTRA_DEVICES=$EXTRA_DEVICES
MAX_WORKERS=$MAX_WORKERS
DEVICE_TIMEOUT=$DEVICE_TIMEOUT
COLLECT_ATTRIBUTES=$COLLECT_ATTRIBUTES
COLLECT_ZFS=$COLLECT_ZFS
TEMP_WARN_HDD=$TEMP_WARN_HDD
TEMP_WARN_SSD=$TEMP_WARN_SSD
TEMP_WARN_NVME=$TEMP_WARN_NVME
WEAR_WARN_PERCENT=$WEAR_WARN_PERCENT
EOF
}

render_telegraf_conf() {
  cat <<EOF
$MARK
# 전용 Telegraf 인스턴스(telegraf-disk-health.service) 의 에이전트/출력 설정

[global_tags]
  env = $(toml "$ENV_TAG")
  role = $(toml "$ROLE_TAG")

[agent]
  interval = $(toml "$IO_INTERVAL")
  round_interval = true
  metric_batch_size = 1000
  ## 컨트롤러 장애 시 버퍼링 (SMART 5분 주기 기준 수일치)
  metric_buffer_limit = 100000
  collection_jitter = "0s"
  flush_interval = "30s"
  flush_jitter = "5s"
  precision = "1s"
  hostname = $(toml "$HOST_TAG")
  omit_hostname = false

EOF
  if [[ "$INFLUX_VERSION" == "1" ]]; then
    cat <<EOF
[[outputs.influxdb]]
  urls = [$(toml "$INFLUX_URL")]
  database = $(toml "$INFLUX_DB")
  ## DB/보존정책은 dhctl.py influx-init 이 만든다 (기본 RP 로 기록)
  skip_database_creation = true
  timeout = "15s"
  content_encoding = "gzip"
EOF
    if [[ -n "$INFLUX_USER" ]]; then
      echo "  username = $(toml "$INFLUX_USER")"
      echo "  password = $(toml "$INFLUX_PASSWORD")"
    fi
  else
    cat <<EOF
[[outputs.influxdb_v2]]
  urls = [$(toml "$INFLUX_URL")]
  token = $(toml "$INFLUX_TOKEN")
  organization = $(toml "$INFLUX_ORG")
  bucket = $(toml "$INFLUX_BUCKET")
  timeout = "15s"
  content_encoding = "gzip"
EOF
  fi
  yes_no "$INFLUX_INSECURE_SKIP_VERIFY" && echo "  insecure_skip_verify = true"
  return 0
}

render_inputs_conf() {
  local cmd devs="" d
  cmd="$PY $PREFIX/collector/disk_health_collector.py -c $ETC/collector.env"
  set -f
  for d in $DISKIO_DEVICES; do devs+="${devs:+, }$(toml "$d")"; done
  set +f
  cat <<EOF
$MARK

## --- SMART (smartctl --json → line protocol) -------------------------------
[[inputs.exec]]
  commands = [$(toml "$cmd")]
  interval = $(toml "$SMART_INTERVAL")
  timeout = $(toml "$SMART_TIMEOUT")
  data_format = "influx"

## --- 디스크 I/O ------------------------------------------------------------
[[inputs.diskio]]
  interval = $(toml "$IO_INTERVAL")
  devices = [$devs]
  skip_serial_number = false

## --- 파일시스템 사용량 ------------------------------------------------------
[[inputs.disk]]
  interval = $(toml "$IO_INTERVAL")
  ignore_fs = ["tmpfs", "devtmpfs", "devfs", "iso9660", "overlay", "aufs", "squashfs", "nsfs", "tracefs", "ramfs", "efivarfs"]
EOF
  if [[ -r /proc/mdstat ]] || [[ -n "$RENDER_ONLY" ]]; then
    cat <<EOF

## --- 소프트웨어 RAID (/proc/mdstat) -----------------------------------------
[[inputs.mdstat]]
  interval = $(toml "$IO_INTERVAL")
EOF
  fi
}

render_smartd_conf() {
  local opts item dev typ
  opts="-a -o on -S on -n standby,q -s $SMARTD_SCHEDULE -W $SMARTD_TEMP"
  echo "$MARK"
  echo "# 원본은 같은 경로의 .orig-disk-health 로 백업됨. 명시 장치를 먼저, DEVICESCAN 은 마지막에."
  set -f
  for item in $EXTRA_DEVICES; do
    dev="${item%%:*}"; typ="${item#*:}"
    [[ "$item" == *:* ]] || typ="auto"
    echo "$dev -d $typ $opts"
  done
  set +f
  echo "DEVICESCAN $opts"
}

render_unit() {
  cat <<EOF
$MARK
[Unit]
Description=Telegraf - disk health (SMART/IO) to InfluxDB
Documentation=file://$PREFIX/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=telegraf
Group=telegraf
ExecStart=/usr/bin/telegraf --config $ETC/telegraf.conf --config-directory $ETC/telegraf.d
ExecReload=/bin/kill -HUP \$MAINPID
Restart=on-failure
RestartSec=10
# 수집기가 sudo smartctl 을 사용하므로 NoNewPrivileges 를 켜면 안 됨

[Install]
WantedBy=multi-user.target
EOF
}

if [[ -n "$RENDER_ONLY" ]]; then
  mkdir -p "$RENDER_ONLY/telegraf.d"
  render_collector_env > "$RENDER_ONLY/collector.env"
  render_telegraf_conf > "$RENDER_ONLY/telegraf.conf"
  render_inputs_conf   > "$RENDER_ONLY/telegraf.d/disk-health.conf"
  render_smartd_conf   > "$RENDER_ONLY/smartd.conf"
  render_unit          > "$RENDER_ONLY/$UNIT"
  ok "렌더링 완료: $RENDER_ONLY"
  exit 0
fi

# --------------------------------------------------------------------------- #
# 실제 설치
# --------------------------------------------------------------------------- #
[[ $EUID -eq 0 ]] || die "root 로 실행하세요: sudo $0"
command -v systemctl >/dev/null || die "systemd 환경이 필요합니다"

if command -v apt-get >/dev/null; then PM=apt
elif command -v dnf >/dev/null; then PM=dnf
elif command -v yum >/dev/null; then PM=yum
else die "지원하지 않는 배포판입니다 (apt / dnf / yum 필요)"; fi

log "1/9 패키지 설치 (smartmontools, nvme-cli, sudo, python3, curl)"
if [[ $PM == apt ]]; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends smartmontools nvme-cli sudo python3 curl gnupg ca-certificates >/dev/null
else
  $PM install -y -q smartmontools nvme-cli sudo python3 curl >/dev/null
fi
PY="$(command -v python3)"
SMARTCTL_BIN="$(command -v smartctl)"
ZPOOL_BIN="$(command -v zpool 2>/dev/null || true)"

SMARTCTL_VER="$("$SMARTCTL_BIN" -j --version 2>/dev/null | "$PY" -c 'import json,sys; v=json.load(sys.stdin)["smartctl"]["version"]; print("%d.%d"%(v[0],v[1]))' 2>/dev/null || true)"
[[ -n "$SMARTCTL_VER" ]] || die "smartctl JSON 출력을 지원하지 않습니다. smartmontools 7.0 이상이 필요합니다 ($("$SMARTCTL_BIN" --version | head -1))"
ok "smartctl $SMARTCTL_VER ($SMARTCTL_BIN)"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 6) else 1)' || die "Python 3.6 이상이 필요합니다"

log "2/9 Telegraf 설치"
FRESH_TELEGRAF=no
if command -v telegraf >/dev/null; then
  ok "기존 Telegraf 사용: $(telegraf --version)"
else
  FRESH_TELEGRAF=yes
  tmpk="$(mktemp -d)"
  curl -fsSL https://repos.influxdata.com/influxdata-archive.key -o "$tmpk/influxdata-archive.key"
  # InfluxData 공식 문서의 주 키 지문 검증
  gpg --show-keys --with-fingerprint --with-colons "$tmpk/influxdata-archive.key" 2>/dev/null \
    | grep -q '^fpr:\+24C975CBA61A024EE1B631787C3D57159FC2F927:$' \
    || die "InfluxData 저장소 키 지문 검증 실패"
  if [[ $PM == apt ]]; then
    install -d -m 0755 /etc/apt/keyrings
    gpg --dearmor < "$tmpk/influxdata-archive.key" > /etc/apt/keyrings/influxdata-archive.gpg
    chmod 0644 /etc/apt/keyrings/influxdata-archive.gpg
    echo 'deb [signed-by=/etc/apt/keyrings/influxdata-archive.gpg] https://repos.influxdata.com/debian stable main' \
      > /etc/apt/sources.list.d/influxdata.list
    apt-get update -qq
    apt-get install -y -qq telegraf >/dev/null
  else
    cat > /etc/yum.repos.d/influxdata.repo <<'EOF'
[influxdata]
name = InfluxData Repository - Stable
baseurl = https://repos.influxdata.com/stable/$basearch/main
enabled = 1
gpgcheck = 1
gpgkey = https://repos.influxdata.com/influxdata-archive.key
EOF
    $PM install -y -q telegraf >/dev/null
  fi
  rm -rf "$tmpk"
  ok "설치됨: $(telegraf --version)"
fi
TELEGRAF_BIN="$(command -v telegraf)"
id telegraf >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin telegraf

if [[ $FRESH_TELEGRAF == yes ]] && systemctl list-unit-files telegraf.service >/dev/null 2>&1; then
  # 패키지 기본 telegraf.service 는 localhost:8086 으로 보내도록 되어 있어 에러만 쌓임 → 비활성화
  systemctl disable --now telegraf.service >/dev/null 2>&1 || true
  ok "패키지 기본 telegraf.service 비활성화 (이 레포는 $UNIT 를 사용)"
fi

log "3/9 수집기 설치 → $PREFIX"
install -d -m 0755 "$PREFIX/collector" "$ETC" "$ETC/telegraf.d" "$STATE"
install -m 0755 "$REPO_DIR/collector/disk_health_collector.py" "$PREFIX/collector/disk_health_collector.py"
install -m 0755 "$REPO_DIR/dhctl.py" "$PREFIX/dhctl.py"
install -m 0644 "$REPO_DIR/README.md" "$PREFIX/README.md" 2>/dev/null || true
install -d -m 0755 "$PREFIX/grafana"
cp -f "$REPO_DIR"/grafana/*.json "$PREFIX/grafana/" 2>/dev/null || true

log "4/9 설정 생성 → $ETC"
render_collector_env > "$ETC/collector.env"
render_telegraf_conf > "$ETC/telegraf.conf"
render_inputs_conf   > "$ETC/telegraf.d/disk-health.conf"
chown root:telegraf "$ETC/collector.env" "$ETC/telegraf.conf" "$ETC/telegraf.d/disk-health.conf"
chmod 0640 "$ETC/telegraf.conf"          # 비밀번호/토큰 포함 가능
chmod 0644 "$ETC/collector.env" "$ETC/telegraf.d/disk-health.conf"
# config.env 사본 (dhctl.py verify 용, 비밀값 포함 → root 전용)
if [[ "$(readlink -f "$CONFIG")" != "$ETC/config.env" ]]; then
  install -m 0600 "$CONFIG" "$ETC/config.env"
fi

log "5/9 sudoers (telegraf → smartctl -j 만 허용)"
tmp_sudo="$(mktemp)"
cat > "$tmp_sudo" <<EOF
$MARK
# Telegraf 의 SMART 수집기가 root 권한 없이 디스크를 조회하기 위한 최소 권한
Defaults:telegraf !requiretty
Defaults:telegraf !syslog
Defaults:telegraf !pam_session
telegraf ALL=(root) NOPASSWD: $SMARTCTL_BIN -j *
EOF
if ! visudo -cqf "$tmp_sudo" >/dev/null 2>&1; then
  # 오래된 sudo 가 pam_session 옵션을 모를 때
  sed -i '/pam_session/d' "$tmp_sudo"
  visudo -cqf "$tmp_sudo" || die "sudoers 검증 실패"
fi
install -m 0440 -o root -g root "$tmp_sudo" "$SUDOERS"
rm -f "$tmp_sudo"
sudo -u telegraf sudo -n "$SMARTCTL_BIN" -j --version >/dev/null || die "telegraf 사용자의 sudo smartctl 실행 실패"
ok "$SUDOERS"

log "6/9 smartd 셀프테스트 스케줄"
if yes_no "$MANAGE_SMARTD"; then
  # Debian/Ubuntu: /etc/smartd.conf , RHEL 계열: /etc/smartmontools/smartd.conf
  if [[ -f /etc/smartd.conf ]]; then SMARTD_CONF=/etc/smartd.conf
  elif [[ -f /etc/smartmontools/smartd.conf ]]; then SMARTD_CONF=/etc/smartmontools/smartd.conf
  else SMARTD_CONF=/etc/smartd.conf; fi
  if [[ -f "$SMARTD_CONF" && ! -f "$SMARTD_CONF.orig-disk-health" ]] && ! grep -q "$MARK" "$SMARTD_CONF"; then
    cp -p "$SMARTD_CONF" "$SMARTD_CONF.orig-disk-health"
  fi
  render_smartd_conf > "$SMARTD_CONF"
  SMARTD_UNIT=""
  for u in smartmontools.service smartd.service; do
    if systemctl cat "$u" >/dev/null 2>&1; then SMARTD_UNIT="$u"; break; fi
  done
  if [[ -n "$SMARTD_UNIT" ]]; then
    systemctl enable "$SMARTD_UNIT" >/dev/null 2>&1 || true
    if systemctl restart "$SMARTD_UNIT"; then
      ok "$SMARTD_CONF 적용, $SMARTD_UNIT 재시작"
    else
      warn "$SMARTD_UNIT 재시작 실패 — journalctl -u $SMARTD_UNIT 확인 (모니터링 자체에는 영향 없음)"
    fi
  else
    warn "smartd 서비스 유닛을 찾지 못했습니다"
  fi
else
  ok "MANAGE_SMARTD=no — 건너뜀"
fi

log "7/9 수집 테스트"
COL_OUT="$(sudo -u telegraf "$PY" "$PREFIX/collector/disk_health_collector.py" -c "$ETC/collector.env")"
n_dev=$(grep -c '^smart_device,' <<<"$COL_OUT" || true)
n_err=$(grep -c '^disk_health_error,' <<<"$COL_OUT" || true)
n_sb=$(grep -c '^disk_health_standby,' <<<"$COL_OUT" || true)
ok "SMART 디스크 ${n_dev}개 / 대기 ${n_sb}개 / 오류 ${n_err}개"
if [[ $n_err -gt 0 ]]; then
  grep '^disk_health_error,' <<<"$COL_OUT" | sed 's/^/     /' >&2
  warn "RAID 컨트롤러 뒤 디스크라면 EXTRA_DEVICES 에 '경로:megaraid,N' 등을 지정하세요"
fi
[[ $n_dev -gt 0 || $n_sb -gt 0 ]] || warn "SMART 를 읽은 디스크가 없습니다 (가상머신이면 정상일 수 있음)"
grep '^smart_device,' <<<"$COL_OUT" | "$PY" -c '
import re, sys
for l in sys.stdin:
    dev = re.search(r"device=((?:\\.|[^,\\ ])+)", l).group(1)
    st = re.search(r"status_code=(\d+)i", l)
    rs = re.search(r"status_reason=\"((?:\\.|[^\"])*)\"", l)
    print("     %-22s status=%s  %s" % (dev, st.group(1) if st else "?", rs.group(1) if rs else ""))
' || true
timeout 300 "$TELEGRAF_BIN" --config "$ETC/telegraf.conf" --config-directory "$ETC/telegraf.d" --test >/dev/null 2>"$STATE/telegraf-test.log" \
  || { cat "$STATE/telegraf-test.log" >&2; die "telegraf --test 실패 (설정 오류)"; }
ok "telegraf --test 통과"

log "8/9 InfluxDB 연결 확인 / 초기화"
"$PY" "$REPO_DIR/dhctl.py" -c "$CONFIG" ping
if yes_no "$INFLUX_INIT"; then
  "$PY" "$REPO_DIR/dhctl.py" -c "$CONFIG" influx-init
fi

log "9/9 서비스 시작: $UNIT"
render_unit > "/etc/systemd/system/$UNIT"
systemctl daemon-reload
systemctl enable "$UNIT" >/dev/null 2>&1
systemctl restart "$UNIT"
sleep 3
systemctl is-active --quiet "$UNIT" || { journalctl -u "$UNIT" -n 30 --no-pager >&2; die "$UNIT 시작 실패"; }
ok "$UNIT active"

if [[ $SKIP_VERIFY != yes ]]; then
  "$PY" "$REPO_DIR/dhctl.py" -c "$CONFIG" verify --host "$HOST_TAG" --wait 120 --measurement diskio \
    || warn "아직 InfluxDB 에서 데이터가 확인되지 않았습니다: journalctl -u $UNIT -f"
fi

cat <<EOF

${c_ok}설치 완료${c_off}
  - SMART 데이터는 ${SMART_INTERVAL} 주기로 올라갑니다 (첫 수집까지 최대 ${SMART_INTERVAL})
  - 상태 확인:    systemctl status $UNIT ; journalctl -u $UNIT -f
  - 데이터 확인:  sudo python3 $PREFIX/dhctl.py -c $ETC/config.env verify --host $HOST_TAG
  - Grafana 등록: python3 dhctl.py -c config.env grafana   (Grafana 에 접근 가능한 곳에서 1회)
EOF
