# Wi-Fi 핫스팟 비교 테스트 보고서

- **테스트 일시**: 2026-06-02
- **클라이언트**: MediaTek Wi-Fi 7 MT7925 (802.11be)
- **비교 대상 (내장)**: physicar-f3f83825 (BSSID: `2e:cf:67:54:ef:d6`)
- **테스트 스트림**: 각 핫스팟의 카메라 HTTPS 비디오 스트림 (10초간 수신)

---

## 장비 정보

| | **제품 A (USB 핫스팟)** | **제품 B (USB 핫스팟)** | **내장 핫스팟** |
|---|---|---|---|
| SSID | physicar-usb-test | physicar-usb-test | physicar-f3f83825 |
| BSSID | `18:69:45:42:6d:73` | `20:e1:5d:68:43:c3` | `2e:cf:67:54:ef:d6` |
| 밴드 | 5 GHz / ch 36 | 5 GHz / ch 36 | 5 GHz / ch 36 |
| 규격 | 802.11ac | 802.11ac | 802.11ac |
| 인증 | WPA2-PSK | WPA2-PSK | WPA-PSK |
| 암호화 | CCMP | CCMP | CCMP |

---

## 1. 제품 A — USB 핫스팟 vs 내장

### 1-1. 가까이

| 항목 | **제품 A (USB)** | **내장** | 비교 |
|---|---|---|---|
| RSSI | **-39 dBm (94%)** | -54 dBm (100%) | USB가 15 dB 강함 |
| 링크 속도 (Tx/Rx) | 173.3 Mbps | **433.3 Mbps** | 내장이 2.5배 |
| 핑 평균 | **3 ms** | 5 ms | USB가 2ms 낮음 |
| 핑 범위 | 3 ~ 3 ms | 4 ~ 8 ms | USB가 안정적 |
| 스트림 속도 | 692 KB/s (≈5.5 Mbps) | 688 KB/s (≈5.5 Mbps) | 동일 |

### 1-2. 멀리

| 항목 | **제품 A (USB)** | **내장** | 비교 |
|---|---|---|---|
| RSSI | **-64 dBm (100%)** | -72 dBm (66%) | USB가 8 dB 강함 |
| 링크 속도 (Tx/Rx) | 173.3 Mbps | 175.5 Mbps | 동일 수준 |
| 핑 평균 | **3 ms** | **113 ms** | 내장 지연 스파이크 발생 |
| 핑 범위 | 3 ~ 4 ms | 5 ~ **777 ms** | 내장 불안정 |
| 스트림 속도 | 743 KB/s (≈5.9 Mbps) | 747 KB/s (≈6.0 Mbps) | 동일 |

### 제품 A 소결

- 가까이/멀리 모두 USB가 **신호 강도 우위**
- 멀리에서 내장은 **심각한 지연 스파이크** (777ms) 발생, USB는 안정 유지
- 스트림 속도는 카메라 비트레이트 병목으로 차이 없음

---

## 2. 제품 B — USB 핫스팟 vs 내장

### 2-1. 가까이

| 항목 | **제품 B (USB)** | **내장** | 비교 |
|---|---|---|---|
| RSSI | **-38 dBm (94%)** | -51 dBm (89%) | USB가 13 dB 강함 |
| 링크 속도 (Tx/Rx) | 173.3 Mbps | **433.3 Mbps** | 내장이 2.5배 |
| 핑 평균 | **3 ms** | 4 ms | USB가 1ms 낮음 |
| 핑 범위 | 3 ~ 4 ms | 3 ~ 8 ms | USB가 안정적 |
| 스트림 속도 | 709 KB/s (≈5.7 Mbps) | 710 KB/s (≈5.7 Mbps) | 동일 |

### 2-2. 멀리

