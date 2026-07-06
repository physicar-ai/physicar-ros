/* ===== DeepRacer Module ===== */
const DR = {
  models: [], actionSpace: [], loadedModels: new Set(), activeModel: null, isRunning: false,
  inferenceActive: false, canvas: null, ctx: null, speedPercent: 100,

  async init() {
    this.canvas = $('dr-action-canvas');
    if (this.canvas) { this.ctx = this.canvas.getContext('2d'); this.drawAction(0, 0, -1, []); }
    await this.fetchModels();
    await this.fetchStatus();
    this._startStatusStream();
  },

  _startStatusStream() {
    if (this._statusES) { try { this._statusES.close(); } catch {} }
    const es = new EventSource('/deepracer/status?stream=true');
    this._statusES = es;
    es.onmessage = (ev) => {
      try {
        const d = JSON.parse(ev.data);
        if (d.error) return;
        this._applyStatus(d);
      } catch {}
    };
    es.onerror = () => {
      // EventSource auto-reconnects; nothing to do.
    };
  },

  async fetchModels() {
    try {
      const res = await api('/deepracer/models');
      const data = await res.json();
      this.models = data.models || [];
      this.renderModels();
    } catch (e) { console.error('fetchModels:', e); }
  },

  async fetchStatus() {
    try {
      const res = await api('/deepracer/status');
      const d = await res.json();
      this._applyStatus(d);
    } catch (e) { console.error('fetchStatus:', e); }
  },

  _applyStatus(d) {
    this.activeModel = d.model_loaded ? d.model_name : null;
    this.isRunning = d.inference_running;
    this.loadedModels = new Set(JSON.parse(d.loaded_models_json || '[]'));
    if (d.model_loaded && d.action_space_json) {
      try { this.actionSpace = JSON.parse(d.action_space_json); } catch {}
    }
    if (d.speed_percent !== undefined) {
      this.speedPercent = d.speed_percent;
      $('dr-speed-pct').textContent = d.speed_percent + '%';
    }
    if (d.action_selection_mode) {
      // Server uses 'stochastic'; <select> option value is 'softmax'
      const v = d.action_selection_mode === 'stochastic' ? 'softmax' : d.action_selection_mode;
      const sel = $('dr-mode');
      if (sel && sel.value !== v) sel.value = v;
    }
    this.updateStatus();
    this.renderModels();
    this.renderProbs();
    this.drawAction(0, 0, -1, []);
    if (this.isRunning && !this.inferenceActive) this.startStream();
  },

  renderModels() {
    const el = $('dr-model-list');
    // Update top-bar active model display
    const nameEl = $('dr-active-name'), stateEl = $('dr-active-state'), wrap = $('dr-active-model');
    if (nameEl && stateEl && wrap) {
      if (this.activeModel) {
        nameEl.textContent = this.activeModel;
        if (this.isRunning) {
          stateEl.textContent = 'running';
          stateEl.className = 'dr-active-state running';
        } else {
          stateEl.textContent = '';
          stateEl.className = 'dr-active-state';
        }
        wrap.classList.add('has-active');
      } else {
        nameEl.textContent = 'No model loaded';
        stateEl.textContent = '';
        stateEl.className = 'dr-active-state';
        wrap.classList.remove('has-active');
      }
    }
    if (!this.models.length) { el.innerHTML = '<div class="model-empty">No models found</div>'; return; }
    el.innerHTML = this.models.map(m => {
      const loaded = this.loadedModels.has(m.name);
      const active = this.activeModel === m.name;
      const cls = active ? 'active' : loaded ? 'loaded' : '';
      const hasLidar = m.sensors && m.sensors.includes('LIDAR');
      const sensorText = hasLidar ? 'Camera + LiDAR' : 'Camera';
      const actionBtn = loaded
        ? `<button class="model-del" title="Unload" onclick="event.stopPropagation();DR.unloadModel('${m.name}')">&times;</button>`
        : `<button class="model-del" title="Delete" onclick="event.stopPropagation();DR.deleteModel('${m.name}')">&#128465;</button>`;
      return `<div class="model-item ${cls}" onclick="DR.loadModel('${m.name}')">
        <div style="flex:1;min-width:0;"><div class="model-name">${m.name}</div><div class="model-sub">${sensorText}</div></div>${actionBtn}
      </div>`;
    }).join('');
  },

  async unloadModel(name) {
    try {
      const res = await api('/deepracer/unload_model', { method: 'POST', body: { model_name: name } });
      const d = await res.json();
      if (!d.success) throw new Error(d.message);
      this.loadedModels.delete(name);
      if (this.activeModel === name) {
        this.activeModel = null; this.isRunning = false;
        this.actionSpace = []; this.stopStream();
        this.updateStatus();
      }
      this.renderModels(); this.renderProbs();
    } catch (e) { showToast(e.message, true); }
  },

  async loadModel(name) {
    this.setStatus('loading', 'Loading...');
    try {
      const res = await api('/deepracer/load_model', { method: 'POST', body: { model_name: name } });
      const d = await res.json();
      if (!d.success) throw new Error(d.message);
      this.activeModel = name;
      this.loadedModels.add(name);
      try { this.actionSpace = JSON.parse(d.action_space_json || '[]'); } catch {}
      this.renderModels(); this.renderProbs();
      this.drawAction(0, 0, -1, []);
      this.updateStatus();
      this.setStatus('stopped', 'Loaded');
    } catch (e) { this.setStatus('stopped', 'Load failed'); showToast(e.message, true); }
  },

  async deleteModel(name) {
    if (!await confirmModal('Delete "' + name + '"?')) return;
    try {
      await api('/deepracer/models/' + encodeURIComponent(name), { method: 'DELETE' });
      this.loadedModels.delete(name);
      if (this.activeModel === name) { this.activeModel = null; this.actionSpace = []; }
      await this.fetchModels();
    } catch (e) { showToast(e.message, true); }
  },

  // Import state
  _importId: null,
  _setupUnloadHandler() {
    if (this._unloadHandlerSet) return;
    this._unloadHandlerSet = true;
    window.addEventListener('beforeunload', () => {
      if (this._importId) {
        const fd = new FormData();
        fd.append('import_id', this._importId);
        navigator.sendBeacon('/deepracer/models/import/cancel', fd);
      }
    });
  },

  _setImportBtn(status, text) {
    const btn = $('dr-import-btn');
    const input = $('dr-import-input');
    if (!btn) return;
    if (status === 'idle') {
      btn.className = 'import-btn';
      btn.innerHTML = `➕ Import model<input type="file" accept=".tar.gz,.gz" id="dr-import-input" onchange="DR.importModel(this)" style="display:none">`;
    } else {
      btn.className = 'import-btn importing';
      btn.innerHTML = `<span class="import-spinner"></span> ${text}`;
    }
  },

  async importModel(input) {
    if (!input.files.length) return;
    const file = input.files[0];
    if (!file.name.endsWith('.tar.gz') && !file.name.endsWith('.gz')) {
      showToast('Please select a .tar.gz file'); input.value = ''; return;
    }
    
    this._setupUnloadHandler();
    
    const CHUNK_SIZE = 5 * 1024 * 1024; // 5MB
    const totalChunks = Math.ceil(file.size / CHUNK_SIZE);
    const modelName = file.name.replace(/\.tar\.gz$|\.gz$/, '');
    
    this._setImportBtn('importing', 'Sending...');
    
    try {
      // 1. Init session
      const initRes = await api('/deepracer/models/import/init', {
        method: 'POST',
        body: { filename: file.name, total_chunks: totalChunks }
      });
      const { import_id } = await initRes.json();
      this._importId = import_id;
      
      // 2. Send all chunks in PARALLEL
      const chunkPromises = [];
      for (let i = 0; i < totalChunks; i++) {
        const start = i * CHUNK_SIZE;
        const end = Math.min(start + CHUNK_SIZE, file.size);
        const chunk = file.slice(start, end);
        
        const fd = new FormData();
        fd.append('import_id', import_id);
        fd.append('chunk_index', i);
        fd.append('chunk', chunk);
        
        chunkPromises.push(api('/deepracer/models/import/chunk', { method: 'POST', formData: fd, noTimeout: true }));
      }
      await Promise.all(chunkPromises);
      
      // 3. Complete
      this._setImportBtn('importing', 'Processing...');
      const completeFd = new FormData();
      completeFd.append('import_id', import_id);
      const completeRes = await api('/deepracer/models/import/complete', { method: 'POST', formData: completeFd, noTimeout: true });
      const d = await completeRes.json();
      
      this._importId = null;
      this._setImportBtn('idle');
      showToast(`Model "${d.model_name}" imported`);
      await this.fetchModels();
    } catch (e) {
      if (this._importId) {
        try {
          const cancelFd = new FormData();
          cancelFd.append('import_id', this._importId);
          await fetch('/deepracer/models/import/cancel', { method: 'POST', body: cancelFd });
        } catch {}
        this._importId = null;
      }
      this._setImportBtn('idle');
      showToast(e.message, true);
    }
    input.value = '';
  },

  async start() {
    if (!this.activeModel) { showToast('Load a model first'); return; }
    try {
      const res = await api('/deepracer/control', { method: 'POST', body: { start: true } });
      const d = await res.json();
      if (!d.success) throw new Error(d.message);
      this.isRunning = true; this.updateStatus(); this.startStream();
    } catch (e) { showToast(e.message, true); }
  },

  async stop() {
    try {
      await api('/deepracer/control', { method: 'POST', body: { start: false } });
      this.isRunning = false; this.updateStatus(); this.stopStream();
      this.drawAction(0, 0, -1, []); $('dr-speed').textContent = '0.00'; $('dr-steering').textContent = '0'; $('dr-rate').textContent = '-';
    } catch (e) { showToast(e.message, true); }
  },

  async setMode(mode) {
    const val = mode === 'softmax' ? 'stochastic' : mode;
    try { await api('/deepracer/set_config', { method: 'POST', body: { key: 'action_selection', string_value: val } }); } catch {}
  },

  async adjustSpeed(delta) {
    const v = this.speedPercent + delta;
    if (v < 50 || v > 150) return;
    try {
      await api('/deepracer/set_config', { method: 'POST', body: { key: 'speed_percent', float_value: v } });
      this.speedPercent = v;
      $('dr-speed-pct').textContent = v + '%';
    } catch {}
  },

  updateStatus() {
    const startBtn = $('dr-btn-start'), stopBtn = $('dr-btn-stop');
    if (this.isRunning) {
      startBtn.disabled = true; stopBtn.disabled = false;
    } else if (this.activeModel) {
      startBtn.disabled = false; stopBtn.disabled = true;
    } else {
      startBtn.disabled = true; stopBtn.disabled = true;
    }
  },

  setStatus(state, text) {
    // Status badge removed - no-op
  },

  /* Inference SSE stream */
  _streamAbort: null,
  startStream() {
    this.stopStream(); this.inferenceActive = true;
    this._streamAbort = new AbortController();
    const signal = this._streamAbort.signal;
    (async () => {
      try {
        const res = await fetch('/deepracer/inference?stream=true', { signal });
        const reader = res.body.getReader();
        const dec = new TextDecoder(); let buf = '';
        while (this.inferenceActive) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          const lines = buf.split('\n'); buf = lines.pop() || '';
          for (const line of lines) {
            if (line.startsWith('data: ')) {
              try { this.onInference(JSON.parse(line.slice(6))); } catch {}
            }
          }
        }
      } catch { if (this.inferenceActive) setTimeout(() => this.startStream(), 2000); }
    })();
  },

  stopStream() {
    this.inferenceActive = false;
    if (this._streamAbort) { this._streamAbort.abort(); this._streamAbort = null; }
  },

  onInference(d) {
    const spd = d.speed || 0, steer = d.steering_angle || 0, idx = d.action_index, probs = d.probabilities || [];
    $('dr-speed').textContent = spd.toFixed(2);
    $('dr-steering').textContent = Math.round(steer);
    if (d.inference_rate) $('dr-rate').textContent = d.inference_rate.toFixed(1);
    this.drawAction(spd, steer, idx, probs);
    if (probs.length) this.updateProbs(probs, idx);
  },

  /* Canvas drawing */
  drawAction(speed, steerDeg, selIdx, probs) {
    const c = this.canvas; if (!c) return;
    const rect = c.getBoundingClientRect();
    const w = Math.round(rect.width), h = Math.round(rect.height);
    if (w < 10 || h < 10) return;
    if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
    const ctx = this.ctx, cx = w/2, cy = h - 12, maxR = h - 24;
    if (maxR < 1) return;
    ctx.clearRect(0,0,w,h);

    // Guides
    ctx.strokeStyle = 'rgba(128,128,128,0.2)'; ctx.lineWidth = 1;
    ctx.setLineDash([3,3]);
    ctx.beginPath(); ctx.arc(cx, cy, maxR, Math.PI, 0); ctx.stroke();
    ctx.beginPath(); ctx.arc(cx, cy, maxR/2, Math.PI, 0); ctx.stroke();
    ctx.setLineDash([]);
    ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(cx, cy - maxR); ctx.strokeStyle = 'rgba(128,128,128,0.1)'; ctx.stroke();

    const p = probs.length ? probs : (this.actionSpace.length ? new Array(this.actionSpace.length).fill(1/this.actionSpace.length) : []);
    if (this.actionSpace.length) {
      const items = this.actionSpace.map((a,i) => ({ a, i, p: p[i]||0 }));
      items.sort((a,b) => a.p - b.p);
      items.forEach(({ a, i, p: prob }) => {
        this._drawArrow(ctx, cx, cy, a.speed, a.steering_angle, maxR, prob, i, i === selIdx);
      });
    }
    if (speed !== 0 || steerDeg !== 0) this._drawCurrent(ctx, cx, cy, speed, steerDeg, maxR);
  },

  _drawArrow(ctx, cx, cy, speed, steerDeg, maxR, prob, label, selected) {
    const len = Math.max(Math.min(Math.abs(speed), 3) / 3 * maxR, 12);
    const a = -Math.PI/2 - (steerDeg * 2 * Math.PI / 180);
    const ex = cx + Math.cos(a)*len, ey = cy + Math.sin(a)*len;
    const gray = Math.floor(200*(1-prob));
    const blue = Math.floor(prob*255);
    const color = selected ? 'rgb(59,130,246)' : `rgb(${gray},${gray},${Math.min(gray+blue,255)})`;
    const lw = selected ? 6 : 3 + prob*4;
    ctx.beginPath(); ctx.moveTo(cx,cy); ctx.lineTo(ex,ey); ctx.strokeStyle = color; ctx.lineWidth = lw; ctx.lineCap = 'round'; ctx.stroke();
    const hl = Math.max(lw*2.5, 9);
    ctx.beginPath(); ctx.moveTo(ex,ey); ctx.lineTo(ex - hl*Math.cos(a-0.45), ey - hl*Math.sin(a-0.45)); ctx.moveTo(ex,ey); ctx.lineTo(ex - hl*Math.cos(a+0.45), ey - hl*Math.sin(a+0.45)); ctx.stroke();
    if (label !== undefined && prob > 0.04) {
      // Font/offset proportional to the chart radius — the page scales as a fixed 4:3 stage.
      const fs = Math.max(9, maxR * 0.1);
      ctx.font = 'bold ' + fs + 'px sans-serif'; ctx.fillStyle = color; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillText(label, ex + Math.cos(a) * fs, ey + Math.sin(a) * fs);
    }
  },

  _drawCurrent(ctx, cx, cy, speed, steerDeg, maxR) {
    const len = Math.max(Math.min(Math.abs(speed), 3) / 3 * maxR, 12);
    const a = -Math.PI/2 - (steerDeg * 2 * Math.PI / 180);
    const ex = cx + Math.cos(a)*len, ey = cy + Math.sin(a)*len;
    ctx.beginPath(); ctx.moveTo(cx,cy); ctx.lineTo(ex,ey); ctx.strokeStyle = '#ef4444'; ctx.lineWidth = 6; ctx.lineCap = 'round'; ctx.stroke();
    ctx.beginPath(); ctx.moveTo(ex,ey); ctx.lineTo(ex - 12*Math.cos(a-0.45), ey - 12*Math.sin(a-0.45)); ctx.moveTo(ex,ey); ctx.lineTo(ex - 12*Math.cos(a+0.45), ey - 12*Math.sin(a+0.45)); ctx.stroke();
  },

  renderProbs() {
    const el = $('dr-prob-chart');
    if (!this.actionSpace.length) { el.innerHTML = '<div class="model-empty">Load a model to see actions</div>'; return; }
    el.innerHTML = this.actionSpace.map((a, i) =>
      `<div class="dr-prob-row">` +
        `<span class="dr-prob-idx">${i}</span>` +
        `<span class="dr-prob-info">${a.speed.toFixed(1)}m/s ${a.steering_angle > 0 ? '+' : ''}${a.steering_angle.toFixed(0)}&deg;</span>` +
        `<div class="dr-prob-bar-wrap"><div class="dr-prob-bar" id="dr-pb-${i}" style="width:0%"></div></div>` +
        `<span class="dr-prob-pct" id="dr-pp-${i}">0%</span>` +
      `</div>`
    ).join('');
  },

  updateProbs(probs, selIdx) {
    probs.forEach((p,i) => {
      const bar = $('dr-pb-' + i);
      const pct = $('dr-pp-' + i);
      if (bar) { bar.style.width = (p*100) + '%'; bar.className = 'dr-prob-bar' + (i === selIdx ? ' selected' : ''); }
      if (pct) { pct.textContent = (p*100).toFixed(0) + '%'; }
    });
  },
};

/* Keyboard shortcut: +/- to adjust speed */
document.addEventListener('keydown', (e) => {
  // Ignore if typing in input/select/textarea
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT' || e.target.tagName === 'TEXTAREA') return;
  if (e.key === '+' || e.key === '=') { e.preventDefault(); DR.adjustSpeed(5); }
  else if (e.key === '-' || e.key === '_') { e.preventDefault(); DR.adjustSpeed(-5); }
});
