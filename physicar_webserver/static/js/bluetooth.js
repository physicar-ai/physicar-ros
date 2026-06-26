/* Bluetooth tab for / (main page).
 * Main panel shows only currently-connected devices (mirrors WiFi UX).
 * "Manage Devices" opens a modal with paired list + scan results, where
 * users can connect / disconnect / pair / forget.
 *
 * Live updates come through /network/stream SSE (handled by WIFI module),
 * which calls BT.applySnapshot with bt_status/bt_devices fields.
 */
const BT = (() => {
  const el = (id) => document.getElementById(id);
  const esc = (s) => { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; };

  let scanning = false;
  let busy = false;
  let lastScanList = [];     // most recent /scan result
  let pairedDevices = [];    // synced from snapshot
  let manageOpen = false;
  let _scanInterval = null;

  function iconFor(d) {
    const i = (d.icon || '').toLowerCase();
    if (i.includes('audio') || i.includes('headset') || i.includes('headphone')) return '🎧';
    if (i.includes('input-keyboard')) return '⌨️';
    if (i.includes('input-mouse')) return '🖱️';
    if (i.includes('input-gaming') || i.includes('gamepad')) return '🎮';
    if (i.includes('phone')) return '📱';
    if (i.includes('computer')) return '💻';
    return '🔵';
  }

  // ── Status / power ──

  function applyStatus(s) {
    // Status element removed from UI; kept as no-op for backward compat with snapshot.
    void s;
  }

  // ── Main BT panel: all paired devices, connected first ──

  function applyConnected(devices) {
    pairedDevices = (devices || []).filter(d => d.paired);
    const c = el('bt-paired-main');
    if (c) {
      if (!pairedDevices.length) {
        c.innerHTML = '<div class="bt-empty">No paired devices</div>';
      } else {
        const sorted = pairedDevices.slice().sort((a, b) => (b.connected ? 1 : 0) - (a.connected ? 1 : 0));
        c.innerHTML = sorted.map(d => mainRow(d)).join('');
        bindActions(c);
      }
    }
    if (manageOpen) renderManage();
  }

  function mainRow(d) {
    const tag = d.connected
      ? '<span class="bt-tag ok">Connected</span>'
      : '<span class="bt-tag muted">Not connected</span>';
    const action = d.connected
      ? `<button class="bt-act" data-act="disconnect" data-mac="${esc(d.mac)}">Disconnect</button>`
      : `<button class="bt-act" data-act="remove" data-mac="${esc(d.mac)}">Forget</button>`;
    const rowCls = d.connected ? 'bt-item' : 'bt-item bt-item-offline';
    return `<div class="${rowCls}">
      <span class="bt-icon">${iconFor(d)}</span>
      <span class="bt-name">${esc(d.name || d.mac)}${tag}</span>
      <span class="bt-actions">${action}</span>
    </div>`;
  }

  // ── Add Device modal: scan results only (pair flow) ──

  function deviceRow(d) {
    return `<div class="bt-item">
      <span class="bt-icon">${iconFor(d)}</span>
      <span class="bt-name">${esc(d.name || d.mac)}</span>
      <span class="bt-actions">
        <button class="bt-act primary" data-act="pair" data-mac="${esc(d.mac)}">Pair</button>
      </span>
    </div>`;
  }

  function renderManage() {
    const sc = el('bt-scan-list');
    if (sc) {
      const pairedMacs = new Set(pairedDevices.map(d => d.mac));
      const list = lastScanList.filter(d => !pairedMacs.has(d.mac));
      if (!list.length) {
        sc.innerHTML = scanning
          ? '<div class="bt-empty">Scanning…</div>'
          : '<div class="bt-empty">No new devices</div>';
      } else {
        sc.innerHTML = list.map(d => deviceRow(d)).join('');
        bindActions(sc);
      }
    }
  }

  function bindActions(container) {
    container.querySelectorAll('[data-act]').forEach(b => {
      b.onclick = () => action(b.dataset.act, b.dataset.mac, b);
    });
  }

  async function action(act, mac, btn) {
    if (busy) return;
    busy = true;
    const orig = btn.textContent;
    btn.disabled = true;
    if (btn.classList.contains('bt-act')) btn.textContent = '…';
    try {
      const res = await fetch('/network/bluetooth/' + act, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mac }),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        if (typeof showToast === 'function') showToast(act + ' failed: ' + (d.detail || res.status), true);
      }
    } catch (e) {
      if (typeof showToast === 'function') showToast('Network error: ' + e.message, true);
    } finally {
      busy = false;
      btn.textContent = orig;
      btn.disabled = false;
    }
  }

  // ── Scan (only meaningful while Manage modal is open) ──

  async function scan() {
    if (scanning) return;
    scanning = true;
    const btn = el('bt-rescan');
    if (btn) btn.classList.add('spinning');
    if (manageOpen) renderManage();
    try {
      const res = await fetch('/network/bluetooth/scan?seconds=8');
      if (res.ok) lastScanList = await res.json();
    } catch (e) { /* keep prior list */ }
    finally {
      scanning = false;
      if (btn) btn.classList.remove('spinning');
      if (manageOpen) renderManage();
    }
  }

  // ── Manage modal open/close ──

  function openManage() {
    manageOpen = true;
    el('bt-manage-overlay').classList.add('active');
    renderManage();
    scan();
    if (_scanInterval) clearInterval(_scanInterval);
    _scanInterval = setInterval(() => { if (manageOpen && !scanning) scan(); }, 12000);
  }

  function closeManage() {
    manageOpen = false;
    el('bt-manage-overlay').classList.remove('active');
    if (_scanInterval) { clearInterval(_scanInterval); _scanInterval = null; }
  }

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && manageOpen) closeManage();
  });

  function applySnapshot(snap) {
    if (!snap) return;
    if ('bt_status' in snap) applyStatus(snap.bt_status);
    if ('bt_devices' in snap) applyConnected(snap.bt_devices || []);
  }

  async function refresh() {
    try {
      const [s, devs] = await Promise.all([
        fetch('/network/bluetooth/status').then(r => r.ok ? r.json() : null),
        fetch('/network/bluetooth/devices').then(r => r.ok ? r.json() : []),
      ]);
      applyStatus(s);
      applyConnected(devs);
    } catch {}
  }

  return { refresh, scan, openManage, closeManage, applySnapshot };
})();
