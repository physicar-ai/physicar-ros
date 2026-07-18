#!/bin/bash
# Studio app bookmark manager.
#
# Responsibilities:
# - Local/non-Codespaces: keep app.physicar at localhost Studio.
# - Codespaces: publish the app's reachable URLs into app.physicar (one per
#   line), which the browser-preview extension opens as the Studio bookmark. The
#   extension tries the lines in order and locks onto whichever actually renders.
#
#   Lines are written incrementally, as each transport becomes available:
#     line 1: <name>-80.app.github.dev/app  -- as soon as nginx:80 is serving
#     line 2: <label>.physicarcs.com/app    -- once the quick tunnel is live
#             (our Worker proxy over the tunnel — the raw upstream tunnel URL
#             is not published)
#   github.dev port-forwarding is fast but intermittently flaky, so the tunnel
#   is offered as a second option; the extension picks whichever works. If a
#   tunnel never becomes reachable it is torn down and a fresh one started —
#   retried indefinitely until one works.
#
#   github.dev is gated by GitHub's own auth, so its line carries no token. The
#   public tunnel URL is token-gated (nginx map, see set_gate_token below) so a
#   leaked hostname can't be accessed without the per-session token.
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

# Wait until nginx is actually serving Studio on localhost:80. curl exits 0 once
# it gets any HTTP response (even a redirect/404), non-zero while the connection
# is refused.
wait_port() {
  while ! curl -s -o /dev/null --max-time 2 "http://localhost/app"; do
    sleep 1
  done
}

# Non-Codespaces: static localhost bookmark and exit.
# Cloud/self-hosted sim: the operator hands us the external URL (e.g.
# https://xxxxx.physicar.dev) — the bookmark must use it, not localhost,
# because students reach this host through that domain.
if [ -n "${PHYSICAR_EXTERNAL_URL:-}" ]; then
  write_bookmark "${PHYSICAR_EXTERNAL_URL%/}/app"
  set_gate_token ""
  exit 0
fi

if [ -z "${CODESPACE_NAME:-}" ]; then
  write_bookmark "http://localhost/app"
  set_gate_token ""
  exit 0
fi

# Codespaces: publish github.dev (line 1) immediately, then a reachable
# tunnel URL (line 2, physicarcs.com), retrying the tunnel indefinitely.
#
# Reachability deadline per tunnel attempt (URL acquisition + routability probe).
REACHABLE_DEADLINE=120

# 1) Start empty so a stale URL from a previous run never lingers.
write_bookmark ""
set_gate_token ""

# 2) As soon as nginx is serving Studio on :80, publish line 1 (the GitHub
#    Codespaces port-forward). It doesn't depend on the tunnel, so the extension
#    can start trying it immediately while the tunnel comes up below. No token --
#    *.app.github.dev bypasses the nginx gate (see conf.d/pc-token.conf); the
#    port-forward is gated by GitHub's own auth instead.
GH_URL="https://${CODESPACE_NAME}-80.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}/app"
wait_port
write_bookmark "$GH_URL"
echo "[app-browser] line 1 published (github.dev)"

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
  # Exclude the api. control-plane hostname — that's the endpoint cloudflared
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
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "${URL}/app?token=${TOKEN}")
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

  # Reachable — publish as physicarcs.com (1:1 Worker proxy, same label).
  # School networks often block the upstream tunnel domain, so only our
  # allow-listable domain is exposed; the raw tunnel URL is NOT written to
  # the bookmark. The Worker's upstream identity check (/health probe) makes
  # sure only this tunnel gets proxied.
  CF_URL="${URL}/app?token=${TOKEN}"
  PCS_URL="${CF_URL/.trycloudflare.com/.physicarcs.com}"
  write_bookmark "$(printf '%s\n%s' "$GH_URL" "$PCS_URL")"
  echo "[app-browser] published: github.dev + physicarcs (ready=${code}, attempt=${attempt})"
  break
done

# Stay tied to the live tunnel so supervisord can supervise/restart it.
wait "$CF_PID"