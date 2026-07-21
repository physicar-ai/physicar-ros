/* ===== PhysiCar page core =====
 * Shared utilities for the standalone pages (/control,
 * /sensors). No routing, no cross-page state, no sensor streaming here —
 * each page loads only what it needs on top of this file.
 */

/* ===== Utility ===== */
const $ = (s) => document.getElementById(s);

/* ===== Generic Confirm Modal (returns Promise<boolean>) ===== */
function confirmModal(message, opts) {
  opts = opts || {};
  return new Promise((resolve) => {
    const overlay = document.createElement('div');
    overlay.className = 'confirm-overlay';
    overlay.innerHTML = `<div class="confirm-box">
      <div class="confirm-msg"></div>
      <div class="confirm-actions">
        <button class="btn btn-ghost" data-act="cancel">${opts.cancelText || 'Cancel'}</button>
        <button class="btn btn-primary" data-act="ok">${opts.okText || 'OK'}</button>
      </div></div>`;
    overlay.querySelector('.confirm-msg').textContent = message;
    document.body.appendChild(overlay);
    function close(v) {
      overlay.remove();
      document.removeEventListener('keydown', onKey);
      resolve(v);
    }
    function onKey(e) {
      if (e.key === 'Escape') close(false);
      else if (e.key === 'Enter') close(true);
    }
    overlay.addEventListener('click', (e) => {
      const t = e.target.closest('button[data-act]');
      if (t) close(t.dataset.act === 'ok');
      else if (e.target === overlay) close(false);
    });
    document.addEventListener('keydown', onKey);
  });
}

/* ===== Toast Notification ===== */
let _toastContainer;
function showToast(msg, isError) {
  if (!_toastContainer) { _toastContainer = document.createElement('div'); _toastContainer.className = 'toast-container'; document.body.appendChild(_toastContainer); }
  const t = document.createElement('div'); t.className = 'toast ' + (isError ? 'error' : 'info'); t.textContent = msg;
  _toastContainer.appendChild(t); setTimeout(() => t.remove(), 3000);
}

/* ===== API ===== */
async function api(path, opts = {}) {
  const url = path.startsWith('http') ? path : path;
  const o = { method: opts.method || 'GET', headers: { ...opts.headers } };
  if (opts.body) { o.headers['Content-Type'] = 'application/json'; o.body = JSON.stringify(opts.body); }
  else if (opts.formData) { o.body = opts.formData; }
  if (opts.signal) o.signal = opts.signal;
  else if (!opts.noTimeout) o.signal = AbortSignal.timeout(opts.timeout || 15000);
  const res = await fetch(url, o);
  if (!res.ok) { const e = await res.json().catch(() => ({})); throw new Error(e.detail || e.message || res.statusText); }
  return res;
}

/* ===== Mode Detection (sim/real) =====
 * Sets window.PHYSICAR_MODE and body.sim-mode once at startup.
 * Returns the resolved mode string.
 */
async function detectMode() {
  try {
    const r = await fetch('/info');
    const info = await r.json();
    window.PHYSICAR_MODE = info.mode || 'real';
  } catch { window.PHYSICAR_MODE = 'real'; }
  if (window.PHYSICAR_MODE === 'sim') document.body.classList.add('sim-mode');
  return window.PHYSICAR_MODE;
}

/* ===== Device Connection Watcher =====
 * Single source of truth for backend connectivity.
 * Polls /health every 15s when healthy, 2s while down. The sensor streams
 * call DeviceWatcher.kick() the moment a stream errors, so failure detection
 * stays instant — the slow poll is only a fallback heartbeat. Hidden tabs
 * pause polling entirely (each poll is a billable request when the page is
 * reached through the tunnel proxy) and re-check on return.
 * Shows overlay; pages can hook extra teardown/restore via
 * DeviceWatcher.onDown / DeviceWatcher.onUp callbacks.
 */
const DeviceWatcher = (() => {
  const PERIOD_UP = 15000;
  const PERIOD_DOWN = 2000;
  const FAIL_THRESHOLD = 2;
  const TIMEOUT = 4000;
  let fails = 0;
  let overlay = null;
  let timer = null;
  let parked = false;  // hidden-tab: poll suspended until visibilitychange

  function ensureOverlay() {
    if (overlay) return overlay;
    overlay = document.createElement('div');
    overlay.className = 'conn-lost-overlay';
    overlay.innerHTML = '<div class="conn-lost"><div class="conn-spinner"></div><div class="conn-lost-title">Connection lost</div><div class="conn-lost-body">Reconnecting to device&hellip;</div></div>';
    document.body.appendChild(overlay);
    return overlay;
  }

  function showOverlay() {
    ensureOverlay().classList.add('show');
    try { if (typeof W.onDown === 'function') W.onDown(); } catch {}
    // Accelerate polling while down
    schedule(PERIOD_DOWN);
  }

  function hideOverlay() {
    if (overlay) overlay.classList.remove('show');
    try { if (typeof W.onUp === 'function') W.onUp(); } catch {}
    // Back to normal polling
    schedule(PERIOD_UP);
  }

  function schedule(ms) {
    if (timer) clearTimeout(timer);
    if (document.hidden) { parked = true; timer = null; return; }
    timer = setTimeout(ping, ms);
  }
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && parked) { parked = false; schedule(0); }
  });

  async function checkAuth() {
    try {
      const ctrl = new AbortController();
      const t = setTimeout(() => ctrl.abort(), TIMEOUT);
      const res = await fetch('/info', {
        signal: ctrl.signal,
        cache: 'no-store',
        redirect: 'manual',
      });
      clearTimeout(t);
      if (res.type === 'opaqueredirect' || res.status === 401 || res.status === 403) {
        location.reload();
        return false;
      }
      return true;
    } catch {
      return false;
    }
  }

  async function ping() {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), TIMEOUT);
    try {
      const res = await fetch('/health', { signal: ctrl.signal, cache: 'no-store' });
      if (!res.ok) throw new Error('status ' + res.status);
      if (fails > 0) {
        const ok = await checkAuth();
        if (!ok) { schedule(PERIOD_DOWN); return; }
        hideOverlay();
      } else {
        schedule(PERIOD_UP);
      }
      fails = 0;
    } catch (e) {
      fails++;
      if (fails >= FAIL_THRESHOLD) showOverlay();
      else schedule(PERIOD_DOWN);
    } finally {
      clearTimeout(t);
    }
  }

  function forceDown() {
    fails = FAIL_THRESHOLD;
    showOverlay();
  }

  // Start
  setTimeout(ping, 500);

  // kick(): immediate health check — streams call this on error so a dead
  // backend is detected in one round-trip instead of waiting out PERIOD_UP.
  function kick() { schedule(0); }

  const W = { forceDown, kick, onDown: null, onUp: null };
  return W;
})();

/* ===== Backdrop close (mousedown+up both on overlay; prevents text-drag close) ===== */
(function() {
  let downOnBackdrop = null;
  document.addEventListener('mousedown', (e) => {
    const el = e.target.closest('[data-close-on-backdrop]');
    if (el && e.target === el) downOnBackdrop = el;
    else downOnBackdrop = null;
  });
  document.addEventListener('mouseup', (e) => {
    if (!downOnBackdrop) return;
    const el = downOnBackdrop;
    downOnBackdrop = null;
    if (e.target !== el) return;
    const path = el.getAttribute('data-close-on-backdrop');
    if (!path) return;
    try {
      const fn = path.split('.').reduce((o, k) => o && o[k], window);
      if (typeof fn === 'function') fn();
    } catch {}
  });
})();
