#!/bin/bash

CONF="/opt/physicar/src/physicar-ros/deploy/sim/supervisord.conf"
DEPLOY_DIR="$(dirname "$CONF")"

# ── src 트리 소유권 보장 (physicar) ──
# 이미지 빌드 시 root 로 클론되면 src/ 가 root 소유가 된다 — 그러면 sim_api(physicar)의
# 월드 임포트(share/ 쓰기)가 PermissionError → 502 로 죽고, updater 의 git fetch 도
# .git 에 못 써서 태그 업데이트가 전면 불능이 된다 (둘 다 실사례). entrypoint 는
# physicar 로 실행되므로 sudo(NOPASSWD)로 부팅마다 멱등 보정한다.
if [ -d /opt/physicar/src ]; then
  sudo mkdir -p /opt/physicar/src/physicar-sim/share/worlds 2>/dev/null || true
  sudo chown -R physicar:physicar /opt/physicar/src 2>/dev/null || true
fi

# ── Stop any previous stack first (script is safe to re-run) ──
# A SIGTERM'd supervisord needs several seconds to stop its children; starting
# a new instance too early fails with "Another program is already listening on
# a port". Shut it down gracefully, WAIT until it is gone, then sweep orphans
# that survive an unclean death and keep the managed ports bound.
if [ -S /tmp/supervisor.sock ]; then
  supervisorctl -c "$CONF" shutdown >/dev/null 2>&1 || true
fi
pkill -f "supervisord.*deploy/sim/supervisord.conf" 2>/dev/null || true
for _ in $(seq 1 20); do
  pgrep -f "supervisord.*deploy/sim/supervisord.conf" >/dev/null || break
  sleep 1
done
pkill -9 -f "supervisord.*deploy/sim/supervisord.conf" 2>/dev/null || true

# Orphan sweep: whatever still holds a managed port or the X display
# (fuser needs root for root-owned nginx; SIGKILL is fine for orphans)
sudo fuser -k 80/tcp 5000/tcp 5901/tcp 6080/tcp 8000/tcp 8080/tcp 9002/tcp 9003/tcp 2>/dev/null || true
pkill -f "gz sim" 2>/dev/null || true
pkill -f "gz-launch" 2>/dev/null || true
pkill Xvfb 2>/dev/null || true
sleep 1

# ── Stale bake self-heal (images built before current layout) ──
# (a) Root-owned /tmp/pc-*.conf|map baked into the image survive in sticky /tmp
#     and block the rewrites below → nginx dies (measured on cloud, twice).
# (b) A leftover /etc/nginx/conf.d/pc-gate.conf symlink from before the
#     zz-pc-gate.conf rename makes nginx fail: map_hash_bucket_size duplicate.
sudo rm -f /tmp/pc-root.conf /tmp/pc-gate.map 2>/dev/null || true
if [ -L /etc/nginx/conf.d/pc-gate.conf ] || [ -e /etc/nginx/conf.d/pc-gate.conf ]; then
  sudo rm -f /etc/nginx/conf.d/pc-gate.conf 2>/dev/null || true
  sudo ln -sf "$DEPLOY_DIR/etc/nginx/conf.d/zz-pc-gate.conf" /etc/nginx/conf.d/zz-pc-gate.conf 2>/dev/null || true
fi

# Select the nginx root (/) snippet BEFORE nginx starts (supervisord child).
# Codespaces: / is not served (VS Code web is the Codespace itself).
# Local sim: / proxies code-server (started by supervisord, non-Codespaces only).
# Copied (not symlinked): fs.protected_symlinks blocks root from following
# a physicar-owned symlink inside sticky /tmp.
if [ -n "${CODESPACE_NAME:-}" ]; then
  cp -f "$DEPLOY_DIR/etc/nginx/root-404.conf" /tmp/pc-root.conf
else
  cp -f "$DEPLOY_DIR/etc/nginx/root-code.conf" /tmp/pc-root.conf
fi
chmod 644 /tmp/pc-root.conf

# ── Origin gate map (see conf.d/pc-gate.conf) ──
# Cloud instances get $PHYSICAR_ORIGIN_GATE_SECRET from the control plane; the
# gate then 403s any request lacking the matching X-PhysiCar-Gate header (blocks
# gateway-bypassing proxying). Without a secret (localhost/Codespaces) → pass.
if [ -n "${PHYSICAR_ORIGIN_GATE_SECRET:-}" ]; then
  printf 'default "deny";\n"%s" "ok";\n' "$PHYSICAR_ORIGIN_GATE_SECRET" > /tmp/pc-gate.map
