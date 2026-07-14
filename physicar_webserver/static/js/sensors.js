/* ===== Sensor panel — camera / lidar / velocity / battery streams =====
 * Used by the /app page (right column). The ONLY module that opens the
 * sensor streams (/states SSE, /lidar SSE, /camera MJPEG) — one browser
 * tab should hold at most one set of streaming connections.
 */

let _currentState = {};
let _currentLidar = null;

/* ===== Sensor Panel Streams ===== */
let stateES = null, lidarES = null;
let _stateCount = 0, _lidarCount = 0;

/* Reconnect backoff: 3s doubling to 30s (reset on successful connect).
 * Flat 3s retries hammer the tunnel proxy (billable per request) when the
 * backend is down for a while; DeviceWatcher.kick() keeps detection instant. */
const _RETRY_MIN = 3000, _RETRY_MAX = 30000;
let _stateRetry = _RETRY_MIN, _lidarRetry = _RETRY_MIN, _camRetry = _RETRY_MIN;

function startStateStream() {
  if (stateES) return;
  console.log('[SSE] State: connecting...');
  stateES = new EventSource('/states?stream=true&include=cmd,odom,battery,imu');
  stateES.onopen = () => { _stateRetry = _RETRY_MIN; console.log('[SSE] State: connected'); };
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
    setTimeout(startStateStream, _stateRetry);
    _stateRetry = Math.min(_stateRetry * 2, _RETRY_MAX);
    DeviceWatcher.kick();
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
    const lin = ((d.odom.velocity?.linear || 0).toFixed(1)).replace(/^-(0(\.0+)?)$/, '$1');
    const ang = Math.abs(d.odom.velocity?.angular || 0) < 0.01 ? '0.0' : (d.odom.velocity?.angular || 0).toFixed(2);
    $('p-linear').textContent = lin + ' m/s';
    $('p-angular').textContent = ang + ' rad/s';
  }
}

function startLidarStream() {
  if (lidarES) return;
  const step = $('lidar-step')?.value || 3;
  console.log('[SSE] Lidar: connecting, step=' + step);
  lidarES = new EventSource('/lidar?stream=true&step=' + step);
  lidarES.onopen = () => { _lidarRetry = _RETRY_MIN; console.log('[SSE] Lidar: connected'); };
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
    setTimeout(startLidarStream, _lidarRetry);
    _lidarRetry = Math.min(_lidarRetry * 2, _RETRY_MAX);
    DeviceWatcher.kick();
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
    const url = '/camera?stream=true&width=' + width + '&t=' + Date.now();
    const res = await fetch(url, { signal: controller.signal });
    if (!res.ok || !res.body) throw new Error('status ' + res.status);
    _camRetry = _RETRY_MIN;

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
  // Retry on disconnect (backoff; reset where the stream delivers frames)
  _camRetryTimer = setTimeout(() => _startMjpegFetch(width), _camRetry);
  _camRetry = Math.min(_camRetry * 2, _RETRY_MAX);
  DeviceWatcher.kick();
}

function changeCameraRes() {
  localStorage.setItem('cam-res', $('cam-res').value);
  startCameraStream();
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

/* ===== Watcher hooks: tear down / restore streams around connection loss ===== */
DeviceWatcher.onDown = () => {
  // Kill stale camera stream so last frame doesn't linger
  _stopCameraStream();
  const cam = $('p-camera');
  if (cam) cam.src = '';
};
DeviceWatcher.onUp = () => {
  startCameraStream();
  const btn = $('btn-restart');
  if (btn) { btn.disabled = false; btn.innerHTML = '&#x21bb; Restart'; }
};
