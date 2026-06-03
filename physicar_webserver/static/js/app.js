/* ===== Utility ===== */
const $ = (s) => document.getElementById(s);
const toDeg = (r) => r * 180 / Math.PI;
const toRad = (d) => d * Math.PI / 180;

/* ===== Global Sensor Data (for Agent) ===== */
let _currentState = {};
let _currentLidar = null;

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

/* ===== Router ===== */
let _currentPage = (location.hash.slice(1) || 'control');
let _internalHashChange = false;
function navigate(page, opts) {
  // No-op if already on this page (prevents redundant POSTs and DOM churn)
  if (page === _currentPage && !(opts && opts.force)) {
    // Still ensure UI matches state (handles initial load)
    const el = $('page-' + page);
    if (el && !el.classList.contains('active')) {
      document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
      el.classList.add('active');
      document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
      const nav = document.querySelector(`.nav-item[data-page="${page}"]`);
      if (nav) nav.classList.add('active');
      const sel = $('nav-select');
      if (sel && sel.value !== page) sel.value = page;
    }
    return;
  }
  // Guard: leaving #deepracer while a model is running.
  // Only ask the user that initiated the navigation (not silent broadcasts).
  if (
    !(opts && opts.silent) &&
    _currentPage === 'deepracer' &&
    page !== 'deepracer' &&
    typeof DR !== 'undefined' && DR.isRunning
  ) {
    // Revert any UI that already changed (select dropdown, URL hash)
    const sel = $('nav-select');
    if (sel) sel.value = _currentPage;
    if (location.hash.slice(1) !== _currentPage) {
      _internalHashChange = true;
      location.hash = _currentPage;
    }
    confirmModal(
      'A model is currently running. Switching tabs will automatically stop it. Continue?',
      { okText: 'Stop & Continue', cancelText: 'Cancel' }
    ).then(async (ok) => {
      if (!ok) return;
      try { await DR.stop(); } catch {}
      navigate(page);
    });
    return;
  }
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  const el = $('page-' + page);
  if (el) el.classList.add('active');
  const nav = document.querySelector(`.nav-item[data-page="${page}"]`);
  if (nav) nav.classList.add('active');
  const sel = $('nav-select');
  if (sel && sel.value !== page) sel.value = page;
  if (location.hash.slice(1) !== page) {
    _internalHashChange = true;
    location.hash = page;
  }
  _currentPage = page;
  if (page === 'control' && typeof CTRL !== 'undefined') {
    requestAnimationFrame(() => { CTRL._drawJoystick('drive'); CTRL._drawJoystick('cam'); });
  }
  if (page === 'deepracer' && typeof DR !== 'undefined') {
    requestAnimationFrame(() => DR.drawAction(0, 0, -1, []));
  }
  // Broadcast tab change to other browsers (unless this navigate() was
  // triggered by an incoming broadcast).
  if (!(opts && opts.silent)) {
    try { fetch('/uistate/tab', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ tab: page }) }); } catch {}
  }
}
document.querySelectorAll('.nav-item').forEach(btn => {
  btn.addEventListener('click', () => navigate(btn.dataset.page));
});
{
  const sel = $('nav-select');
  if (sel) sel.addEventListener('change', () => navigate(sel.value));
}
window.addEventListener('hashchange', () => {
  if (_internalHashChange) { _internalHashChange = false; return; }
  const h = location.hash.slice(1);
  if (h && h !== _currentPage) navigate(h);
});

/* ===== Cross-browser tab sync (SSE) ===== */
(function () {
  let es = null;
  function connect() {
    try { if (es) es.close(); } catch {}
    es = new EventSource('/uistate?stream=true');
    es.onmessage = (ev) => {
      try {
        const d = JSON.parse(ev.data);
        if (!d.tab) return;
        const cur = location.hash.slice(1) || 'control';
        if (d.tab !== cur) navigate(d.tab, { silent: true });
      } catch {}
    };
    es.onerror = () => { /* EventSource auto-reconnects */ };
  }
  connect();
})();

/* ===== Toast Notification ===== */
let _toastContainer;
function showToast(msg, isError) {
  if (!_toastContainer) { _toastContainer = document.createElement('div'); _toastContainer.className = 'toast-container'; document.body.appendChild(_toastContainer); }
  const t = document.createElement('div'); t.className = 'toast ' + (isError ? 'error' : 'info'); t.textContent = msg;
  _toastContainer.appendChild(t); setTimeout(() => t.remove(), 3000);
}