else
  printf 'default "pass";\n' > /tmp/pc-gate.map
fi
chmod 644 /tmp/pc-gate.map

STUDENT_WS="/home/physicar/physicar_ws"

# ── code-server 버전 수렴 (레포 핀, idempotent) ──
# deploy/code-server-version 이 단일 진실 — 설치본과 다르면 그 버전을 설치한다.
# 베이크 박제 방지: AMI 리베이크 없이 핀 한 줄 갱신으로 전 인스턴스가 다음
# 부팅에 따라온다. 핀을 올리기 전 새 버전 릴리스 tar 를 받아 아래 패치 패턴
# (B/C·folder)과 브랜딩 경로가 유효한지 반드시 grep 으로 확인할 것.
# 실패·오프라인·타임아웃은 현재 버전 유지 — 부팅을 절대 막지 않는다 (다음
# 부팅에 재시도). 이 시점엔 supervisord(=code-server)가 아직 죽어 있어 재시작
# 이 필요 없다. Codespaces 는 code-server 를 쓰지 않으므로 건너뛴다.
ROS_DIR="$(dirname "$(dirname "$DEPLOY_DIR")")"
if [ -z "${CODESPACE_NAME:-}" ]; then
  CS_PIN=$(tr -d '[:space:]' < "$ROS_DIR/deploy/code-server-version" 2>/dev/null || true)
  # --version 첫 줄이 설정파일 생성 로그일 수 있어 버전 라인만 골라낸다
  CS_CUR=$(code-server --version 2>/dev/null | grep -oEm1 '^[0-9]+\.[0-9]+\.[0-9]+' || true)
  if [ -n "$CS_PIN" ] && [ "$CS_PIN" != "$CS_CUR" ]; then
    echo "[code-server] ${CS_CUR:-none} -> $CS_PIN"
    timeout 300 bash -c "curl -fsSL https://code-server.dev/install.sh | sudo sh -s -- --version='$CS_PIN'" \
      || echo "[code-server] WARNING: update failed — keeping ${CS_CUR:-none}"
  fi
fi

