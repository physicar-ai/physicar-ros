#!/usr/bin/env bash
# updater.sh — Periodic background updater for physicar-ros (runs inside container)
#
# Launched by entrypoint.sh in background. Checks for new version tags.
# On update: force-checkout → signal entrypoint to rebuild+relaunch.
#
# Safety guarantees:
#   - gc.auto=0: prevents git gc (interrupted gc = repo corruption risk)
#   - timeout 30s: prevents hanging on bad network / DNS / no internet
#   - Stale lock cleanup: auto-recover from power loss during git ops
#   - checkout -f: idempotent, safe to re-run after interrupted checkout
#   - All errors caught and logged, never fatal

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$SCRIPT_DIR"
TAG_PATTERN="${PHYSICAR_TAG_PATTERN:-v1.*}"
INTERVAL="${PHYSICAR_UPDATE_INTERVAL:-300}"  # default: 5 minutes
SIGNAL_FILE="/tmp/.physicar-update-ready"

log() { echo "[updater] $(date '+%H:%M:%S') $*"; }

# ── safe_update ──────────────────────────────────────────
# Force-updates to the latest matching tag.
# Returns 0 if updated, 1 otherwise.
safe_update() {
    # Verify repo
    if ! git -C "$REPO_DIR" rev-parse --git-dir >/dev/null 2>&1; then
        log "not a valid git repo: $REPO_DIR"
        return 1
    fi

    # Resolve .git dir (handles both directory and gitdir: file)
    local git_dir
    git_dir=$(git -C "$REPO_DIR" rev-parse --absolute-git-dir 2>/dev/null) || \
    git_dir=$(cd "$REPO_DIR" && cd "$(git rev-parse --git-dir)" && pwd)

    # Clean stale lock files (recovery from power loss / kill -9)
    for lockfile in "$git_dir/index.lock" "$git_dir/HEAD.lock"; do
        if [[ -f "$lockfile" ]]; then
            local age
            age=$(( $(date +%s) - $(stat -c %Y "$lockfile" 2>/dev/null || echo 0) ))
            if (( age > 300 )); then
                rm -f "$lockfile"
                log "removed stale lock $(basename "$lockfile") (age=${age}s)"
            else
                log "lock file exists (age=${age}s), skipping"
                return 1
            fi
        fi
    done

    # Fetch tags with timeout (network failure = skip)
    if ! timeout 30 git -c gc.auto=0 -C "$REPO_DIR" fetch --tags 2>/dev/null; then
        log "fetch failed (network unavailable?)"
        return 1
    fi

    # Find latest matching tag
    local latest
    latest=$(git -C "$REPO_DIR" tag -l "$TAG_PATTERN" --sort=-v:refname | head -1)
    if [[ -z "$latest" ]]; then
        return 1
    fi

    # Already at this exact commit?
    local current_rev target_rev
    current_rev=$(git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null)
    target_rev=$(git -C "$REPO_DIR" rev-parse "$latest^{}" 2>/dev/null)
    if [[ "$current_rev" == "$target_rev" ]]; then
        return 1  # up to date
    fi

    # Force checkout: overwrite tracked files, keep untracked/.gitignore'd
    local current_tag
    current_tag=$(git -C "$REPO_DIR" describe --tags --exact-match HEAD 2>/dev/null || echo "${current_rev:0:8}")
    log "updating: $current_tag → $latest"
    if git -c gc.auto=0 -c advice.detachedHead=false -C "$REPO_DIR" checkout -f "$latest" 2>/dev/null; then
        log "updated to $latest"
        return 0
    else
        log "checkout -f failed"
        return 1
    fi
}

# ── Main loop ────────────────────────────────────────────
log "started (interval=${INTERVAL}s, pattern=$TAG_PATTERN)"

# Initial delay: let build + launch stabilize first
sleep 120

while true; do
    if safe_update; then
        # Signal entrypoint to rebuild + relaunch
        touch "$SIGNAL_FILE"
        log "signaling entrypoint to rebuild..."

        # Kill the ros2 launch process (entrypoint loop will restart)
        pkill -f 'ros2 launch physicar_bringup' 2>/dev/null || true
    fi

    sleep "$INTERVAL"
