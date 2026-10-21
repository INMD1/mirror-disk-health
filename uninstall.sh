#!/usr/bin/env bash
# mirror-disk-health 제거 (root)
#   sudo ./uninstall.sh            # 서비스/설정/수집기/sudoers 제거, smartd.conf 원복
#   sudo ./uninstall.sh --purge    # + telegraf 패키지와 InfluxData 저장소까지 제거
# InfluxDB 의 데이터와 Grafana 대시보드는 건드리지 않습니다.
set -Eeuo pipefail
PURGE=no
[[ "${1:-}" == "--purge" ]] && PURGE=yes
[[ $EUID -eq 0 ]] || { echo "root 로 실행하세요" >&2; exit 1; }

UNIT=telegraf-disk-health.service
MARK="# managed by mirror-disk-health"

systemctl disable --now "$UNIT" >/dev/null 2>&1 || true
rm -f "/etc/systemd/system/$UNIT"
systemctl daemon-reload
rm -f /etc/sudoers.d/telegraf-disk-health
rm -rf /opt/disk-health /etc/disk-health /var/lib/disk-health

for conf in /etc/smartd.conf /etc/smartmontools/smartd.conf; do
  if [[ -f "$conf.orig-disk-health" ]]; then
    mv -f "$conf.orig-disk-health" "$conf"
    echo "복원: $conf"
  elif [[ -f "$conf" ]] && grep -q "$MARK" "$conf"; then
    echo "DEVICESCAN" > "$conf"
    echo "기본값으로 초기화: $conf"
  fi
done
for u in smartmontools.service smartd.service; do
  systemctl cat "$u" >/dev/null 2>&1 && { systemctl restart "$u" || true; break; }
done

if [[ $PURGE == yes ]]; then
  if command -v apt-get >/dev/null; then
    apt-get remove -y -qq telegraf || true
    rm -f /etc/apt/sources.list.d/influxdata.list /etc/apt/keyrings/influxdata-archive.gpg
  else
    (dnf remove -y -q telegraf || yum remove -y -q telegraf) || true
    rm -f /etc/yum.repos.d/influxdata.repo
  fi
fi
echo "제거 완료"