| 항목 | **제품 B (USB)** | **내장** | 비교 |
|---|---|---|---|
| RSSI | **-57 dBm (83%)** | -75 dBm (63%) | USB가 18 dB 강함 |
| 링크 속도 (Tx/Rx) | 173.3 Mbps | 234 / 117 Mbps | - |
| 핑 평균 | **2 ms** | 5 ms | USB가 3ms 낮음 |
| 핑 범위 | 2 ~ 4 ms | 5 ~ 7 ms | USB가 안정적 |
| 스트림 속도 | 764 KB/s (≈6.1 Mbps) | 765 KB/s (≈6.1 Mbps) | 동일 |

### 제품 B 소결

- 가까이/멀리 모두 USB가 **신호 강도 및 지연 우위**
- 멀리에서 내장은 -75 dBm으로 5GHz 한계 근처 (더 멀어지면 끊김 위험)
- 제품 A 대비 원거리 신호 감쇠가 적음 (-57 vs -64)

---

## 3. 전체 종합

### RSSI 비교 (dBm)

| 거리 | 제품 A (USB) | 제품 B (USB) | 내장 |
|---|---|---|---|
| 가까이 | -39 | **-38** | -51 ~ -54 |
| 멀리 | -64 | **-57** | -72 ~ -75 |

### 핑 평균 비교 (ms)

| 거리 | 제품 A (USB) | 제품 B (USB) | 내장 |
|---|---|---|---|
| 가까이 | 3 | 3 | 4 ~ 5 |
| 멀리 | 3 | **2** | 5 ~ 113 |

### 스트림 속도 비교 (KB/s)

| 거리 | 제품 A (USB) | 제품 B (USB) | 내장 |
|---|---|---|---|
| 가까이 | 692 | 709 | 688 ~ 710 |
| 멀리 | 743 | **764** | 747 ~ 765 |

> 스트림 속도는 모든 조건에서 약 5.5~6.1 Mbps로 카메라 인코딩 비트레이트가 병목.

---

## 4. 최종 결론

1. **USB 핫스팟이 내장 대비 신호 강도 10~18 dB, 지연 1~3ms 우위**
2. **제품 B가 제품 A보다 원거리 성능 우수** (멀리에서 RSSI -57 vs -64, 핑 2ms vs 3ms)
3. 내장 핫스팟은 원거리에서 **지연 불안정** (제품 A 테스트 시 777ms 스파이크 관측)
4. 비디오 스트림 처리량은 Wi-Fi 성능과 무관 — **카메라 비트레이트(~6 Mbps)가 병목**
5. 실시간 제어가 필요한 경우 **USB 핫스팟 (특히 제품 B) 권장**

### 참고: 제품 B 연결 이슈

- 제품 B는 초기 WPA2/WPA3 혼합 모드로 인해 MediaTek 드라이버와 GCMP 협상 충돌 발생
- **AP를 WPA2-PSK + CCMP 전용**으로 설정 변경 후 정상 연결
- 다양한 클라이언트 호환을 위해 WPA2-PSK + CCMP 전용 설정 권장

---

## 5. 발생한 문제 및 재발 방지

### 문제 1: USB WiFi 드라이버 segfault (커널 oops)

| | |
|---|---|
| **증상** | TX20U Nano (35bc:0108) 바인딩 시 커널 NULL pointer dereference → USB 컨트롤러 사망 |
| **근본 원인** | `new_id`로 USB ID만 등록하면 `driver_info=0`이 됨. Realtek 드라이버는 `driver_info`로 칩 타입(RTL8852A/B)을 판별하는데, 0이면 `hal_ops` 테이블이 NULL → 크래시 |
| **해결** | (1) rtl8852bu 소스에 35bc:0108을 `RTL8852B`로 하드코딩 (DKMS 빌드 시 자동 패치)<br>(2) 미지의 어댑터는 **ref-ID 메커니즘** 사용: `echo "VID PID ff refVID refPID" > new_id` → 기존 테이블 엔트리에서 driver_info 복사 |
| **방지책** | `new_id`에 VID/PID만 넣지 말 것. **반드시 ref-ID 포맷 사용**. autobind 스크립트에 이미 적용됨 |