/* ===== Sensor Modal (camera/lidar zoom) =====
 * Camera: re-parents the existing #p-camera <img> into the modal so we keep
 *   a single MJPEG connection (otherwise two open streams = double bandwidth).
 *   On close, the <img> is moved back to its original placeholder.
 * Lidar: the modal canvas is drawn from the same SSE handler (drawLidar()).
 *   No polling — frames are pushed only when the LiDAR SSE actually delivers.
 */
let _modalLidarCanvas = null;
function openSensorModal(type) {
  closeSensorModal();
  const overlay = document.createElement('div');
  overlay.className = 'sensor-modal-overlay';
  overlay.onclick = (e) => { if (e.target === overlay) closeSensorModal(); };
  if (type === 'camera') {
    overlay.innerHTML = `<div class="sensor-modal"><div class="sensor-modal-header"><span>Camera</span><button class="sensor-modal-close" onclick="closeSensorModal()" aria-label="Close">&times;</button></div><div class="sensor-modal-body" id="modal-camera-host"></div></div>`;
    document.body.appendChild(overlay);
    const img = $('p-camera');
    if (img) {
      // Stash original parent + an anchor to restore in place on close.
      const placeholder = document.createComment('p-camera-placeholder');
      img.parentNode.insertBefore(placeholder, img);
      overlay._cameraPlaceholder = placeholder;
      overlay._cameraImg = img;
      $('modal-camera-host').appendChild(img);
      img.classList.add('modal-camera-img');
    }
  } else if (type === 'lidar') {
    overlay.innerHTML = `<div class="sensor-modal sensor-modal-lidar"><div class="sensor-modal-header"><span>LiDAR</span><button class="sensor-modal-close" onclick="closeSensorModal()" aria-label="Close">&times;</button></div><canvas id="modal-lidar"></canvas></div>`;
    document.body.appendChild(overlay);
    _modalLidarCanvas = $('modal-lidar');
    // Paint last known frame immediately; subsequent frames arrive via drawLidar().
    if (_currentLidar) _drawLidarOnCanvas(_modalLidarCanvas, _currentLidar);
  }
  document.addEventListener('keydown', _sensorModalEsc);
}
function closeSensorModal() {
  const o = document.querySelector('.sensor-modal-overlay');
  if (!o) return;
  // Restore camera img back to its original location.
  if (o._cameraImg && o._cameraPlaceholder) {
    o._cameraImg.classList.remove('modal-camera-img');
    o._cameraPlaceholder.parentNode.insertBefore(o._cameraImg, o._cameraPlaceholder);
    o._cameraPlaceholder.remove();
  }
  _modalLidarCanvas = null;
  o.remove();
  document.removeEventListener('keydown', _sensorModalEsc);
}
function _sensorModalEsc(e) { if (e.key === 'Escape') closeSensorModal(); }

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

/* ===== Connection Status ===== */
async function checkHealth() {
  const dot = $('status-dot');
  const text = $('status-text');
  try {
    const res = await fetch('/health', { signal: AbortSignal.timeout(3000) });
    if (res.ok) {
      dot?.classList.add('ok');
      if (text) text.textContent = 'Connected';
      return true;
    }
  } catch {}
  dot?.classList.remove('ok');
  if (text) text.textContent = 'Disconnected';
  return false;
}

/* ===== Sensor Panel Streams ===== */
let stateES = null, lidarES = null;
let _stateCount = 0, _lidarCount = 0;

function startStateStream() {
  if (stateES) return;
  console.log('[SSE] State: connecting...');
  stateES = new EventSource('/state?stream=true&include=cmd,odom,battery,imu');
  stateES.onopen = () => console.log('[SSE] State: connected');
  stateES.onmessage = (e) => {
    _stateCount++;
    try {
      const d = JSON.parse(e.data);
      if (_stateCount <= 3 || _stateCount % 100 === 0) console.log('[SSE] State #' + _stateCount, Object.keys(d));
      updateState(d);
    } catch (err) { console.error('[SSE] State parse error:', err, e.data?.slice(0,100)); }
  };
  stateES.onerror = (e) => {
    console.warn('[SSE] State error, readyState=' + stateES.readyState, e);
    stateES.close(); stateES = null; _stateCount = 0;
    setTimeout(startStateStream, 3000);
  };
}

