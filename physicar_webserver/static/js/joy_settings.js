/* Joystick Settings Panel — shared between / (settings modal) and /kiosk.
 *
 * Usage:
 *   const panel = createJoyPanel({
 *     liveId: 'settings-joy-live',
 *     mapId:  'settings-joy-map',
 *     tuneId: 'settings-joy-tune',
 *     statusId: 'settings-joy-status',  // optional
 *     enabledId: 'settings-joy-enabled',
 *   });
 *   panel.open();   // when tab becomes visible
 *   panel.close();  // when tab/modal closes
 *
 * Live input arrives via SSE on /joy/raw?stream=true.
 * Mapping is loaded/persisted via /joy/mapping (GET / POST).
 * Enabled flag via /joy/enabled (GET / POST).
 *
 * Capture flow: clicking [Capture] on a mapping row arms it.  The next
 * axis movement (>0.5 from rest) or button press is recorded as the new
 * index and POSTed to /joy/mapping (save=true).
 */
window.createJoyPanel = function createJoyPanel(opts) {
  const {
    liveId, mapId, tuneId, statusId = null, enabledId = null,
    connectedId = null,
  } = opts;

  // Mapping role definitions — display label + which kind of input we
  // expect when the user clicks Capture on this row.
  const MAP_AXES = [
    { key: 'axis_steering', label: 'Steering (left/right)' },
    { key: 'axis_speed',    label: 'Speed (forward/back)' },
    { key: 'axis_pan',      label: 'Camera Pan' },
    { key: 'axis_tilt',     label: 'Camera Tilt' },
  ];
  const MAP_BUTTONS = [
    { key: 'deadman_button',       label: 'Engage Drive (hold to drive)' },
    { key: 'camera_assist_button', label: 'Engage Camera (hold to aim)' },
    { key: 'estop_button',         label: 'Emergency Stop (toggle)' },
    { key: 'center_camera_button', label: 'Recenter Camera' },
  ];
  const TUNE_FLOATS = [
    { key: 'max_speed',    label: 'Max speed (m/s)',  min: 0.5, max: 3.0,  step: 0.1 },
    { key: 'max_steering', label: 'Max steering (deg)', min: 1,  max: 20,  step: 1 },
    { key: 'max_pan',      label: 'Max pan (deg)',    min: 1,   max: 30,   step: 1 },
    { key: 'max_tilt',     label: 'Max tilt (deg)',   min: 1,   max: 30,   step: 1 },
    { key: 'deadzone',     label: 'Deadzone',         min: 0.0, max: 0.1,  step: 0.01 },
  ];
  const TUNE_BOOLS = [
    { key: 'invert_speed',    label: 'Invert speed' },
    { key: 'invert_steering', label: 'Invert steering' },
    { key: 'invert_pan',      label: 'Invert pan' },
    { key: 'invert_tilt',     label: 'Invert tilt' },
  ];

  // Axis Tuning is rendered as 5 rows: 4 paired (Speed / Steering /
  // Camera Pan / Camera Tilt) each carrying a max-value input *and* an
  // invert toggle on the same row, plus a final solo row for Deadzone.
  const TUNE_PAIRS = [
    { label: 'Speed',       maxKey: 'max_speed',    invKey: 'invert_speed' },
    { label: 'Steering',    maxKey: 'max_steering', invKey: 'invert_steering' },
    { label: 'Camera Pan',  maxKey: 'max_pan',      invKey: 'invert_pan' },
    { label: 'Camera Tilt', maxKey: 'max_tilt',     invKey: 'invert_tilt' },
  ];

  let mapping = {};
  let mappingJson = '';   // last applied mapping serialised, for change-detect
  let lastAxes = [];      // for capture rest-baseline
  let captureRow = null;  // { kind: 'axis'|'button', key }
  let evtSrc = null;
  let stateSrc = null;    // SSE on /joy/state?stream=true (cross-window sync)
  let liveData = null;    // last {axes, buttons}

  function el(id) { return document.getElementById(id); }
  function setStatus(msg, kind) {
    if (!statusId) return;
    const s = el(statusId);
    if (!s) return;
    s.textContent = msg || '';
    s.className = 'settings-hint' + (kind ? ' ' + kind : '');
  }
  function toast(msg, err) {
    if (typeof showToast === 'function') showToast(msg, !!err);
    else if (err) console.error(msg);
    else console.log(msg);
  }

  // ──────────────── LIVE INPUT (SSE /joy/raw) ────────────────
  function startStream() {
    if (evtSrc) return;
    try {
      evtSrc = new EventSource('/teleop/joy/raw?stream=true');
      evtSrc.onmessage = (e) => {
        try {
          const d = JSON.parse(e.data);
          if (d && Array.isArray(d.axes) && Array.isArray(d.buttons)) {
            liveData = d;
            renderLive(d);
            handleCapture(d);
          }
        } catch {}
      };
      evtSrc.onerror = () => {
        // EventSource auto-reconnects; ignore.
      };
    } catch {}
  }
  function stopStream() {
    if (evtSrc) { try { evtSrc.close(); } catch {} evtSrc = null; }
    captureRow = null;
    document.querySelectorAll(`#${mapId} .joy-card.capturing`).forEach(r => r.classList.remove('capturing'));
  }

  function renderLive(d) {
    const cont = el(liveId);
    if (!cont) return;
    if (!d || (!d.axes.length && !d.buttons.length)) {
      cont.innerHTML = '<div class="joy-live-empty">Waiting for /joy …</div>';
      return;
    }

    let html = '<div class="joy-live-cols">';

    html += '<div class="joy-live-col"><div class="joy-live-col-title">Axes</div><div class="joy-axes">';
    d.axes.forEach((v, i) => {
      const pct = Math.max(-1, Math.min(1, v));
      const left = pct >= 0 ? 50 : 50 + pct * 50;
      const w = Math.abs(pct) * 50;
      const usedFor = roleForAxis(i);
      const role = usedFor ? `<span class="joy-role">${usedFor}</span>` : '<span class="joy-role"></span>';
      html += `<div class="joy-axis" data-idx="${i}">
        <span class="joy-axis-idx">${i}</span>
        ${role}
        <div class="joy-axis-track"><div class="joy-axis-fill" style="left:${left}%;width:${w}%"></div><div class="joy-axis-mid"></div></div>
        <span class="joy-axis-val">${v.toFixed(2)}</span>
      </div>`;
    });
    if (!d.axes.length) html += '<div class="joy-live-empty">No axes</div>';
    html += '</div></div>';

    html += '<div class="joy-live-col"><div class="joy-live-col-title">Buttons</div><div class="joy-buttons">';
    d.buttons.forEach((v, i) => {
      const usedFor = roleForButton(i);
      const role = usedFor ? `<span class="joy-btn-role">${usedFor}</span>` : '<span class="joy-btn-role"></span>';
      html += `<div class="joy-btn ${v ? 'pressed' : ''}" data-idx="${i}">
        <span class="joy-btn-idx">${i}</span>
        ${role}
        <span class="joy-btn-state">${v ? 'On' : 'Off'}</span>
      </div>`;
    });
    if (!d.buttons.length) html += '<div class="joy-live-empty">No buttons</div>';
    html += '</div></div>';

    html += '</div>';

    // Structure-stable update: only rebuild on shape change so scroll
    // position inside the columns stays put across SSE frames.
    const shape = `${d.axes.length}|${d.buttons.length}`;
    if (cont._shape !== shape) {
      cont.innerHTML = html;
      cont._shape = shape;
      return;
    }
    // Same shape — patch values in-place
    const axes = cont.querySelectorAll('.joy-axis');
    d.axes.forEach((v, i) => {
      const row = axes[i]; if (!row) return;
      const pct = Math.max(-1, Math.min(1, v));
      const left = pct >= 0 ? 50 : 50 + pct * 50;
      const w = Math.abs(pct) * 50;
      const fill = row.querySelector('.joy-axis-fill');
      if (fill) { fill.style.left = left + '%'; fill.style.width = w + '%'; }
      const valEl = row.querySelector('.joy-axis-val');
      if (valEl) valEl.textContent = v.toFixed(2);
    });
    const btns = cont.querySelectorAll('.joy-btn');
    d.buttons.forEach((v, i) => {
      const row = btns[i]; if (!row) return;
      row.classList.toggle('pressed', !!v);
      const st = row.querySelector('.joy-btn-state');
      if (st) st.textContent = v ? 'On' : 'Off';
    });
  }

  function roleForAxis(i) {
    for (const a of MAP_AXES) if (mapping[a.key] === i) return shortLabel(a.label);
    return null;
  }
  function roleForButton(i) {
    for (const b of MAP_BUTTONS) if (mapping[b.key] === i) return shortLabel(b.label);
    return null;
  }
  // "Camera Pan", "Drive", "Emergency Stop" — drop the parenthetical
  // hint that's only useful in the mapping editor, but keep the full name.
  function shortLabel(s) {
    const m = String(s).match(/^([^(]+)/);
    return m ? m[1].trim() : s;
  }

  // ──────────────── MAPPING (GET/POST /joy/mapping) ────────────────
  async function loadMapping() {
    try {
      const r = await fetch('/teleop/joy/mapping');
      const d = await r.json();
      let m = null;
      if (d && d.success && d.mapping) m = d.mapping;
      else if (d && typeof d === 'object') m = d.mapping || d;
      if (m) applyMapping(m);
    } catch (e) {
      setStatus('Failed to load mapping: ' + e.message, 'err');
    }
  }

  // Apply a mapping snapshot from any source (initial GET, SSE push, or
  // local POST result).  No-op when the payload is identical so SSE-driven
  // updates don't cause flicker / scroll jumps in other windows.
  function applyMapping(m) {
    if (!m || typeof m !== 'object') return;
    let json;
    try { json = JSON.stringify(m, Object.keys(m).sort()); }
    catch { json = ''; }
    if (json && json === mappingJson) return;
    mapping = m;
    mappingJson = json;
    renderMapping();
  }

  function applyEnabled(v) {
    if (!enabledId) return;
    const el2 = el(enabledId);
    if (!el2) return;
    const next = !!v;
    if (el2.checked !== next) el2.checked = next;
  }

  function applyConnected(v, deviceName) {
    if (!connectedId) return;
    const el2 = el(connectedId);
    if (!el2) return;
    const ok = !!v;
    if (ok) {
      // Show the actual product name when we have it (e.g. "Xbox Wireless
      // Controller") rather than a generic "Yes" — answers the question
      // "which controller did joy_node bind to?".
      el2.textContent = deviceName || 'Yes';
      el2.title = deviceName || '';
      el2.style.color = '#7cf57c';
    } else {
      el2.textContent = 'No';
      el2.title = '';
      el2.style.color = '#888';
    }
  }

  // SSE on /joy/state — single connection that delivers mapping + enabled
  // + connected updates so multiple open windows mirror each other within
  // ~50 ms of any change.  Uses the broadcaster bump() pattern from
  // routers/joy.py (see _JoyBroadcaster).
  function startStateStream() {
    if (stateSrc) return;
    try {
      stateSrc = new EventSource('/teleop/joy/state?stream=true');
      stateSrc.onmessage = (e) => {
        try {
          const d = JSON.parse(e.data);
          if (!d || typeof d !== 'object') return;
          if (d.mapping) applyMapping(d.mapping);
          if ('enabled' in d) applyEnabled(d.enabled);
          if ('connected' in d) applyConnected(d.connected, d.device);
        } catch {}
      };
      stateSrc.onerror = () => { /* auto-reconnects */ };
    } catch {}
  }
  function stopStateStream() {
    if (stateSrc) { try { stateSrc.close(); } catch {} stateSrc = null; }
  }

  async function saveKey(key, value) {
    try {
      const r = await fetch('/teleop/joy/mapping', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key, value, save: true }),
      });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        toast(d.detail || 'Failed to save', true);
        return false;
      }
      // Optimistic local update — the SSE broadcaster will also push the
      // new value to every open window (including this one).  applyMapping
      // dedupes so we don't double-render.
      const next = { ...mapping, [key]: value };
      applyMapping(next);
      return true;
    } catch (e) {
      toast('Error: ' + e.message, true);
      return false;
    }
  }

  function renderMapping() {
    // Mapping changed → invalidate live-view cache so role labels redraw.
    const live = el(liveId);
    if (live) live._shape = null;
    const map = el(mapId);
    if (map) {
      const cardHtml = (r) => {
        const cur = mapping[r.key];
        const tag = r.kind === 'axis' ? 'Axis' : 'Button';
        const idxLabel = (cur === undefined || cur === null) ? '—' : `${tag} ${cur}`;
        return `<button type="button" class="joy-card" data-key="${r.key}" data-kind="${r.kind}">
          <span class="joy-card-label">${shortLabel(r.label)}</span>
          <span class="joy-card-idx">${idxLabel}</span>
        </button>`;
      };
      const axesHtml  = MAP_AXES.map(r    => cardHtml({ ...r, kind: 'axis' })).join('');
      const btnsHtml  = MAP_BUTTONS.map(r => cardHtml({ ...r, kind: 'button' })).join('');
      map.innerHTML = `
        <div class="joy-map-cols">
          <div class="joy-map-col">
            <div class="joy-map-col-title">Axes</div>
            <div class="joy-map-grid">${axesHtml}</div>
          </div>
          <div class="joy-map-col">
            <div class="joy-map-col-title">Buttons</div>
            <div class="joy-map-grid">${btnsHtml}</div>
          </div>
        </div>`;
      map.querySelectorAll('.joy-card').forEach(card => {
        card.addEventListener('click', () => {
          if (captureRow && captureRow.key === card.dataset.key) {
            // Click again to cancel.
            captureRow = null;
            card.classList.remove('capturing');
            setStatus('', '');
          } else {
            beginCapture(card);
          }
        });
      });
    }

    const tune = el(tuneId);
    if (tune) {
      const findFloat = (key) => TUNE_FLOATS.find(f => f.key === key);
      const fmt = (def, v) => {
        // Match step decimal places so 25.0 stays 25, 0.08 stays 0.08
        const s = String(def.step);
        const dot = s.indexOf('.');
        const decimals = dot < 0 ? 0 : (s.length - dot - 1);
        return Number(v).toFixed(decimals);
      };
      const stepper = (def) => {
        const v = mapping[def.key] ?? def.min;
        return `<div class="joy-step" data-key="${def.key}">
          <button type="button" class="joy-step-btn" data-dir="-1" aria-label="Decrease">−</button>
          <span class="joy-step-val">${fmt(def, v)}</span>
          <button type="button" class="joy-step-btn" data-dir="1" aria-label="Increase">+</button>
        </div>`;
      };
      // Unit (m/s, deg) is shown next to the row name rather than inside
      // the stepper so all five steppers stay perfectly column-aligned.
      const nameWithUnit = (label, def) => {
        const unit = (def.label.match(/\(([^)]+)\)/) || [,''])[1];
        return unit ? `${label} <span class="joy-tune-unit">(${unit})</span>` : label;
      };
      let html = '';
      TUNE_PAIRS.forEach(p => {
        const fdef = findFloat(p.maxKey);
        const inv = !!mapping[p.invKey];
        html += `<div class="joy-tune-row joy-tune-row--pair">
          <span class="joy-tune-name">${nameWithUnit(p.label, fdef)}</span>
          <span class="joy-tune-max">${stepper(fdef)}</span>
          <label class="joy-tune-invert">
            <span class="joy-tune-invert-label">Invert</span>
            <label class="cal-switch"><input type="checkbox" data-key="${p.invKey}" ${inv ? 'checked' : ''}><span class="cal-slider"></span></label>
          </label>
        </div>`;
      });
      const dz = findFloat('deadzone');
      html += `<div class="joy-tune-row joy-tune-row--pair">
        <span class="joy-tune-name">${nameWithUnit('Deadzone', dz)}</span>
        <span class="joy-tune-max">${stepper(dz)}</span>
        <span class="joy-tune-invert"></span>
      </div>`;
      tune.innerHTML = html;
      tune.querySelectorAll('.joy-step').forEach(st => {
        const key = st.dataset.key;
        const def = findFloat(key);
        st.querySelectorAll('.joy-step-btn').forEach(b => {
          b.addEventListener('click', async () => {
            const cur = Number(mapping[key] ?? def.min);
            const dir = Number(b.dataset.dir);
            // Snap to step grid so floating-point dust doesn't accumulate
            // (e.g. 0.08 + 0.01 + 0.01 -> 0.09999...).
            let next = Math.round((cur + dir * def.step) / def.step) * def.step;
            next = Math.max(def.min, Math.min(def.max, next));
            const dot = String(def.step).indexOf('.');
            if (dot >= 0) next = +next.toFixed(String(def.step).length - dot - 1);
            if (next === cur) return;
            const ok = await saveKey(key, next);
            if (ok) {
              const valEl = st.querySelector('.joy-step-val');
              if (valEl) valEl.textContent = fmt(def, next);
            }
          });
        });
      });
      tune.querySelectorAll('input[type=checkbox]').forEach(inp => {
        inp.addEventListener('change', async () => {
          await saveKey(inp.dataset.key, !!inp.checked);
        });
      });
    }
  }

  // ──────────────── CAPTURE FLOW ────────────────
  function beginCapture(card) {
    document.querySelectorAll(`#${mapId} .joy-card.capturing`).forEach(r => r.classList.remove('capturing'));
    card.classList.add('capturing');
    captureRow = { key: card.dataset.key, kind: card.dataset.kind };
    // baseline current axes so we don't false-trigger on resting drift
    lastAxes = liveData ? liveData.axes.slice() : [];
    setStatus(`Move/press the control to bind "${card.dataset.key}"… (tap card again to cancel)`, 'info');
  }

  function handleCapture(d) {
    if (!captureRow) return;
    if (captureRow.kind === 'button') {
      const idx = d.buttons.findIndex(b => b);
      if (idx >= 0) finishCapture(idx);
    } else {
      // axis: detect first axis whose magnitude crossed 0.5 from baseline
      for (let i = 0; i < d.axes.length; i++) {
        const base = (lastAxes[i] ?? 0);
        if (Math.abs(d.axes[i] - base) > 0.5) {
          finishCapture(i);
          return;
        }
      }
    }
  }

  async function finishCapture(idx) {
    const cap = captureRow;
    captureRow = null;
    document.querySelectorAll(`#${mapId} .joy-card.capturing`).forEach(r => r.classList.remove('capturing'));
    setStatus('Saving…', 'info');
    const ok = await saveKey(cap.key, idx);
    setStatus(ok ? `Bound ${cap.key} → ${cap.kind === 'axis' ? 'Axis' : 'Button'} ${idx}` : 'Save failed', ok ? 'ok' : 'err');
    setTimeout(() => setStatus('', ''), 2500);
  }

  // ──────────────── ENABLED TOGGLE ────────────────
  async function loadEnabled() {
    if (!enabledId) return;
    try {
      const r = await fetch('/teleop/joy/enabled');
      const d = await r.json();
      applyEnabled(!!d.enabled);
      if ('connected' in d) applyConnected(!!d.connected, d.device);
    } catch {}
  }
  async function toggleEnabled() {
    if (!enabledId) return;
    const v = el(enabledId).checked;
    try {
      const r = await fetch('/teleop/joy/enabled', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: v }),
      });
      if (!r.ok) {
        el(enabledId).checked = !v;
        toast('Failed to toggle joystick', true);
      }
    } catch (e) {
      el(enabledId).checked = !v;
      toast('Error: ' + e.message, true);
    }
  }

  async function reset() {
    if (!confirm('Reset joystick mapping to defaults?\n(Defaults apply within ~2 seconds.)')) return;
    try {
      const r = await fetch('/teleop/joy/mapping/reset', { method: 'POST' });
      const d = await r.json().catch(() => ({}));
      if (r.ok) {
        toast(d.restarted ? 'Mapping reset (joy node restarting…)' : 'Mapping reset', false);
        // Give the respawned node a moment to come back, then reload the UI
        setTimeout(loadMapping, 2500);
      } else {
        toast(d.detail || 'Reset failed', true);
      }
    } catch (e) {
      toast('Error: ' + e.message, true);
    }
  }

  function open() {
    loadMapping();
    loadEnabled();
    startStream();
    startStateStream();
  }
  function close() {
    stopStream();
    stopStateStream();
  }

  return { open, close, toggleEnabled, reset, loadMapping };
};
