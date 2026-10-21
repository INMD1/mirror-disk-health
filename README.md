# mirror-disk-health

미러 서버의 **물리 디스크 건강 상태(SMART)** 를 smartmontools 로 수집해서 컨트롤러의 **InfluxDB** 에 저장하고,
**Grafana** 대시보드로 모니터링하는 레포입니다. (Kolla-Ansible 이 띄운 InfluxDB 1.x / Grafana 를 기준으로 작성, InfluxDB 2.x 도 지원)

```
 ┌──────────────── 미러 서버 ─────────────────┐            ┌────────── 컨트롤러 ──────────┐
 │ smartd ── 정기 셀프테스트(Short 매일/Long 매주) │            │                               │
 │                                            │  HTTP 8086 │  InfluxDB  db=disk_health      │
 │ telegraf-disk-health.service               │ ─────────▶ │  (RP disk_health, 400d)        │
 │  ├ exec: disk_health_collector.py (5m)     │            │        ▲                      │
 │  │    └ sudo smartctl -j  (ATA/NVMe/SAS/   │            │        │ InfluxQL              │
 │  │       MegaRAID) + zpool (ZFS)           │            │  Grafana "Disk Health (SMART)" │
 │  ├ diskio / disk / mdstat (30s)            │            │                               │
 └────────────────────────────────────────────┘            └───────────────────────────────┘
```

## 무엇을 보나