# ── code-server webview microphone/camera patch (idempotent, every boot) ──
# The install script patches once, but a code-server update restores the
# bundle — re-apply here (no-op when already patched, or in Codespaces
# where the bundle string simply won't match anything running).
patch_codeserver_webview_media() {
  local cs_bin cs_vscode
  cs_bin=$(readlink -f "$(command -v code-server)" 2>/dev/null) || return 0
  cs_vscode=$(dirname "$cs_bin")/../lib/vscode
  [ -d "$cs_vscode/out" ] || cs_vscode=/usr/lib/code-server/lib/vscode
  [ -d "$cs_vscode/out" ] || { echo "[media-patch] vscode bundle not found"; return 0; }

  # Allow-list patterns per code-server generation (each patched idempotently):
  #  A) legacy literal allow string
  #  B) 4.12x workbench JS — allow list built as a JS array
  #  C) 4.12x inner webview iframe (pre/index.html) — allowRules array
  local A_OLD='clipboard-read; clipboard-write'
  local A_NEW='clipboard-read; clipboard-write; microphone; camera'
  local B_OLD='"cross-origin-isolated","autoplay","local-network-access"'
  local B_NEW='"cross-origin-isolated","autoplay","local-network-access","microphone","camera"'
  local C_OLD="'cross-origin-isolated;', 'autoplay;', 'local-network-access;'"
  local C_NEW="'cross-origin-isolated;', 'autoplay;', 'local-network-access;', 'microphone;', 'camera;'"

  local n=0 f changed
  while IFS= read -r f; do
    changed=0
    if grep -qF "$A_OLD" "$f" && ! grep -qF "$A_NEW" "$f"; then
      sudo sed -i "s/$A_OLD/$A_NEW/g" "$f" && changed=1
    fi
    if grep -qF "$B_OLD" "$f" && ! grep -qF "$B_NEW" "$f"; then
      sudo sed -i "s/$B_OLD/$B_NEW/g" "$f" && changed=1
    fi
    if grep -qF "$C_OLD" "$f" && ! grep -qF "$C_NEW" "$f"; then
      sudo sed -i "s|$C_OLD|$C_NEW|g" "$f" && changed=1
    fi
    [ "$changed" = "1" ] && n=$((n+1))
  done < <(grep -rlF -e "$A_OLD" -e "$B_OLD" -e "$C_OLD" "$cs_vscode/out" 2>/dev/null)
  echo "[media-patch] patched $n file(s) under $cs_vscode/out"

  # Silent-failure guard: after patching, at least one file must carry one of
  # the patched allow-lists. If none do, a code-server update changed the
  # pattern shape (it happened at 4.12x already) — warn loudly so it shows up
  # in the boot log instead of mic/cam just silently breaking.
  if ! grep -rqF -e "$A_NEW" -e "$B_NEW" -e "$C_NEW" "$cs_vscode/out" 2>/dev/null; then
    echo "[media-patch] WARNING: no known allow-list pattern found in this code-server version — webview mic/cam will stay blocked until the patterns in this function are updated"
  fi

  # CSP 해시 재동기화: 최신 code-server 의 webview index.html 은 인라인 스크립트를
  # CSP 'sha256-…' 로 고정한다. C 패턴이 그 스크립트를 수정하는 순간 해시가 어긋나
  # 브라우저가 스크립트를 차단 → webview(확장 패널·커스텀 에디터) 전면 빈 화면.
  # → 패치된 HTML 마다 인라인 스크립트의 sha256 을 재계산해 CSP 를 맞춘다 (멱등).
  while IFS= read -r f; do
    sudo python3 - "$f" <<'PYCSP'
import sys, re, hashlib, base64
p = sys.argv[1]
s = open(p, encoding='utf-8').read()
m = re.search(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", s, re.S)
if not m: sys.exit(0)
h = base64.b64encode(hashlib.sha256(m.group(1).encode()).digest()).decode()
s2, n = re.subn(r"'sha256-[A-Za-z0-9+/=]+'", "'sha256-" + h + "'", s)
if n and s2 != s:
    open(p, 'w', encoding='utf-8').write(s2)
    print('[media-patch] CSP hash resynced: ' + p)
PYCSP
  done < <(grep -rlF "$C_NEW" "$cs_vscode/out" --include='*.html' 2>/dev/null)
}
patch_codeserver_webview_media || true

# ── code-server 기본 폴더 패치 (idempotent, every boot) ──
# 쿼리 없는 / 를 기본 워크스페이스(/home/physicar/physicar_ws)로 연다.
# 워크벤치는 folder 를 브라우저 주소창에서만 읽으므로 서버측 리다이렉트(302)는
# 주소창을 ?folder=… 로 더럽히고, 주소창을 잠깐 조작하는 방식은 워크벤치가
# 쿼리를 읽는 시점과 레이스가 난다 → 워크스페이스 프로바이더 번들에 "쿼리
# 부재 시 기본 폴더" 폴백을 직접 심는 것이 유일하게 결정론적이다.
# (nginx 내부 rewrite 가 서버 302 를 막고, 이 패치가 클라이언트 기본값을 만든다)
patch_codeserver_default_folder() {
  local cs_bin cs_vscode wb
  cs_bin=$(readlink -f "$(command -v code-server)" 2>/dev/null) || return 0
  cs_vscode=$(dirname "$cs_bin")/../lib/vscode
  [ -d "$cs_vscode/out" ] || cs_vscode=/usr/lib/code-server/lib/vscode
  wb="$cs_vscode/out/vs/code/browser/workbench/workbench.js"
  [ -f "$wb" ] || { echo "[folder-patch] workbench.js not found"; return 0; }
  grep -q 'pc-default-folder' "$wb" && return 0
  sudo python3 - "$wb" <<'PYFOLD'
import sys
p = sys.argv[1]
s = open(p, encoding='utf-8').read()
old = 'new URL(document.location.href).searchParams.forEach'
new = ('(()=>{/*pc-default-folder*/const u=new URL(document.location.href);'
       'u.searchParams.has("folder")||u.searchParams.has("workspace")||u.searchParams.has("ew")||'
       'u.searchParams.set("folder","/home/physicar/physicar_ws");return u})().searchParams.forEach')
n = s.count(old)
if n != 1:
    print('[folder-patch] WARNING: pattern x%d (expected 1) — skipped, default folder inactive' % n)
    sys.exit(0)
open(p, 'w', encoding='utf-8').write(s.replace(old, new))
print('[folder-patch] default folder patched into workbench.js')
PYFOLD
}
patch_codeserver_default_folder || true

# ── 브랜딩 재적용 (idempotent, every boot) ──
# 파비콘/PWA/타이틀바 아이콘은 설치 트리 안에 있어 code-server 업데이트가
# 원복시킨다 — media patch 와 같은 이유로 부팅마다 다시 덮는다 (install-sim.sh
# 의 브랜딩 블록과 동일 로직, 여기는 physicar 사용자라 sudo 필요).
brand_codeserver() {
  local static res media om b64 _svg _png
  static="$ROS_DIR/physicar_webserver/static"
  [ -f "$static/favicon.ico" ] || return 0
  res=$(find /usr/lib /usr/local/lib -path '*code-server*/lib/vscode/resources/server' -type d 2>/dev/null | head -1)
  if [ -n "$res" ]; then
    sudo cp "$static/favicon.ico" "$res/favicon.ico"
    sudo cp "$static/img/code-192.png" "$res/code-192.png"
    sudo cp "$static/img/code-512.png" "$res/code-512.png"
  fi
  media=$(find /usr/lib /usr/local/lib -path '*code-server*/src/browser/media' -type d 2>/dev/null | head -1)
  if [ -n "$media" ]; then
    sudo cp "$static/favicon.ico" "$media/favicon.ico"
    b64=$(base64 -w0 "$static/img/code-192.png")
    for _svg in favicon.svg favicon-dark-support.svg; do
      printf '<svg xmlns="http://www.w3.org/2000/svg" width="192" height="192"><image width="192" height="192" href="data:image/png;base64,%s"/></svg>' "$b64" | sudo tee "$media/$_svg" >/dev/null
    done
    for _png in pwa-icon-192.png pwa-icon-maskable-192.png; do
      [ -f "$media/$_png" ] && sudo cp "$static/img/code-192.png" "$media/$_png"
    done
    for _png in pwa-icon-512.png pwa-icon-maskable-512.png; do
      [ -f "$media/$_png" ] && sudo cp "$static/img/code-512.png" "$media/$_png"
    done
  fi
  om=$(find /usr/lib /usr/local/lib -path '*code-server*/lib/vscode/out/media' -type d 2>/dev/null | head -1)
  if [ -n "$om" ] && [ -f "$om/code-icon.svg" ]; then
    b64=$(base64 -w0 "$static/img/code-192.png")
    printf '<svg xmlns="http://www.w3.org/2000/svg" width="192" height="192"><image width="192" height="192" href="data:image/png;base64,%s"/></svg>' "$b64" | sudo tee "$om/code-icon.svg" >/dev/null
  fi
  return 0
}
brand_codeserver || true

# ── 알림 방해금지(DND) 기본값 시드 (idempotent) ──
# 우측 하단 알림 팝업을 "에러만" 남기고 숨긴다. DND 는 settings.json 키가 아니라
# 전역 상태 DB(state.vscdb)의 토글이라 여기서 심는다. 키가 이미 있으면(사용자가
# 직접 토글했다면) 그 선택을 존중한다. 실패해도 부팅은 계속 (best-effort).
python3 - <<'PYDND' || true
import sqlite3, os
p = os.path.expanduser('~/.local/share/code-server/User/globalStorage/state.vscdb')
os.makedirs(os.path.dirname(p), exist_ok=True)
try:
    db = sqlite3.connect(p, timeout=3)
    db.execute('CREATE TABLE IF NOT EXISTS ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)')
    cur = db.execute("SELECT value FROM ItemTable WHERE key='notifications.doNotDisturbMode'").fetchone()
    if cur is None:
        db.execute("INSERT OR REPLACE INTO ItemTable (key, value) VALUES ('notifications.doNotDisturbMode','true')")
        db.commit()
    db.close()
except Exception:
    pass
PYDND

# ── settings.json 병합 시드 (심링크 → 실파일 전환) ──
# 종전 심링크(레포行, root 소유·읽기전용)는 사용자의 설정 저장이 실패하게 만들어
# "저장 안 된 settings.json" 편집기가 세션마다 복원되는 문제를 낳았다.
# → 사용자 소유 실파일로 전환하고 부팅마다 관리 기본값을 병합한다
#   (사용자가 바꾼 키는 사용자 값 우선, 새 기본값 키는 계속 전파).
python3 - "$DEPLOY_DIR/home/physicar/.local/share/code-server/User/settings.json" <<'PYSET' || true
import json, os, sys
managed_path = sys.argv[1]
user_path = os.path.expanduser('~/.local/share/code-server/User/settings.json')
os.makedirs(os.path.dirname(user_path), exist_ok=True)
try:
    managed = json.load(open(managed_path))
except Exception:
    sys.exit(0)
user = {}
if os.path.islink(user_path):
    os.remove(user_path)   # 구 심링크 제거 (읽기전용 저장 실패의 원인)
elif os.path.exists(user_path):
    try:
        user = json.load(open(user_path))
    except Exception:
        user = {}
merged = {**managed, **user}
if not os.path.exists(user_path) or merged != user:
    json.dump(merged, open(user_path, 'w'), indent=2, ensure_ascii=False)
PYSET
# 위 병합은 root 로 실행되므로 소유권을 사용자에게 되돌린다 — root 소유로 남으면
# code-server(physicar)의 설정 저장이 EACCES 로 실패한다 (Claude Code 등 확장이
# 켜지며 설정 키를 기록하는 순간 포함). 이미 오염된 인스턴스도 부팅마다 여기서 수복.
chown physicar:physicar /home/physicar/.local/share/code-server/User \
  /home/physicar/.local/share/code-server/User/settings.json 2>/dev/null || true



# Prune orphaned bytecode from the persistent pycache: entries whose source
# file was deleted or renamed (updates, student edits) would otherwise
# accumulate forever. Background — boot must not wait. A false delete is
# harmless (recompiles lazily); the cache stays bounded by the live sources.
(
  CACHE="/opt/physicar/pycache"
  if [ -d "$CACHE" ]; then
    find "$CACHE" -name '*.pyc' 2>/dev/null | while IFS= read -r pyc; do
      rel="${pyc#"$CACHE"}"
      src="$(dirname "$rel")/$(basename "$pyc" | cut -d. -f1).py"
      [ -f "$src" ] || rm -f "$pyc"
    done
    find "$CACHE" -type d -empty -delete 2>/dev/null
  fi
) &

# ── physicar-ext 최신화 (베이크 박제 방지) ─────────────────────────────
# 확장은 AMI 베이크 시점 버전으로 박히므로, 부팅(신규 생성·재개 모두 이 스크립트를
# 재실행)마다 마켓플레이스 최신을 확인해 code-server 시작 전에 반영한다.
# 실패(오프라인·open-vsx 장애)는 무시 — 베이크 버전으로 계속, 부팅을 절대 막지 않는다.
if [ -z "${CODESPACE_NAME:-}" ]; then
  timeout 25 sudo -u physicar code-server --install-extension physicar.physicar-ext --force \
    >/dev/null 2>&1 || true
  # jupyter 는 --force 없이 — 없을 때만 설치되는 자가치유 (구 골든에 미탑재였던
  # 세대 구제용). 이미 있으면 즉시 no-op 이라 부팅 지연 없음.
  timeout 25 sudo -u physicar code-server --list-extensions 2>/dev/null | grep -qi '^ms-toolsai.jupyter$' \
    || timeout 60 sudo -u physicar code-server --install-extension ms-toolsai.jupyter \
      >/dev/null 2>&1 || true
fi

# ── X 락 잔재 제거 (컨테이너 보존형이라 /tmp 가 재시작에도 살아남는다) ──
# 인스턴스 정지→재개(또는 컨테이너 재시작) 시 이전 Xvfb 의 /tmp/.X1-lock 과
# /tmp/.X11-unix/X1 이 남아 있으면 Xvfb 가 "Server is already active for display 1"
# 로 기동을 거부한다. 그러면 openbox/tint2/x11vnc 가 연쇄 FATAL 이 되고 DISPLAY 가
# 없어 Gazebo 가 렌더를 못 해 시뮬레이터·카메라·라이다가 전부 빈 화면이 된다
# (2026-08-04 실제 장애). supervisord 시작 전 항상 정리 — 살아있는 X 가 있으면
# 락을 지워도 소켓 사용 중이라 무해하고, 죽은 잔재만 제거된다.
if ! pgrep -x Xvfb >/dev/null 2>&1; then
  rm -f /tmp/.X1-lock /tmp/.X11-unix/X1 2>/dev/null || true
fi

# Start supervisord
supervisord -c "$CONF"
sleep 2

# Start the student app only if one has actually been deployed. The myapp program
# is autostart=false so that a fresh sim with no run.sh never creates an empty
# run.log; here we bring it up when the script already exists (e.g. after a
# container restart). The web UI starts/restarts it on deploy.
if [ -f "/opt/physicar/userdata/myapp.sh" ]; then
  supervisorctl -c "$CONF" start myapp 2>/dev/null || true
fi
