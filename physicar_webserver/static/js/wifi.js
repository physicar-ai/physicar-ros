/* WiFi modal for / (main page).
 * Calls /network/* endpoints. PC/laptop UX (no virtual keyboard).
 */
const WIFI = (() => {
  const el = (id) => document.getElementById(id);
  const esc = (s) => { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; };

  let connectedSsid = '';
  let ownApSsid = '';   // this device's hotspot SSID — filter from scan list
  let savedConns = []; // [{name, ssid, autoconnect, active}]
  let selected = { ssid: '', security: 'wpa' };
  let connecting = false;
  let apPasswordRaw = '';
  // True when the password step was reached by clicking a network in the
  // list popup. Cancel/back should then re-open the list instead of
  // dropping back to the main wifi tab (which forces the user to hit
  // "Change" again).
  let _enteredPwFromList = false;

  // ───────────────────────────────────────────────────────────────────────
  // Connect Spinner (full-screen overlay shown during long wifi ops)
  // ───────────────────────────────────────────────────────────────────────
  let _spinnerSubTimer = null;
  function showSpinner(msg, sub) {
    const ov = el('connect-spinner');
    const m = el('connect-spinner-msg');
    const s = el('connect-spinner-sub');
    if (!ov) return;
    if (m) m.textContent = msg || 'Connecting…';
    if (s) s.textContent = sub || '';
    ov.classList.add('active');
    ov.setAttribute('aria-hidden', 'false');
    if (_spinnerSubTimer) clearTimeout(_spinnerSubTimer);
    _spinnerSubTimer = setTimeout(() => {
      if (s && ov.classList.contains('active') && !s.textContent) {
        s.textContent = 'Still working… switching networks can take up to 30s.';
      }
    }, 8000);
  }
  function hideSpinner() {
    const ov = el('connect-spinner');
    if (!ov) return;
    ov.classList.remove('active');
    ov.setAttribute('aria-hidden', 'true');
    if (_spinnerSubTimer) { clearTimeout(_spinnerSubTimer); _spinnerSubTimer = null; }
  }
  let apPwVisible = false;
  let activeNetTab = 'ap';

  function setStatus(msg, kind) {
    const s = el('wifi-status');
    s.textContent = msg || '';
    s.className = 'wifi-status' + (msg ? ' show ' + (kind || '') : '');
  }

  function showStep(name) {
    el('wifi-step-scan').style.display = name === 'scan' ? '' : 'none';
    el('wifi-step-pw').style.display = name === 'pw' ? '' : 'none';
  }

  async function loadInfo() {
    let info = {};
    try {
      const res = await fetch('/network/info');
      if (res.ok) info = await res.json();
    } catch {}
    applyInfo(info);
  }

  async function loadAp() {
    try {
      const res = await fetch('/network/ap');
      if (!res.ok) throw new Error('ap');
      applyAp(await res.json());
    } catch {
      applyAp(null);
    }
  }

  function toggleApPw() {
    apPwVisible = !apPwVisible;
    const pwEl = el('net-ap-pw');
    if (pwEl) pwEl.textContent = apPwVisible ? (apPasswordRaw || '—') : '••••••••';
  }
  let wifiPwVisible = false;
  function toggleWifiPw() {
    wifiPwVisible = !wifiPwVisible;
    const wifiPwEl = el('wifi-current-pw');
    if (wifiPwEl) wifiPwEl.textContent = wifiPwVisible ? (apPasswordRaw || '—') : '••••••••';
  }
  let ethPwVisible = false;
  function toggleEthPw() {
    ethPwVisible = !ethPwVisible;
    const ethPwEl = el('net-eth-pw');
    if (ethPwEl) ethPwEl.textContent = ethPwVisible ? (apPasswordRaw || '—') : '••••••••';
  }

  function switchTab(tab) {
    activeNetTab = tab;
    document.querySelectorAll('.wifi-net-tab').forEach(b => {
      b.classList.toggle('active', b.dataset.nettab === tab);
    });
    document.querySelectorAll('.wifi-net-panel').forEach(p => {
      p.classList.remove('active');
    });
    const panel = el('wifi-net-' + tab);
    if (panel) panel.classList.add('active');
    if (tab === 'ap') loadAp();
    else if (tab === 'internet') { loadAp(); loadInfo(); loadInternet(); }
    else if (tab === 'bluetooth') { if (typeof BT !== 'undefined') BT.refresh(); }
  }

  // ── Pure DOM updaters (data only). loadX() wrappers below fetch then call. ──

  function applyInfo(info) {
    info = info || {};
    connectedSsid = info.wifi_ssid || '';
    const wifiOn = !!(connectedSsid && info.wifi_ip);
    const wConn = el('wifi-pane-connected');
    const wDisc = el('wifi-pane-disconnected');
    if (wConn) wConn.style.display = wifiOn ? '' : 'none';
    if (wDisc) wDisc.style.display = wifiOn ? 'none' : '';
    const ssidEl = el('wifi-current-ssid');
    if (ssidEl) { ssidEl.textContent = connectedSsid || 'Not connected'; ssidEl.className = 'wifi-card-value'; }
    const ipEl = el('wifi-current-ip');
    if (ipEl) { ipEl.textContent = info.wifi_ip || '—'; ipEl.className = 'wifi-card-value'; }
    const ethConnected = !!info.eth_ip;
    const eConn = el('eth-pane-connected');
    const eDisc = el('eth-pane-disconnected');
    if (eConn) eConn.style.display = ethConnected ? '' : 'none';
    if (eDisc) eDisc.style.display = ethConnected ? 'none' : '';
    const ethIp = el('net-eth-ip'); if (ethIp) ethIp.textContent = info.eth_ip || '—';
  }

  function applyInternet(d) {
    const elv = el('internet-status');
    if (!elv) return;
    if (!d) { elv.textContent = 'Unknown'; elv.className = 'wifi-card-value'; return; }
    const sources = [];
    if (d.wifi) sources.push('WiFi');
    if (d.ethernet) sources.push('LAN');
    if (sources.length) { elv.textContent = 'Connected (' + sources.join(', ') + ')'; elv.className = 'wifi-card-value ok'; }
    else { elv.textContent = 'Disconnected'; elv.className = 'wifi-card-value err'; }
  }

  function applyAp(d) {
    d = d || {};
    const ssidEl = el('net-ap-ssid'); if (ssidEl) ssidEl.textContent = d.ssid || '—';
    ownApSsid = d.ssid || '';
    // net-ap-url is hard-coded to https://device.physicar.ai in HTML — don't overwrite.
    apPasswordRaw = d.password || '';
    const pwEl = el('net-ap-pw');
    if (pwEl) pwEl.textContent = apPwVisible ? (apPasswordRaw || '—') : '••••••••';
    const wifiPwEl = el('wifi-current-pw');
    if (wifiPwEl) wifiPwEl.textContent = wifiPwVisible ? (apPasswordRaw || '—') : '••••••••';
    const ethPwEl = el('net-eth-pw');
    if (ethPwEl) ethPwEl.textContent = ethPwVisible ? (apPasswordRaw || '—') : '••••••••';
  }

  function applySaved(arr) { savedConns = Array.isArray(arr) ? arr : []; }

  // Apply a full snapshot from /network/stream. Re-renders the WiFi list too
  // if it's currently visible so other tabs see connect/forget instantly.
  function applySnapshot(snap) {
    if (!snap || typeof snap !== 'object') return;
    if (snap.info) applyInfo(snap.info);
    if (snap.internet) applyInternet(snap.internet);
    if (snap.ap) applyAp(snap.ap);
    if (snap.saved) applySaved(snap.saved);
    if (typeof BT !== 'undefined' && BT.applySnapshot) BT.applySnapshot(snap);
    // If list popup is open, refresh its rendering (but don't re-scan WiFi).
    if (el('wifi-list-overlay') && el('wifi-list-overlay').classList.contains('active') && _lastNetworks) {
      renderList(_lastNetworks);
    }
  }

  let _lastNetworks = null;

  // ── Persistent SSE: opened on first WIFI.open() and shared by all clients. ──
  let _es = null;
  function ensureStream() {
    if (_es) return;
    try {
      _es = new EventSource('/network/stream');
      _es.onmessage = (e) => {
        try { applySnapshot(JSON.parse(e.data)); } catch {}
      };
      _es.onerror = () => {
        // EventSource auto-reconnects; nothing to do.
      };
    } catch {}
  }

  async function loadInternet() {
    let d = null;
    try {
      const res = await fetch('/network/internet');
      if (res.ok) d = await res.json();
    } catch {}
    applyInternet(d);
  }

  async function loadSaved() {
    try {
      const res = await fetch('/network/wifi/saved');
      if (!res.ok) throw new Error('saved');
      savedConns = await res.json();
    } catch (e) {
      savedConns = [];
    }
  }

  function findSaved(ssid) {
    return savedConns.find(c => c.ssid === ssid || c.name === ssid);
  }

  async function forget(name, ssid) {
    // Close the list popup, run delete, reopen list with fresh entries.
    closeList();
    try {
      const res = await fetch('/network/wifi/saved/' + encodeURIComponent(name), { method: 'DELETE' });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        if (typeof showToast === 'function') showToast('Forget failed: ' + (d.detail || res.status), true);
      } else {
        if (typeof showToast === 'function') showToast('Forgot ' + ssid);
      }
    } catch (e) {
      if (typeof showToast === 'function') showToast('Network error: ' + e.message, true);
    }
    openList();
  }

  function renderList(networks) {
    const list = el('wifi-list');
    // Filter out our own hotspot and any other PhysiCar devices' hotspots
    // so users can't accidentally hop onto a sibling kit's AP.
    networks = (networks || []).filter(n => {
      if (!n || !n.ssid) return false;
      if (ownApSsid && n.ssid === ownApSsid) return false;
      if (/^physicar-/i.test(n.ssid)) return false;
      return true;
    });
    if (!networks.length) { list.innerHTML = '<div class="wifi-empty">No networks found</div>'; return; }
    // Sort: currently connected first, then by signal strength.
    networks.sort((a, b) => {
      const aCur = a.ssid === connectedSsid ? 0 : 1;
      const bCur = b.ssid === connectedSsid ? 0 : 1;
      if (aCur !== bCur) return aCur - bCur;
      return b.signal - a.signal;
    });
    list.innerHTML = networks.slice(0, 20).map(n => {
      const isCur = n.ssid === connectedSsid;
      const saved = findSaved(n.ssid);
      const bars = n.signal > -50 ? 4 : n.signal > -60 ? 3 : n.signal > -70 ? 2 : 1;
      const barEls = [1,2,3,4].map(i =>
        `<span style="height:${i*3 + 3}px" class="${i <= bars ? '' : 'dim'}"></span>`).join('');
      const sec = n.security === 'open' ? '' :
        n.security === 'enterprise' ? '🏢' : '🔒';
      const freq = n.frequency > 5000 ? '<span class="wifi-freq">5G</span>' : '';
      const savedLabel = '';
      const forgetBtn = (saved && !isCur)
        ? `<button class="wifi-forget" data-name="${esc(saved.name)}" data-ssid="${esc(n.ssid)}" title="Forget saved network" aria-label="Forget"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-2 14a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2"/></svg></button>`
        : '';
      // Mark saved networks for click handler dispatch (activate vs select).
      const dataSavedName = saved ? ` data-saved-name="${esc(saved.name)}"` : '';
      return `<div class="wifi-item${isCur ? ' connected' : ''}${saved ? ' saved' : ''}" data-ssid="${esc(n.ssid)}" data-sec="${n.security}"${dataSavedName}>
        <span class="wifi-check">${isCur ? '✔' : ''}</span>
        <span class="wifi-ssid">${esc(n.ssid)}</span>
        ${savedLabel}${freq}<span class="wifi-sec">${sec}</span>
        <span class="wifi-bars">${barEls}</span>
        ${forgetBtn}
      </div>`;
    }).join('');
    list.querySelectorAll('.wifi-item').forEach(item => {
      item.onclick = (e) => {
        if (e.target.closest('.wifi-forget')) return; // handled separately
        // Already-connected items: no-op (cursor:default via CSS).
        if (item.classList.contains('connected')) return;
        const savedName = item.dataset.savedName;
        if (savedName) {
          activateSaved(savedName, item.dataset.ssid, item.dataset.sec);
        } else {
          select(item.dataset.ssid, item.dataset.sec);
        }
      };
    });
    list.querySelectorAll('.wifi-forget').forEach(btn => {
      btn.onclick = (e) => {
        e.stopPropagation();
        forget(btn.dataset.name, btn.dataset.ssid);
      };
    });
  }

  // Activate a saved network using stored credentials. If NM rejects with 401
  // (stored secret invalid), fall through to the password modal so the user
  // can retype.
  //
  // Note: switching wlan0 between APs can drop the in-flight HTTP fetch (the
  // browser's NetworkChangeNotifier kills the request) even when the backend
  // completed successfully. We fall back to polling /network/info to confirm.
  async function activateSaved(name, ssid, security) {
    // Saved-network click also reassociates wlan0 — confirm first, even when
    // currently disconnected (the connect itself can still take ≥10s).
    const fromList = el('wifi-list-overlay') && el('wifi-list-overlay').classList.contains('active');
    if (connectedSsid !== ssid) {
      _showSwitchConfirm(connectedSsid, ssid,
        () => _doActivateSaved(name, ssid, security),
        () => { if (fromList) openList(); });
      return;
    }
    return _doActivateSaved(name, ssid, security);
  }

  async function _doActivateSaved(name, ssid, security) {
    closeList();
    showSpinner('Connecting to ' + ssid + '…');
    try {
      const res = await fetch('/network/wifi/activate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      if (res.ok) {
        await Promise.all([loadInfo(), loadSaved()]);
        renderList(_lastNetworks || []);
        hideSpinner();
        if (typeof showToast === 'function') showToast('Connected to ' + ssid);
        return;
      }
      if (res.status === 401) {
        hideSpinner();
        select(ssid, security);
        return;
      }
      const d = await res.json().catch(() => ({}));
      hideSpinner();
      if (typeof showToast === 'function') showToast('Connect failed: ' + (d.detail || res.status), true);
    } catch (e) {
      // Fetch may have died because wlan0 switched APs mid-request — verify.
      showSpinner('Connecting to ' + ssid + '…', 'Verifying network change…');
      let verified = false;
      for (let i = 0; i < 6; i++) {
        await new Promise(r => setTimeout(r, 1500));
        try {
          const info = await fetch('/network/info', { cache: 'no-store' }).then(r => r.json());
          if (info && info.wifi_ssid === ssid) { verified = true; break; }
        } catch {}
      }
      hideSpinner();
      if (verified) {
        await Promise.all([loadInfo(), loadSaved()]);
        renderList(_lastNetworks || []);
        if (typeof showToast === 'function') showToast('Connected to ' + ssid);
      } else if (typeof showToast === 'function') {
        showToast('Network error: ' + e.message, true);
      }
    }
  }

  async function scan() {
    const btn = el('wifi-rescan');
    btn.classList.add('spinning');
    el('wifi-list').innerHTML = '<div class="wifi-empty">Scanning…</div>';
    // nmcli rescans flake intermittently (radio busy, recent rescan, etc.)
    // and the resulting empty list lands as "Scan failed" in the UI. Retry
    // once before giving up so transient flakes don't bother the user.
    async function tryScan() {
      await Promise.all([loadInfo(), loadSaved(), loadInternet()]);
      const res = await fetch('/network/wifi/scan');
      if (!res.ok) throw new Error('scan');
      return await res.json();
    }
    try {
      let nets = await tryScan();
      if (!nets || nets.length === 0) {
        // Empty result — wait a moment and retry once.
        await new Promise(r => setTimeout(r, 1500));
        try { nets = await tryScan(); } catch {}
      }
      _lastNetworks = nets || [];
      renderList(_lastNetworks);
    } catch (e) {
      // First attempt errored — retry once.
      try {
        await new Promise(r => setTimeout(r, 1500));
        _lastNetworks = await tryScan();
        renderList(_lastNetworks);
      } catch {
        el('wifi-list').innerHTML = '<div class="wifi-empty error">Scan failed</div>';
      }
    } finally {
      btn.classList.remove('spinning');
    }
  }

  function select(ssid, security) {
    selected = { ssid, security };
    el('wifi-pw-ssid').textContent = ssid;
    el('wifi-password').value = '';
    el('wifi-password').type = 'password';
    el('wifi-eye').classList.remove('active');
    el('wifi-identity').value = '';
    el('wifi-identity-field').style.display = security === 'enterprise' ? '' : 'none';
    setStatus('');
    el('wifi-connect').disabled = false;
    el('wifi-back').disabled = false;
    // User picked a network from the list popup — close it so the password
    // step (or open-network connect) is visible underneath. Remember we
    // came from the list so Cancel can pop the list back open.
    const fromList = el('wifi-list-overlay') && el('wifi-list-overlay').classList.contains('active');
    _enteredPwFromList = !!fromList;
    closeList();
    const proceed = () => {
      if (security === 'open') { doConnect(ssid, null, null); return; }
      showStep('pw');
      setTimeout(() => {
        (security === 'enterprise' ? el('wifi-identity') : el('wifi-password')).focus();
      }, 50);
    };
    // Any connect/switch reassociates wlan0 — confirm via modal first.
    if (connectedSsid !== ssid) {
      _showSwitchConfirm(connectedSsid, ssid, proceed, () => { if (fromList) openList(); });
      return;
    }
    proceed();
  }

  async function doConnect(ssid, password, identity) {
    connecting = true;
    el('wifi-connect').disabled = true;
    el('wifi-back').disabled = true;
    setStatus('Connecting to ' + ssid + '…', 'connecting');
    showSpinner('Connecting to ' + ssid + '…');
    try {
      const body = { ssid, password: password || null };
      if (identity) body.identity = identity;
      const res = await fetch('/network/wifi/connect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await res.json().catch(() => ({}));
      connecting = false;
      el('wifi-connect').disabled = false;
      el('wifi-back').disabled = false;
      hideSpinner();
      if (res.ok) {
        // Optimistic: close immediately on success. SSE stream will refresh
        // the rest of the panel; we just need info+saved for the list.
        await Promise.all([loadInfo(), loadSaved()]);
        renderList(_lastNetworks || []);
        showStep('scan');
        setStatus('');
      } else {
        const msg = (data && data.detail) || 'Connection failed';
        const lower = String(msg).toLowerCase();
        if (lower.includes('password') || lower.includes('auth') || lower.includes('secrets')) {
          setStatus('Wrong password. Please try again.', 'error');
        } else if (lower.includes('timeout')) {
          setStatus('Connection timeout.', 'error');
        } else {
          setStatus(msg, 'error');
        }
        // make sure we are on password step to allow retry
        showStep('pw');
      }
    } catch (e) {
      // Browser may have killed the fetch (NetworkChangeNotifier) when wlan0
      // hopped APs even though the backend completed successfully. Verify by
      // polling /network/info before declaring failure.
      setStatus('Verifying connection…', 'connecting');
      showSpinner('Connecting to ' + ssid + '…', 'Verifying network change…');
      let verified = false;
      for (let i = 0; i < 6; i++) {
        await new Promise(r => setTimeout(r, 1500));
        try {
          const info = await fetch('/network/info', { cache: 'no-store' }).then(r => r.json());
          if (info && info.wifi_ssid === ssid) { verified = true; break; }
        } catch {}
      }
      connecting = false;
      el('wifi-connect').disabled = false;
      el('wifi-back').disabled = false;
      hideSpinner();
      if (verified) {
        await Promise.all([loadInfo(), loadSaved()]);
        renderList(_lastNetworks || []);
        showStep('scan');
        setStatus('');
      } else {
        setStatus('Network error: ' + e.message, 'error');
        showStep('pw');
      }
    }
  }

  function connect() {
    const pw = el('wifi-password').value;
    const id = el('wifi-identity').value;
    if (selected.security !== 'open' && !pw) { setStatus('Enter password.', 'error'); return; }
    if (selected.security === 'enterprise' && !id) { setStatus('Enter username.', 'error'); return; }
    doConnect(selected.ssid, pw, selected.security === 'enterprise' ? id : null);
  }

  function togglePw() {
    const inp = el('wifi-password');
    const eye = el('wifi-eye');
    if (inp.type === 'password') { inp.type = 'text'; eye.classList.add('active'); }
    else { inp.type = 'password'; eye.classList.remove('active'); }
  }

  function back() {
    if (connecting) return;
    showStep('scan');
    setStatus('');
    // If we were inside the network list when the user picked the wrong
    // SSID, return them to the list rather than the main wifi tab — saves
    // an extra "Change" click.
    if (_enteredPwFromList) {
      _enteredPwFromList = false;
      openList();
      // Re-render with the cached scan so they don't have to wait again,
      // and kick a fresh scan in the background.
      if (_lastNetworks) renderList(_lastNetworks);
      scan();
    }
  }

  function open(initialTab) {
    el('wifi-overlay').classList.add('active');
    showStep('scan');
    setStatus('');
    // 'wifi-list' = open WiFi tab and immediately pop the network list.
    // Used by agent offline / signin offline paths.
    const wantList = initialTab === 'wifi-list';
    const tab = wantList ? 'wifi'
      : (initialTab === 'wifi' || initialTab === 'ethernet') ? initialTab : 'ap';
    switchTab(tab);
    loadAp();
    loadInfo();
    loadInternet();
    scan();
    if (wantList) openList();
  }

  function openList() {
    el('wifi-list-overlay').classList.add('active');
    scan();
  }

  function closeList() {
    el('wifi-list-overlay').classList.remove('active');
  }

  function close() {
    if (connecting) return;
    el('wifi-overlay').classList.remove('active');
    closeList();
    if (typeof AGENT !== 'undefined' && AGENT._refreshGate) AGENT._refreshGate(true);
  }

  // Esc/Enter handlers
  document.addEventListener('keydown', (e) => {
    if (!el('wifi-overlay') || !el('wifi-overlay').classList.contains('active')) return;
    // Close list popup first if it's open
    if (e.key === 'Escape' && el('wifi-list-overlay') && el('wifi-list-overlay').classList.contains('active')) {
      closeList();
      return;
    }
    if (e.key === 'Escape') close();
    if (e.key === 'Enter' && el('wifi-step-pw').style.display !== 'none') connect();
  });

  // Switch-network confirm modal. Shown before any wlan0 reassociation that
  // would drop the in-flight HTTP connection (saved-activate or fresh
  // connect). Same modal for both paths — message stays short on purpose.
  let _switchConfirmHandlers = null;
  function _showSwitchConfirm(fromSsid, toSsid, onOk, onCancel) {
    const ov = el('wifi-switch-confirm');
    if (!ov) { onOk(); return; }
    const tEl = el('wifi-switch-to'); if (tEl) tEl.textContent = toSsid || 'this network';
    const fEl = el('wifi-switch-from'); if (fEl) fEl.textContent = (ownApSsid ? ownApSsid + ' WiFi' : 'WiFi');
    _switchConfirmHandlers = { onCancel };
    const okBtn = el('wifi-switch-ok');
    if (okBtn) okBtn.onclick = () => {
      _switchConfirmHandlers = null;
      ov.style.display = 'none';
      ov.classList.remove('active');
      try { onOk(); } catch (e) { console.error(e); }
    };
    ov.style.display = 'flex';
    ov.classList.add('active');
  }
  function _closeSwitchConfirm() {
    const ov = el('wifi-switch-confirm');
    if (ov) { ov.style.display = 'none'; ov.classList.remove('active'); }
    const h = _switchConfirmHandlers; _switchConfirmHandlers = null;
    if (h && h.onCancel) { try { h.onCancel(); } catch {} }
  }

  // Persistent SSE: keeps every tab in sync (own + cross-device changes,
  // plus host-level events like LAN cable plug/unplug picked up at the next
  // server poll). Started once at module load.
  ensureStream();

  return { open, close, openList, closeList, scan, connect, back, togglePw, switchTab, toggleApPw, toggleWifiPw, toggleEthPw, _showSwitchConfirm, _closeSwitchConfirm };
})();