| 구분 | 항목 |
|---|---|
| 판정 | SMART overall(PASSED/FAILED), smartctl 종료코드 비트(디스크 실패·prefail·에러로그·셀프테스트 실패), **상태 코드 0 정상 / 1 경고 / 2 위험** + 사유 문자열 |
| HDD (ATA) | 재할당(#5)·보류(#197)·복구불가(#198)·UNC(#187)·CRC(#199)·Command Timeout(#188)·Spin Retry(#10), 온도, 가동시간, 전원 사이클, 누적 읽기/쓰기(Device Statistics), ATA 에러 로그 수, 원본 SMART 속성 전체 |
| SSD (ATA) | 위 항목 + 수명 사용률 (Device Statistics → 벤더 속성 #231/#233/#177/#202/#169/#173 순으로 판단) |
| NVMe | Critical Warning, Available Spare/Threshold, Percentage Used, Media Errors, 에러 로그 수, Unsafe Shutdowns, 경고/위험 온도 누적 시간, 누적 읽기/쓰기 |
| SAS (SCSI) | Grown Defect List, 읽기/쓰기/검증 미정정 에러, 처리량, 수명 사용률, 셀프테스트 |
| 셀프테스트 | 마지막 결과(PASS/FAIL), 종류, 몇 시간 전인지, 진행 중 여부/남은 % |
| RAID | `/proc/mdstat` (실패/다운/스페어/리빌드 진행률), ZFS 풀 health·용량·단편화·READ/WRITE/CKSUM 에러·비정상 vdev·리실버/스크럽 |
| I/O | IOPS, 처리량, %util, await(평균 응답시간), 큐 길이, 파일시스템 사용률 |

스핀다운된 HDD 는 깨우지 않고(`-n standby`) 건너뛰며, RAID 컨트롤러(MegaRAID 등) 뒤의 디스크도 `EXTRA_DEVICES` 로 수집할 수 있습니다.

### 상태 판정 기준 (`status_code`)

| 코드 | 조건 |
|---|---|
| **2 위험** | SMART FAILED, smartctl "disk failing"/prefail 임계값 이하, 지금 실패 중인 속성(WHEN_FAILED=now), 보류 섹터 > 0, 복구불가 섹터 > 0, NVMe Critical Warning ≠ 0, NVMe 미디어 에러 > 0, Available Spare ≤ Threshold, 마지막 셀프테스트 실패, 수명 사용률 ≥ 100% |
| **1 경고** | 재할당 섹터 > 0, UNC > 0, CRC > 0 (케이블/백플레인), SAS grown defect/미정정 에러 > 0, 과거에 임계값 이하였던 속성, 수명 사용률 ≥ `WEAR_WARN_PERCENT`, 온도 ≥ `TEMP_WARN_*` |
| **0 정상** | 위에 해당 없음 |

> NVMe 의 `num_err_log_entries` 는 드라이버가 미지원 기능을 조회할 때도 증가하는 경우가 많아 상태에 반영하지 않고 값만 기록합니다.

## 요구사항

- 미러 서버: Linux + systemd, Debian/Ubuntu(apt) 또는 RHEL/Rocky(dnf/yum), Python 3.6+, smartmontools **7.0+** (Ubuntu 20.04 / Debian 11 / RHEL 8 이상 기본 패키지면 충족)
- 인터넷: InfluxData 저장소(`repos.influxdata.com`)에서 Telegraf 설치 (이미 Telegraf 가 있으면 생략)
- 네트워크: 미러 서버 → 컨트롤러 InfluxDB `:8086`, Grafana → InfluxDB `:8086`

## 빠른 시작

### 0. 컨트롤러 정보 확인 (Kolla-Ansible)

```bash
# 컨트롤러에서
grep -E '^(enable_influxdb|enable_grafana|kolla_internal_vip_address|network_interface|api_interface)' /etc/kolla/globals.yml
docker ps --format '{{.Names}}' | grep -E 'influxdb|grafana'
ss -ltnp | grep 8086                       # InfluxDB 가 바인딩된 IP 확인 (api_interface IP)
grep grafana_admin_password /etc/kolla/passwords.yml

# 미러 서버에서 접근 확인 (204 가 나와야 함)
curl -i http://<컨트롤러 내부 IP>:8086/ping
```

Kolla 의 InfluxDB 는 기본적으로 인증이 꺼져 있습니다. 켜 두었다면 `INFLUX_USER/PASSWORD`(쓰기용)와 `INFLUX_ADMIN_USER/PASSWORD`(DB 생성용)를 채우세요.

### 1. 미러 서버에 설치

```bash
git clone <this-repo> mirror-disk-health && cd mirror-disk-health
cp config.env.example config.env
vi config.env            # INFLUX_URL, (필요 시) EXTRA_DEVICES 등
sudo ./install.sh
```

`install.sh` 가 하는 일 (재실행해도 안전 — 설정 변경 후 다시 돌리면 됩니다)

1. smartmontools, nvme-cli, sudo, python3 설치 + smartctl JSON 지원 확인
2. Telegraf 설치 (InfluxData 공식 저장소, 키 지문 검증). 새로 설치한 경우 패키지 기본 `telegraf.service` 는 비활성화 (localhost 로 보내도록 되어 있어서)
3. 수집기 → `/opt/disk-health/`, 설정 → `/etc/disk-health/`
4. `/etc/sudoers.d/telegraf-disk-health` — telegraf 사용자에게 **`smartctl -j …` 만** 허용 (`visudo` 검증)
5. smartd 정기 셀프테스트 설정 (기존 설정은 `*.orig-disk-health` 로 백업)
6. 수집기 1회 실행 결과 요약 + `telegraf --test` 로 설정 검증
7. InfluxDB 연결 확인 및 DB/보존정책 생성 (`dhctl.py influx-init`)
8. `telegraf-disk-health.service` 시작 후 InfluxDB 에 데이터가 들어오는지 확인

기존에 다른 용도로 Telegraf 를 쓰고 있어도 **별도 인스턴스**(`telegraf-disk-health.service`, 설정 `/etc/disk-health/`)로 돌기 때문에 서로 간섭하지 않습니다.

### 2. Grafana 등록 (1회)

Grafana 에 접근 가능한 곳(컨트롤러, 미러 서버, 작업 PC 어디든)에서:

```bash
python3 dhctl.py -c config.env grafana
```

- 데이터소스 `Disk Health InfluxDB` (uid `disk-health-influx`) 생성/갱신 + 연결 테스트
- 폴더 `Disk Health` 에 대시보드 `Disk Health (SMART)` 등록 (재실행 시 덮어씀)
- Grafana 가 InfluxDB 를 다른 주소로 봐야 하면 `GRAFANA_INFLUX_URL` 지정

수동으로 하려면 `grafana/disk-health.json` 을 Import 하고 상단의 `InfluxDB` 변수에서 데이터소스를 고르면 됩니다.

### 3. 확인

```bash
sudo python3 /opt/disk-health/dhctl.py -c /etc/disk-health/config.env verify --host $(hostname -s)
systemctl status telegraf-disk-health
journalctl -u telegraf-disk-health -f
sudo -u telegraf python3 /opt/disk-health/collector/disk_health_collector.py   # 수집기 출력 직접 보기
```

SMART 는 `SMART_INTERVAL`(기본 5분) 마다 올라가므로 첫 데이터까지 최대 5분 걸립니다. I/O 지표는 30초 안에 보입니다.

## 대시보드

| 행 | 내용 |
|---|---|
| 개요 | 디스크 수, 위험/경고/정상 개수, 최고 온도, 스핀다운 수, 수집 오류, ZFS 비정상 vdev, MD 실패 디스크 |
| 디스크 상태 | 호스트·장치·모델·시리얼별 상태/사유/온도/가동시간/각종 에러 카운터/수명/셀프테스트/용량/펌웨어 (색상 표시, 상태순 정렬) |
| 온도 | 디스크별 온도 추이 (50/60°C 기준선) |
| 에러 카운터 | 재할당 · 보류/복구불가 · CRC/UNC · NVMe 미디어 에러 · SAS 결함 추이 + **선택 기간 동안 증가한 카운터** 표 |
| SSD / NVMe 수명 | 수명 사용률 게이지, 일별 기록량(30일), NVMe 예비 공간, 불안전 종료 |
| 디스크 I/O | IOPS, 처리량, %util, await, 큐 길이 |
| 파일시스템 | 사용률, 사용량 추이 |
| RAID / ZFS | mdstat 표, ZFS 풀 표 |
| ATA SMART 속성 원본 (접힘) | 모든 속성의 value/worst/thresh/raw |
| 수집기 (접힘) | 수집 소요 시간, 열지 못한 장치와 메시지, smartctl/수집기 버전 |

변수: `호스트`, `SMART 장치`, `I/O 장치` (다중 선택). 대시보드를 고칠 때는 `grafana/build_dashboard.py` 를 수정하고 `python3 grafana/build_dashboard.py` 로 다시 생성하세요.

### 알림 예시 (Grafana Alerting)

데이터소스 `Disk Health InfluxDB`, 쿼리:

```sql
SELECT max("status_code") FROM "smart_device" WHERE $timeFilter GROUP BY "host", "device", "model"
```

조건: Reduce `Last` → Threshold `IS ABOVE 1` (위험), 평가 주기 5m / pending 10m. 경고까지 받으려면 `IS ABOVE 0`.
수집 중단 감지는 `SELECT count("duration_ms") FROM "disk_health_collector" WHERE $timeFilter GROUP BY "host"` 에 No Data → Alerting.

## 설정 (`config.env`)

| 키 | 기본값 | 설명 |
|---|---|---|
| `INFLUX_VERSION` | `1` | 1 = InfluxDB 1.x (Kolla), 2 = 2.x |
| `INFLUX_URL` | — | 컨트롤러 InfluxDB 주소 |
| `INFLUX_DB` / `INFLUX_RETENTION` | `disk_health` / `400d` | 1.x DB 와 기본 보존정책 `disk_health` 기간 (`INF` 가능) |
| `INFLUX_ORG` / `INFLUX_BUCKET` / `INFLUX_TOKEN` | | 2.x 용. Grafana 조회용 DBRP(`INFLUX_DB/autogen → bucket`)도 자동 생성 |
| `SMART_INTERVAL` / `SMART_TIMEOUT` | `5m` / `240s` | SMART 수집 주기 / 제한 시간 |
| `IO_INTERVAL` | `30s` | diskio/disk/mdstat 주기 |
| `SKIP_STANDBY` | `yes` | 스핀다운된 디스크를 깨우지 않음 |
| `EXTRA_DEVICES` | | 스캔에 안 잡히는 디스크 `경로:타입` (예: `/dev/bus/0:megaraid,0 /dev/sda:cciss,1`) |
| `EXCLUDE_REGEX` | | 제외할 장치 정규식 |
| `TEMP_WARN_HDD/SSD/NVME` | `50/65/70` | 경고 온도 |
| `WEAR_WARN_PERCENT` | `80` | 수명 사용률 경고 기준 |
| `MANAGE_SMARTD` / `SMARTD_SCHEDULE` | `yes` / `(S/../.././02\|L/../../7/03)` | 매일 02시 Short, 일요일 03시 Long |
| `GRAFANA_*` | | `dhctl.py grafana` 용 |

`/etc/disk-health/collector.env` 는 `install.sh` 가 생성합니다. 수집기 옵션만 바꾸고 싶으면 이 파일을 고쳐도 되지만 재설치 시 덮어써집니다.

## 데이터 스키마

| measurement | 태그 | 주요 필드 |
|---|---|---|
| `smart_device` | host, env, role, device, dev_type, protocol(ATA/NVMe/SCSI), kind(hdd/ssd/nvme), model, serial | status_code, status_reason, health_ok, temperature_c, power_on_hours, power_cycles, capacity_bytes, reallocated_sectors, pending_sectors, offline_uncorrectable, reported_uncorrect, udma_crc_errors, command_timeout, critical_warning, available_spare(_threshold), media_errors, error_log_count, unsafe_shutdowns, grown_defects, read/write/verify_uncorrected, life_used_percent, life_remaining_percent, bytes_read/written, last_selftest_passed/status/type/age_hours, selftest_in_progress, attrs_failing_now, exit_* 비트, firmware, wwn |
| `smart_attribute` | host, device, serial, model, kind, attr_id, attr_name | value, worst, thresh, raw, prefailure, failing_now, failed_past |
| `zfs_pool` | host, pool | health, health_ok, state_code, status_code, size/alloc/free_bytes, capacity_percent, fragmentation_percent, read/write/checksum_errors, vdevs_not_online, scrub/resilver_in_progress, last_scrub |
| `disk_health_standby` | host, device | standby |
| `disk_health_error` | host, device, dev_type | exit_status, message |
| `disk_health_collector` | host | devices_ok/standby/error/warning/critical, duration_ms, smartctl_version, collector_version |
| `diskio`, `disk`, `mdstat` | Telegraf 표준 | Telegraf 표준 |

## 트러블슈팅

| 증상 | 확인 / 해결 |
|---|---|
| `disk_health_error: Unable to detect device type` | USB 브리지/RAID 컨트롤러. `sudo smartctl --scan-open` 결과와 `smartctl -d sat,auto`, `-d megaraid,N` 등으로 직접 확인 후 `EXTRA_DEVICES` 에 지정 |
| MegaRAID 뒤 디스크가 안 보임 | `sudo smartctl --scan-open` 에 `/dev/bus/0 -d megaraid,N` 이 나오는지 확인. 안 나오면 `storcli /c0 show` 의 DID 로 `EXTRA_DEVICES="/dev/bus/0:megaraid,<DID> …"` |
| SMART 디스크 0개 | VM(virtio) 이면 정상 — SMART 는 하이퍼바이저/물리 서버에서 수집해야 함 |
| `sudo: a password is required` | `/etc/sudoers.d/telegraf-disk-health` 존재, `#includedir /etc/sudoers.d` 활성 여부 확인. `sudo -u telegraf sudo -n smartctl -j --version` |
| InfluxDB 401 | `INFLUX_USER/PASSWORD`(1.x) 또는 `INFLUX_TOKEN`(2.x) 확인. `python3 dhctl.py -c config.env ping` |
| 데이터는 들어오는데 Grafana 가 빈 화면 | 대시보드 상단 `InfluxDB` 변수가 올바른 데이터소스인지, 데이터소스의 DB 가 `disk_health` 인지 확인. 2.x 면 DBRP 매핑 존재 확인 |
| 기존 DB 를 재사용했더니 옛 데이터가 안 보임 | `influx-init` 이 기본 RP 를 `disk_health` 로 바꿨기 때문. 옛 데이터는 `"autogen".measurement` 로 조회 |
| Long 셀프테스트 때 I/O 지연 | `SMARTD_SCHEDULE` 에서 Long 주기를 월 1회(`L/../01/./03`) 등으로 조정 후 `install.sh` 재실행 |

## 제거

```bash
sudo ./uninstall.sh            # 서비스·설정·sudoers 제거, smartd.conf 원복 (InfluxDB 데이터/Grafana 는 유지)
sudo ./uninstall.sh --purge    # + telegraf 패키지·InfluxData 저장소 제거
```

## 개발 / 테스트

```bash
make test        # 수집기(ATA HDD/SSD, NVMe, SAS, MegaRAID 실패 디스크, 스탠바이, ZFS 픽스처) + dhctl(목 InfluxDB/Grafana) 단위 테스트
make lint        # shellcheck + pyflakes
make dashboard   # grafana/disk-health.json 재생성
make render      # 시스템 변경 없이 생성될 설정을 ./out 에 출력
make influxql    # 대시보드의 모든 InfluxQL 을 공식 influxql 파서로 문법 검증 (Go 필요)
```

```
.
├── install.sh / uninstall.sh       미러 서버 설치·제거
├── config.env.example              설정 템플릿
├── dhctl.py                        ping / influx-init / grafana / verify / query
├── collector/disk_health_collector.py   smartctl JSON → line protocol
├── grafana/build_dashboard.py      대시보드 생성기
├── grafana/disk-health.json        생성된 대시보드
└── tests/                          단위 테스트, 픽스처, InfluxQL 검사기
```
