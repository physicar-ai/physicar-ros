/* ===== Control Module ===== */
const CTRL = {
  // Config
  MAX_STEER: 0.349,          // rad (~20°) max steering
  MAX_PAN: 0.5236,           // rad (30°) - matches real kit max_pan
  MAX_TILT: 0.5236,          // rad (30°) - matches real kit max_tilt
  SEND_HZ: 10,               // control send rate

  // State
  maxSpeed: 1.0,             // m/s, adjustable
  joy: { drive: { x: 0, y: 0 }, cam: { x: 0, y: 0 } },
  _sendTimer: null,
  _joyDragging: { drive: false, cam: false },
  _joyPointer: { drive: null, cam: null },

  init() {
    this._initJoystick('drive');
    this._initJoystick('cam');
    this._initKeyboard();
    this._sendTimer = setInterval(() => this._sendCommands(), 1000 / this.SEND_HZ);
    this._openStream('/speed');    // pre-connect before the first input
    this._openStream('/steering');
    // 25s keepalive ({} frame — ignored by the server): protects idle sockets
    // from NAT/tunnel idle timeouts. Not a value publish, so it never touches
    // the dead-man switch or robot state.
    setInterval(() => {
      for (const p in this._streams) {
        const ws = this._streams[p];
        if (ws && ws.readyState === WebSocket.OPEN) { try { ws.send('{}'); } catch (e) {} }
      }
    }, 25000);
  },

  setMaxSpeed(val) {
    this.maxSpeed = parseFloat(val);
    const label = $('ctrl-max-speed-val');
    if (label) label.textContent = this.maxSpeed.toFixed(1);
  },

  // ─── Joystick ─────────────────────────────────────────────
  _initJoystick(group) {
    const canvas = $(group === 'drive' ? 'ctrl-drive-canvas' : 'ctrl-cam-canvas');
    if (!canvas) return;

    const getPos = (e) => {
      const rect = canvas.getBoundingClientRect();
      const clientX = e.touches ? e.touches[0].clientX : e.clientX;
      const clientY = e.touches ? e.touches[0].clientY : e.clientY;
      // Normalize to -1..1
      let x = ((clientX - rect.left) / rect.width) * 2 - 1;
      let y = -(((clientY - rect.top) / rect.height) * 2 - 1); // Y up
      // Clamp to square [-1, 1]
      x = Math.max(-1, Math.min(1, x));
      y = Math.max(-1, Math.min(1, y));
      return { x, y };
    };

    const onStart = (e) => {
      e.preventDefault();
      this._joyDragging[group] = true;
      this._joyPointer[group] = e.pointerId ?? null;
      if (e.pointerId != null) canvas.setPointerCapture(e.pointerId);
      const pos = getPos(e);
      this.joy[group] = pos;
      this._drawJoystick(group);
    };

    const onMove = (e) => {
      if (!this._joyDragging[group]) return;
      e.preventDefault();
      const pos = getPos(e);
      this.joy[group] = pos;
      this._drawJoystick(group);
    };

    const onEnd = (e) => {
      if (!this._joyDragging[group]) return;
      this._joyDragging[group] = false;
      this.joy[group] = { x: 0, y: 0 };
      this._drawJoystick(group);
    };

    canvas.addEventListener('pointerdown', onStart);
    canvas.addEventListener('pointermove', onMove);
    canvas.addEventListener('pointerup', onEnd);
    canvas.addEventListener('pointercancel', onEnd);
    canvas.addEventListener('pointerleave', onEnd);

    // Initial draw
    requestAnimationFrame(() => this._drawJoystick(group));
  },

  _drawJoystick(group) {
    const canvas = $(group === 'drive' ? 'ctrl-drive-canvas' : 'ctrl-cam-canvas');
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const rw = rect.width, rh = rect.height;
    if (rw < 10 || rh < 10) return;
    if (canvas.width !== rw || canvas.height !== rh) {
      canvas.width = rw;
      canvas.height = rh;
    }
    const ctx = canvas.getContext('2d');
    const w = canvas.width, h = canvas.height;
    const cx = w / 2, cy = h / 2;
    const halfSize = Math.min(w, h) / 2 * 0.9;   // proportional margin for edge labels

    ctx.clearRect(0, 0, w, h);

    // Outer square
    ctx.beginPath();
    ctx.rect(cx - halfSize, cy - halfSize, halfSize * 2, halfSize * 2);
    ctx.strokeStyle = 'rgba(128,128,128,0.25)';
    ctx.lineWidth = 2;
    ctx.stroke();

    // Cross guides
    ctx.beginPath();
    ctx.moveTo(cx - halfSize, cy); ctx.lineTo(cx + halfSize, cy);
    ctx.moveTo(cx, cy - halfSize); ctx.lineTo(cx, cy + halfSize);
    ctx.strokeStyle = 'rgba(128,128,128,0.1)';
    ctx.lineWidth = 1;
    ctx.stroke();

    // Thumb (proportional — the page scales as a fixed 4:3 stage)
    const { x, y } = this.joy[group];
    const tx = cx + x * halfSize;
    const ty = cy - y * halfSize; // Y inverted
    const thumbR = Math.max(8, halfSize * 0.18);

    // Line from center to thumb
    if (x !== 0 || y !== 0) {
      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.lineTo(tx, ty);
      ctx.strokeStyle = 'rgba(126,87,194,0.4)';
      ctx.lineWidth = 3;
      ctx.stroke();
    }

    // Thumb circle
    ctx.beginPath();
    ctx.arc(tx, ty, thumbR, 0, Math.PI * 2);
    const active = x !== 0 || y !== 0;
    ctx.fillStyle = active ? '#7e57c2' : 'rgba(128,128,128,0.3)';
    ctx.fill();
    ctx.strokeStyle = active ? 'rgba(126,87,194,0.6)' : 'rgba(128,128,128,0.15)';
    ctx.lineWidth = 2;
    ctx.stroke();

    // Labels (font/offsets proportional to the joystick size)
    const fs = Math.max(8, halfSize * 0.09);
    ctx.font = fs + 'px sans-serif';
    ctx.fillStyle = 'rgba(128,128,128,0.4)';
    ctx.textAlign = 'center';
    const off = fs * 0.4;
    if (group === 'drive') {
      ctx.fillText('FWD', cx, cy - halfSize - off);
      ctx.fillText('BWD', cx, cy + halfSize + fs);
      ctx.fillText('L', cx - halfSize - fs * 0.8, cy + off);
      ctx.fillText('R', cx + halfSize + fs * 0.8, cy + off);
    } else {
      ctx.fillText('UP', cx, cy - halfSize - off);
      ctx.fillText('DN', cx, cy + halfSize + fs);
      ctx.fillText('L', cx - halfSize - fs * 0.8, cy + off);
      ctx.fillText('R', cx + halfSize + fs * 0.8, cy + off);
    }
  },

  // ─── Keyboard ─────────────────────────────────────────────
  _keys: {},

  _initKeyboard() {
    document.addEventListener('keydown', (e) => {
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT' || e.target.tagName === 'TEXTAREA') return;
      if (!['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(e.key)) return;
      e.preventDefault();
      this._keys[e.key] = true;
    });
    document.addEventListener('keyup', (e) => {
      if (!['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(e.key)) return;
      e.preventDefault();
      delete this._keys[e.key];
    });
  },

  _getKeyboardDrive() {
    const up = this._keys['ArrowUp'];
    const dn = this._keys['ArrowDown'];
    const lt = this._keys['ArrowLeft'];
    const rt = this._keys['ArrowRight'];
    if (!up && !dn && !lt && !rt) return null;
    const speed = up ? this.maxSpeed : dn ? -this.maxSpeed : 0;
    const steer = lt ? this.MAX_STEER : rt ? -this.MAX_STEER : 0;
    return { speed, steer };
  },

  // ─── Command Sending ──────────────────────────────────────
  _lastSent: { speed: null, steer: null, pan: null, tilt: null },

  _sendCommands() {
    // Only send if Control page is active
    const page = $('page-control');
    if (!page || !page.classList.contains('active')) {
      // If we were sending non-zero, send stop
      if (this._lastSent.speed !== 0 || this._lastSent.steer !== 0) {
        this._send({ speed: 0, steering: 0 });
        this._lastSent.speed = 0; this._lastSent.steer = 0;
      }
      return;
    }

    let speed = 0, steer = 0, pan = 0, tilt = 0;

    // Keyboard override, else joystick: y = speed, x = steering
    const kb = this._getKeyboardDrive();
    if (kb) {
      speed = kb.speed;
      steer = kb.steer;
    } else {
      speed = this.joy.drive.y * this.maxSpeed;
      steer = -this.joy.drive.x * this.MAX_STEER;
    }

    // Camera
    pan = -this.joy.cam.x * this.MAX_PAN;
    tilt = this.joy.cam.y * this.MAX_TILT;

    // Round to avoid float noise
    speed = Math.round(speed * 100) / 100;
    steer = Math.round(steer * 1000) / 1000;
    pan = Math.round(pan * 1000) / 1000;
    tilt = Math.round(tilt * 1000) / 1000;

    // Send only on change — batched into ONE request per tick (a joystick
    // drag used to cost up to 4 POSTs/tick through the tunnel proxy)
    const body = {};
    if (speed !== this._lastSent.speed || steer !== this._lastSent.steer) {
      body.speed = speed;
      body.steering = steer;
      this._lastSent.speed = speed;
      this._lastSent.steer = steer;
    }
    if (pan !== this._lastSent.pan || tilt !== this._lastSent.tilt) {
      body.pan = pan;
      body.tilt = tilt;
      this._lastSent.pan = pan;
      this._lastSent.tilt = tilt;
    }
    if (Object.keys(body).length) this._send(body);

    // Update UI
    $('ctrl-speed').textContent = speed.toFixed(2);
    $('ctrl-steer').textContent = Math.round(steer * 180 / Math.PI);
    $('ctrl-pan').textContent = Math.round(pan * 180 / Math.PI);
    $('ctrl-tilt').textContent = Math.round(tilt * 180 / Math.PI);
  },

  // ── Transport ──
  // Drive (speed/steering): WS /speed/stream + /steering/stream — a whole
  //   driving session costs one request through tunnels/proxies, and the
  //   server zeroes the value when the socket drops (dead-man switch).
  //   Falls back to the documented POST endpoints when the WS isn't up
  //   (same behavior, just more requests).
  // Camera (pan/tilt): occasional input — plain POST is enough, no stream.
  _streams: {},  // path -> WebSocket

  _openStream(path) {
    const cur = this._streams[path];
    if (cur && (cur.readyState === WebSocket.CONNECTING || cur.readyState === WebSocket.OPEN)) return cur;
    try {
      const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://')
                               + location.host + path + '/stream');
      ws.onopen = () => {
        // Right after (re)connecting: the server dead-man may have zeroed the
        // value during the drop — invalidate the on-change filter so the next
        // tick force-resends the current value. (Otherwise holding the joystick
        // steady across a reconnect means the value "never changed" and would
        // never be sent again.)
        if (path === '/speed') this._lastSent.speed = NaN;
        if (path === '/steering') this._lastSent.steer = NaN;
      };
      ws.onclose = () => {
        if (this._streams[path] === ws) delete this._streams[path];
        // Middleboxes on the tunnel path drop sockets occasionally — if the
        // tab is visible, auto-reconnect so the pipe is ready before the next
        // input (hidden tabs reconnect lazily on the next send).
        if (!document.hidden) setTimeout(() => this._openStream(path), 3000);
      };
      ws.onerror = () => { try { ws.close(); } catch (e) {} };
      this._streams[path] = ws;
      return ws;
    } catch (e) {
      return null;
    }
  },

  _post(path, value) {
    fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ value }),
    }).catch(() => {}); // fire-and-forget
  },

  _streamSend(path, value) {
    const ws = this._openStream(path); // reconnect for the next tick if dropped
    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify({ value })); return; } catch (e) {}
    }
    this._post(path, value); // connecting/unavailable — REST fallback
  },

  _send(fields) {
    if ('speed' in fields) this._streamSend('/speed', fields.speed);
    if ('steering' in fields) this._streamSend('/steering', fields.steering);
    if ('pan' in fields) this._post('/camera/pan', fields.pan);
    if ('tilt' in fields) this._post('/camera/tilt', fields.tilt);
  },
};

// Init when DOM ready (called from init block in index.html)
