/* ===== Platform Auth (physicar.ai) ===== */
const PlatformAuth = {
  API_URL: 'https://api.physicar.ai',
  TOKEN_KEY: 'physicar_session',
  user: null,

  getToken() { return localStorage.getItem(this.TOKEN_KEY); },
  setToken(t) { localStorage.setItem(this.TOKEN_KEY, t); },
  clearToken() { localStorage.removeItem(this.TOKEN_KEY); localStorage.removeItem(this.TOKEN_KEY + '_user'); },
  _saveUser() { if (this.user) localStorage.setItem(this.TOKEN_KEY + '_user', JSON.stringify(this.user)); },
  _loadUser() { try { return JSON.parse(localStorage.getItem(this.TOKEN_KEY + '_user')); } catch { return null; } },

  async init() {
    const token = this.getToken();
    if (!token) { this._updateUI(); return; }
    this.user = this._loadUser();
    this._updateUI();
    try {
      const res = await fetch(this.API_URL + '/auth/me', { headers: { 'Authorization': 'Bearer ' + token } });
      if (res.status === 401) { this.clearToken(); this.user = null; this._updateUI(); return; }
      if (!res.ok) throw new Error();
      const d = await res.json();
      this.user = d.user;
      await this._mergeProfile(token);
      this._saveUser();
    } catch {}
    this._updateUI();
  },

  async signin(email, password) {
    const res = await fetch(this.API_URL + '/auth/signin', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password })
    });
    if (!res.ok) { const e = await res.json().catch(() => ({})); throw new Error(e.error || e.message || 'Sign in failed'); }
    const d = await res.json();
    this.setToken(d.token);
    this.user = d.user;
    await this._mergeProfile(d.token);
    this._saveUser();
    this._updateUI();
  },

  async logout() {
    const token = this.getToken();
    if (token) fetch(this.API_URL + '/auth/logout', { method: 'POST', headers: { 'Authorization': 'Bearer ' + token } }).catch(() => {});
    this.clearToken(); this.user = null; this._updateUI();
    if (typeof AGENT !== 'undefined') AGENT.onAuthChange();
  },

  isLoggedIn() { return !!this.user; },

  // Re-validate token against /auth/me. Returns true if still valid.
  // Clears token+user and updates UI on 401.
  async verify() {
    const token = this.getToken();
    if (!token) { if (this.user) { this.user = null; this._updateUI(); } return false; }
    try {
      const res = await fetch(this.API_URL + '/auth/me', { headers: { 'Authorization': 'Bearer ' + token } });
      if (res.status === 401) {
        this.clearToken(); this.user = null; this._updateUI();
        return false;
      }
      if (!res.ok) return !!this.user; // network/server hiccup — keep current state
      const d = await res.json();
      this.user = d.user;
      await this._mergeProfile(token);
      this._saveUser();
      this._updateUI();
      return true;
    } catch {
      return !!this.user; // offline/transient: keep current state
    }
  },

  showSigninModal(section) {
    PlatformAuth._openSection = section || null;
    document.querySelector('.physicar-modal-overlay')?.remove();
    const overlay = document.createElement('div');
    overlay.className = 'physicar-modal-overlay';
    // Use mousedown+up tracking (via data-close-on-backdrop) so a text-drag
    // that ends outside the modal does NOT close it. Plain onclick was buggy
    // because clicking a label/text and dragging would fire on the overlay.
    overlay.setAttribute('data-close-on-backdrop', 'PlatformAuth._closeSigninModal');

    // Offline variant: show "no internet" message with WiFi Settings action.
    const isOffline = (typeof AGENT !== 'undefined' && AGENT._lastOnline === false);
    if (isOffline) {
      overlay.innerHTML = `<div class="signin-modal" style="text-align:center">
        <div style="margin:8px auto 12px;color:var(--accent)"><svg viewBox="0 0 24 24" width="40" height="40" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M2 12h20"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/><line x1="3" y1="3" x2="21" y2="21" stroke-width="2.5"/></svg></div>
        <h3>No internet connection</h3>
        <div style="color:var(--muted);margin:8px 0 18px;font-size:0.9rem">Sign in requires an internet connection.<br>Connect to WiFi to continue.</div>
        <div class="signin-actions"><button class="btn-cancel" onclick="PlatformAuth._closeSigninModal()">Close</button><button class="btn-signin" id="signin-wifi-btn">WiFi Settings</button></div></div>`;
      document.body.appendChild(overlay);
      overlay.querySelector('#signin-wifi-btn').onclick = () => {
        overlay.remove();
        if (typeof WIFI !== 'undefined') WIFI.open('wifi-list');
      };
      return;
    }

    overlay.innerHTML = `<div class="signin-modal">
      <div class="auth-close" onclick="PlatformAuth._closeSigninModal()">&times;</div>
      <div class="auth-branding"><img src="/static/img/physicar-ai-logo.png" alt="Physicar AI" class="auth-logo"><p class="auth-branding-desc" id="auth-branding-desc">Sign in to your Physicar AI account</p></div>

      <!-- Login section -->
      <div id="auth-login-section">
        <form id="login-form">
          <div class="auth-form-group" data-field="email"><label for="signin-email">Email</label><input type="email" id="signin-email" required maxlength="254" autocomplete="email" /></div>
          <div class="auth-form-group" data-field="password"><label for="signin-password">Password</label><input type="password" id="signin-password" required maxlength="128" autocomplete="current-password" /></div>
          <div class="auth-form-message" id="login-msg"></div>
          <div class="auth-resend-notice" id="resend-notice" style="display:none"><p>Please verify your email to sign in.</p><a href="javascript:void(0)" id="resend-link">Resend verification email</a></div>
          <button type="submit" class="auth-submit-btn">Sign in</button>
        </form>
        <div class="auth-links"><a href="javascript:void(0)" class="auth-link-forgot">Forgot password?</a><a href="javascript:void(0)" class="auth-link-signup">Don't have an account? Sign up</a></div>
      </div>

      <!-- Signup section -->
      <div id="auth-signup-section" style="display:none">
        <form id="register-form">
          <div class="auth-form-group" data-field="email"><label for="signup-email">Email</label><input type="email" id="signup-email" required maxlength="254" autocomplete="email" /></div>
          <div class="auth-form-group" data-field="password"><label for="signup-password">Password</label><input type="password" id="signup-password" required minlength="8" maxlength="128" autocomplete="new-password" /><span class="auth-hint">At least 8 characters</span></div>
          <div class="auth-form-group" data-field="password_confirm"><label for="signup-password2">Confirm Password</label><input type="password" id="signup-password2" required maxlength="128" autocomplete="new-password" /></div>
          <div class="auth-form-group" data-field="name"><label for="signup-name">Full Name</label><input type="text" id="signup-name" required maxlength="100" autocomplete="name" /></div>
          <div class="auth-form-group" data-field="birth_date"><label for="signup-dob">Date of Birth</label><input type="date" id="signup-dob" required /></div>
          <div class="auth-consent-section" data-field="consent">
            <label class="auth-consent-all"><input type="checkbox" id="consent-all" /><span>I agree to all of the following.</span></label>
            <div class="auth-consent-items">
              <label class="auth-consent-item"><input type="checkbox" class="consent-item-check" /><span><a href="https://physicar.ai/legal/consent/collection/" target="_blank">Collection and Use of Personal Information (Required)</a></span></label>
              <label class="auth-consent-item"><input type="checkbox" class="consent-item-check" /><span><a href="https://physicar.ai/legal/consent/third-party/" target="_blank">Third-Party Sharing of Personal Information (Required)</a></span></label>
              <label class="auth-consent-item"><input type="checkbox" class="consent-item-check" /><span><a href="https://physicar.ai/legal/consent/cross-border/" target="_blank">Cross-Border Transfer of Personal Information (Required)</a></span></label>
            </div>
          </div>
          <div id="turnstile-container" class="auth-turnstile"></div>
          <div class="auth-form-message" id="register-msg"></div>
          <button type="submit" class="auth-submit-btn" disabled>Sign up</button>
          <p class="auth-legal-notice">By clicking "Sign up", you agree to our <a href="https://physicar.ai/legal/terms/" target="_blank">Terms of Use</a> and acknowledge that you have read our <a href="https://physicar.ai/legal/privacy/" target="_blank">Privacy Policy</a>.</p>
        </form>
        <div class="auth-links"><a href="javascript:void(0)" class="auth-link-signin">Already have an account? Sign in</a></div>
      </div>

      <!-- Forgot password section -->
      <div id="auth-forgot-section" style="display:none">
        <p class="auth-page-desc">Enter your email and we'll send you a password reset link.</p>
        <form id="forgot-form">
          <div class="auth-form-group" data-field="email"><label for="forgot-email">Email</label><input type="email" id="forgot-email" required maxlength="254" autocomplete="email" /></div>
          <div class="auth-form-message" id="forgot-msg"></div>
          <button type="submit" class="auth-submit-btn">Send reset link</button>
        </form>
        <div class="auth-links"><a href="javascript:void(0)" class="auth-link-signin">Back to sign in</a></div>
      </div>
    </div>`;
    document.body.appendChild(overlay);

    // --- Helper functions (mirrors deepracer auth.js) ---
    const esc = (s) => { if (!s) return ''; const d = document.createElement('div'); d.textContent = s; return d.innerHTML; };
    const API = PlatformAuth.API_URL;

    function getErrorMessage(data) {
      if (!data || !data.code) return data?.error || 'Network error occurred.';
      if (data.code === 'EMAIL_EXISTS_SIMILAR' && data.registered_email)
        return 'A similar email (' + data.registered_email + ') is already registered.';
      let m = data.error || 'An error occurred.';
      if (data.ray) m += ' (ref: ' + data.ray + ')';
      return m;
    }

    function mapErrorToField(code) {
      return { INVALID_EMAIL: 'email', EMAIL_NOT_ALLOWED: 'email', EMAIL_EXISTS: 'email', EMAIL_EXISTS_SIMILAR: 'email',
        PASSWORD_TOO_SHORT: 'password', PASSWORD_COMPROMISED: 'password', INVALID_NAME: 'name',
        INVALID_BIRTH_DATE: 'birth_date', TURNSTILE_REQUIRED: 'turnstile', TURNSTILE_FAILED: 'turnstile' }[code] || null;
    }

    function showFormMsg(formId, msg, isError) {
      const form = overlay.querySelector('#' + formId);
      if (!form) return;
      let el = form.querySelector('.auth-form-message');
      if (el) { el.textContent = msg; el.className = 'auth-form-message ' + (isError !== false ? 'error' : 'success'); el.style.display = 'block'; }
    }
    function clearFormMsg(formId) {
      const el = overlay.querySelector('#' + formId + ' .auth-form-message');
      if (el) el.style.display = 'none';
    }
    function setLoading(formId, loading) {
      const btn = overlay.querySelector('#' + formId + ' button[type="submit"]');
      if (!btn) return;
      if (!btn.dataset.origText) btn.dataset.origText = btn.textContent;
      btn.disabled = loading; btn.textContent = loading ? 'Loading...' : btn.dataset.origText;
    }
    function showFieldError(formId, fieldName, msg) {
      const group = overlay.querySelector('#' + formId + ' [data-field="' + fieldName + '"]');
      if (!group) return;
      group.classList.add('auth-field-error');
      let existing = group.querySelector('.auth-field-error-msg');
      if (existing) existing.remove();
      const el = document.createElement('div'); el.className = 'auth-field-error-msg'; el.textContent = msg;
      group.appendChild(el);
    }
    function clearFieldErrors(formId) {
      overlay.querySelectorAll('#' + formId + ' .auth-field-error').forEach(el => el.classList.remove('auth-field-error'));
      overlay.querySelectorAll('#' + formId + ' .auth-field-error-msg').forEach(el => el.remove());
    }

    // --- Section switching ---
    function showSection(name) {
      ['auth-login-section', 'auth-signup-section', 'auth-forgot-section'].forEach(id => {
        const el = overlay.querySelector('#' + id);
        if (el) el.style.display = id === 'auth-' + name + '-section' ? '' : 'none';
      });
      const desc = overlay.querySelector('#auth-branding-desc');
      if (desc) desc.textContent = name === 'signup' ? 'Create your Physicar AI account'
        : name === 'forgot' ? 'Reset your password' : 'Sign in to your Physicar AI account';
      if (name === 'signup') setupSignupPage();
    }
    overlay.querySelectorAll('.auth-link-signup').forEach(a => { a.onclick = () => showSection('signup'); });
    overlay.querySelectorAll('.auth-link-signin').forEach(a => { a.onclick = () => showSection('login'); });
    overlay.querySelectorAll('.auth-link-forgot').forEach(a => { a.onclick = () => showSection('forgot'); });

    // --- Auto-clear errors on input ---
    overlay.querySelectorAll('input').forEach(input => {
      input.addEventListener('input', () => {
        const group = input.closest('[data-field]');
        if (group && group.classList.contains('auth-field-error')) {
          group.classList.remove('auth-field-error');
          const msg = group.querySelector('.auth-field-error-msg');
          if (msg) msg.remove();
        }
        const form = input.closest('form');
        if (form) { const fmsg = form.querySelector('.auth-form-message'); if (fmsg) fmsg.style.display = 'none'; }
      });
      // Strip whitespace from email
      if (input.type === 'email') input.addEventListener('input', () => {
        const t = input.value.replace(/\s/g, ''); if (t !== input.value) input.value = t;
      });
    });

    // --- Login handler ---
    let pendingResendEmail = null;
    overlay.querySelector('#login-form').onsubmit = async (e) => {
      e.preventDefault();
      const email = overlay.querySelector('#signin-email').value.trim();
      const pass = overlay.querySelector('#signin-password').value;
      clearFormMsg('login-form'); clearFieldErrors('login-form');
      setLoading('login-form', true);
      try {
        const res = await fetch(API + '/auth/signin', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email, password: pass }) });
        const data = await res.json();
        if (!res.ok) {
          if (data.code === 'EMAIL_NOT_VERIFIED') { pendingResendEmail = email; overlay.querySelector('#resend-notice').style.display = 'block'; }
          showFormMsg('login-form', getErrorMessage(data)); return;
        }
        overlay.querySelector('#resend-notice').style.display = 'none';
        localStorage.setItem('physicar_session', data.token);
        overlay.remove();
        await PlatformAuth.verify();
        if (typeof AGENT !== 'undefined') AGENT.onAuthChange();
      } catch (err) { showFormMsg('login-form', 'Network error occurred.'); }
      finally { setLoading('login-form', false); }
    };

    // Resend verification
    overlay.querySelector('#resend-link').onclick = async () => {
      if (!pendingResendEmail) return;
      try {
        await fetch(API + '/auth/resend-verification', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email: pendingResendEmail }) });
        showFormMsg('login-form', 'Verification email sent. Please check your inbox.', false);
        overlay.querySelector('#resend-notice').style.display = 'none';
      } catch (err) { showFormMsg('login-form', 'Failed to send email. Please try again.'); }
    };

    // --- Signup page setup ---
    let turnstileWidgetId = null;
    function setupSignupPage() {
      // DOB default (18 years ago)
      const dobEl = overlay.querySelector('#signup-dob');
      if (dobEl && !dobEl.value) {
        dobEl.max = new Date().toISOString().split('T')[0];
        const d = new Date(); d.setFullYear(d.getFullYear() - 18);
        dobEl.value = d.toISOString().split('T')[0];
      }
      // Consent-all logic
      const allCheck = overlay.querySelector('#consent-all');
      const items = overlay.querySelectorAll('.consent-item-check');
      const submitBtn = overlay.querySelector('#register-form button[type="submit"]');
      const inputs = overlay.querySelectorAll('#register-form input[required]');
      function updateSubmitState() {
        const allFilled = Array.from(inputs).every(i => i.value.trim() !== '');
        const allChecked = items.length === 0 || Array.from(items).every(c => c.checked);
        submitBtn.disabled = !(allFilled && allChecked);
      }
      if (allCheck && !allCheck._bound) {
        allCheck._bound = true;
        allCheck.addEventListener('change', () => { items.forEach(i => { i.checked = allCheck.checked; }); updateSubmitState(); });
        items.forEach(i => { i.addEventListener('change', () => { allCheck.checked = Array.from(items).every(c => c.checked); updateSubmitState(); }); });
      }
      inputs.forEach(i => { i.addEventListener('input', updateSubmitState); i.addEventListener('change', updateSubmitState); });
      items.forEach(i => { i.addEventListener('change', updateSubmitState); });
      updateSubmitState();

      // Turnstile
      const tc = overlay.querySelector('#turnstile-container');
      if (tc && window.PHYSICAR_TURNSTILE_SITE_KEY && window.turnstile) {
        if (turnstileWidgetId !== null) try { window.turnstile.remove(turnstileWidgetId); } catch(e) {}
        turnstileWidgetId = window.turnstile.render(tc, {
          sitekey: window.PHYSICAR_TURNSTILE_SITE_KEY,
          callback: (token) => { tc.dataset.token = token; },
          'expired-callback': () => { tc.dataset.token = ''; },
          theme: 'dark'
        });
      } else if (tc && window.PHYSICAR_TURNSTILE_SITE_KEY && !window.turnstile) {
        // Load Turnstile script dynamically
        if (!document.querySelector('script[src*="challenges.cloudflare.com"]')) {
          const s = document.createElement('script');
          s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
          s.async = true; s.defer = true;
          s.onload = () => { setTimeout(() => setupSignupPage(), 100); };
          document.head.appendChild(s);
        }
      }
    }

    // --- Register handler ---
    overlay.querySelector('#register-form').onsubmit = async (e) => {
      e.preventDefault();
      const email = overlay.querySelector('#signup-email').value.trim();
      const pw = overlay.querySelector('#signup-password').value;
      const pw2 = overlay.querySelector('#signup-password2').value;
      const name = overlay.querySelector('#signup-name').value.trim();
      const dob = overlay.querySelector('#signup-dob').value;
      clearFormMsg('register-form'); clearFieldErrors('register-form');
      if (!name) { showFieldError('register-form', 'name', 'Name is required.'); return; }
      if (!dob) { showFieldError('register-form', 'birth_date', 'Date of birth is required.'); return; }
      if (pw.length < 8) { showFieldError('register-form', 'password', 'Password must be at least 8 characters.'); return; }
      if (pw !== pw2) { showFieldError('register-form', 'password_confirm', 'Passwords do not match.'); return; }
      const consentItems = overlay.querySelectorAll('.consent-item-check');
      if (consentItems.length > 0 && !Array.from(consentItems).every(c => c.checked)) { showFieldError('register-form', 'consent', 'You must agree to all required items.'); return; }
      setLoading('register-form', true);
      try {
        const tc = overlay.querySelector('#turnstile-container');
        const turnstileToken = tc ? tc.dataset.token : undefined;
        const res = await fetch(API + '/auth/signup', { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ email, password: pw, name, birth_date: dob, turnstile_token: turnstileToken }) });
        const data = await res.json();
        if (!res.ok) {
          const field = data.code ? mapErrorToField(data.code) : null;
          if (field) showFieldError('register-form', field, getErrorMessage(data));
          else showFormMsg('register-form', getErrorMessage(data));
          if (window.turnstile && turnstileWidgetId !== null) window.turnstile.reset(turnstileWidgetId);
          return;
        }
        showFormMsg('register-form', 'Verification email sent! Please check your inbox.', false);
        setTimeout(() => showSection('login'), 3000);
      } catch (err) { showFormMsg('register-form', 'Network error occurred.'); }
      finally { setLoading('register-form', false); }
    };

    // --- Forgot password handler ---
    overlay.querySelector('#forgot-form').onsubmit = async (e) => {
      e.preventDefault();
      clearFormMsg('forgot-form');
      setLoading('forgot-form', true);
      try {
        const email = overlay.querySelector('#forgot-email').value.trim();
        await fetch(API + '/auth/forgot-password', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email }) });
        showFormMsg('forgot-form', 'If an account exists with that email, a reset link has been sent.', false);
      } catch (err) { showFormMsg('forgot-form', 'Network error occurred.'); }
      finally { setLoading('forgot-form', false); }
    };

    // --- Show requested section ---
    if (PlatformAuth._openSection) { showSection(PlatformAuth._openSection); PlatformAuth._openSection = null; }

    overlay.querySelector('#signin-email')?.focus();
  },

  _closeSigninModal() {
    document.querySelector('.physicar-modal-overlay')?.remove();
  },

  // Fetch /api/profile and merge classroom + credit fields onto this.user.
  // Site mirrors this dual-mode display:
  //  - personal user: credit_balance ($X.XX)
  //  - classroom student: classroom_sponsor_used / classroom_sponsor_limit
  async _mergeProfile(token) {
    try {
      const cr = await fetch(this.API_URL + '/api/profile', { headers: { 'Authorization': 'Bearer ' + token } });
      if (!cr.ok) return;
      const p = await cr.json();
      const prof = p.profile || {};
      this.user.credit_balance = Number(prof.credit_balance || 0);
      this.user.classroom_id = prof.classroom_id || null;
      this.user.classroom_name = p.classroom_name || null;
      this.user.classroom_sponsor_used = Number(prof.classroom_sponsor_used || 0);
      this.user.classroom_sponsor_limit = Number(prof.classroom_sponsor_limit || 0);
      this.user.classroom_sponsor_active = !!prof.classroom_sponsor_active;
      this.user.display_name = prof.display_name || this.user.display_name;
    } catch {}
  },

  _initials(name, email) {
    if (name) {
      const parts = name.trim().split(/\s+/);
      if (parts.length >= 2) return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
      return name.substring(0, 2).toUpperCase();
    }
    if (email) return email.substring(0, 2).toUpperCase();
    return '??';
  },

  _updateUI() {
    const c = $('nav-user-area'); if (!c) return;
    let html;
    if (this.user) {
      const u = this.user;
      const initials = this._initials(u.display_name, u.email);
      const displayLabel = u.display_name || (u.email || '').split('@')[0] || 'User';
      const fmt = (n) => '$' + (Number(n) || 0).toFixed(2);
      let creditsBlock;
      if (u.classroom_id) {
        creditsBlock = `<div class="menu-credits-label">Classroom credits</div>
          <div class="menu-credits-value">
            <span class="menu-credits-sub">used</span> <strong>${fmt(u.classroom_sponsor_used)}</strong>
            <span class="menu-credits-sep">/</span>
            <span class="menu-credits-sub">limit</span> <strong>${fmt(u.classroom_sponsor_limit)}</strong>
          </div>
          ${u.classroom_name ? `<div class="menu-credits-classroom">${u.classroom_name}</div>` : ''}`;
      } else {
        creditsBlock = `<div class="menu-credits-label">Credits</div>
          <div class="menu-credits-value"><strong>${fmt(u.credit_balance)}</strong></div>`;
      }
      html = `<div class="nav-user" onclick="PlatformAuth._toggleMenu()"><span class="user-avatar">${initials}</span></div>
        <div class="nav-user-menu" id="nav-user-menu">
          <div class="menu-header">
            <div class="menu-name">${displayLabel}</div>
            <div class="menu-email">${u.email || ''}</div>
          </div>
          <div class="menu-divider"></div>
          <div class="menu-credits">${creditsBlock}</div>
          <div class="menu-divider"></div>
          <button class="menu-item" onclick="PlatformAuth.showProfile()">Profile</button>
          <button class="menu-item" onclick="PlatformAuth.logout()">Sign Out</button>
        </div>`;
    } else {
      html = `<button class="nav-user" onclick="PlatformAuth.showSigninModal()" style="border-color:var(--border)"><span style="font-size:0.9rem; padding: 0.2rem 0.4rem;">Sign In</span></button>`;
    }
    if (c._lastHTML === html) return; // idempotent: keep dropdown open
    // Preserve dropdown open state across re-render
    const wasOpen = $('nav-user-menu')?.classList.contains('open');
    c.innerHTML = html;
    c._lastHTML = html;
    if (wasOpen) $('nav-user-menu')?.classList.add('open');
  },

  _toggleMenu() {
    const m = $('nav-user-menu');
    if (!m) return;
    m.classList.toggle('open');
    if (m.classList.contains('open')) {
      // Refresh profile/credits on demand (instead of waiting for 10-min poll).
      this.verify();
      const close = (e) => { if (!m.contains(e.target) && !m.previousElementSibling?.contains(e.target)) { m.classList.remove('open'); document.removeEventListener('click', close); } };
      setTimeout(() => document.addEventListener('click', close), 0);
    }
  },

  async showProfile() {
    // Close dropdown + existing profile modal
    $('nav-user-menu')?.classList.remove('open');
    document.querySelector('.physicar-modal-overlay')?.remove();
    if (!this.user) return this.showSigninModal();
    const overlay = document.createElement('div');
    overlay.className = 'physicar-modal-overlay';
    overlay.innerHTML = `<div class="profile-modal"><div class="profile-close" onclick="PlatformAuth._closeProfile()">&times;</div>
      <h3 style="margin:0 0 1rem; text-align:center; color:var(--fg)">My Profile</h3>
      <div class="pp" id="pp-container"><div class="pp-loading">Loading...</div></div></div>`;
    overlay.onclick = (e) => { if (e.target === overlay) PlatformAuth._closeProfile(); };
    document.body.appendChild(overlay);

    const esc = (s) => { if (!s) return ''; const d = document.createElement('div'); d.textContent = s; return d.innerHTML; };
    const tk = localStorage.getItem('physicar_session');
    if (!tk) return;
    try {
      const r = await fetch(this.API_URL + '/api/profile', { headers: { 'Authorization': 'Bearer ' + tk } });
      if (!r.ok) throw 0;
      const d = await r.json();
      const p = d.profile || {};
      const C = document.getElementById('pp-container');
      if (!C) return;

      const crHtml = d.classroom_name ? esc(d.classroom_name) : '<span class="pp-muted">None</span>';
      const creditsLabel = p.classroom_id ? 'Credits (Classroom)' : 'Credits';
      const creditsHtml = p.classroom_id
        ? '<span style="font-size:.7rem;color:var(--fg3)">Used</span> $' + (p.classroom_sponsor_used || 0).toFixed(2)
          + ' <span class="pp-muted">/</span> '
          + '<span style="font-size:.7rem;color:var(--fg3)">Limit</span> $' + (p.classroom_sponsor_limit || 0).toFixed(2)
        : '$' + (p.credit_balance || 0).toFixed(2);

      const ghValHtml = p.github_login
        ? '<a href="https://github.com/' + esc(p.github_login) + '" target="_blank" rel="noopener" class="pp-gh-user">'
          + '<img src="https://avatars.githubusercontent.com/u/' + p.github_id + '?s=40" class="pp-gh-avatar" alt="">' + esc(p.github_login) + '</a>'
        : '<span class="pp-muted">Not connected</span>';
      const ghBtnHtml = p.github_login
        ? '<button class="pp-btn-danger" onclick="PlatformAuth._ghDisconnect()">Disconnect</button>'
        : '<button class="pp-btn-link" onclick="PlatformAuth._ghConnect()">Connect →</button>';

      C.innerHTML =
        '<div class="pp-field"><div class="pp-left"><div class="pp-label">Joined Classroom</div><div class="pp-val">' + crHtml + '</div></div></div>'
        + '<div class="pp-field"><div class="pp-left"><div class="pp-label">Email</div><div class="pp-val">' + esc(p.email) + '</div></div></div>'
        + '<div class="pp-field"><div class="pp-left"><div class="pp-label">Name</div><div class="pp-val">' + esc(p.name) + '</div></div></div>'
        + '<div class="pp-field"><div class="pp-left"><div class="pp-label">Date of Birth</div><div class="pp-val">' + esc(p.birth_date) + '</div></div></div>'
        + '<div class="pp-field"><div class="pp-left"><div class="pp-label">Display Name</div><div class="pp-val">' + esc(p.display_name || '') + '</div></div></div>'
        + '<div class="pp-field"><div class="pp-left"><div class="pp-label">' + creditsLabel + '</div><div class="pp-val">' + creditsHtml + '</div></div></div>'
        + '<div class="pp-field"><div class="pp-left"><div class="pp-label">Pay for GitHub Codespaces with Credits</div><div class="pp-val" id="gh-val">' + ghValHtml + '</div></div><div class="pp-right" id="gh-btn">' + ghBtnHtml + '</div></div>'
        + '<div id="pp-gh-msg"></div>';
    } catch (e) {
      const C = document.getElementById('pp-container');
      if (C) C.innerHTML = '<div class="pp-loading" style="color:#f44">Failed to load profile</div>';
    }
  },

  _closeProfile() {
    document.querySelector('.physicar-modal-overlay')?.remove();
  },

  _ghConnect() {
    const tk = localStorage.getItem('physicar_session');
    if (!tk) return;
    const btn = document.querySelector('#gh-btn .pp-btn-link');
    const ghUrl = PlatformAuth.API_URL + '/auth/github?token=' + encodeURIComponent(tk);
    const ret = window.location.origin + window.location.pathname;

    // Detect VS Code Simple Browser: cross-origin top frame → can't access top.location
    const needsCodeCli = (() => {
      try { window.top.location.href; return false; } // same-origin or top-level → regular browser
      catch(e) { return true; } // cross-origin → VS Code Simple Browser
    })();

    if (needsCodeCli) {
      // VS Code Simple Browser — open via server-side $BROWSER (code CLI)
      if (btn) { btn.textContent = 'Opening...'; btn.disabled = true; btn.style.opacity = '0.6'; }
      fetch('/api/codespaces/open-external?url=' + encodeURIComponent(ghUrl))
        .then(r => { if (!r.ok) throw 0; return r.json(); })
        .then(d => {
          if (d.ok && btn) {
            btn.textContent = 'Done? Click to refresh';
            btn.style.border = '1px solid #4caf50'; btn.style.color = '#4caf50';
            btn.style.opacity = '1'; btn.disabled = false;
            btn.onclick = () => PlatformAuth.showProfile();
          }
        })
        .catch(() => { if (btn) { btn.textContent = 'Connect →'; btn.disabled = false; btn.style.opacity = '1'; } });
    } else {
      // Regular browser (top-level or same-origin iframe like /studio) — new tab or navigate
      const popup = window.open(ghUrl + '&return_to=' + encodeURIComponent(ret), '_blank');
      if (popup) {
        if (btn) {
          btn.textContent = 'Done? Click to refresh';
          btn.style.border = '1px solid #4caf50'; btn.style.color = '#4caf50';
          btn.onclick = () => PlatformAuth.showProfile();
        }
      } else {
        location.href = ghUrl + '&return_to=' + encodeURIComponent(ret);
      }
    }
  },

  _ghDisconnect() {
    document.getElementById('gh-btn').innerHTML =
      '<span style="font-size:.75rem;color:var(--fg3)">Sure?</span> '
      + '<button class="pp-sm pp-btn-danger" onclick="PlatformAuth._ghDoDisconnect()">Disconnect</button> '
      + '<button class="pp-sm pp-cancel" onclick="PlatformAuth._ghCancelDisconnect()">Cancel</button>';
  },

  _ghCancelDisconnect() {
    document.getElementById('gh-btn').innerHTML =
      '<button class="pp-btn-danger" onclick="PlatformAuth._ghDisconnect()">Disconnect</button>';
  },

  async _ghDoDisconnect() {
    const tk = localStorage.getItem('physicar_session');
    try {
      const r = await fetch(PlatformAuth.API_URL + '/api/github/account', { method: 'DELETE', headers: { 'Authorization': 'Bearer ' + tk, 'Content-Type': 'application/json' } });
      if (!r.ok) { const d = await r.json(); PlatformAuth._ghMsg(d.error || 'Network error', false); PlatformAuth._ghCancelDisconnect(); return; }
      PlatformAuth.showProfile(); // reload profile
    } catch (e) { PlatformAuth._ghMsg('Network error', false); PlatformAuth._ghCancelDisconnect(); }
  },

  _ghMsg(text, ok) {
    const e = document.getElementById('pp-gh-msg');
    if (e) e.innerHTML = '<div class="pp-msg ' + (ok ? 'ok' : 'err') + '">' + text + '</div>';
  }
};