function updateState(d) {
  Object.assign(_currentState, d);
  if (d.battery) {
    const pct = Math.round(d.battery.percentage || 0);
    const v = (d.battery.voltage || 0).toFixed(1);
    $('p-battery').textContent = pct + '%';
    $('p-voltage').textContent = v + 'V';
    const fill = $('p-battery-fill');
    fill.style.width = pct + '%';
    fill.className = 'battery-fill' + (pct > 60 ? '' : pct > 20 ? ' medium' : ' low');
  }
  if (d.odom) {
    const spd = Math.abs(d.odom.velocity?.linear || 0) < 0.05 ? '0.0' : (d.odom.velocity?.linear || 0).toFixed(1);
    $('p-speed').textContent = spd + ' m/s';
  }
  if (d.cmd) {
    $('p-steering').textContent = toDeg(d.cmd.steering || 0).toFixed(0) + '\u00b0';
    $('p-pantilt').textContent = toDeg(d.cmd.pan || 0).toFixed(0) + '\u00b0 / ' + toDeg(d.cmd.tilt || 0).toFixed(0) + '\u00b0';
  }
  if (d.imu && d.imu.acceleration) {
    const ax = (a => Math.abs(a) < 0.05 ? '0.0' : a.toFixed(1))(d.imu.acceleration.x || 0);
    $('p-accel').textContent = ax + ' m/s²';
  }
}

function startLidarStream() {
  if (lidarES) return;
  const step = $('lidar-step')?.value || 3;
  console.log('[SSE] Lidar: connecting, step=' + step);
  lidarES = new EventSource('/state/lidar?stream=true&step=' + step);
  lidarES.onopen = () => console.log('[SSE] Lidar: connected');
  lidarES.onmessage = (e) => {
    _lidarCount++;
    try {
      const d = JSON.parse(e.data);
      _currentLidar = d;
      if (_lidarCount <= 3 || _lidarCount % 50 === 0) console.log('[SSE] Lidar #' + _lidarCount, 'count=' + d.count);
      drawLidar(d);
    } catch (err) { console.error('[SSE] Lidar parse error:', err, e.data?.slice(0,100)); }
  };
  lidarES.addEventListener('keepalive', () => {});
  lidarES.onerror = (e) => {
    console.warn('[SSE] Lidar error, readyState=' + lidarES.readyState, e);
    lidarES.close(); lidarES = null; _lidarCount = 0;
    setTimeout(startLidarStream, 3000);
  };
}
function changeLidarStep() {
  localStorage.setItem('lidar-step', $('lidar-step').value);
  if (lidarES) { lidarES.close(); lidarES = null; _lidarCount = 0; }
  startLidarStream();
}
function changeLidarRange() {
  localStorage.setItem('lidar-range', $('lidar-range').value);
}

function drawLidar(lidar) {
  _drawLidarOnCanvas($('p-lidar'), lidar);
  if (_modalLidarCanvas) _drawLidarOnCanvas(_modalLidarCanvas, lidar);
}

function _drawLidarOnCanvas(c, lidar) {
  if (!c) return;
  const rect = c.getBoundingClientRect();
  if (rect.width < 10 || rect.height < 10) return;
  if (c.width !== rect.width || c.height !== rect.height) { c.width = rect.width; c.height = rect.height; }
  const ctx = c.getContext('2d');
  const w = c.width, h = c.height, cx = w/2, cy = h/2;
  ctx.fillStyle = '#0a0a0a'; ctx.fillRect(0,0,w,h);

  let entries = [];
  if (Array.isArray(lidar.ranges)) {
    const step = lidar.step || 1;
    entries = lidar.ranges.map((r,i) => [i*step, r]);
  } else if (lidar.ranges && typeof lidar.ranges === 'object') {
    entries = Object.entries(lidar.ranges).map(([k,v]) => [parseFloat(k), v]);
  }
  if (!entries.length) return;

  const rMin = lidar.range_min || 0.1, rMax = lidar.range_max || 16;
  const dispRange = parseFloat($('lidar-range')?.value || 4);
  const scale = (Math.min(w,h)/2 - 6) / dispRange;

  ctx.strokeStyle = '#222'; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.arc(cx, cy, dispRange*scale, 0, Math.PI*2); ctx.stroke();
  ctx.setLineDash([3,3]); ctx.beginPath(); ctx.arc(cx, cy, dispRange/2*scale, 0, Math.PI*2); ctx.stroke(); ctx.setLineDash([]);

  ctx.fillStyle = '#0f0';
  for (const [deg, r] of entries) {
    if (r != null && r > rMin && r < rMax && isFinite(r)) {
      const a = deg * Math.PI / 180;
      ctx.fillRect(cx - Math.sin(a)*r*scale - 1, cy - Math.cos(a)*r*scale - 1, 2, 2);
    }
  }
  ctx.fillStyle = '#f00'; ctx.beginPath(); ctx.moveTo(cx, cy-5); ctx.lineTo(cx-3, cy+3); ctx.lineTo(cx+3, cy+3); ctx.closePath(); ctx.fill();
}

