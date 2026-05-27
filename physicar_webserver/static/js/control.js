/* ===== Control Module ===== */
const CTRL = {
  // Config
  MAX_STEER: 0.349,          // rad (~20°) max steering
  MAX_PAN: 0.5236,           // rad (30°) - matches real kit max_pan
  MAX_TILT: 0.5236,          // rad (30°) - matches real kit max_tilt
  SEND_HZ: 10,               // control send rate

  // State
  maxSpeed: 1.0,             // m/s, adjustable
  mode: { drive: 'continuous', cam: 'continuous' },
  joy: { drive: { x: 0, y: 0 }, cam: { x: 0, y: 0 } },
  dpad: { drive: null, cam: null },
  _sendTimer: null,
  _joyDragging: { drive: false, cam: false },
  _joyPointer: { drive: null, cam: null },

  init() {
    // Restore saved modes
    const savedDrive = localStorage.getItem('ctrl-mode-drive');
    const savedCam = localStorage.getItem('ctrl-mode-cam');
    if (savedDrive === 'continuous' || savedDrive === 'discrete') this.mode.drive = savedDrive;
    if (savedCam === 'continuous' || savedCam === 'discrete') this.mode.cam = savedCam;

    this._initJoystick('drive');
    this._initJoystick('cam');
    this._initDpad('drive');
    this._initDpad('cam');
    this._initKeyboard();
    // Apply restored modes to UI
    this.setMode('drive', this.mode.drive, true);
    this.setMode('cam', this.mode.cam, true);
    this._sendTimer = setInterval(() => this._sendCommands(), 1000 / this.SEND_HZ);
  },

  setMode(group, mode, _skipSave) {
    this.mode[group] = mode;
    if (!_skipSave) localStorage.setItem(`ctrl-mode-${group}`, mode);
    // Update UI buttons
    document.querySelectorAll(`.ctrl-mode[data-group="${group}"]`).forEach(b => {
      b.classList.toggle('active', b.dataset.mode === mode);
    });
    // Toggle panels
    const joyEl = $(group === 'drive' ? 'ctrl-drive-joy' : 'ctrl-cam-joy');
    const dpadEl = $(group === 'drive' ? 'ctrl-drive-dpad' : 'ctrl-cam-dpad');
    if (mode === 'continuous') {
      joyEl.classList.remove('hidden');
      dpadEl.classList.add('hidden');
    } else {
      joyEl.classList.add('hidden');
      dpadEl.classList.remove('hidden');
    }
    // Reset values
    this.joy[group] = { x: 0, y: 0 };
    this.dpad[group] = null;
    this._drawJoystick(group);
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
    const halfSize = Math.min(w, h) / 2 - 12;

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

    // Thumb
    const { x, y } = this.joy[group];
    const tx = cx + x * halfSize;
    const ty = cy - y * halfSize; // Y inverted
    const thumbR = Math.max(24, halfSize * 0.2);

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

    // Labels
    ctx.font = '11px sans-serif';
    ctx.fillStyle = 'rgba(128,128,128,0.4)';
    ctx.textAlign = 'center';
    if (group === 'drive') {
      ctx.fillText('FWD', cx, cy - halfSize - 4);
      ctx.fillText('BWD', cx, cy + halfSize + 12);
      ctx.fillText('L', cx - halfSize - 8, cy + 4);
      ctx.fillText('R', cx + halfSize + 8, cy + 4);
    } else {
      ctx.fillText('UP', cx, cy - halfSize - 4);
      ctx.fillText('DN', cx, cy + halfSize + 12);
      ctx.fillText('L', cx - halfSize - 8, cy + 4);
      ctx.fillText('R', cx + halfSize + 8, cy + 4);
    }
  },

  // ─── D-Pad ────────────────────────────────────────────────
  _initDpad(group) {
    const svg = (group === 'drive' ? 'ctrl-drive-dpad' : 'ctrl-cam-dpad');
    const el = $(svg);
    if (!el) return;

    const btns = el.querySelectorAll('.dpad-btn');
    btns.forEach(btn => {
      const dir = btn.dataset.dir;

      const activate = (e) => {
        e.preventDefault();
        this.dpad[group] = dir;
        btn.classList.add('active');
      };
      const deactivate = (e) => {
        e.preventDefault();
        this.dpad[group] = null;
        btn.classList.remove('active');
      };

      btn.addEventListener('pointerdown', activate);
      btn.addEventListener('pointerup', deactivate);
      btn.addEventListener('pointerleave', deactivate);
      btn.addEventListener('pointercancel', deactivate);
    });
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
        this._send('speed', 0); this._send('steering', 0);
        this._lastSent.speed = 0; this._lastSent.steer = 0;
      }
      return;
    }

    let speed = 0, steer = 0, pan = 0, tilt = 0;

    // Keyboard override
    const kb = this._getKeyboardDrive();
    if (kb) {
      speed = kb.speed;
      steer = kb.steer;
    } else if (this.mode.drive === 'continuous') {
      // Joystick: y = speed, x = steering
      speed = this.joy.drive.y * this.maxSpeed;
      steer = -this.joy.drive.x * this.MAX_STEER;
    } else {
      // D-pad
      const d = this.dpad.drive;
      if (d) {
        const map = {
          'fwd':   { s:  1, st: 0 },
          'fwd-r': { s:  1, st: -1 },
          'right': { s:  0, st: -1 },
          'bwd-r': { s: -1, st: -1 },
          'bwd':   { s: -1, st: 0 },
          'bwd-l': { s: -1, st: 1 },
          'left':  { s:  0, st: 1 },
          'fwd-l': { s:  1, st: 1 },
        };
        const m = map[d];
        if (m) { speed = m.s * this.maxSpeed; steer = m.st * this.MAX_STEER; }
      }
    }

    // Camera
    if (this.mode.cam === 'continuous') {
      pan = -this.joy.cam.x * this.MAX_PAN;
      tilt = this.joy.cam.y * this.MAX_TILT;
    } else {
      const d = this.dpad.cam;
      if (d) {
        const map = {
          'up':    { p: 0,  t: 1 },
          'up-r':  { p: -1, t: 1 },
          'right': { p: -1, t: 0 },
          'dn-r':  { p: -1, t: -1 },
          'dn':    { p: 0,  t: -1 },
          'dn-l':  { p: 1,  t: -1 },
          'left':  { p: 1,  t: 0 },
          'up-l':  { p: 1,  t: 1 },
        };
        const m = map[d];
        if (m) { pan = m.p * this.MAX_PAN; tilt = m.t * this.MAX_TILT; }
      }
    }

    // Round to avoid float noise
    speed = Math.round(speed * 100) / 100;
    steer = Math.round(steer * 1000) / 1000;
    pan = Math.round(pan * 1000) / 1000;
    tilt = Math.round(tilt * 1000) / 1000;

    // Send only on change
    if (speed !== this._lastSent.speed || steer !== this._lastSent.steer) {
      this._send('speed', speed);
      this._send('steering', steer);
      this._lastSent.speed = speed;
      this._lastSent.steer = steer;
    }
    if (pan !== this._lastSent.pan || tilt !== this._lastSent.tilt) {
      this._send('camera/pan', pan);
      this._send('camera/tilt', tilt);
      this._lastSent.pan = pan;
      this._lastSent.tilt = tilt;
    }

    // Update UI
    $('ctrl-speed').textContent = speed.toFixed(2);
    $('ctrl-steer').textContent = Math.round(steer * 180 / Math.PI);
    $('ctrl-pan').textContent = Math.round(pan * 180 / Math.PI);
    $('ctrl-tilt').textContent = Math.round(tilt * 180 / Math.PI);
  },

  _send(endpoint, value) {
    fetch('/control/' + endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ value }),
    }).catch(() => {}); // fire-and-forget
  },
};

// Init when DOM ready (called from init block in index.html)
