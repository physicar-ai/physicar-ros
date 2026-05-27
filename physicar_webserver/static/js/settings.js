/* Settings modal for / (main page).
 * Mirrors kiosk settings panel: password display + calibration controls.
 */
const SETTINGS = (() => {
  const el = (id) => document.getElementById(id);

  let devicePassword = '';
  let pwVisible = false;
  let deviceSerial = '';
  let _qrInst = null;
  let cal = {
    steering_center: 0,
    pan_center: 0,
    tilt_center: 0,
    reverse_direction: false,
  };

  // Joystick sub-panel (created lazily on first switch to its tab so the
  // SSE /joy/raw stream is only opened while the user is actually looking
  // at the panel).
  let joyPanel = null;
  function ensureJoyPanel() {
    if (joyPanel) return joyPanel;
    if (typeof createJoyPanel !== 'function') return null;
    joyPanel = createJoyPanel({
      liveId: 'settings-joy-live',
      mapId:  'settings-joy-map',
      tuneId: 'settings-joy-tune',
      statusId: 'settings-joy-status',
      enabledId: 'settings-joy-enabled',
      connectedId: 'settings-joy-conn',
    });
    return joyPanel;
  }

  function switchTab(tab) {
    document.querySelectorAll('#settings-overlay .wifi-net-tab').forEach(b => {
      b.classList.toggle('active', b.dataset.settab === tab);
    });
    document.querySelectorAll('#settings-overlay .wifi-net-panel').forEach(p => {
      p.classList.remove('active');
    });
    const panel = el('settings-panel-' + tab);
    if (panel) panel.classList.add('active');

    // Open/close joy live stream only while its tab is visible.
    const jp = ensureJoyPanel();
    if (jp) {
      if (tab === 'joystick') jp.open();
      else jp.close();
    }
  }

  async function loadPassword() {
    try {
      const res = await fetch('/network/password');
      const d = await res.json();
      devicePassword = d.password || '';
    } catch {
      devicePassword = '';
    }
    el('settings-pw-value').textContent = pwVisible ? (devicePassword || '—') : '••••••••';
  }

  function togglePw() {
    pwVisible = !pwVisible;
    el('settings-pw-value').textContent = pwVisible ? (devicePassword || '—') : '••••••••';
  }

  async function changePassword() {
    const np = el('settings-pw-new').value;
    const status = el('settings-pw-status');
    const btn = el('settings-pw-submit');
    function setMsg(msg, kind) {
      status.textContent = msg || '';
      status.className = 'settings-form-status' + (msg ? ' ' + (kind || '') : '');
    }
    const v = validatePw(np);
    if (!v.ok) { setMsg(v.message, 'err'); return; }
    if (!confirm('This will reboot the device. Continue?')) return;
    // Close the Settings modal so the connection-lost overlay (and the
    // automatic reload on auth failure) is visible while the device reboots.
    close();
    btn.disabled = true;
    setMsg('Saving and rebooting…', 'info');
    try {
      const res = await fetch('/auth/password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ new_password: np }),
      });
      const d = await res.json().catch(() => ({}));
      if (!res.ok) {
        setMsg(d.detail || 'Failed to change password.', 'err');
        btn.disabled = false;
        return;
      }
      setMsg('Password changed. Device is rebooting…', 'ok');
    } catch (e) {
      setMsg('Network error: ' + e.message, 'err');
      btn.disabled = false;
    }
  }

  async function resetPassword() {
    const status = el('settings-pw-reset-status');
    const btn = el('settings-pw-reset');
    function setMsg(msg, kind) {
      if (!status) return;
      status.textContent = msg || '';
      status.className = 'settings-form-status' + (msg ? ' ' + (kind || '') : '');
    }
    if (!confirm('Reset the device password to its default and reboot now?\nAll active sessions will be logged out.')) return;
    close();
    if (btn) btn.disabled = true;
    setMsg('Resetting and rebooting…', 'info');
    try {
      const res = await fetch('/auth/password', { method: 'DELETE' });
      const d = await res.json().catch(() => ({}));
      if (!res.ok) {
        setMsg(d.detail || 'Failed to reset password.', 'err');
        if (btn) btn.disabled = false;
        return;
      }
      setMsg('Password reset. Device is rebooting…', 'ok');
    } catch (e) {
      setMsg('Network error: ' + e.message, 'err');
      if (btn) btn.disabled = false;
    }
  }

  async function loadCalibration() {
    try {
      const res = await fetch('/kiosk/calibration');
      if (!res.ok) return;
      const d = await res.json();
      if (d.success) {
        cal = d;
        renderCal();
      }
    } catch {}
  }

  let calEvtSrc = null;
  function startCalibrationStream() {
    if (calEvtSrc) return;
    try {
      calEvtSrc = new EventSource('/kiosk/calibration?stream=true');
      calEvtSrc.onmessage = (e) => {
        try {
          const d = JSON.parse(e.data);
          if (d && d.success) { cal = d; renderCal(); }
        } catch {}
      };
      calEvtSrc.onerror = () => {
        // EventSource auto-reconnects; nothing to do.
      };
    } catch {}
  }
  function stopCalibrationStream() {
    if (calEvtSrc) { try { calEvtSrc.close(); } catch {} calEvtSrc = null; }
  }

  function renderCal() {
    el('settings-cal-steering').textContent = `${cal.steering_center}°`;
    el('settings-cal-pan').textContent = `${cal.pan_center}°`;
    el('settings-cal-tilt').textContent = `${cal.tilt_center}°`;
    el('settings-cal-reverse').checked = !!cal.reverse_direction;
  }

  async function adjust(channel, delta) {
    const key = channel + '_center';
    const newValue = Math.max(-15, Math.min(15, (cal[key] || 0) + delta));
    try {
      const res = await fetch('/kiosk/calibration/center', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ channel, center_value: newValue }),
      });
      if (res.ok) {
        cal[key] = newValue;
        renderCal();
      } else {
        const d = await res.json().catch(() => ({}));
        if (typeof showToast === 'function') showToast(d.detail || 'Failed', true);
      }
    } catch (e) {
      if (typeof showToast === 'function') showToast('Error: ' + e.message, true);
    }
  }

  async function toggleReverse() {
    const v = el('settings-cal-reverse').checked;
    try {
      const res = await fetch('/kiosk/calibration/reverse', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reverse_direction: v }),
      });
      if (res.ok) {
        cal.reverse_direction = v;
      } else {
        el('settings-cal-reverse').checked = !v;
        if (typeof showToast === 'function') showToast('Failed', true);
      }
    } catch (e) {
      el('settings-cal-reverse').checked = !v;
      if (typeof showToast === 'function') showToast('Error: ' + e.message, true);
    }
  }

  function open() {
    el('settings-overlay').classList.add('active');
    switchTab('password');
    loadPassword();
    loadCalibration();
    loadSerial();
    startCalibrationStream();
    // Disable change-password form in SIM mode
    const isSim = window.PHYSICAR_MODE === 'sim';
    const btn = el('settings-pw-submit');
    if (btn) {
      btn.disabled = isSim;  // refreshPwUi() will re-enable once user types a valid pw
      btn.textContent = isSim ? 'Not supported in simulation mode' : 'Change';
    }
    const resetBtn = el('settings-pw-reset');
    if (resetBtn) {
      resetBtn.disabled = isSim;
      resetBtn.textContent = isSim ? 'Not supported in simulation mode' : 'Reset to Default';
    }
    ['settings-pw-new'].forEach(id => {
      const input = el(id);
      if (input) {
        input.disabled = isSim;
        input.value = '';
        input.type = 'password';
      }
    });
    newPwVisible = false;
    refreshPwUi();
  }

  function close() {
    el('settings-overlay').classList.remove('active');
    pwVisible = false;
    stopCalibrationStream();
    if (joyPanel) joyPanel.close();
  }

  document.addEventListener('keydown', (e) => {
    const ov = el('settings-overlay');
    if (!ov || !ov.classList.contains('active')) return;
    if (e.key === 'Escape') close();
  });

  function sanitizePw(input) {
    // Strip any character outside ASCII printable, no-space (0x21..0x7E).
    // Mirrors the backend rule in routers/auth.py:change_password.
    const before = input.value;
    const cleaned = before.replace(/[^\x21-\x7E]/g, '');
    if (cleaned !== before) {
      const removed = before.length - cleaned.length;
      const pos = Math.max(0, (input.selectionStart || 0) - removed);
      input.value = cleaned;
      try { input.setSelectionRange(pos, pos); } catch {}
    }
    refreshPwUi();
  }

  // Returns {ok, message}.  Mirrors backend validation exactly.
  function validatePw(pw) {
    if (!pw) return { ok: false, message: 'Enter a new password.' };
    if (pw.length < 8 || pw.length > 63) {
      return { ok: false, message: 'Password must be 8–63 characters.' };
    }
    if (/\s/.test(pw)) {
      return { ok: false, message: 'Password cannot contain spaces.' };
    }
    if (!/^[\x21-\x7E]+$/.test(pw)) {
      return { ok: false, message: 'Password contains invalid characters.' };
    }
    return { ok: true, message: '' };
  }

  // Sync inline status + Change-button disabled state with the current
  // input value.  Skips the disabled state in SIM mode (where the form
  // is permanently disabled by open()).
  function refreshPwUi() {
    const input = el('settings-pw-new');
    const btn = el('settings-pw-submit');
    const status = el('settings-pw-status');
    if (!input) return;
    const isSim = window.PHYSICAR_MODE === 'sim';
    const v = validatePw(input.value);
    if (status) {
      // Empty input: clear the message; non-empty + invalid: show why.
      if (!input.value) {
        status.textContent = '';
        status.className = 'settings-form-status';
      } else if (!v.ok) {
        status.textContent = v.message;
        status.className = 'settings-form-status err';
      } else {
        status.textContent = '';
        status.className = 'settings-form-status';
      }
    }
    if (btn && !isSim) btn.disabled = !v.ok;
  }

  let newPwVisible = false;
  function toggleNewPw() {
    newPwVisible = !newPwVisible;
    const input = el('settings-pw-new');
    if (input) input.type = newPwVisible ? 'text' : 'password';
  }

  async function loadSerial() {
    const valEl = el('settings-sn-value');
    const qrEl = el('settings-sn-qr');
    if (!valEl || !qrEl) return;
    if (!deviceSerial) {
      try {
        const r = await fetch('/info');
        const d = await r.json();
        deviceSerial = d.serial || '';
      } catch { deviceSerial = ''; }
    }
    valEl.textContent = deviceSerial || '(unavailable)';
    if (deviceSerial && typeof QRCode !== 'undefined') {
      qrEl.innerHTML = '';
      try {
        _qrInst = new QRCode(qrEl, {
          text: deviceSerial,
          width: 180,
          height: 180,
          colorDark: '#000000',
          colorLight: '#ffffff',
          correctLevel: QRCode.CorrectLevel.M,
        });
      } catch (e) { qrEl.textContent = 'QR error'; }
    } else if (!deviceSerial) {
      qrEl.innerHTML = '';
    }
  }

  return {
    open, close, switchTab, togglePw, adjust, toggleReverse,
    changePassword, resetPassword, sanitizePw, toggleNewPw,
    joyToggleEnabled: () => { const jp = ensureJoyPanel(); if (jp) jp.toggleEnabled(); },
    joyReset:        () => { const jp = ensureJoyPanel(); if (jp) jp.reset(); },
  };
})();