### 문제 2: WPA2/WPA3 혼합 모드 → Windows 연결 실패

| | |
|---|---|
| **증상** | USB AP에 Windows (MediaTek MT7925) 연결 시 4-way 핸드셰이크 실패 |
| **근본 원인** | NM + wpa_supplicant가 `WPA-PSK`와 함께 `WPA-PSK-SHA256` AKM을 자동 광고. Windows MediaTek 드라이버가 이를 WPA3 전환모드로 해석 → GCMP 협상 시도 → AP는 CCMP만 지원 → 불일치로 실패 |
| **해결** | AP 프로필에 `wifi-sec.pmf disable` 명시 설정. 이로 인해 Group cipher도 CCMP로 정상화 (이전엔 TKIP 혼입) |
| **방지책** | USB AP 프로필 생성 시 아래 설정 **필수**:<br>`wifi-sec.key-mgmt wpa-psk`<br>`wifi-sec.proto rsn`<br>`wifi-sec.pairwise ccmp`<br>`wifi-sec.group ccmp`<br>`wifi-sec.pmf disable` |

### 문제 3: 드라이버 크래시 → 모듈 refcount=-1 → 재부팅 필요

| | |
|---|---|
| **증상** | 8852au `netdev_open`에서 크래시 후 `lsmod`에 refcount=-1 표시. `modprobe -r` 불가, `modprobe` 불가 → 좀비 상태 |
| **근본 원인** | 크래시가 `netdev_open` 도중 발생하여 모듈 참조 카운트 증가 후 정상 감소 경로를 타지 못함 |
| **해결** | 재부팅 외 방법 없음 (커널 모듈 refcount가 음수이므로 어떤 언로드 명령도 거부) |
| **방지책** | (1) 문제 1의 ref-ID를 올바르게 사용하여 크래시 원천 차단<br>(2) AP 올리기 전 인터페이스가 `disconnected` (not `unavailable`) 상태인지 확인<br>(3) 크래시 발생 시 무한 재시도하지 말고 즉시 재부팅 |

### 문제 4: NM 핫스팟 프로필 재부팅 시 소멸

| | |
|---|---|
| **증상** | `usb-hotspot` 프로필이 재부팅 후 `unknown connection` |
| **근본 원인** | NM이 AP 모드 프로필을 volatile(휘발성)로 처리하거나, 디바이스 미감지 시 정리할 수 있음 |
| **해결** | 재생성 (동일 명령) |
| **방지책** | 프로필을 영구화하려면 `/etc/NetworkManager/system-connections/`에 파일이 실제 존재하는지 확인. 또는 부팅 스크립트에서 자동 생성하도록 구성 |

### USB AP 프로필 생성 표준 명령 (복붙용)

```bash
sudo nmcli connection add type wifi \
  ifname <USB_IFACE> con-name "usb-hotspot" ssid "<SSID>" \
  wifi.mode ap wifi.band a wifi.channel 36 \
  wifi-sec.key-mgmt wpa-psk \
  wifi-sec.proto rsn \
  wifi-sec.pairwise ccmp \
  wifi-sec.group ccmp \
  wifi-sec.pmf disable \
  wifi-sec.psk "<PASSWORD>" \
  ipv4.method shared ipv4.addresses 10.42.1.1/24 \
  connection.autoconnect no
```

### ref-ID 바인딩 표준 명령 (복붙용)

```bash
# 드라이버별 참조 ID (기존 USB ID 테이블에 있는 안전한 엔트리)
# rtl8852au: 0bda:8832 (RTL8852A)
# rtl8852bu: 0bda:b832 (RTL8852B)  
# rtl8852cu: 0bda:8832 (RTL8852A)

echo "<NEW_VID> <NEW_PID> ff <REF_VID> <REF_PID>" > /sys/bus/usb/drivers/<DRIVER>/new_id
```