function startCameraStream() {
  const w = $('cam-res')?.value || 480;
  console.log('[CAM] Starting camera stream, width=' + w);
  _startMjpegFetch(w);
}

let _camRetryTimer = null;
let _camAbort = null;

function _stopCameraStream() {
  if (_camRetryTimer) { clearTimeout(_camRetryTimer); _camRetryTimer = null; }
  if (_camAbort) { _camAbort.abort(); _camAbort = null; }
}

async function _startMjpegFetch(width) {
  _stopCameraStream();
  const img = $('p-camera');
  if (!img) return;

  const controller = new AbortController();
  _camAbort = controller;

  try {
    const url = '/state/camera?stream=true&width=' + width + '&t=' + Date.now();
    const res = await fetch(url, { signal: controller.signal });
    if (!res.ok || !res.body) throw new Error('status ' + res.status);

    const reader = res.body.getReader();
    let buf = new Uint8Array(0);

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      // Append chunk to buffer
      const tmp = new Uint8Array(buf.length + value.length);
      tmp.set(buf); tmp.set(value, buf.length);
      buf = tmp;

      // Extract JPEG frames from multipart stream
      while (true) {
        // Find JPEG start (FFD8) and end (FFD9)
        let jpegStart = -1;
        for (let i = 0; i < buf.length - 1; i++) {
          if (buf[i] === 0xFF && buf[i+1] === 0xD8) { jpegStart = i; break; }
        }
        if (jpegStart < 0) break;

        let jpegEnd = -1;
        for (let i = jpegStart + 2; i < buf.length - 1; i++) {
          if (buf[i] === 0xFF && buf[i+1] === 0xD9) { jpegEnd = i + 2; break; }
        }
        if (jpegEnd < 0) break;

        // Got a complete JPEG frame
        const frame = buf.slice(jpegStart, jpegEnd);
        buf = buf.slice(jpegEnd);

        // Display frame
        const blob = new Blob([frame], { type: 'image/jpeg' });
        const blobUrl = URL.createObjectURL(blob);
        const prev = img.src;
        img.src = blobUrl;
        if (prev && prev.startsWith('blob:')) URL.revokeObjectURL(prev);
      }
    }
  } catch (e) {
    if (e.name === 'AbortError') return;
    console.warn('[CAM] Stream error, retrying in 3s...', e.message);
  }
  // Retry on disconnect
  _camRetryTimer = setTimeout(() => _startMjpegFetch(width), 3000);
}

function changeCameraRes() {
  localStorage.setItem('cam-res', $('cam-res').value);
  startCameraStream();
}

/* ===== Container Restart ===== */
async function restartContainer() {
  const ok = await confirmModal('Restart PhysiCar ROS?\nThis will rebuild and relaunch all nodes.', { danger: true, confirmText: 'Restart' });
  if (!ok) return;
  const btn = $('btn-restart');
  if (btn) { btn.disabled = true; btn.textContent = 'Restarting…'; }
  try {
    const res = await fetch('/api/restart', { method: 'POST' });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || 'Failed');
  } catch (e) {
    if (btn) { btn.disabled = false; btn.innerHTML = '&#x21bb; Restart'; }
    showToast('Restart failed: ' + e.message, true);
    return;
  }
  // Hand off to the single connection watcher — it handles overlay, camera, button
  DeviceWatcher.forceDown();
}

/* ===== Device Connection Watcher =====
 * Single source of truth for backend connectivity.
 * Polls /health every 2s (accelerated while down, normal 5s when up).
 * Shows overlay, kills/restarts camera, resets restart button.
 */
const DeviceWatcher = (() => {
  const PERIOD_UP = 5000;
  const PERIOD_DOWN = 2000;
  const FAIL_THRESHOLD = 2;
  const TIMEOUT = 4000;
  let fails = 0;
  let overlay = null;
  let timer = null;

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
    // Kill stale camera stream so last frame doesn't linger
    _stopCameraStream();
    const cam = $('p-camera');
    if (cam) cam.src = '';
    // Accelerate polling while down
    schedule(PERIOD_DOWN);
  }

  function hideOverlay() {
    if (overlay) overlay.classList.remove('show');
    // Restore everything
    startCameraStream();
    const btn = $('btn-restart');
    if (btn) { btn.disabled = false; btn.innerHTML = '&#x21bb; Restart'; }
    // Back to normal polling
    schedule(PERIOD_UP);
  }

  function schedule(ms) {
    if (timer) clearTimeout(timer);
    timer = setTimeout(ping, ms);
  }

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

  return { forceDown };
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
