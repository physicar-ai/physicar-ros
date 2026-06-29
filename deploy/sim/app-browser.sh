#!/bin/bash
# Studio app bookmark manager.
#
# Responsibilities:
# - Local/non-Codespaces: keep app.physicar at localhost Studio.
# - Codespaces: start app.physicar empty, then publish a trycloudflare URL once
#   it is confirmed reachable. If a tunnel never becomes reachable, it is torn
#   down and a fresh tunnel is started — retried indefinitely until one works.
set -uo pipefail

APP_FILE="$HOME/physicar_ws/app.physicar"
LOG="/tmp/cloudflared.log"
TOKEN_MAP="/tmp/pc-token.map"

# Keep the map file present even if called outside normal boot flow.
# nginx conf includes this path and fails to load when missing.
: > "$TOKEN_MAP"

# Write $1 into the (immutable) bookmark file.
write_bookmark() {
  sudo chattr -i "$APP_FILE" 2>/dev/null || true
  chmod u+w "$APP_FILE" 2>/dev/null || true
  printf '%s\n' "$1" > "$APP_FILE"
  chmod 444 "$APP_FILE" 2>/dev/null || true
  sudo chattr +i "$APP_FILE" 2>/dev/null || true
}

# Point the nginx access-token gate at $1 (the session token). The nginx config
# (conf.d/pc-token.conf) is static and `include`s $TOKEN_MAP; we only rewrite
# that one-line map file and reload. Empty token -> empty map -> tunnel traffic
# is denied (non-tunnel hosts always bypass the gate). The file must always
# exist, else nginx `include` fails to load.
set_gate_token() {
  local tok="$1"
  if [ -n "$tok" ]; then
    printf '"%s" 1;\n' "$tok" > "$TOKEN_MAP"
  else
    : > "$TOKEN_MAP"
  fi
  sudo nginx -s reload 2>/dev/null || true
}

# Non-Codespaces: static localhost bookmark and exit.
if [ -z "${CODESPACE_NAME:-}" ]; then
  write_bookmark "http://localhost/studio"
  set_gate_token ""
  exit 0
fi

# Codespaces: publish a reachable trycloudflare tunnel, retrying indefinitely.
#
# Reachability deadline per tunnel attempt (URL acquisition + routability probe).
REACHABLE_DEADLINE=120

# 1) Start empty so a stale URL from a previous run never lingers.
write_bookmark ""

CF_PID=""
# Propagate stop signals to whatever tunnel is currently running.
trap 'kill "$CF_PID" 2>/dev/null' TERM INT

# Tear down the current tunnel (if any) and clear its PID.
kill_tunnel() {
  [ -n "$CF_PID" ] && kill "$CF_PID" 2>/dev/null
  [ -n "$CF_PID" ] && wait "$CF_PID" 2>/dev/null
  CF_PID=""
}

attempt=0
while true; do
  attempt=$((attempt + 1))

  # Start a fresh quick tunnel (URL banner is printed to the log).
  : > "$LOG"
  cloudflared tunnel --url http://localhost:80 --no-autoupdate --protocol http2 > "$LOG" 2>&1 &
  CF_PID=$!

  # Single deadline covers both URL acquisition and the routability probe.
  # If the tunnel is not confirmed reachable within REACHABLE_DEADLINE seconds,
  # we discard it and loop to start a brand-new tunnel.
  deadline=$((SECONDS + REACHABLE_DEADLINE))

  # Wait for the public URL (banner extraction).
  # -a: treat the log as text. Once Studio opens its SSE streams through the
  # tunnel, cloudflared's log can pick up non-UTF8 bytes; without -a grep prints
  # "Binary file matches" instead of the URL and extraction silently fails.
  # Exclude api.trycloudflare.com — that's the control-plane endpoint cloudflared
  # calls to request the tunnel, not the public hostname we want.
  URL=""
  while [ "$SECONDS" -lt "$deadline" ]; do
    URL=$(grep -aoE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" \
          | grep -v '://api\.trycloudflare\.com' | head -1)
    [ -n "$URL" ] && break
    kill -0 "$CF_PID" 2>/dev/null || break
    sleep 1
  done

  if [ -z "$URL" ]; then
    echo "[app-browser] attempt ${attempt}: no tunnel URL within deadline; retunneling" >&2
    kill_tunnel
    continue
  fi

  # Mint a per-session token and arm the gate BEFORE exposing the URL.
  TOKEN="$(openssl rand -hex 64)"
  set_gate_token "$TOKEN"

  # Wait until the hostname is actually routable before publishing. A fresh
  # quick-tunnel hostname needs a few seconds before global DNS resolves and
  # the edge serves it. If we write the bookmark too early, the auto-opened
  # Simple Browser hits NXDOMAIN and negative-caches it (a later manual tab
  # works, but the bookmark stays broken). Any HTTP status (even the gate's
  # 403/302) proves DNS + tunnel + nginx are live.
  # curl's %{http_code} already prints 000 on connection failure (and exits
  # non-zero); don't append our own 000 or $code becomes multi-line and the
  # "!= 000" check trips on the very first failed probe.
  code=000
  while [ "$SECONDS" -lt "$deadline" ]; do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "${URL}/studio?token=${TOKEN}")
    [ "$code" != "000" ] && break
    kill -0 "$CF_PID" 2>/dev/null || break
    sleep 1
  done

  if [ "$code" = "000" ]; then
    echo "[app-browser] attempt ${attempt}: ${URL} not reachable within deadline; retunneling" >&2
    set_gate_token ""
    kill_tunnel
    continue
  fi

  # Reachable — publish the token-gated bookmark and stop retrying.
  write_bookmark "${URL}/studio?token=${TOKEN}"
  echo "[app-browser] public URL: ${URL}/studio (token-gated, ready=${code}, attempt=${attempt})"
  break
done

# Stay tied to the live tunnel so supervisord can supervise/restart it.
wait "$CF_PID"