/* ===== Agent Chat Module ===== */
const AGENT = {
  chatId: null,
  turn: null,
  isWaiting: false,
  tools: [],
  systemToolNames: new Set(),
  // Opt-out tool selection: every tool is enabled unless its name is in
  // `disabledTools`. System meta-tools (tool_code/tool_set) are disabled by
  // default since they let the agent rewrite its own tools.py.
  DEFAULT_DISABLED_TOOLS: ['tool_code', 'tool_set'],
  disabledTools: new Set(['tool_code', 'tool_set']),
  lastSentPrompt: null,
  // History of completed/cancelled pairs (rebuilt on reload from localStorage).
  // Each entry: { turn, status: 'done'|'cancelled', user_message, assistant_message }
  _pairs: [],
  // Reference to the pair currently being assembled (one of `_pairs[-1]`, kept
  // separate so streaming text can append to its assistant_message in place).
  _currentPair: null,
  _abortCtl: null,
  _sendSeq: 0,
  _persistKey: 'agent_chat_v1',

  // TTS
  ttsEnabled: true,
  ttsTarget: 'browser',  // 'off' | 'browser' | 'kit'
  isSimMode: false,
  audioContext: null,
  gainNode: null,
  audioQueue: [],
  isPlayingAudio: false,
  scheduledSources: [],
  scheduledEndTime: 0,
  isFirstChunk: true,
  initialBufferingDone: false,
  bufferingTimer: null,
  // When a tool-call continuation is in flight, we don't cut the currently
  // playing TTS immediately; instead we flush it only once the NEW response's
  // first audio chunk arrives (so the old voice isn't cut off prematurely).
  _newTtsPending: false,
  // ── STT (browser speech-to-text) ──
  _sttRec: null,
  _sttRecording: false,
  _sttConfirmed: '',
  _sttLastIndex: -1,
  MIN_BUFFER_DURATION_MS: 1500,

  init() {
    this.loadConfig();
    this._initStt();
    this._refreshGate(true);
    this._initSharedConfig();
    this._detectMode();
    this._restoreChat();
    // Re-check internet/login gate periodically (3s for fast feedback)
    setInterval(() => this._refreshGate(true), 3000);
    // Teachers may change the allowed models during class, so refetch the list periodically.
    // If nothing changed, _loadModels skips touching the DOM via signature comparison (prevents flicker).
    setInterval(() => { if (this._gateState === 'ready') this._loadModels(); }, 120000);
    document.addEventListener('click', (e) => {
      const pop = $('tts-popover');
      if (pop && pop.classList.contains('show') && !e.target.closest('.tts-wrap')) {
        pop.classList.remove('show');
      }
    });
  },

  async _detectMode() {
    try {
      const res = await api('/info');
      const info = await res.json();
      this.isSimMode = info.mode === 'sim';
    } catch { this.isSimMode = false; }
    // If sim mode and currently set to kit, force to browser
    if (this.isSimMode && this.ttsTarget === 'kit') {
      this.ttsTarget = 'browser';
      this.ttsEnabled = true;
      this.saveConfig();
    }
    this._updateTTSBtn();
  },

  onAuthChange() { this._refreshGate(); },

  // Gate: check internet connectivity first, then login.
  // States: 'offline' (no internet) | 'signin' (need login) | 'ready'
  async _refreshGate(force = false) {
    const overlay = $('agent-signin-overlay'), chat = $('agent-chat-area');
    if (!overlay || !chat) return;

    // 1) Internet check
    let online = this._lastOnline;
    if (force || online === undefined) {
      try {
        const res = await fetch('/network/internet');
        const d = await res.json();
        online = !!(d.wifi || d.ethernet);
      } catch { online = false; }
      this._lastOnline = online;
    }

    if (!online) {
      this._didFirstVerify = false; // re-verify when we come back online
      this._gateState = 'offline';
      // Globe with a diagonal slash (no internet)
      $('agent-gate-icon').innerHTML = '<svg viewBox="0 0 24 24" width="48" height="48" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M2 12h20"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/><line x1="3" y1="3" x2="21" y2="21" stroke-width="2.5"/></svg>';
      $('agent-gate-msg').innerHTML = 'No internet connection.<br>Connect to a WiFi network to use the AI agent.';
      $('agent-gate-btn').textContent = 'WiFi Settings';
      overlay.style.display = 'flex';
      chat.style.display = 'none';
      return;
    }

    // 2) Token re-verification only on first online entry. Periodic verify removed —
    //    profile is refreshed when user opens the dropdown, and 401 during chat
    //    triggers immediate re-gate.
    if (PlatformAuth.getToken() && !this._didFirstVerify) {
      this._didFirstVerify = true;
      await PlatformAuth.verify();
    }

    // 3) Login check
    if (!PlatformAuth.isLoggedIn()) {
      this._gateState = 'signin';
      $('agent-gate-icon').innerHTML = '<img src="/static/img/logo.png" alt="PhysiCar" style="width:80px;height:auto;opacity:0.5">';
      $('agent-gate-msg').textContent = 'Sign in to physicar.ai to use the AI agent.';
      $('agent-gate-btn').textContent = 'Sign In';
      overlay.style.display = 'flex';
      chat.style.display = 'none';
      return;
    }

    const wasReady = this._gateState === 'ready';
    this._gateState = 'ready';
    overlay.style.display = 'none';
    chat.style.display = 'flex';
    // Reload models on transition into ready so the per-user `allowed` flags
    // (classroom restrictions) are applied with the now-available auth token.
    if (!wasReady) this._loadModels();
  },

  gateAction() {
    if (this._gateState === 'offline') {
      if (typeof WIFI !== 'undefined') WIFI.open('wifi-list');
    } else {
      PlatformAuth.showSigninModal();
    }
  },

  // ── Fingerprint ──
  getPromptId() {
    const canvas = document.createElement('canvas');
    const ctx = canvas.getContext('2d');
    ctx.textBaseline = 'top'; ctx.font = '14px Arial'; ctx.fillText('fingerprint', 2, 2);
    const data = canvas.toDataURL();
    let hash = 0;
    for (let i = 0; i < data.length; i++) { hash = ((hash << 5) - hash) + data.charCodeAt(i); hash |= 0; }
    return 'agent-' + Math.abs(hash).toString(36);
  },

  // ── Config ──
  // Shared agent config (instructions, model, reasoning, tool selection) lives
  // on the server (GET/POST /agent/config) and is broadcast to every browser
  // via SSE. Only client-local preferences (TTS target, volume) are kept in
  // localStorage. Chat history is persisted separately (see _persistChat).
  _sharedTs: 0,
  _applyingRemote: false,

  loadConfig() {
    // Client-local preferences only (TTS + volume); shared config is fetched
    // from the server in _initSharedConfig().
    const saved = localStorage.getItem('agent_config');
    if (saved) {
      try {
        const cfg = JSON.parse(saved);
        if (cfg.ttsTarget != null) { this.ttsTarget = cfg.ttsTarget; this.ttsEnabled = cfg.ttsTarget !== 'off'; }
        else if (cfg.ttsEnabled != null) { this.ttsEnabled = cfg.ttsEnabled; this.ttsTarget = cfg.ttsEnabled ? 'browser' : 'off'; }
        if (cfg.volume != null) { const el = $('agent-volume'); if (el) el.value = cfg.volume; }
      } catch {}
    }
    this._updateTTSBtn();
    this._updateInstructionsPreview();
  },

  saveConfig() {
    // Persist client-local preferences (TTS + volume) to localStorage.
    const local = {
      ttsEnabled: this.ttsEnabled,
      ttsTarget: this.ttsTarget,
      volume: parseInt($('agent-volume')?.value) || 80,
    };
    localStorage.setItem('agent_config', JSON.stringify(local));
    // Push shared config to the server (debounced) unless we are currently
    // applying an inbound broadcast (avoids echo loops).
    if (this._applyingRemote) return;
    clearTimeout(this._sharedSaveTimer);
    this._sharedSaveTimer = setTimeout(() => this._postSharedConfig(), 400);
  },

  // Fetch the shared config once, apply it, then load models/tools and open
  // the SSE stream so later changes from other browsers arrive live.
  async _initSharedConfig() {
    try {
      const res = await api('/agent/config');
      const cfg = await res.json();
      this._applySharedConfig(cfg);
    } catch {}
    this._loadModels();
    this.refreshTools();
    this._subscribeConfig();
  },

  // Apply a shared-config object to the UI without triggering a save-back.
  _applySharedConfig(cfg) {
    if (!cfg) return;
    this._sharedTs = cfg.updated_at || 0;
    this._applyingRemote = true;
    try {
      if (cfg.instructions != null) {
        const el = $('agent-instructions'); if (el) el.value = cfg.instructions;
        this._updateInstructionsPreview();
      }
      if (cfg.model != null) {
        this._savedModel = cfg.model;
        const sel = $('agent-model');
        if (sel && cfg.model && this._modelInfo && this._modelInfo[cfg.model]) {
          sel.value = cfg.model;
          this.onModelChange();
        }
      }
      if (cfg.reasoning_effort != null) { const el = $('agent-reasoning'); if (el) el.value = cfg.reasoning_effort; }
      if (Array.isArray(cfg.disabled_tools)) this.disabledTools = new Set(cfg.disabled_tools);
      this._syncToolChecks();
    } finally {
      this._applyingRemote = false;
    }
  },

  // Reflect the current disabledTools set onto the rendered checkboxes (no
  // re-fetch, no save). Programmatic .checked changes don't fire onchange.
  _syncToolChecks() {
    const list = $('agent-tools-list'); if (!list) return;
    list.querySelectorAll('.tool-item').forEach((item) => {
      const cb = item.querySelector('input[type=checkbox]');
      const nameEl = item.querySelector('.tool-name');
      if (cb && nameEl) cb.checked = !this.disabledTools.has(nameEl.textContent);
    });
  },

  async _postSharedConfig() {
    const body = {
      instructions: $('agent-instructions')?.value || '',
      model: $('agent-model')?.value || '',
      reasoning_effort: $('agent-reasoning')?.value || 'low',
      disabled_tools: Array.from(this.disabledTools),
    };
    try {
      const res = await api('/agent/config', { method: 'POST', body });
      const cfg = await res.json();
      // Remember our own timestamp so the SSE echo of this change is ignored.
      if (cfg && cfg.updated_at) this._sharedTs = cfg.updated_at;
    } catch {}
  },

  _subscribeConfig() {
    if (this._configES) { try { this._configES.close(); } catch {} }
    const es = new EventSource('/agent/config?stream=true');
    this._configES = es;
    es.onmessage = (ev) => {
      try {
        const cfg = JSON.parse(ev.data);
        if ((cfg.updated_at || 0) <= (this._sharedTs || 0)) return;
        this._applySharedConfig(cfg);
      } catch {}
    };
    es.onerror = () => { /* EventSource auto-reconnects */ };
  },

  // ── Models ──
  // Loads the public model catalog from the platform API and populates the
  // <select id="agent-model"> dropdown. Each model is tagged with its
  // `reasoning_effort_supported` flag so the Reasoning row can be shown only
  // when the currently selected model supports it.
  async _loadModels() {
    const sel = $('agent-model'); if (!sel) return;
    try {
      // Pass auth so the server can apply classroom `allowed` flags per user.
      const token = PlatformAuth.getToken();
      const res = await fetch(PlatformAuth.API_URL + '/chat/models', token ? { headers: { 'Authorization': 'Bearer ' + token } } : undefined);
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      const models = (data && data.models) || [];
      // Skip DOM rebuild if the model list / allowed state is unchanged from last time
      // (prevents dropdown / selection flicker on periodic refresh).
      const sig = models.map((m) => m.model + ':' + (m.allowed !== false ? 1 : 0)).join('|');
      if (sig === this._modelsSig) return;
      this._modelsSig = sig;
      this._modelInfo = {};
      sel.innerHTML = '';
      // Allowed models first, disallowed (classroom-restricted) last.
      // `allowed` is true unless a classroom restriction blocks it.
      const sorted = models.slice().sort((a, b) => {
        const aa = a.allowed !== false, ba = b.allowed !== false;
        return aa === ba ? 0 : (aa ? -1 : 1);
      });
      for (const m of sorted) {
        this._modelInfo[m.model] = m;
        const opt = document.createElement('option');
        opt.value = m.model;
        opt.textContent = m.model;
        if (m.allowed === false) {
          // Greyed out and unselectable for classroom-restricted models.
          opt.disabled = true;
        }
        sel.appendChild(opt);
      }
      // Restore saved selection only if still allowed, else default to first allowed.
      const allowedModels = sorted.filter((m) => m.allowed !== false);
      const saved = this._savedModel;
      if (saved && this._modelInfo[saved] && this._modelInfo[saved].allowed !== false) sel.value = saved;
      else if (allowedModels.length) sel.value = allowedModels[0].model;
    } catch (e) {
      this._modelsSig = null; // retry on the next poll if it failed
      sel.innerHTML = '<option value="" disabled selected>(failed to load models)</option>';
      console.error('loadModels:', e);
    }
    this.onModelChange();
  },

  // Toggle the Reasoning row visibility based on the currently selected model.
  onModelChange() {
    const sel = $('agent-model');
    const row = $('agent-reasoning-row');
    const info = this._modelInfo && sel ? this._modelInfo[sel.value] : null;
    const supports = !!(info && info.reasoning_effort_supported);
    if (row) row.style.display = supports ? '' : 'none';
    this.saveConfig();
  },

  // ── Tools ──
  async refreshTools() {
    const list = $('agent-tools-list'); if (!list) return;
    list.innerHTML = '<span class="cfg-label">Loading...</span>';
    try {
      const res = await api('/agent/tool/list?include_system=true');
      const data = await res.json();
      this.tools = data.tools || [];
      this.systemToolNames = new Set(data.system_tool_names || []);
      if (!this.tools.length) { list.innerHTML = '<span class="cfg-label">No tools available</span>'; return; }
      // Opt-out model: every tool is enabled unless its name is in
      // `disabledTools` (system meta-tools are disabled by default). New tools
      // are therefore enabled automatically. Prune entries for tools that no
      // longer exist so the stored set stays clean.
      if (!this.disabledTools) this.disabledTools = new Set();
      const toolNames = new Set(this.tools.map(t => t.name));
      let pruned = false;
      this.disabledTools.forEach(n => { if (!toolNames.has(n)) { this.disabledTools.delete(n); pruned = true; } });
      const sorted = [...this.tools].sort((a, b) => {
        const as = this.systemToolNames.has(a.name), bs = this.systemToolNames.has(b.name);
        if (as !== bs) return as ? 1 : -1;
        return 0;
      });
      list.innerHTML = sorted.map(t => {
        const sys = this.systemToolNames.has(t.name);
        return `<div class="tool-item"><input type="checkbox" ${this.disabledTools.has(t.name)?'':'checked'} onchange="AGENT.toggleTool('${t.name}',this.checked)">
          <span class="tool-name${sys?' system':''}" onclick="AGENT.showToolDetail('${t.name}')">${t.name}</span></div>`;
      }).join('');
      if (pruned) this.saveConfig();
    } catch (e) { list.innerHTML = '<span class="cfg-label">Error: ' + e.message + '</span>'; }
  },

  toggleTool(name, on) { if (on) this.disabledTools.delete(name); else this.disabledTools.add(name); this.saveConfig(); },

  async editToolsFile() {
    let code = '';
    try {
      const res = await api('/agent/tool/file');
      const data = await res.json();
      code = data.code || '';
    } catch (e) { showToast('Failed to load tools.py: ' + e.message, true); return; }
    const modal = document.createElement('div');
    modal.className = 'tool-modal-overlay';
    modal.innerHTML = `
      <div class="tool-modal tool-modal-wide">
        <div class="tool-modal-header">
          <span class="tool-modal-title">tools.py</span>
          <div style="flex:1"></div>
          <button class="tool-modal-close" onclick="AGENT.closeToolModal()">&times;</button>
        </div>
        <div class="tool-modal-body">
          <div id="ace-file-container" class="ace-editor-host" style="height:100%"></div>
        </div>
        <div class="tool-modal-footer">
          <button class="btn-primary tool-file-save-btn" disabled onclick="AGENT.saveToolsFile()">Save</button>
          <div style="flex:1"></div>
        </div>
      </div>`;
    document.body.appendChild(modal);
    this._modalEsc = (ev) => { if (ev.key === 'Escape') this.closeToolModal(); };
    document.addEventListener('keydown', this._modalEsc);
    this._initAceEditor('ace-file-container', code, false, 'python');
    this._aceCodeEditor = this._aceLastEditor;
    if (this._aceCodeEditor) {
      const origCode = code;
      this._aceCodeEditor.session.on('change', () => {
        const dirty = this._aceCodeEditor.getValue() !== origCode;
        const btn = document.querySelector('.tool-file-save-btn');
        if (btn) btn.disabled = !dirty;
      });
    }
  },

  async reloadTools() {
    const list = $('agent-tools-list');
    if (list) list.innerHTML = '<span class="cfg-label">Loading...</span>';
    try {
      const res = await api('/agent/tool/load', { method: 'POST' });
      const data = await res.json();
      if (!data.success) { showToast('Load failed', true); }
      else { showToast(`Loaded ${data.tool_count} tools`); }
      this.refreshTools();
    } catch (e) { showToast(e.message, true); this.refreshTools(); }
  },

  async initTools() {
    if (!await confirmModal('Initialize tools to defaults?')) return;
    const list = $('agent-tools-list');
    if (list) list.innerHTML = '<span class="cfg-label">Initializing...</span>';
    try {
      await api('/agent/tool/init', { method: 'POST' });
      this.disabledTools = new Set(this.DEFAULT_DISABLED_TOOLS);
      this.refreshTools();
    } catch (e) { showToast(e.message, true); if (list) list.innerHTML = ''; }
  },

  // ── Instructions preview/editor ──
  // Refresh the read-only preview block so it mirrors the hidden textarea.
  _updateInstructionsPreview() {
    const preview = $('agent-instructions-preview');
    const ta = $('agent-instructions');
    if (!preview || !ta) return;
    preview.textContent = ta.value || '';
  },

  // Open a modal to edit the system instructions in a comfortable area.
  editInstructions() {
    const ta = $('agent-instructions');
    const current = ta ? (ta.value || '') : '';
    const modal = document.createElement('div');
    modal.className = 'tool-modal-overlay';
    modal.innerHTML = `
      <div class="tool-modal">
        <div class="tool-modal-header">
          <span class="tool-modal-title">Instructions</span>
          <div style="flex:1"></div>
          <button class="tool-modal-close" onclick="AGENT.closeToolModal()">&times;</button>
        </div>
        <div class="tool-modal-body instructions-modal-body">
          <textarea class="instructions-modal-textarea" id="instructions-modal-text"></textarea>
        </div>
        <div class="tool-modal-footer">
          <button class="btn-primary instructions-save-btn" disabled onclick="AGENT.saveInstructions()">Save</button>
          <div style="flex:1"></div>
          <button class="btn-secondary" onclick="AGENT.resetInstructions()">Load Default</button>
        </div>
      </div>`;
    document.body.appendChild(modal);
    const textEl = $('instructions-modal-text');
    if (textEl) {
      textEl.value = current;
      const saveBtn = modal.querySelector('.instructions-save-btn');
      const orig = current;
      // Enable Save only when the text differs from what was loaded.
      this._updateInstrDirty = () => { if (saveBtn) saveBtn.disabled = (textEl.value === orig); };
      textEl.addEventListener('input', this._updateInstrDirty);
      setTimeout(() => textEl.focus(), 50);
    }
    this._modalEsc = (ev) => { if (ev.key === 'Escape') this.closeToolModal(); };
    document.addEventListener('keydown', this._modalEsc);
  },

  saveInstructions() {
    const textEl = $('instructions-modal-text');
    const ta = $('agent-instructions');
    if (textEl && ta) ta.value = textEl.value;
    this._updateInstructionsPreview();
    this.saveConfig();
    this.closeToolModal();
  },

  // Reset the modal's textarea to the built-in default instructions (does not
  // save until the user clicks Save).
  async resetInstructions() {
    const textEl = $('instructions-modal-text');
    if (!textEl) return;
    let def = '';
    try {
      const res = await api('/agent/config/default');
      const d = await res.json();
      def = d.instructions || '';
    } catch (e) { showToast('Failed to load default: ' + e.message, true); return; }
    // Replace via execCommand so the change stays in the textarea's native
    // undo history (Ctrl+Z can revert the Load Default).
    textEl.focus();
    textEl.select();
    if (!document.execCommand('insertText', false, def)) {
      textEl.value = def;  // fallback if execCommand is unsupported
    }
    if (this._updateInstrDirty) this._updateInstrDirty();
  },

  // ── Tool detail / edit / add modal ──
  // Open a modal showing the tool's definition + code in tabs.
  // Definition tab: read-only JSON view.
  // Code tab: editable for non-system tools; disabled for system tools.
  async showToolDetail(name) {
    const isSystem = this.systemToolNames.has(name);
    let definition = {};
    try {
      const res = await api('/agent/tool/get/' + name + '?include_code=false');
      const data = await res.json();
      definition = { name: data.name, description: data.description, properties: data.properties };
    } catch (e) {
      showToast('Failed to load tool: ' + e.message, true);
      return;
    }
    const defHtml = this._buildDefHtml(definition);
    const modal = document.createElement('div');
    modal.className = 'tool-modal-overlay';
    modal.onclick = (e) => { if (e.target === modal) this.closeToolModal(); };
    modal.innerHTML = `
      <div class="tool-modal">
        <div class="tool-modal-header">
          <span class="tool-modal-title">${name}${isSystem ? ' <span class="system-badge">system</span>' : ''}</span>
          <div style="flex:1"></div>
          <button class="tool-modal-close" onclick="AGENT.closeToolModal()">&times;</button>
        </div>
        <div class="tool-modal-body">
          ${defHtml}
        </div>
      </div>`;
    document.body.appendChild(modal);
    this._modalEsc = (ev) => { if (ev.key === 'Escape') this.closeToolModal(); };
    document.addEventListener('keydown', this._modalEsc);
  },

  _escHtml(s) {
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  },

  _buildDefHtml(def) {
    const esc = this._escHtml.bind(this);
    const props = def.properties || [];
    let propsHtml = '';
    if (Array.isArray(props) && props.length) {
      propsHtml = props.map(p => {
        const req = p.required ? '<span class="def-required">required</span>' : '<span class="def-optional">optional</span>';
        return `<div class="def-prop">
          <div class="def-prop-header">
            <span class="def-prop-name">${esc(p.name || '')}</span>
            <span class="def-prop-type">${esc(p.type || 'any')}</span>
            ${req}
          </div>
          ${p.description ? `<div class="def-prop-desc">${esc(p.description)}</div>` : ''}
        </div>`;
      }).join('');
    } else {
      propsHtml = '<div class="def-empty">No parameters</div>';
    }
    return `<div class="def-card">
      <div class="def-section">
        <div class="def-label">Description</div>
        <div class="def-value-card" style="white-space:pre-wrap">${def.description ? esc(def.description) : '<span class="def-empty">No description</span>'}</div>
      </div>
      <div class="def-section">
        <div class="def-label">Parameters</div>
        <div class="def-props">${propsHtml}</div>
      </div>
    </div>`;
  },

  _switchToolTab(tab) {
    document.querySelectorAll('.tool-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
    document.querySelectorAll('.tool-tab-content').forEach(c => c.classList.toggle('active', c.dataset.tab === tab));
    // Show/hide Save based on tab (Delete always visible)
    const saveBtn = document.querySelector('.tool-save-btn');
    if (saveBtn) saveBtn.style.display = (tab === 'code') ? '' : 'none';
    // Resize ace on tab switch
    if (tab === 'code' && this._aceCodeEditor) {
      setTimeout(() => this._aceCodeEditor.resize(), 0);
    }
  },

  async saveToolsFile() {
    if (!this._aceCodeEditor) return;
    const code = this._aceCodeEditor.getValue();
    if (!code.trim()) { showToast('Code is empty', true); return; }
    try {
      const res = await api('/agent/tool/set', { method: 'POST', body: { code } });
      const data = await res.json().catch(() => ({}));
      if (data && data.success === false) throw new Error(data.message || 'Failed to save');
      this.closeToolModal();
      await this.refreshTools();
      showToast('Saved ' + (data.tool_count || '') + ' tools');
    } catch (e) { showToast(e.message, true); }
  },

  showAddToolDialog() { this.editToolsFile(); },

  addTool() { this.editToolsFile(); },

  saveTool() { this.editToolsFile(); },

  closeToolModal() {
    document.querySelectorAll('.tool-modal-overlay').forEach(m => m.remove());
    if (this._modalEsc) { document.removeEventListener('keydown', this._modalEsc); this._modalEsc = null; }
    this._aceCodeEditor = null;
    this._aceLastEditor = null;
  },

  // Initialize Ace Editor on a container; lazy-waits until the global `ace`
  // is available (loaded from CDN in index.html).
  _initAceEditor(containerId, code, readOnly, mode) {
    const tryInit = () => {
      if (typeof ace === 'undefined') { setTimeout(tryInit, 80); return; }
      const el = document.getElementById(containerId);
      if (!el) return;
      const editor = ace.edit(el);
      editor.setTheme('ace/theme/monokai');
      editor.session.setMode('ace/mode/' + (mode || 'python'));
      editor.setValue(code || '', -1);
      editor.setReadOnly(!!readOnly);
      editor.setOptions({
        fontSize: '13px',
        showPrintMargin: false,
        tabSize: 4,
        useSoftTabs: true,
        highlightActiveLine: !readOnly,
      });
      this._aceLastEditor = editor;
    };
    tryInit();
  },

  getEnabledToolsForAPI() {
    return this.tools.filter(t => !this.disabledTools.has(t.name))
      .map(t => ({ name: t.name, description: t.description || '', properties: t.properties || [] }));
  },

  // ── YAML pretty render ──
  _yamlify(obj, indent = 0) {
    const pad = '  '.repeat(indent);
    if (obj === null || obj === undefined) return 'null';
    if (typeof obj === 'string') return obj;
    if (typeof obj !== 'object') return String(obj);
    if (Array.isArray(obj)) {
      if (!obj.length) return '[]';
      return obj.map(v => {
        if (v !== null && typeof v === 'object') {
          const inner = this._yamlify(v, indent + 1);
          return `${pad}-\n${inner}`;
        }
        return `${pad}- ${this._yamlify(v, 0)}`;
      }).join('\n');
    }
    const keys = Object.keys(obj);
    if (!keys.length) return '{}';
    return keys.map(k => {
      const v = obj[k];
      if (v !== null && typeof v === 'object') {
        return `${pad}${k}:\n${this._yamlify(v, indent + 1)}`;
      }
      return `${pad}${k}: ${this._yamlify(v, 0)}`;
    }).join('\n');
  },

  _escapeHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  },

  // Pretty structured renderer (HTML, not yaml text).
  // Returns an HTMLElement representing the value.
  _renderPretty(value) {
    const wrap = document.createElement('div');
    wrap.className = 'pretty';
    wrap.appendChild(this._prettyNode(value));
    return wrap;
  },

  _prettyNode(value) {
    // null/undefined
    if (value === null || value === undefined) {
      const s = document.createElement('span');
      s.className = 'pretty-null';
      s.textContent = 'null';
      return s;
    }
    // primitive
    if (typeof value !== 'object') {
      const s = document.createElement('span');
      s.className = 'pretty-' + typeof value;
      s.textContent = typeof value === 'string' ? value : String(value);
      return s;
    }
    // array
    if (Array.isArray(value)) {
      if (value.length === 0) {
        const s = document.createElement('span'); s.className = 'pretty-empty'; s.textContent = '(empty)'; return s;
      }
      // primitive array → inline chips
      const allPrim = value.every(v => v === null || typeof v !== 'object');
      if (allPrim) {
        const row = document.createElement('div'); row.className = 'pretty-chips';
        value.forEach(v => {
          const chip = document.createElement('span');
          chip.className = 'pretty-chip';
          chip.textContent = v === null ? 'null' : String(v);
          row.appendChild(chip);
        });
        return row;
      }
      // object array → list
      const ul = document.createElement('ul'); ul.className = 'pretty-list';
      value.forEach(v => {
        const li = document.createElement('li');
        li.appendChild(this._prettyNode(v));
        ul.appendChild(li);
      });
      return ul;
    }
    // object
    const keys = Object.keys(value);
    if (keys.length === 0) {
      const s = document.createElement('span'); s.className = 'pretty-empty'; s.textContent = '(empty)'; return s;
    }
    const dl = document.createElement('div'); dl.className = 'pretty-kv';
    for (const k of keys) {
      const v = value[k];
      const row = document.createElement('div'); row.className = 'pretty-row';
      const key = document.createElement('div'); key.className = 'pretty-key'; key.textContent = k;
      const val = document.createElement('div'); val.className = 'pretty-val';
      val.appendChild(this._prettyNode(v));
      row.appendChild(key); row.appendChild(val);
      dl.appendChild(row);
    }
    return dl;
  },

  // Normalize a tool result to a content array.
  // - {type:'text'|'image',...} → [it]
  // - [{type:...}, ...] → as-is
  // - else → [{type:'text', text: yaml(it)}]
  _resultToContents(result) {
    if (Array.isArray(result) && result.length && result.every(c => c && typeof c === 'object' && typeof c.type === 'string')) {
      return result;
    }
    if (result && typeof result === 'object' && (result.type === 'text' || result.type === 'image')) {
      return [result];
    }
    return [{ type: 'text', text: this._yamlify(result) }];
  },

  // ── Chat ──
  clearChat() {
    if (this._abortCtl) { try { this._abortCtl.abort(); } catch {} this._abortCtl = null; }
    this.chatId = null; this.turn = null; this.lastSentPrompt = null; this.stopTTS();
    this._pairs = []; this._currentPair = null;
    this._updateChatIdDisplay();
    this._pairEls = null;
    const msgs = $('agent-messages');
    if (msgs) msgs.innerHTML = '<div class="chat-welcome">Ask the AI agent to control your PhysiCar</div>';
    this._persistChat();
  },

  handleKey(e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); this.send(); } },

  adjustInput() {
    const el = $('agent-input'); if (!el) return;
    el.style.height = 'auto'; el.style.height = Math.min(el.scrollHeight, 120) + 'px';
    this._refreshInputUI();
  },

  // Send button click router: sends if idle, stops if busy.
  onSendBtn() {
    if (this.isWaiting) this.stop();
    else this.send();
  },

  // ===== STT (browser speech-to-text) =====
  _sttSupported() { return !!(window.SpeechRecognition || window.webkitSpeechRecognition); },

  // Add the `stt-on` class so the mic button can appear (idle + empty only).
  _initStt() {
    const area = $('agent-input-area');
    if (area && this._sttSupported()) area.classList.add('stt-on');
  },

  // Toggle the `has-text` state used to swap the mic/send buttons.
  _refreshInputUI() {
    const area = $('agent-input-area'); const el = $('agent-input');
    if (area && el) area.classList.toggle('has-text', !!el.value.trim());
  },

  // Recognition language: browser language, normalised to a full BCP-47 code.
  _sttLang() {
    const MAP = { ko:'ko-KR', en:'en-US', ja:'ja-JP', zh:'zh-CN', es:'es-ES', fr:'fr-FR', de:'de-DE', pt:'pt-BR', ru:'ru-RU', it:'it-IT', vi:'vi-VN', th:'th-TH', ar:'ar-SA', hi:'hi-IN', id:'id-ID', ms:'ms-MY', nl:'nl-NL', pl:'pl-PL', tr:'tr-TR', uk:'uk-UA' };
    const nav = navigator.language || navigator.userLanguage || 'en-US';
    if (nav.includes('-')) return nav;
    const low = nav.toLowerCase();
    return MAP[low] || (low + '-' + low.toUpperCase());
  },

  // Fill the wave bar container with animated bars sized to its width.
  _genWaveBars() {
    const bars = $('agent-wave-bars'); if (!bars) return;
    bars.innerHTML = '';
    const w = bars.offsetWidth || 200;
    const count = Math.max(3, Math.floor(w / 7)); // ~3px bar + 4px gap
    for (let i = 0; i < count; i++) {
      const s = document.createElement('span');
      s.style.animationDelay = (Math.random() * 0.6).toFixed(2) + 's';
      bars.appendChild(s);
    }
  },

  // Build a fresh SpeechRecognition with all handlers wired.
  _makeSttRec() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) return null;
    const rec = new SR();
    rec.lang = this._sttLang();
    rec.continuous = false;     // ends per-utterance; we restart in onend
    rec.interimResults = true;  // show partial text live
    rec.maxAlternatives = 1;
    rec.onstart = () => {
      this._sttRecording = true;
      $('agent-input-area')?.classList.add('recording');
      this._genWaveBars();
    };
    rec.onresult = (e) => {
      let newFinal = '', interim = '';
      for (let i = 0; i < e.results.length; i++) {
        const r = e.results[i];
        if (r.isFinal) { if (i > this._sttLastIndex) { newFinal += r[0].transcript; this._sttLastIndex = i; } }
        else interim += r[0].transcript;
      }
      if (newFinal) this._sttConfirmed = this._sttConfirmed ? this._sttConfirmed + ' ' + newFinal : newFinal;
      let display = this._sttConfirmed;
      if (interim) display = display ? display + ' ' + interim : interim;
      display = display.trim();
      const input = $('agent-input');
      if (input && display !== input.value) {
        input.value = display;
        this.adjustInput();
        input.scrollTop = input.scrollHeight;
      }
    };
    rec.onerror = (e) => {
      if (e.error === 'no-speech' || e.error === 'aborted') return; // restart in onend
      if (e.error === 'not-allowed' || e.error === 'service-not-allowed') {
        this.stopSTT();
        if (typeof showToast === 'function') showToast('Microphone permission required', true);
        return;
      }
      // 'network' and others: let onend restart.
    };
    rec.onend = () => {
      if (!this._sttRecording) return; // stopped intentionally
      // continuous=false ends each utterance; restart a fresh object while recording.
      setTimeout(() => {
        if (!this._sttRecording) return;
        this._sttRec = null; this._sttLastIndex = -1;
        this._sttRec = this._makeSttRec();
        if (this._sttRec) {
          try { this._sttRec.start(); }
          catch (err) {
            setTimeout(() => {
              if (this._sttRecording && !this._sttRec) {
                this._sttRec = this._makeSttRec();
                try { this._sttRec && this._sttRec.start(); } catch (e2) {}
              }
            }, 500);
          }
        }
      }, 100);
    };
    return rec;
  },

  startSTT() {
    if (!this._sttSupported() || this._sttRecording) return;
    this.stopTTS();
    this._sttLastIndex = -1; this._sttConfirmed = '';
    const input = $('agent-input'); if (input) input.readOnly = true;
    if (this._sttRec) { try { this._sttRec.abort(); } catch (e) {} this._sttRec = null; }
    this._sttRec = this._makeSttRec();
    if (this._sttRec) {
      try { this._sttRec.start(); }
      catch (e) { setTimeout(() => { if (!this._sttRecording && this._sttRec) { try { this._sttRec.start(); } catch (e2) {} } }, 200); }
    }
  },

  // Stop recognition. Keeps whatever was transcribed so the user can edit/send.
  stopSTT() {
    this._sttRecording = false;
    $('agent-input-area')?.classList.remove('recording');
    this._sttConfirmed = ''; this._sttLastIndex = -1;
    const input = $('agent-input'); if (input) input.readOnly = false;
    if (this._sttRec) { try { this._sttRec.abort(); } catch (e) {} this._sttRec = null; }
    this._refreshInputUI();
  },

  // ===== Config sections (collapse on desktop, modal on mobile) =====
  toggleCfg(header) {
    const group = header.parentElement; if (!group) return;
    const mobile = window.matchMedia('(max-width: 1024px)').matches;
    if (!mobile) { group.classList.toggle('collapsed'); return; }
    const title = (header.childNodes[0]?.textContent || header.textContent || '').trim();
    // Instructions already has its own editor modal.
    if (title.toLowerCase().startsWith('instruction')) { this.editInstructions(); return; }
    this.openCfgModal(group, title);
  },

  // Move a config group's body into a modal (and back on close) so the real
  // controls are reused without duplicating element IDs.
  openCfgModal(group, title) {
    const body = group.querySelector('.cfg-body'); if (!body) return;
    const modal = document.createElement('div');
    modal.className = 'tool-modal-overlay';
    modal.innerHTML = `
      <div class="tool-modal" style="height:auto;max-height:80vh">
        <div class="tool-modal-header">
          <span class="tool-modal-title">${title}</span>
          <div style="flex:1"></div>
          <button class="tool-modal-close" type="button">&times;</button>
        </div>
        <div class="tool-modal-body cfg-modal-body"></div>
      </div>`;
    modal.querySelector('.cfg-modal-body').appendChild(body);
    document.body.appendChild(modal);
    const close = () => {
      group.appendChild(body);              // restore real controls
      modal.remove();
      document.removeEventListener('keydown', esc);
    };
    const esc = (ev) => { if (ev.key === 'Escape') close(); };
    modal.querySelector('.tool-modal-close').addEventListener('click', close);
    modal.addEventListener('mousedown', (ev) => { if (ev.target === modal) close(); });
    document.addEventListener('keydown', esc);
  },

  // Abort the in-flight request and any TTS playback. Marks the in-progress
  // pair as `cancelled` so the next send visually overwrites it.
  stop() {
    if (!this.isWaiting) return;
    if (this._abortCtl) { try { this._abortCtl.abort(); } catch {} this._abortCtl = null; }
    this.stopTTS();
    // Keep whatever streamed so far as a normal (done) message — no "cancelled"
    // fade, no overwrite. The next message simply appends after it.
    if (this._currentPair) {
      this._currentPair.status = 'done';
      const am = this._currentPair.assistant_message || { contents: [], tool_calls: [] };
      if (!am.contents || !am.contents.length) {
        const txt = (this._pairEls && this._pairEls.aContentsBody) ? this._pairEls.aContentsBody.textContent.trim() : '';
        if (txt) am.contents = [{ type: 'text', text: txt }];
      }
      this._currentPair.assistant_message = am;
    }
    this._persistChat();
    this._setWaiting(false);
  },

  // Drop the trailing `cancelled` pair (if any) so the next request overwrites
  // it both visually (DOM) and on the wire (same `turn` is re-sent).
  _dropTrailingCancelled() {
    while (this._pairs.length && this._pairs[this._pairs.length - 1].status === 'cancelled') {
      const dropped = this._pairs.pop();
      // Remove the matching DOM node — `dataset.turn` matches when the server
      // had already assigned a turn before cancel; otherwise it's still '?'.
      const msgs = $('agent-messages'); if (!msgs) continue;
      const candidate = msgs.querySelector('.chat-pair.cancelled:last-of-type');
      if (candidate) candidate.remove();
    }
    this._pairEls = null;
    this._currentPair = null;
  },

  async send() {
    if (this.isWaiting) return;
    if (this._sttRecording) this.stopSTT();
    // A previous turn may have finished its text early and still be draining
    // its TTS audio in the background — abort that tail before a new request.
    if (this._abortCtl) { try { this._abortCtl.abort(); } catch (e) {} this._abortCtl = null; }
    const sendId = ++this._sendSeq;
    const input = $('agent-input'), text = input?.value.trim();
    if (!text) return;
    input.value = ''; input.style.height = 'auto';
    this._refreshInputUI();
    this.saveConfig();

    this._dropTrailingCancelled();

    const userMsg = { contents: [{ type: 'text', text }] };
    this._openPair(userMsg);
    this._setWaiting(true);
    this.stopTTS();

    // Build contents (sensor state is now fetched on-demand via the states tool)
    const contents = [{ type: 'text', text }];

    // Prompt (change detection)
    const instructions = $('agent-instructions')?.value || '';
    const model = $('agent-model')?.value || '';
    const info = this._modelInfo && this._modelInfo[model];
    const supportsReasoning = !!(info && info.reasoning_effort_supported);
    const re = $('agent-reasoning')?.value || 'low';
    const tools = this.getEnabledToolsForAPI();
    const currentPrompt = { instructions, model, reasoning_effort: supportsReasoning ? re : null, tools };
    const promptChanged = JSON.stringify(currentPrompt) !== JSON.stringify(this.lastSentPrompt);

    const body = { prompt_id: this.getPromptId(), user_message: { contents }, stream: true, audio: this.ttsEnabled };
    if (promptChanged) { body.prompt = currentPrompt; this.lastSentPrompt = currentPrompt; }
    if (this.chatId) body.chat_id = this.chatId;
    // `turn` is intentionally NOT sent: the server auto-continues from chat_id.
    // (Only needed for an explicit go-back/regenerate of a past turn.)

    // Endpoint: /chat (logged in only — guest mode removed)
    const token = PlatformAuth.getToken();
    if (!token) { this._addError('Sign in required'); this._setWaiting(false); return; }
    const headers = { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token };

    this._abortCtl = new AbortController();
    try {
      const res = await fetch(PlatformAuth.API_URL + '/chat', { method: 'POST', headers, body: JSON.stringify(body), signal: this._abortCtl.signal });
      if (res.status === 401) { PlatformAuth.clearToken(); PlatformAuth.user = null; PlatformAuth._updateUI(); this._refreshGate(true); throw new Error('Session expired. Please sign in again.'); }
      if (!res.ok) { const e = await res.json().catch(() => ({})); throw new Error(e.error || e.message || `HTTP ${res.status}`); }
      await this._processStream(res);
    } catch (e) {
      if (e.name === 'AbortError') {
        // Cancellation already handled by stop().
      } else {
        this._showTyping(false); this._addError(e.message);
        if (this._currentPair) { this._currentPair.status = 'cancelled'; this._pairEls?.pair?.classList.add('cancelled'); }
      }
    }
    finally { if (this._sendSeq === sendId) { this._abortCtl = null; this._setWaiting(false); } this._persistChat(); }
  },

  // Toggle the busy state: shows the typing indicator, swaps the send button
  // into a stop (square) button, and disables the input so users can't
  // double-submit while a response is streaming.
  _setWaiting(busy) {
    this.isWaiting = !!busy;
    this._showTyping(!!busy);
    const btn = $('agent-send');
    const input = $('agent-input');
    $('agent-input-area')?.classList.toggle('busy', !!busy);
    if (btn) {
      btn.classList.toggle('busy', !!busy);
      btn.disabled = false;  // keep clickable so it can act as Stop
      btn.title = busy ? 'Stop' : 'Send';
    }
    // Keep the input editable while streaming so the user can pre-type the next
    // message; sending is still blocked until Stop (see send()'s isWaiting guard).
    if (input) input.disabled = false;
  },

  async _processStream(res) {
    const reader = res.body.getReader(), dec = new TextDecoder();
    let buf = '', content = '', toolCalls = [], typingHidden = false, textPart = null;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split('\n'); buf = lines.pop() || '';
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const raw = line.slice(6);
        if (raw === '[DONE]') continue;
        try {
          const ev = JSON.parse(raw);
          switch (ev.type) {
            case 'text':
              if (!typingHidden) { this._showTyping(false); typingHidden = true; }
              content += ev.content;
              if (!textPart) textPart = this._appendAssistantText(content);
              else this._updateAssistantText(textPart, content);
              this._scrollBottom();
              break;
            case 'audio':
              if (this.ttsEnabled && ev.data) this._handleAudioChunk(ev.data);
              break;
            case 'tool_call':
              if (!typingHidden) { this._showTyping(false); typingHidden = true; }
              toolCalls.push({ call_id: ev.call_id, name: ev.name, arguments: ev.arguments });
              break;
            case 'done':
              if (!typingHidden) { this._showTyping(false); typingHidden = true; }
              if (ev.chat_id) { this.chatId = ev.chat_id; this._updateChatIdDisplay(); }
              if (ev.turn !== undefined) {
                this.turn = ev.turn + 1;
                // Stamp the current pair label with the server-assigned turn.
                if (this._currentPair) this._currentPair.turn = ev.turn;
                if (this._pairEls && this._pairEls.pair) {
                  this._pairEls.pair.dataset.pairIndex = String(ev.turn);
                  const idxEl = this._pairEls.pairLabel?.querySelector('.pair-index');
                  if (idxEl) idxEl.textContent = '#' + ev.turn;
                }
              }
              if (ev.tool_calls?.length) toolCalls = ev.tool_calls;
              // Text turn finished: if no tool calls follow, end the waiting
              // state now (Stop → Send, re-enable input) even though TTS audio
              // may still be streaming in the background.
              if (!toolCalls.length) this._setWaiting(false);
              // Persist the completed assistant_message into the current pair.
              if (this._currentPair) {
                this._currentPair.status = 'done';
                this._currentPair.assistant_message = this._currentPair.assistant_message || { contents: [], tool_calls: [] };
                if (content) this._currentPair.assistant_message.contents = [{ type: 'text', text: content }];
              }
              if (ev.usage && PlatformAuth.user) {
                const u = PlatformAuth.user;
                const usage = Number(ev.usage) || 0;
                if (u.classroom_id) {
                  // Classroom student: sponsor_used increments toward sponsor_limit.
                  u.classroom_sponsor_used = Math.max(0, (u.classroom_sponsor_used || 0) + usage);
                } else {
                  // Personal user: credit_balance decrements.
                  u.credit_balance = Math.max(0, (u.credit_balance || 0) - usage);
                }
                PlatformAuth._saveUser(); PlatformAuth._updateUI();
              }
              break;
            case 'error':
              this._addError(ev.message || 'Stream error');
              break;
          }
        } catch {}
      }
    }
    // After assistant is done, render its tool_calls into the assistant box.
    for (const tc of toolCalls) {
      let args = {};
      if (typeof tc.arguments === 'string') { try { args = JSON.parse(tc.arguments); } catch { args = tc.arguments; } }
      else if (tc.arguments) args = tc.arguments;
      const callId = tc.call_id || tc.id || ('tc_' + (++this._callSeq));
      tc._uiCallId = callId;
      this._appendAssistantToolCall(tc.name, args, callId);
    }
    if (toolCalls.length > 0) await this._executeToolCalls(toolCalls);
    this._scrollBottom();
  },

  async _executeToolCalls(toolCalls) {
    // Each tool call → one tool_output part inside a new user message (next turn).
    const outputs = [];
    // Open the next user message bubble that will carry tool_call_outputs.
    this._openPair({ contents: [], tool_call_outputs: [] });
    for (const tc of toolCalls) {
      let args = {};
      if (typeof tc.arguments === 'string') { try { args = JSON.parse(tc.arguments); } catch {} }
      else if (tc.arguments) args = tc.arguments;
      const callId = tc._uiCallId || tc.call_id || tc.id || ('tc_' + (++this._callSeq));
      try {
        const res = await api('/agent/tool/call/' + tc.name, { method: 'POST', body: args, timeout: 30000 });
        const resp = await res.json();
        // /agent/tool/call wraps as {success, result}. Unwrap.
        const payload = (resp && typeof resp === 'object' && 'result' in resp) ? resp.result : resp;
        const cs = this._resultToContents(payload);
        const ok = !(resp && resp.success === false);
        this._appendUserToolOutput(tc.name, cs, !ok, callId);
        outputs.push({ call_id: tc.call_id, contents: cs });
      } catch (e) {
        const cs = [{ type: 'text', text: 'Error: ' + e.message }];
        this._appendUserToolOutput(tc.name, cs, true, callId);
        outputs.push({ call_id: tc.call_id, contents: cs });
      }
    }
    if (outputs.length) {
      // Tool outputs carry no user text — don't cut the assistant's current
      // TTS now; it will be replaced when the next response's audio arrives.
      this._showTyping(true); this._newTtsPending = true;
      const token = PlatformAuth.getToken();
      if (!token) return;
      const headers = { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token };
      const body = { prompt_id: this.getPromptId(), user_message: { contents: [], tool_call_outputs: outputs }, stream: true, audio: this.ttsEnabled };
      if (this.chatId) body.chat_id = this.chatId;
      try {
        const res = await fetch(PlatformAuth.API_URL + '/chat', { method: 'POST', headers, body: JSON.stringify(body), signal: this._abortCtl?.signal });
        if (res.status === 401) { PlatformAuth.clearToken(); PlatformAuth.user = null; PlatformAuth._updateUI(); this._refreshGate(true); throw new Error('Session expired. Please sign in again.'); }
        if (!res.ok) throw new Error('HTTP ' + res.status);
        await this._processStream(res);
      } catch (e) {
        if (e.name === 'AbortError') {
          // Cancelled — already handled by stop().
        } else {
          this._showTyping(false); this._addError(e.message);
          if (this._currentPair) { this._currentPair.status = 'cancelled'; this._pairEls?.pair?.classList.add('cancelled'); }
        }
      }
    }
    this.refreshTools();
  },

  // ── TTS ──
  toggleTTS() {
    const pop = $('tts-popover');
    if (pop) pop.classList.toggle('show');
  },
  setTTSTarget(target) {
    this.ttsTarget = target;
    this.ttsEnabled = target !== 'off';
    if (!this.ttsEnabled) this.stopTTS();
    this._updateTTSBtn();
    const pop = $('tts-popover');
    if (pop) pop.classList.remove('show');
    this.saveConfig();
  },
  _updateTTSBtn() {
    const b = $('agent-tts-btn');
    if (!b) return;
    const off = this.ttsTarget === 'off';
    b.classList.toggle('active', !off);
    b.classList.toggle('tts-off', off);
    const label = $('tts-label');
    if (label) {
      if (off) { label.textContent = 'Off'; label.style.display = ''; }
      else { label.textContent = this.ttsTarget === 'kit' ? 'Device' : 'Local'; label.style.display = ''; }
    }
    const pop = $('tts-popover');
    if (pop) {
      pop.querySelectorAll('.tts-opt').forEach(o => {
        o.classList.toggle('selected', o.dataset.target === this.ttsTarget);
        if (o.dataset.target === 'kit') {
          o.disabled = this.isSimMode;
          o.classList.toggle('disabled', this.isSimMode);
        }
      });
    }
    const vol = $('agent-volume');
    if (vol) {
      vol.disabled = off;
      vol.closest('.tts-vol')?.classList.toggle('disabled', off);
    }
    this._updateVolLabel();
  },
  _updateVolLabel() {
    const pct = $('tts-vol-pct');
    const vol = $('agent-volume');
    if (pct && vol) pct.textContent = vol.value + '%';
    // Apply volume change immediately to playing audio
    if (this.gainNode && vol) {
      this.gainNode.gain.value = parseInt(vol.value) / 100;
    }
    // Apply to device immediately (volume-only message)
    if (this.ttsTarget === 'kit' && vol) {
      api('/audio', { method: 'POST', body: {
        channel: 'tts', volume: parseInt(vol.value) / 100
      }}).catch(() => {});
    }
  },

  _handleAudioChunk(base64Data) {
    // A new response's audio is starting: flush any TTS still playing from the
    // previous turn now (deferred from the tool-call continuation).
    if (this._newTtsPending) { this.stopTTS(); }
    // Kit playback — volume is controlled separately via _updateVolLabel
    if (this.ttsTarget === 'kit') {
      // Send initial volume on first chunk
      if (this.isFirstChunk) {
        const vol = (parseInt($('agent-volume')?.value) || 80) / 100;
        api('/audio', { method: 'POST', body: {
          channel: 'tts', volume: vol
        }}).catch(() => {});
      }
      api('/audio', { method: 'POST', body: {
        data: base64Data, format: 'pcm', channel: 'tts',
        sample_rate: 24000, audio_channels: 1, bits_per_sample: 16
      }}).catch(() => {});
      this.isFirstChunk = false;
      return;  // kit only — skip browser playback
    }

    // Browser Web Audio API playback
    const binary = atob(base64Data);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    const view = new DataView(bytes.buffer);
    const samples = new Float32Array(bytes.length / 2);
    for (let i = 0; i < samples.length; i++) samples[i] = view.getInt16(i * 2, true) / 32768;
    if (this.isFirstChunk) {
      this.isFirstChunk = false;
      const fadeLen = Math.min(480, samples.length);
      for (let i = 0; i < fadeLen; i++) { const t = i / fadeLen; samples[i] *= t * t * (3 - 2 * t); }
    }
    this.audioQueue.push(samples);
    if (!this.isPlayingAudio) {
      if (!this.initialBufferingDone) {
        const totalMs = this.audioQueue.reduce((s, a) => s + (a.length / 24), 0);
        if (totalMs >= this.MIN_BUFFER_DURATION_MS) { this.initialBufferingDone = true; this._startAudioPlayback(); }
        else if (!this.bufferingTimer) {
          this.bufferingTimer = setTimeout(() => { if (!this.isPlayingAudio && this.audioQueue.length > 0) { this.initialBufferingDone = true; this._startAudioPlayback(); } }, this.MIN_BUFFER_DURATION_MS);
        }
      } else this._scheduleAudioChunks();
    }
  },

  _startAudioPlayback() {
    if (!this.audioContext) { this.audioContext = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 24000 }); this.gainNode = this.audioContext.createGain(); this.gainNode.connect(this.audioContext.destination); }
    const vol = (parseInt($('agent-volume')?.value) || 80) / 100;
    this.gainNode.gain.setValueAtTime(0, this.audioContext.currentTime);
    this.gainNode.gain.linearRampToValueAtTime(vol, this.audioContext.currentTime + 0.1);
    this.isPlayingAudio = true; this.scheduledEndTime = this.audioContext.currentTime;
    this._scheduleAudioChunks();
  },

  _scheduleAudioChunks() {
    if (!this.audioContext || !this.isPlayingAudio) return;
    while (this.audioQueue.length > 0) {
      const samples = this.audioQueue.shift();
      const buffer = this.audioContext.createBuffer(1, samples.length, 24000);
      buffer.getChannelData(0).set(samples);
      const source = this.audioContext.createBufferSource();
      source.buffer = buffer; source.connect(this.gainNode);
      const startAt = Math.max(this.scheduledEndTime, this.audioContext.currentTime);
      source.start(startAt); this.scheduledEndTime = startAt + buffer.duration;
      this.scheduledSources.push(source);
      source.onended = () => {
        this.scheduledSources = this.scheduledSources.filter(s => s !== source);
        if (this.audioQueue.length > 0) this._scheduleAudioChunks();
        else if (this.scheduledSources.length === 0) { this.isPlayingAudio = false; this.isFirstChunk = true; this.initialBufferingDone = false; }
      };
    }
  },

  stopTTS() {
    if (this.bufferingTimer) { clearTimeout(this.bufferingTimer); this.bufferingTimer = null; }
    this.audioQueue = [];
    this.scheduledSources.forEach(s => { try { s.stop(); } catch {} });
    this.scheduledSources = [];
    this.isPlayingAudio = false; this.isFirstChunk = true; this.initialBufferingDone = false;
    this._newTtsPending = false;
    api('/audio', { method: 'POST', body: { channel: 'tts', stop: true } }).catch(() => {});
  },

  // ── Markdown ──
  _formatMarkdown(text) {
    if (typeof marked !== 'undefined' && marked.parse) {
      try {
        return marked.parse(text, { breaks: true });
      } catch (e) {
        // fallback below
      }
    }
    // Fallback: basic escaping + line breaks
    return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/\n/g, '<br>');
  },

  // ── DOM (pair-based rendering reflecting the API MessagePair model) ──
  // A "pair" = one user message + one assistant message (matches the API).
  // - User message body holds: text/image contents + tool_call_outputs (from prior turn).
  // - Assistant message body holds: text contents + tool_calls (this turn).
  // _pairEls tracks the currently open (in-progress) pair so streamed text /
  // tool_calls can be appended to its assistant box.

  _newPart(kind) {
    const p = document.createElement('div');
    p.className = 'msg-part ' + kind;
    return p;
  },

  _renderContentParts(host, contents) {
    for (const c of (contents || [])) {
      if (!c || !c.type) continue;
      if (c.type === 'image' && c.base64) {
        const mime = c.mime || 'image/jpeg';
        const part = this._newPart('image');
        part.innerHTML = `<img class="part-image" src="data:${mime};base64,${c.base64}" alt="image" onclick="window.open(this.src, '_blank')">`;
        host.appendChild(part);
      } else if (c.type === 'text') {
        const part = this._newPart('text');
        part.innerHTML = '<div class="part-text">' + this._formatMarkdown(c.text || '') + '</div>';
        host.appendChild(part);
      }
    }
  },

  _renderToolPart(kind /* 'tool-call' | 'tool-output' */, name, body /* args | contents */, isError, callId) {
    // <details> = native collapsible; <summary> = clickable header.
    const part = document.createElement('details');
    part.className = 'msg-part ' + kind;
    if (isError) part.classList.add('error');
    if (callId) {
      part.dataset.callId = callId;
      // Track pair number for visual linking (#1, #2, ...).
      if (!this._callIdToIndex) this._callIdToIndex = new Map();
      let idx = this._callIdToIndex.get(callId);
      if (!idx) { idx = this._callIdToIndex.size + 1; this._callIdToIndex.set(callId, idx); }
      part.dataset.callIndex = String(idx);
      // Hover linking: highlight matching partner part.
      part.addEventListener('mouseenter', () => this._highlightCall(callId, true));
      part.addEventListener('mouseleave', () => this._highlightCall(callId, false));
    }

    const head = document.createElement('summary');
    head.className = 'part-tool-head';
    const chev = document.createElement('span');
    chev.className = 'part-tool-chev';
    chev.textContent = '▸';
    const nameEl = document.createElement('span');
    nameEl.className = 'part-tool-name';
    nameEl.textContent = name || '';
    head.appendChild(chev);
    head.appendChild(nameEl);
    if (kind === 'tool-output' && isError) {
      const err = document.createElement('span');
      err.className = 'part-tool-err';
      err.textContent = 'error';
      head.appendChild(err);
    }
    part.appendChild(head);

    const bodyEl = document.createElement('div');
    bodyEl.className = 'part-tool-body';

    if (kind === 'tool-call') {
      const args = body || {};
      if (args && typeof args === 'object' && Object.keys(args).length > 0) {
        bodyEl.appendChild(this._renderPretty(args));
      } else {
        const empty = document.createElement('span');
        empty.className = 'pretty-empty';
        empty.textContent = '(no arguments)';
        bodyEl.appendChild(empty);
      }
    } else {
      // tool-output: contents array (text + images)
      for (const c of (body || [])) {
        if (!c || !c.type) continue;
        if (c.type === 'image' && c.base64) {
          const mime = c.mime || 'image/jpeg';
          const img = document.createElement('img');
          img.className = 'part-image';
          img.src = `data:${mime};base64,${c.base64}`;
          img.onclick = () => window.open(img.src, '_blank');
          bodyEl.appendChild(img);
        } else if (c.type === 'text') {
          const txt = c.text == null ? '' : String(c.text);
          const trimmed = txt.trim();
          // Try to parse as JSON for structured render
          let parsed = null;
          if (trimmed && (trimmed[0] === '{' || trimmed[0] === '[')) {
            try { parsed = JSON.parse(trimmed); } catch {}
          }
          if (parsed !== null && typeof parsed === 'object') {
            bodyEl.appendChild(this._renderPretty(parsed));
          } else {
            // Plain text — keep newlines but use normal font (not monospace)
            const p = document.createElement('div');
            p.className = 'pretty-text';
            p.textContent = txt;
            bodyEl.appendChild(p);
          }
        }
      }
    }
    part.appendChild(bodyEl);
    return part;
  },

  // Open a new chat pair. `userMsg` = { contents, tool_call_outputs? }.
  // Layout per message bubble:
  //   contents (rendered directly, no section header)
  //   tool_calls / tool_call_outputs (section header `<key>[N]` below)
  _openPair(userMsg) {
    const msgs = $('agent-messages'); if (!msgs) return null;
    msgs.querySelector('.chat-welcome')?.remove();

    const pair = document.createElement('div'); pair.className = 'chat-pair';
    const pairLabel = document.createElement('div'); pairLabel.className = 'pair-label';
    // Predict the index as (last persisted turn) + 1 so the label doesn't
    // pop from "?" → "N" once the server `done` event arrives. The server's
    // authoritative `turn` overwrites this in the done handler.
    const predicted = (this.turn != null) ? this.turn : this._pairs.length;
    pairLabel.innerHTML = '<span class="pair-index">#' + predicted + '</span>';
    pair.appendChild(pairLabel);

    // ── user message ──
    const userBox = document.createElement('div'); userBox.className = 'chat-msg user';
    const userBody = document.createElement('div'); userBody.className = 'msg-body';
    userBox.appendChild(userBody);
    pair.appendChild(userBox);

    // contents (no section header — rendered inline)
    const userContentsBody = document.createElement('div');
    userContentsBody.className = 'msg-contents';
    userBody.appendChild(userContentsBody);
    this._renderContentParts(userContentsBody, (userMsg && userMsg.contents) || []);

    // tool_call_outputs[] section (with header)
    const tcoOutputs = (userMsg && userMsg.tool_call_outputs) || [];
    const tcoSection = this._makeSchemaSection('tool_call_outputs', tcoOutputs.length);
    userBody.appendChild(tcoSection);
    for (const o of tcoOutputs) {
      tcoSection.querySelector('.schema-section-body').appendChild(
        this._renderToolPart('tool-output', o.name || '', o.contents || [], !!o.error, o.call_id)
      );
    }

    // ── assistant message ──
    const assistantBox = document.createElement('div'); assistantBox.className = 'chat-msg assistant';
    const assistantBody = document.createElement('div'); assistantBody.className = 'msg-body';
    assistantBox.appendChild(assistantBody);
    pair.appendChild(assistantBox);

    // contents (no section header — rendered inline)
    const aContentsBody = document.createElement('div');
    aContentsBody.className = 'msg-contents';
    assistantBody.appendChild(aContentsBody);

    // Inline typing indicator — lives inside the assistant bubble so the
    // "waiting" wave shows in the same bubble instead of a separate one below.
    const aTyping = document.createElement('div');
    aTyping.className = 'chat-typing-inline';
    aTyping.innerHTML = '<span></span><span></span><span></span>';
    assistantBody.appendChild(aTyping);

    // tool_calls[] section (with header)
    const aToolCallsSection = this._makeSchemaSection('tool_calls', 0);
    assistantBody.appendChild(aToolCallsSection);

    msgs.appendChild(pair);
    this._pairEls = {
      pair, pairLabel, userBox, userBody, assistantBox, assistantBody,
      userContentsBody,
      tcoBody: tcoSection.querySelector('.schema-section-body'),
      tcoSection,
      aContentsBody,
      aToolCallsBody: aToolCallsSection.querySelector('.schema-section-body'),
      aToolCallsSection,
      typing: aTyping,
    };
    // Track this pair as the current in-progress pair for persistence.
    const pairData = {
      turn: null,
      status: 'pending',
      user_message: {
        contents: (userMsg && userMsg.contents) ? JSON.parse(JSON.stringify(userMsg.contents)) : [],
        tool_call_outputs: (userMsg && userMsg.tool_call_outputs) ? JSON.parse(JSON.stringify(userMsg.tool_call_outputs)) : [],
      },
      assistant_message: { contents: [], tool_calls: [] },
    };
    this._pairs.push(pairData);
    this._currentPair = pairData;
    this._refreshSchemaCounts();
    this._scrollBottom();
    return this._pairEls;
  },

  // Build a labeled schema section. `key` is the API field name (snake_case);
  // displayed as Title Case (e.g. tool_call_outputs → "Tool Call Outputs").
  _makeSchemaSection(key, count) {
    const wrap = document.createElement('div');
    wrap.className = 'schema-section';
    wrap.dataset.key = key;
    const title = key.split('_').map(w => w ? w[0].toUpperCase() + w.slice(1) : w).join(' ');
    const head = document.createElement('div');
    head.className = 'schema-section-head';
    head.innerHTML = '<span class="schema-key">' + title + '</span>'
      + '<span class="schema-count">[<span class="schema-count-n">' + (count || 0) + '</span>]</span>';
    const body = document.createElement('div');
    body.className = 'schema-section-body';
    wrap.appendChild(head);
    wrap.appendChild(body);
    return wrap;
  },

  // Refresh `[N]` counts on every schema section in the current pair, and
  // hide entire sections that have zero items so the chat doesn't show
  // empty "Tool Calls [0]" / "Tool Call Outputs [0]" headers.
  _refreshSchemaCounts() {
    if (!this._pairEls) return;
    const update = (section) => {
      if (!section) return;
      const body = section.querySelector('.schema-section-body');
      const n = body ? body.children.length : 0;
      const nEl = section.querySelector('.schema-count-n');
      if (nEl) nEl.textContent = String(n);
      section.style.display = n > 0 ? '' : 'none';
    };
    update(this._pairEls.tcoSection);
    update(this._pairEls.aToolCallsSection);
  },

  // Streamed assistant text: append/create the text part inside assistant_message.contents[].
  _appendAssistantText(text) {
    if (!this._pairEls) this._openPair({ contents: [] });
    const { aContentsBody } = this._pairEls;
    let part = aContentsBody.querySelector('.msg-part.text:last-of-type');
    if (!part || part !== aContentsBody.lastChild) {
      part = this._newPart('text');
      part.innerHTML = '<div class="part-text"></div>';
      aContentsBody.appendChild(part);
      this._refreshSchemaCounts();
    }
    part.querySelector('.part-text').innerHTML = this._formatMarkdown(text);
    return part;
  },

  _updateAssistantText(part, text) {
    if (!part) return;
    const t = part.querySelector('.part-text');
    if (t) t.innerHTML = this._formatMarkdown(text);
  },

  _appendAssistantToolCall(name, args, callId) {
    if (!this._pairEls) this._openPair({ contents: [] });
    const { aToolCallsBody } = this._pairEls;
    aToolCallsBody.appendChild(this._renderToolPart('tool-call', name, args, false, callId));
    if (this._currentPair) {
      this._currentPair.assistant_message.tool_calls.push({ call_id: callId, name, arguments: args });
    }
    this._refreshSchemaCounts();
    this._scrollBottom();
  },

  // After a tool runs locally, append its result as a tool-output part inside
  // user_message.tool_call_outputs[] of the *current* (already-open) pair.
  _appendUserToolOutput(name, contents, isError, callId) {
    if (!this._pairEls) this._openPair({ contents: [], tool_call_outputs: [] });
    const { tcoBody } = this._pairEls;
    tcoBody.appendChild(this._renderToolPart('tool-output', name, contents, isError, callId));
    if (this._currentPair) {
      this._currentPair.user_message.tool_call_outputs.push({ call_id: callId, name, contents, error: !!isError });
    }
    this._refreshSchemaCounts();
    this._scrollBottom();
  },

  // Highlight both ends of a tool_call ↔ tool_output pair across messages.
  _highlightCall(callId, on) {
    const msgs = $('agent-messages'); if (!msgs) return;
    const els = msgs.querySelectorAll('.msg-part[data-call-id="' + CSS.escape(callId) + '"]');
    els.forEach(el => el.classList.toggle('linked', on));
  },

  _addError(msg) {
    const msgs = $('agent-messages'); if (!msgs) return null;
    msgs.querySelector('.chat-welcome')?.remove();
    const div = document.createElement('div'); div.className = 'chat-msg error';
    div.textContent = msg;
    msgs.appendChild(div); this._scrollBottom(); return div;
  },

  _showTyping(show) { const el = (this._pairEls && this._pairEls.typing) || $('agent-typing'); if (el) el.classList.toggle('visible', show); },
  _scrollBottom() { const m = $('agent-messages'); if (m) m.scrollTop = m.scrollHeight; },

  // Show/hide the chat_id pill in the chat header. We set the textContent
  // and rely on the CSS `:empty` rule to hide it when there is no chat_id.
  _updateChatIdDisplay() {
    const el = $('agent-chat-id'); if (!el) return;
    el.textContent = this.chatId || '';
  },

  // ── Persistence ──
  // Save the current chat (chat_id, next-turn, all pairs) to localStorage so
  // a page reload restores it.
  _persistChat() {
    try {
      const data = {
        chat_id: this.chatId,
        turn: this.turn,
        pairs: this._pairs,
      };
      localStorage.setItem(this._persistKey, JSON.stringify(data));
    } catch {}
  },

  // Rehydrate chat from localStorage. Any pair without a final `done` becomes
  // a `cancelled` pair (since reload definitely interrupted streaming).
  _restoreChat() {
    let data;
    try {
      const raw = localStorage.getItem(this._persistKey);
      if (!raw) return;
      data = JSON.parse(raw);
    } catch { return; }
    if (!data || !Array.isArray(data.pairs) || !data.pairs.length) return;
    this.chatId = data.chat_id || null;
    this.turn = data.turn != null ? data.turn : null;
    this._updateChatIdDisplay();
    const msgs = $('agent-messages');
    if (msgs) msgs.querySelector('.chat-welcome')?.remove();
    this._pairs = [];
    for (const p of data.pairs) {
      const status = p.status === 'done' ? 'done' : 'cancelled';
      // Re-open the DOM scaffold using the saved user_message.
      this._openPair(p.user_message || { contents: [] });
      // _openPair pushed a fresh entry; replace it with the saved one (preserving status).
      const fresh = this._pairs.pop();
      const restored = {
        turn: p.turn != null ? p.turn : null,
        status,
        user_message: p.user_message || { contents: [] },
        assistant_message: p.assistant_message || { contents: [], tool_calls: [] },
      };
      this._pairs.push(restored);
      this._currentPair = restored;
      // Stamp the turn label.
      if (this._pairEls && this._pairEls.pair) {
        if (restored.turn != null) {
          this._pairEls.pair.dataset.pairIndex = String(restored.turn);
          const idxEl = this._pairEls.pairLabel?.querySelector('.pair-index');
          if (idxEl) idxEl.textContent = '#' + restored.turn;
        }
        if (status === 'cancelled') this._pairEls.pair.classList.add('cancelled');
      }
      // Re-render the assistant_message contents and tool_calls.
      const am = restored.assistant_message;
      if (am.contents && am.contents.length && this._pairEls) {
        this._renderContentParts(this._pairEls.aContentsBody, am.contents);
      }
      for (const tc of (am.tool_calls || [])) {
        if (this._pairEls) {
          this._pairEls.aToolCallsBody.appendChild(
            this._renderToolPart('tool-call', tc.name || '', tc.arguments || {}, false, tc.call_id)
          );
        }
      }
      this._refreshSchemaCounts();
    }
    this._currentPair = null;
    this._scrollBottom();
  }
};