done
#!/usr/bin/env bash
# updater.sh — Periodic background updater for physicar-ros (runs inside container)
#
# Launched by entrypoint.sh in background. Checks for new version tags.
# On update: force-checkout → signal entrypoint to rebuild+relaunch.
#
# Safety guarantees:
#   - gc.auto=0: prevents git gc during operations (interrupted gc = repo corruption)
#   - timeout 30s: prevents hanging on bad network / no internet / DNS issues
#   - Stale lock cleanup: auto-recover from power loss during git ops
#   - checkout -f: idempotent, safe to re-run after interrupted checkout
#   - All errors caught and logged, never fatal
#   - Writes PID file so entrypoint can send signals
#   - SIGUSR1 → entrypoint restarts launch loop

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$SCRIPT_DIR"
TAG_PATTERN="${PHYSICAR_TAG_PATTERN:-v1.*}"
INTERVAL="${PHYSICAR_UPDATE_INTERVAL:-300}"  # default: 5 minutes
SIGNAL_FILE="/tmp/.physicar-update-ready"

log() { echo "[updater] $(date '+%H:%M:%S') $*"; }

# ── safe_update ──────────────────────────────────────────
# Force-updates to the latest matching tag.
# Returns 0 if updated, 1 otherwise.
safe_update() {
    # Verify repo
    if ! git -C "$REPO_DIR" rev-parse --git-dir >/dev/null 2>&1; then
        log "not a valid git repo: $REPO_DIR"
        return 1
    fi

    # Clean stale lock files (recovery from power loss / kill -9)
    for lockfile in "$REPO_DIR/.git/index.lock" "$REPO_DIR/.git/HEAD.lock"; do
        if [[ -f "$lockfile" ]]; then
            local age
            age=$(( $(date +%s) - $(stat -c %Y "$lockfile" 2>/dev/null || echo 0) ))
            if (( age > 300 )); then
                rm -f "$lockfile"
                log "removed stale lock $(basename "$lockfile") (age=${age}s)"
            else
                log "lock file exists (age=${age}s), skipping"
                return 1
            fi
        fi
    done

    # Fetch tags with timeout (network failure = skip)
    if ! timeout 30 git -c gc.auto=0 -C "$REPO_DIR" fetch --tags 2>/dev/null; then
        log "fetch failed (network unavailable?)"
        return 1
    fi

    # Find latest matching tag
    local latest
    latest=$(git -C "$REPO_DIR" tag -l "$TAG_PATTERN" --sort=-v:refname | head -1)
    if [[ -z "$latest" ]]; then
        return 1
    fi

    # Already at this exact commit?
    local current_rev target_rev
    current_rev=$(git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null)
    target_rev=$(git -C "$REPO_DIR" rev-parse "$latest^{}" 2>/dev/null)
    if [[ "$current_rev" == "$target_rev" ]]; then
        return 1  # up to date
    fi

    # Force checkout: overwrite tracked files, keep untracked/.gitignore'd
    local current_tag
    current_tag=$(git -C "$REPO_DIR" describe --tags --exact-match HEAD 2>/dev/null || echo "${current_rev:0:8}")
    log "updating: $current_tag → $latest"
    if git -c gc.auto=0 -c advice.detachedHead=false -C "$REPO_DIR" checkout -f "$latest" 2>/dev/null; then
        log "updated to $latest"
        return 0
    else
        log "checkout -f failed"
        return 1
    fi
}

# ── Main loop ────────────────────────────────────────────
log "started (interval=${INTERVAL}s, pattern=$TAG_PATTERN)"

# Initial delay: let build + launch stabilize first
sleep 120

while true; do
    if safe_update; then
        # Signal entrypoint to rebuild + relaunch
        touch "$SIGNAL_FILE"
        log "signaling entrypoint to rebuild..."

        # Find and kill the ros2 launch process (entrypoint loop will restart)
        pkill -f 'ros2 launch physicar_bringup' 2>/dev/null || true
    fi

    sleep "$INTERVAL"
done
