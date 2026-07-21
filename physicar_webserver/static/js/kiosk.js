        let selectedSsid = '';
        let passwordVisible = false;
        let devicePassword = '';
        let modalPasswordVisible = false;
        let shiftActive = false;
        let shiftLocked = false;
        let symbolMode = false;
        let isConnecting = false;
        let selectedSecurity = '';
        let apPasswordRaw = '';
        let apPasswordVisible = false;
        // True when the password modal was opened by clicking an item in the
        // wifi list popup (vs. opened directly). Cancel should then re-open
        // the popup so the user can pick a different SSID.
        let _wifiModalFromPopup = false;

        // ─────────────────────────────────────────────────────────────────────
        // Confirm modal (replaces native confirm())
        // ─────────────────────────────────────────────────────────────────────
        function confirmModal(message, opts) {
          opts = opts || {};
          return new Promise((resolve) => {
            const overlay = document.createElement('div');
            overlay.className = 'kiosk-confirm-overlay';
            overlay.innerHTML = `<div class="kiosk-confirm">
              <div class="kiosk-confirm-body"></div>
              <div class="kiosk-confirm-actions">
                <button class="kiosk-confirm-btn cancel" data-act="cancel">${opts.cancelText || 'Cancel'}</button>
                <button class="kiosk-confirm-btn danger" data-act="ok">${opts.okText || 'OK'}</button>
              </div></div>`;
            overlay.querySelector('.kiosk-confirm-body').textContent = message;
            document.body.appendChild(overlay);
            function close(v) { overlay.remove(); document.removeEventListener('keydown', onKey); resolve(v); }
            function onKey(e) { if (e.key === 'Escape') close(false); else if (e.key === 'Enter') close(true); }
            overlay.addEventListener('click', (e) => {
              const t = e.target.closest('button[data-act]');
              if (t) close(t.dataset.act === 'ok');
              else if (e.target === overlay) close(false);
            });
            document.addEventListener('keydown', onKey);
          });
        }

        // ─────────────────────────────────────────────────────────────────────
        // Connect Spinner (shown during long-running wifi connect/activate)
        // ─────────────────────────────────────────────────────────────────────
        let _connectSpinnerSubTimer = null;
        function showConnectSpinner(msg, sub) {
            const ov = document.getElementById('connect-spinner');
            const m = document.getElementById('connect-spinner-msg');
            const s = document.getElementById('connect-spinner-sub');
            if (!ov) return;
            if (m) m.textContent = msg || 'Connecting…';
            if (s) s.textContent = sub || '';
            ov.classList.add('active');
            ov.setAttribute('aria-hidden', 'false');
            // After 8s, nudge the user that this can take a bit while wlan0 hops.
            if (_connectSpinnerSubTimer) clearTimeout(_connectSpinnerSubTimer);
            _connectSpinnerSubTimer = setTimeout(() => {
                if (s && ov.classList.contains('active') && !s.textContent) {
                    s.textContent = 'Still working… switching networks can take up to 30s.';
                }
            }, 8000);
        }
        function hideConnectSpinner() {
            const ov = document.getElementById('connect-spinner');
            if (!ov) return;
            ov.classList.remove('active');
            ov.setAttribute('aria-hidden', 'true');
            if (_connectSpinnerSubTimer) { clearTimeout(_connectSpinnerSubTimer); _connectSpinnerSubTimer = null; }
        }
        
        // ─────────────────────────────────────────────────────────────────────
        // Network Sub-tab Switching
        // ─────────────────────────────────────────────────────────────────────
        function switchNetTab(tab) {
            const panel = document.getElementById('panel-network');
            panel.querySelectorAll('.net-tab').forEach(t => t.classList.remove('active'));
            panel.querySelectorAll('.net-tab-panel').forEach(p => p.classList.remove('active'));
            panel.querySelector('.net-tab[data-nettab="' + tab + '"]').classList.add('active');
            document.getElementById('nettab-' + tab).classList.add('active');
            
            // Load data for the tab
            if (tab === 'internet') {
                loadNetworkInfo();
                loadInternetStatus();
            } else if (tab === 'ap') {
                loadApInfo();
            } else if (tab === 'bluetooth') {
                btRefresh();
            }
        }
        
        // ─────────────────────────────────────────────────────────────────────
        // Settings Sub-tab Switching
        // ─────────────────────────────────────────────────────────────────────
        function switchSetTab(tab) {
            const panel = document.getElementById('panel-settings');
            panel.querySelectorAll('.set-tab').forEach(t => t.classList.remove('active'));
            panel.querySelectorAll('.set-tab-panel').forEach(p => p.classList.remove('active'));
            panel.querySelector('[data-settab="' + tab + '"]').classList.add('active');
            document.getElementById('settab-' + tab).classList.add('active');
            
            if (tab === 'calibration') {
                loadCalibration();
            }

            // Open joy live SSE only while joystick tab is visible.
            const jp = ensureKioskJoyPanel();
            if (jp) {
                if (tab === 'joystick') jp.open();
                else jp.close();
            }
            if (tab !== 'joystick') closeKioskJoyModal();
        }

        // ─────────────────────────────────────────────────────────────────────
        // Joystick Settings (shared joy_settings.js module)
        // ─────────────────────────────────────────────────────────────────────
        let _kioskJoyPanel = null;
        function ensureKioskJoyPanel() {
            if (_kioskJoyPanel) return _kioskJoyPanel;
            if (typeof createJoyPanel !== 'function') return null;
            _kioskJoyPanel = createJoyPanel({
                liveId: 'kiosk-joy-live',
                mapId: 'kiosk-joy-map',
                tuneId: 'kiosk-joy-tune',
                enabledId: 'kiosk-joy-enabled',
                connectedId: 'kiosk-joy-conn',
            });
            return _kioskJoyPanel;
        }
        function kioskJoyToggleEnabled() {
            const jp = ensureKioskJoyPanel(); if (jp) jp.toggleEnabled();
        }
        function kioskJoyReset() {
            const jp = ensureKioskJoyPanel(); if (jp) jp.reset();
        }

        // Joystick sub-modals (Live Input / Mapping / Limits)
        function openKioskJoyModal(kind) {
            const id = { live: 'joy-live-modal', map: 'joy-map-modal', tune: 'joy-tune-modal' }[kind];
            if (!id) return;
            const m = document.getElementById(id);
            if (m) m.classList.add('active');
        }
        function closeKioskJoyModal() {
            ['joy-live-modal','joy-map-modal','joy-tune-modal'].forEach(id => {
                const m = document.getElementById(id); if (m) m.classList.remove('active');
            });
        }

        // Watch live-input DOM to derive Connected status (no extra polling).
        // (Now driven by /joy/state SSE via createJoyPanel({connectedId: ...});
        //  the MutationObserver below is unused but kept as a fallback if the
        //  panel is ever opened without connectedId.)
        let _kioskJoyConnObserver = null;
        function kioskJoyWatchConn(start) {
            const live = document.getElementById('kiosk-joy-live');
            const conn = document.getElementById('kiosk-joy-conn');
            if (!live || !conn) return;
            const update = () => {
                const empty = !!live.querySelector('.joy-live-empty');
                conn.textContent = empty ? 'No' : 'Yes';
                conn.style.color = empty ? '#888' : '#7cf57c';
            };
            if (start) {
                update();
                if (!_kioskJoyConnObserver) {
                    _kioskJoyConnObserver = new MutationObserver(update);
                }
                _kioskJoyConnObserver.observe(live, { childList: true, subtree: true });
            } else if (_kioskJoyConnObserver) {
                _kioskJoyConnObserver.disconnect();
            }
        }
        
        // ─────────────────────────────────────────────────────────────────────
        // AP Info
        // ─────────────────────────────────────────────────────────────────────
        async function loadApInfo() {
            try {
                const res = await fetch('/network/ap');
                const data = await res.json();
                document.getElementById('ap-ssid').textContent = data.ssid || '-';
                apPasswordRaw = data.password || '';
                document.getElementById('ap-password').textContent = apPasswordVisible ? apPasswordRaw : '••••••••';
                // ap-url is hard-coded to the fixed cert domain in the HTML
                // (https://device.physicar.ai) — don't overwrite it with the
                // mDNS name from the backend.
            } catch (e) {
                console.error('Failed to load AP info:', e);
            }
        }
        
        function toggleApPassword() {
            apPasswordVisible = !apPasswordVisible;
            document.getElementById('ap-password').textContent = apPasswordVisible ? apPasswordRaw : '••••••••';
        }
        
        // ─────────────────────────────────────────────────────────────────────
        // Internet Status
        // ─────────────────────────────────────────────────────────────────────
        async function loadInternetStatus() {
            try {
                const res = await fetch('/network/internet');
                const data = await res.json();
                applyInternetStatus(data);
            } catch (e) {
                console.error('Failed to load internet status:', e);
            }
        }
        
        function applyInternetStatus(data) {
            const el = document.getElementById('internet-status');
            if (!el) return;
            const online = !!(data && (data.wifi || data.ethernet));
            if (online) {
                const sources = [];
                if (data.wifi) sources.push('WiFi');
                if (data.ethernet) sources.push('LAN');
                el.textContent = 'Connected (' + sources.join(', ') + ')';
                el.className = 'net-value net-status online';
            } else {
                el.textContent = 'Disconnected';
                el.className = 'net-value net-status offline';
            }
        }
        
        // Toast notification
        function showToast(message, type) {
            const toast = document.getElementById('toast');
            toast.textContent = message;
            toast.className = 'toast ' + type + ' show';
            setTimeout(() => { toast.classList.remove('show'); }, 3000);
        }
        
        // Status message in modal
        function setStatus(message, type) {
            const status = document.getElementById('status-msg');
            status.className = 'status-msg ' + type;
            if (type === 'connecting') {
                status.innerHTML = '<div class="spinner"></div>' + message;
            } else {
                status.textContent = message;
            }
        }
        
        function clearStatus() {
            const status = document.getElementById('status-msg');
            status.className = 'status-msg';
            status.textContent = '';
        }
        
        // Keyboard layouts
        const layouts = {
            lower: [
                ['1','2','3','4','5','6','7','8','9','0'],
                ['q','w','e','r','t','y','u','i','o','p'],
                ['a','s','d','f','g','h','j','k','l'],
                ['⇧','z','x','c','v','b','n','m','⌫'],
                ['!@#','Space','Enter']
            ],
            upper: [
                ['1','2','3','4','5','6','7','8','9','0'],
                ['Q','W','E','R','T','Y','U','I','O','P'],
                ['A','S','D','F','G','H','J','K','L'],
                ['⇧','Z','X','C','V','B','N','M','⌫'],
                ['!@#','Space','Enter']
            ],
            symbol: [
                ['!','@','#','$','%','^','&','*','(',')'],
                ['-','_','=','+','[',']','{','}','|','\\'],
                ['`','~',';',':','\'','"',',','.','/','?'],
                ['<','>','⌫'],
                ['abc','Space','Enter']
            ]
        };
        
        // Active keyboard target — set when an input gains focus.
        // Default to wifi-password (the original WiFi modal behavior).
        let activeKbTarget = null;
        const KB_INPUT_IDS = ['wifi-password', 'wifi-identity', 'kiosk-pw-new'];
        const KB_RENDER_IDS = ['keyboard', 'keyboard-pw'];
        document.addEventListener('focusin', (e) => {
            if (e.target && KB_INPUT_IDS.includes(e.target.id)) {
                activeKbTarget = e.target;
            }
        });

        function renderKeyboard() {
            const layout = symbolMode ? layouts.symbol : (shiftActive ? layouts.upper : layouts.lower);

            // Per-keyboard layout adjustments.
            // Device password disallows spaces (host script strips whitespace),
            // so the password keyboard renders without a Space key.
            const layoutFor = (kbId) => {
                if (kbId !== 'keyboard-pw') return layout;
                return layout.map(row => row.filter(k => k !== 'Space'));
            };

            const buildHtml = (rows) => rows.map(row => {
                return '<div class="keyboard-row">' + row.map(key => {
                    let cls = 'key';
                    if (key === '⇧') {
                        cls += ' fn shift';
                        if (shiftLocked) cls += ' locked';
                        else if (shiftActive) cls += ' active';
                    }
                    else if (key === '⌫') cls += ' fn bksp';
                    else if (key === 'Space') cls += ' space';
                    else if (key === '!@#' || key === 'abc') cls += ' fn shift';
                    else if (key === 'Enter') cls += ' fn';

                    const display = key === 'Space' ? '␣' : (key === '⇧' && shiftLocked ? '⇪' : key);
                    return '<div class="' + cls + '" data-key="' + key + '">' + display + '</div>';
                }).join('') + '</div>';
            }).join('');

            KB_RENDER_IDS.forEach(id => {
                const kb = document.getElementById(id);
                if (!kb) return;
                kb.innerHTML = buildHtml(layoutFor(id));
                kb.querySelectorAll('.key').forEach(el => {
                    el.addEventListener('click', () => handleKey(el.dataset.key, kb));
                });
            });
        }

        function handleKey(key, kbEl) {
            if (isConnecting) return; // Block input while connecting

            // Pick the input bound to this keyboard.
            // - keyboard-pw → kiosk-pw-new
            // - keyboard    → focused wifi-identity / wifi-password (default password)
            let input;
            if (kbEl && kbEl.id === 'keyboard-pw') {
                input = document.getElementById('kiosk-pw-new');
            } else {
                const identityInput = document.getElementById('wifi-identity');
                const passwordInput = document.getElementById('wifi-password');
                input = (document.activeElement === identityInput) ? identityInput : passwordInput;
            }
            if (!input) return;
            let val = input.value;

            // Hide error message when input starts (WiFi modal status)
            if (typeof clearStatus === 'function') clearStatus();
            // pw status reset is handled by refreshKioskPwUi() at the
            // bottom of this function whenever the pw input changes.

            if (key === '⇧') {
                if (shiftLocked) {
                    shiftLocked = false;
                    shiftActive = false;
                } else if (shiftActive) {
                    shiftLocked = true;
                } else {
                    shiftActive = true;
                }
                renderKeyboard();
            } else if (key === '!@#') {
                symbolMode = true;
                renderKeyboard();
            } else if (key === 'abc') {
                symbolMode = false;
                renderKeyboard();
            } else if (key === '⌫') {
                input.value = val.slice(0, -1);
            } else if (key === 'Space') {
                // Device password disallows spaces (host script strips whitespace).
                if (kbEl && kbEl.id === 'keyboard-pw') return;
                input.value = val + ' ';
            } else if (key === 'Enter') {
                if (kbEl && kbEl.id === 'keyboard-pw') {
                    if (typeof changeDevicePassword === 'function') changeDevicePassword();
                } else {
                    connectWifi();
                }
            } else {
                input.value = val + key;
                if (shiftActive && !shiftLocked && !symbolMode) {
                    shiftActive = false;
                    renderKeyboard();
                }
            }
            // After any value change in the device-password input,
            // re-validate and gate the Change button.
            if (kbEl && kbEl.id === 'keyboard-pw' && typeof refreshKioskPwUi === 'function') {
                refreshKioskPwUi();
            }
        }
        
        // Navigation
        // App panels load their iframe only when clicked, and unload (about:blank)
        // when navigating away — keeps idle CPU/battery low and ensures
        // re-clicking an already-active app gives a clean reload.
        const iframeSrcs = {
            'display-vnc': '/vnc',
            'display-myapp': '/myapp',
        };

        // Monotonic token: any in-flight reload that doesn't match the
        // current token is dropped, so rapid clicks can't leave a hidden
        // iframe loaded in the background.
        let appNavToken = 0;

        function unloadAllAppIframes() {
            Object.keys(iframeSrcs).forEach(p => {
                const f = document.getElementById('iframe-' + p.replace('display-', ''));
                if (f && f.src && f.src !== 'about:blank') f.src = 'about:blank';
            });
        }

        document.querySelectorAll('.nav-btn[data-panel]').forEach(btn => {
            btn.onclick = () => {
                const panel = btn.dataset.panel;
                const wasActive = btn.classList.contains('active');
                const myToken = ++appNavToken;
                document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
                document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
                btn.classList.add('active');
                const panelId = 'panel-' + panel;
                document.getElementById(panelId).classList.add('active');

                if (iframeSrcs[panel]) {
                    // Clicked an Apps button: ensure all OTHER app iframes
                    // are unloaded, then (re)load just this one.
                    Object.keys(iframeSrcs).forEach(p => {
                        if (p === panel) return;
                        const f = document.getElementById('iframe-' + p.replace('display-', ''));
                        if (f && f.src && f.src !== 'about:blank') f.src = 'about:blank';
                    });
                    const frame = document.getElementById('iframe-' + panel.replace('display-', ''));
                    if (wasActive) {
                        // Re-click on already-active app → force refresh.
                        frame.src = 'about:blank';
                        setTimeout(() => {
                            if (myToken !== appNavToken) return;  // user navigated away
                            frame.src = iframeSrcs[panel];
                        }, 0);
                    } else {
                        frame.src = iframeSrcs[panel];
                    }
                } else {
                    // Non-app panel: free all app iframes.
                    unloadAllAppIframes();
                }

                if (panel === 'network') {
                    loadApInfo();
                    loadNetworkInfo();
                    loadInternetStatus();
                } else if (panel === 'settings') {
                    loadCalibration();
                }
            };
        });

        // Auto-refresh network every 30s
        setInterval(() => {
            const activePanel = document.querySelector('.nav-btn.active');
            if (activePanel && activePanel.dataset.panel === 'network') {
                loadApInfo();
                loadNetworkInfo();
                loadInternetStatus();
            }
        }, 30000);
        
        function toggleModalPassword() {
            modalPasswordVisible = !modalPasswordVisible;
            const input = document.getElementById('wifi-password');
            input.type = modalPasswordVisible ? 'text' : 'password';
        }

        // WiFi Popup
        let connectedSsid = '';

        function openWifiPopup() {
            document.getElementById('wifi-popup').classList.add('active');
            scanWifiPopup();
            updateWifiPopupCloseAffordance();
        }

        function closeWifiPopup() {
            document.getElementById('wifi-popup').classList.remove('active');
        }

        // Show the X button only when internet is reachable (via WiFi or
        // Ethernet); otherwise show a small "Skip" link.  This nudges the
        // user to actually connect when they have no internet path.
        async function updateWifiPopupCloseAffordance() {
            const xBtn = document.getElementById('wifi-popup-close');
            const skip = document.getElementById('wifi-popup-skip');
            if (!xBtn || !skip) return;
            let online = false;
            try {
                const r = await fetch('/network/internet', { cache: 'no-store' });
                if (r.ok) {
                    const d = await r.json();
                    online = !!(d && (d.wifi || d.ethernet));
                }
            } catch {}
            xBtn.style.display = online ? '' : 'none';
            skip.style.display = online ? 'none' : '';
        }

        async function scanWifiPopup() {
            const list = document.getElementById('wifi-popup-list');
            list.innerHTML = '<div class="wifi-popup-scanning">Scanning...</div>';

            try {
                const netRes = await fetch('/network/info');
                const netData = await netRes.json();
                connectedSsid = netData.wifi_ssid || '';
                document.getElementById('net-wifi-ssid').textContent = connectedSsid || 'Not connected';
                document.getElementById('net-wifi-ip').textContent = netData.wifi_ip || '-';
                document.getElementById('net-eth-ip').textContent = netData.eth_ip || '-';

                // nmcli rescans flake intermittently (radio busy / recent
                // rescan / etc.) → retry once on empty before showing the
                // user "No networks found".
                const res = await fetch('/network/wifi/scan');
                let networks = await res.json();
                if (!networks || networks.length === 0) {
                    await new Promise(r => setTimeout(r, 1500));
                    try {
                        const res2 = await fetch('/network/wifi/scan');
                        if (res2.ok) networks = await res2.json();
                    } catch {}
                }

                // Saved connections (for forget button)
                let saved = [];
                try {
                    const sres = await fetch('/network/wifi/saved');
                    if (sres.ok) saved = await sres.json();
                } catch {}
                const savedMap = new Map();
                saved.forEach(s => { savedMap.set(s.ssid, s); savedMap.set(s.name, s); });

                if (networks.length === 0) {
                    list.innerHTML = '<div class="wifi-popup-scanning">No networks found</div>';
                    return;
                }
                // Sort: currently connected first, then by signal strength.
                networks.sort((a, b) => {
                    const aCur = a.ssid === connectedSsid ? 0 : 1;
                    const bCur = b.ssid === connectedSsid ? 0 : 1;
                    if (aCur !== bCur) return aCur - bCur;
                    return b.signal - a.signal;
                });
                list.innerHTML = networks.slice(0, 10).map(n => {
                    const isConnected = n.ssid === connectedSsid;
                    const savedConn = savedMap.get(n.ssid);
                    const bars = n.signal > -50 ? 4 : n.signal > -60 ? 3 : n.signal > -70 ? 2 : 1;
                    const signalBars = [1,2,3,4].map(i =>
                        '<span style="height:' + (i*4) + 'px" class="' + (i <= bars ? '' : 'dim') + '"></span>'
                    ).join('');
                    const secIcon = n.security === 'open' ? '' :
                        n.security === 'enterprise' ? '<span class="security-icon" title="Enterprise">🏢</span>' :
                        '<span class="security-icon" title="Password required">🔒</span>';
                    const freqBadge = n.frequency > 5000 ? '<span style="color:#888;font-size:0.8rem;margin-right:8px;">5G</span>' : '';
                    const safeSsid = n.ssid.replace(/'/g, "\\'");
                    const forgetBtn = savedConn
                        ? '<button class="wifi-popup-forget" onclick="event.stopPropagation();forgetWifi(\'' + savedConn.name.replace(/'/g, "\\'") + '\', \'' + safeSsid + '\')" title="Forget saved network" aria-label="Forget"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-2 14a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2"/></svg></button>'
                        : '';
                    // Connected items: no click handler (already connected, nothing to do).
                    // Saved items: activate via stored credentials (no password prompt).
                    // Other items: open password modal as before.
                    let clickAttr;
                    if (isConnected) {
                        clickAttr = '';
                    } else if (savedConn) {
                        const safeName = savedConn.name.replace(/'/g, "\\'");
                        clickAttr = ' onclick="closeWifiPopup(); activateSavedWifi(\'' + safeName + '\', \'' + safeSsid + '\')"';
                    } else {
                        clickAttr = ' onclick="closeWifiPopup(); selectWifi(\'' + safeSsid + '\', \'' + n.security + '\')"';
                    }
                    return '<div class="wifi-popup-item' + (isConnected ? ' connected' : '') + (savedConn ? ' saved' : '') + '"' + clickAttr + '>'
                        + (isConnected ? '<span class="connected-icon">✔</span>' : '')
                        + '<span class="ssid">' + escapeHtml(n.ssid) + '</span>'
                        + secIcon + freqBadge
                        + '<div class="signal-bars">' + signalBars + '</div>'
                        + forgetBtn
                        + '</div>';
                }).join('');
            } catch (e) {
                list.innerHTML = '<div class="wifi-popup-scanning" style="color:#ff6666">Scan failed</div>';
            }
        }

        async function scanWifi() { await scanWifiPopup(); }

        // Activate a saved network via stored credentials. If NM rejects
        // (e.g. AP changed password), fall back to the password modal so the
        // user can retype.
        //
        // Note: switching wlan0 from one AP to another can drop the in-flight
        // HTTP fetch (Chromium's NetworkChangeNotifier kills the request) even
        // though the backend completes successfully. We fall back to polling
        // /network/info to confirm — same pattern as connectWifi().
        async function activateSavedWifi(name, ssid) {
            showConnectSpinner('Connecting to ' + ssid + '…');
            try {
                const res = await fetch('/network/wifi/activate', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name }),
                });
                if (res.ok) {
                    hideConnectSpinner();
                    showToast('Connected to ' + ssid, 'success');
                    loadNetworkInfo();
                    return;
                }
                if (res.status === 401) {
                    hideConnectSpinner();
                    // Stored secret invalid — open password modal as fallback.
                    let security = 'wpa';
                    try {
                        const sc = await fetch('/network/wifi/scan');
                        if (sc.ok) {
                            const list = await sc.json();
                            const hit = list.find(n => n.ssid === ssid);
                            if (hit) security = hit.security;
                        }
                    } catch {}
                    selectWifi(ssid, security);
                    return;
                }
                hideConnectSpinner();
                const d = await res.json().catch(() => ({}));
                showToast('Connect failed: ' + (d.detail || res.status), 'error');
            } catch (e) {
                // Verify the switch actually happened despite the fetch error.
                showConnectSpinner('Connecting to ' + ssid + '…', 'Verifying network change…');
                let verified = false;
                for (let i = 0; i < 6; i++) {
                    await new Promise(r => setTimeout(r, 1500));
                    try {
                        const info = await fetch('/network/info', { cache: 'no-store' }).then(r => r.json());
                        if (info && info.wifi_ssid === ssid) { verified = true; break; }
                    } catch {}
                }
                hideConnectSpinner();
                if (verified) {
                    showToast('Connected to ' + ssid, 'success');
                    loadNetworkInfo();
                } else {
                    showToast('Network error: ' + e.message, 'error');
                }
            }
        }

        async function forgetWifi(name, ssid) {
            // Close popup so user sees the action took effect, then reopen.
            closeWifiPopup();
            try {
                const res = await fetch('/network/wifi/saved/' + encodeURIComponent(name), { method: 'DELETE' });
                if (!res.ok) {
                    const d = await res.json().catch(() => ({}));
                    showToast('Forget failed: ' + (d.detail || res.status), 'error');
                } else {
                    showToast('Forgot ' + ssid, 'success');
                }
            } catch (e) {
                showToast('Network error: ' + e.message, 'error');
            }
            // Reopen with fresh list
            openWifiPopup();
        }

        function getSignalBars(signal) {
            const bars = signal > -50 ? 4 : signal > -60 ? 3 : signal > -70 ? 2 : 1;
            return [1,2,3,4].map(i =>
                '<span style="height:' + (i*5) + 'px;opacity:' + (i<=bars?1:0.3) + '"></span>'
            ).join('');
        }
        
        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }
        
        function selectWifi(ssid, security) {
            selectedSsid = ssid;
            selectedSecurity = security;
            // Track that we landed on the password modal from the popup so
            // Cancel returns the user to the network list, not all the way
            // back to the network settings screen.
            _wifiModalFromPopup = true;
            document.getElementById('modal-ssid').textContent = ssid;
            document.getElementById('wifi-password').value = '';
            document.getElementById('wifi-password').type = 'password';
            document.getElementById('wifi-identity').value = '';
            modalPasswordVisible = false;
            shiftActive = false;
            shiftLocked = false;
            symbolMode = false;
            isConnecting = false;
            document.getElementById('btn-show-modal-pw').className = 'btn-show-pw';
            document.getElementById('btn-connect').disabled = false;
            document.getElementById('btn-cancel').disabled = false;
            clearStatus();
            
            // Show/hide identity field for enterprise
            const identityWrapper = document.getElementById('identity-wrapper');
            if (security === 'enterprise') {
                identityWrapper.classList.remove('hidden');
            } else {
                identityWrapper.classList.add('hidden');
            }
            
            if (security === 'open') {
                connectWifiDirect(ssid, null);
            } else {
                document.getElementById('wifi-modal').classList.add('active');
                renderKeyboard();
                // Focus appropriate field
                if (security === 'enterprise') {
                    document.getElementById('wifi-identity').focus();
                } else {
                    document.getElementById('wifi-password').focus();
                }
            }
        }
        
        function closeModal() {
            if (isConnecting) return; // Don't close while connecting
            document.getElementById('wifi-modal').classList.remove('active');
            clearStatus();
            // If we came from the network list popup, reopen it so the user
            // can pick a different SSID without re-clicking "Change".
            if (_wifiModalFromPopup) {
                _wifiModalFromPopup = false;
                openWifiPopup();
            }
        }
        
        async function connectWifi() {
            const password = document.getElementById('wifi-password').value;
            const identity = document.getElementById('wifi-identity').value;
            
            if (!password && selectedSecurity !== 'open') {
                setStatus('Please enter the password', 'error');
                return;
            }
            if (selectedSecurity === 'enterprise' && !identity) {
                setStatus('Please enter username (ID)', 'error');
                return;
            }
            
            isConnecting = true;
            document.getElementById('btn-connect').disabled = true;
            document.getElementById('btn-cancel').disabled = true;
            setStatus('Connecting to ' + selectedSsid + '...', 'connecting');
            showConnectSpinner('Connecting to ' + selectedSsid + '…');
            
            try {
                const body = {ssid: selectedSsid, password: password || null};
                if (selectedSecurity === 'enterprise' && identity) {
                    body.identity = identity;
                }
                const res = await fetch('/network/wifi/connect', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(body)
                });
                const data = await res.json();
                
                isConnecting = false;
                document.getElementById('btn-connect').disabled = false;
                document.getElementById('btn-cancel').disabled = false;
                
                if (res.ok) {
                    hideConnectSpinner();
                    _wifiModalFromPopup = false;
                    closeModal();
                    showToast('Connected to ' + selectedSsid, 'success');
                    loadNetworkInfo();
                } else {
                    hideConnectSpinner();
                    // Parse error message
                    const errMsg = data.detail || 'Unknown error';
                    if (errMsg.includes('password') || errMsg.includes('auth') || errMsg.includes('Secrets')) {
                        setStatus('Wrong password. Please try again.', 'error');
                    } else if (errMsg.includes('timeout') || errMsg.includes('Timeout')) {
                        setStatus('Connection timeout. Try again.', 'error');
                    } else if (errMsg.includes('NetworkManager')) {
                        setStatus('WiFi service not available', 'error');
                    } else {
                        setStatus('' + errMsg, 'error');
                    }
                    document.getElementById('wifi-password').value = '';
                    document.getElementById('wifi-password').focus();
                }
            } catch (e) {
                // Browser fetches are killed by Chromium's NetworkChangeNotifier
                // when wlan0 transitions during nmcli connection up — we get
                // "Failed to fetch" / ERR_NETWORK_CHANGED even though the
                // backend completes the connection successfully.  Verify by
                // re-querying /network/info: if we're now associated with the
                // SSID we asked for, treat it as success instead of erroring.
                setStatus('Verifying connection...', 'connecting');
                showConnectSpinner('Connecting to ' + selectedSsid + '…', 'Verifying network change…');
                let verified = false;
                for (let i = 0; i < 6; i++) {
                    await new Promise(r => setTimeout(r, 1500));
                    try {
                        const info = await fetch('/network/info', { cache: 'no-store' }).then(r => r.json());
                        if (info && info.wifi_ssid === selectedSsid) { verified = true; break; }
                    } catch {}
                }
                isConnecting = false;
                document.getElementById('btn-connect').disabled = false;
                document.getElementById('btn-cancel').disabled = false;
                hideConnectSpinner();
                if (verified) {
                    _wifiModalFromPopup = false;
                    closeModal();
                    showToast('Connected to ' + selectedSsid, 'success');
                    loadNetworkInfo();
                } else {
                    setStatus('Network error: ' + e.message, 'error');
                }
            }
        }
        
        async function connectWifiDirect(ssid, password) {
            showConnectSpinner('Connecting to ' + ssid + '…');
            
            try {
                const res = await fetch('/network/wifi/connect', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ssid: ssid, password: password})
                });
                const data = await res.json();
                hideConnectSpinner();
                
                if (res.ok) {
                    showToast('Connected to ' + ssid, 'success');
                    loadNetworkInfo();
                } else {
                    showToast('Connection failed', 'error');
                }
            } catch (e) {
                // Same NetworkChangeNotifier issue as connectWifi() — the
                // fetch may die mid-flight while the backend completes.
                showConnectSpinner('Connecting to ' + ssid + '…', 'Verifying network change…');
                let verified = false;
                for (let i = 0; i < 6; i++) {
                    await new Promise(r => setTimeout(r, 1500));
                    try {
                        const info = await fetch('/network/info', { cache: 'no-store' }).then(r => r.json());
                        if (info && info.wifi_ssid === ssid) { verified = true; break; }
                    } catch {}
                }
                hideConnectSpinner();
                if (verified) {
                    showToast('Connected to ' + ssid, 'success');
                    loadNetworkInfo();
                } else {
                    showToast('Network error', 'error');
                }
            }
        }
        
        async function loadNetworkInfo() {
            try {
                const res = await fetch('/network/info');
                const data = await res.json();
                connectedSsid = data.wifi_ssid || '';
                document.getElementById('net-wifi-ssid').textContent = connectedSsid || 'Not connected';
                document.getElementById('net-wifi-ip').textContent = data.wifi_ip || '-';
                document.getElementById('net-eth-ip').textContent = data.eth_ip || '-';

                // Pane visibility: WiFi is "connected" as soon as we are associated
                // (have an SSID), regardless of internet reachability or DHCP state.
                // Internet reachability is shown separately in the Internet row.
                const wifiOn = !!connectedSsid;
                document.getElementById('wifi-pane-connected').style.display = wifiOn ? '' : 'none';
                document.getElementById('wifi-pane-disconnected').style.display = wifiOn ? 'none' : '';
                const ethOn = !!data.eth_ip;
                document.getElementById('eth-pane-connected').style.display = ethOn ? '' : 'none';
                document.getElementById('eth-pane-disconnected').style.display = ethOn ? 'none' : '';
            } catch (e) {
                console.error('Failed to load network info:', e);
            }
        }
        
        async function refreshNetwork() {
            const btn = document.querySelector('#panel-network .btn-refresh');
            btn.classList.add('spinning');
            
            await Promise.all([
                loadApInfo(),
                loadNetworkInfo(),
                loadInternetStatus(),
            ]);
            
            btn.classList.remove('spinning');
        }

        // Persistent SSE — same /network/stream as / so kiosk auto-syncs with
        // changes made from any other tab/device (and host events at the next
        // server poll, e.g. LAN cable plug/unplug).
        function applyNetworkSnapshot(snap) {
            if (!snap || typeof snap !== 'object') return;
            // info → reuse existing DOM updaters where possible
            if (snap.info) {
                const data = snap.info;
                connectedSsid = data.wifi_ssid || '';
                document.getElementById('net-wifi-ssid').textContent = connectedSsid || 'Not connected';
                document.getElementById('net-wifi-ip').textContent = data.wifi_ip || '-';
                document.getElementById('net-eth-ip').textContent = data.eth_ip || '-';
                // SSID alone => associated; IP/internet shown separately.
                const wifiOn = !!connectedSsid;
                document.getElementById('wifi-pane-connected').style.display = wifiOn ? '' : 'none';
                document.getElementById('wifi-pane-disconnected').style.display = wifiOn ? 'none' : '';
                const ethOn = !!data.eth_ip;
                document.getElementById('eth-pane-connected').style.display = ethOn ? '' : 'none';
                document.getElementById('eth-pane-disconnected').style.display = ethOn ? 'none' : '';
            }
            if (snap.internet) {
                applyInternetStatus(snap.internet);
            }
            if (snap.ap) {
                const data = snap.ap;
                document.getElementById('ap-ssid').textContent = data.ssid || '-';
                apPasswordRaw = data.password || '';
                document.getElementById('ap-password').textContent = apPasswordVisible ? apPasswordRaw : '••••••••';
                // ap-url stays hard-coded to https://device.physicar.ai in HTML.
            }
            if ('bt_status' in snap) btApplyStatus(snap.bt_status);
            if ('bt_devices' in snap) btApplyPaired(snap.bt_devices || []);
        }
        let _netES = null;
        function ensureNetworkStream() {
            if (_netES) return;
            try {
                _netES = new EventSource('/network/stream');
                _netES.onmessage = (e) => {
                    try { applyNetworkSnapshot(JSON.parse(e.data)); } catch {}
                };
            } catch {}
        }
        ensureNetworkStream();
        
        async function loadPassword() {
            try {
                const res = await fetch('/network/password');
                const data = await res.json();
                devicePassword = data.password;
            } catch (e) {
                devicePassword = 'Error';
            }
        }

        // ─────────────────────────────────────────────────────────────────────
        // Serial Number (S/N) — sha256(rpi-serial)[:16]
        // ─────────────────────────────────────────────────────────────────────
        let deviceSerial = '';
        let _kioskQrInst = null;
        async function loadSerial() {
            const valEl = document.getElementById('kiosk-sn-value');
            const qrEl = document.getElementById('kiosk-sn-qr');
            if (!valEl || !qrEl) return;
            if (!deviceSerial) {
                try {
                    const r = await fetch('/info');
                    const d = await r.json();
                    deviceSerial = d.serial || '';
                } catch { deviceSerial = ''; }
            }
            valEl.textContent = deviceSerial || '(unavailable)';
            if (deviceSerial && typeof QRCode !== 'undefined' && !qrEl.firstChild) {
                try {
                    _kioskQrInst = new QRCode(qrEl, {
                        text: deviceSerial,
                        width: 220,
                        height: 220,
                        colorDark: '#000000',
                        colorLight: '#ffffff',
                        correctLevel: QRCode.CorrectLevel.M,
                    });
                } catch (e) { qrEl.textContent = 'QR error'; }
            }
        }
        async function copySerialKiosk() { /* removed */ }
        window.copySerialKiosk = copySerialKiosk;
        
        document.getElementById('btn-toggle-pw').onclick = () => {
            passwordVisible = !passwordVisible;
            document.getElementById('password-value').textContent = 
                passwordVisible ? devicePassword : '••••••••';
            document.getElementById('btn-toggle-pw').setAttribute(
                'aria-label', passwordVisible ? 'Hide password' : 'Show password');
        };
        
        // ─────────────────────────────────────────────────────────────────────
        // Calibration
        // ─────────────────────────────────────────────────────────────────────
        let calibrationData = {
            steering_center: 0,
            pan_center: 0,
            tilt_center: 0,
            reverse_direction: false,
            speed_gain: 1.0
        };
        
        async function loadCalibration() {
            try {
                const res = await fetch('/kiosk/calibration');
                if (res.ok) {
                    const data = await res.json();
                    if (data.success) {
                        calibrationData = data;
                        updateCalibrationUI();
                    }
                }
            } catch (e) {
                console.error('Failed to load calibration:', e);
            }
        }

        // Real-time sync via SSE: when any client changes calibration,
        // every other tab receives the update within ~100ms.
        let calEvtSrc = null;
        function startCalibrationStream() {
            if (calEvtSrc) return;
            try {
                calEvtSrc = new EventSource('/kiosk/calibration?stream=true');
                calEvtSrc.onmessage = (e) => {
                    try {
                        const d = JSON.parse(e.data);
                        if (d && d.success) {
                            calibrationData = d;
                            updateCalibrationUI();
                        }
                    } catch {}
                };
            } catch {}
        }
        
        function updateCalibrationUI() {
            document.getElementById('cal-steering').textContent = `${calibrationData.steering_center}°`;
            document.getElementById('cal-pan').textContent = `${calibrationData.pan_center}°`;
            document.getElementById('cal-tilt').textContent = `${calibrationData.tilt_center}°`;
            
            // Reverse Direction
            document.getElementById('cal-reverse').checked = calibrationData.reverse_direction;
            // Speed Gain
            document.getElementById('cal-speed-gain').textContent = `${(calibrationData.speed_gain || 1.0).toFixed(1)}x`;
        }
        
        async function adjustCenter(channel, delta) {
            const key = `${channel}_center`;
            const newValue = Math.max(-15, Math.min(15, calibrationData[key] + delta));
            
            try {
                const res = await fetch('/kiosk/calibration/center', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({channel: channel, center_value: newValue})
                });
                if (res.ok) {
                    calibrationData[key] = newValue;
                    updateCalibrationUI();
                } else {
                    const err = await res.json();
                    showToast(err.detail || 'Failed', true);
                }
            } catch (e) {
                showToast('Error: ' + e.message, true);
            }
        }
        
        async function toggleReverse() {
            const newValue = document.getElementById('cal-reverse').checked;
            
            try {
                const res = await fetch('/kiosk/calibration/reverse', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({reverse_direction: newValue})
                });
                if (res.ok) {
                    calibrationData.reverse_direction = newValue;
                } else {
                    const err = await res.json();
                    showToast(err.detail || 'Failed', true);
                    document.getElementById('cal-reverse').checked = !newValue;
                }
            } catch (e) {
                showToast('Error: ' + e.message, true);
                document.getElementById('cal-reverse').checked = !newValue;
            }
        }

        async function adjustSpeedGain(delta) {
            const newValue = Math.round(Math.max(0.1, Math.min(5.0, (calibrationData.speed_gain || 1.0) + delta)) * 10) / 10;
            try {
                const res = await fetch('/kiosk/calibration/speed_gain', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({speed_gain: newValue})
                });
                if (res.ok) {
                    calibrationData.speed_gain = newValue;
                    updateCalibrationUI();
                } else {
                    const err = await res.json();
                    showToast(err.detail || 'Failed', true);
                }
            } catch (e) {
                showToast('Error: ' + e.message, true);
            }
        }

        // ─────────────────────────────────────────────────────────────────────
        // Cert Gate Overlay
        // ─────────────────────────────────────────────────────────────────────
        // Forces the operator onto WiFi when /etc/nginx/ssl/le.crt is missing
        // or expired (i.e. /run/physicar/le-cert-valid is absent on host).
        //
        // Lifecycle:
        //   1. On page load, query /network/cert-status ONCE to decide if we
        //      need to nag.  If valid → stop polling forever (no overhead).
        //   2. If invalid → open overlay and start light polling of
        //      /network/info.  Close as soon as wifi_ssid is non-empty.
        //      We do NOT wait for the cert install — the host watcher does
        //      that in the background; the user shouldn't be blocked on it.
        //   3. Once the overlay closes (skip OR auto), polling stops
        //      permanently.  A page reload re-evaluates from scratch.

        let _certGateTimer = null;
        let _certGateOpen = false;
        // Skip closes the overlay until the next page reload.  Stays sticky
        // even if cert flips valid→invalid mid-session — user explicitly
        // dismissed it, don't nag again.
        let _certGateSkipped = false;
        // Forced debug mode (FORCE_FOR_TESTING / ?cert_gate=force) — disables
        // the wifi-connected auto-close so the overlay stays put for styling.
        let _certGateForced = false;

        function certGateInit() {
            // One-shot cert validity check.  If invalid, opens overlay and
            // starts wifi polling.  If valid, does nothing further.
            certGateInitialCheck();
        }

        function certGateStop() {
            if (_certGateTimer) {
                clearInterval(_certGateTimer);
                _certGateTimer = null;
            }
        }

        async function certGateInitialCheck() {
            // Debug: ?cert_gate=force in URL forces the overlay open regardless
            // of /network/cert-status (so we can preview/style it without
            // actually breaking the cert).
            const forced = new URLSearchParams(location.search).get('cert_gate') === 'force';
            // Check cert validity AND current wifi association in parallel.
            // We only open the gate if the cert is bad AND wlan0 isn't
            // already associated to something — otherwise the overlay
            // would flash on screen and close itself within a few seconds,
            // which is jarring.
            let valid = true;
            let wifiConnected = false;
            try {
                const [certRes, infoRes] = await Promise.all([
                    fetch('/network/cert-status', { cache: 'no-store' }),
                    fetch('/network/info', { cache: 'no-store' }),
                ]);
                if (certRes.ok) {
                    const d = await certRes.json();
                    valid = !!d.valid;
                }
                if (infoRes.ok) {
                    const d = await infoRes.json();
                    wifiConnected = !!(d && d.wifi_ssid);
                }
            } catch {
                // Backend hiccup → assume OK rather than flashing the gate.
                return;
            }
            if (forced) {
                // Forced mode: skip the wifi-already-connected short-circuit
                // so we can actually see/debug the gate.  Also disable
                // auto-close so it stays open even after wifi connects.
                valid = false;
                wifiConnected = false;
                _certGateForced = true;
            }
            if (valid || wifiConnected) {
                // Either cert is good, or wifi is already up (host watcher
                // will refresh the cert in the background) — nothing to do.
                return;
            }
            certGateOpen();
        }

        async function certGateWifiCheck() {
            // Light polling target while overlay is open: just check whether
            // wlan0 is associated.  /network/info is cheap (no nsenter+openssl).
            if (_certGateForced) return;  // debug mode: never auto-close
            try {
                const r = await fetch('/network/info', { cache: 'no-store' });
                if (!r.ok) return;
                const d = await r.json();
                if (d && d.wifi_ssid) {
                    certGateClose(true);
                }
            } catch {
                // Transient error → next tick will retry.
            }
        }

        function certGateOpen() {
            // No intermediate "Internet Required" page: the cert is bad and
            // wlan0 isn't connected, so the user needs to pick a WiFi.  Jump
            // straight to the wifi popup.  certGateWifiCheck() polls
            // /network/info and closes things up once SSID is set.
            _certGateOpen = true;
            openWifiPopup();
            certGateStop();
            if (!_certGateForced) {
                _certGateTimer = setInterval(certGateWifiCheck, 3000);
            }
        }

        function certGateClose(autoFromConnect) {
            _certGateOpen = false;
            certGateStop();
            // If wifi popup is still open, dismiss it now that wlan0 is
            // associated. (User may have already navigated away — closeWifiPopup
            // is a no-op in that case.)
            if (autoFromConnect) {
                closeWifiPopup();
                showToast('WiFi connected', 'success');
            }
        }

        function certGateSkip() {
            // In-memory only: the gate stays closed for this page session;
            // a page reload brings it back if cert is still bad.
            _certGateSkipped = true;
            certGateClose(false);
        }

        async function certGateScan() {
            const list = document.getElementById('cert-gate-list');
            if (!list) return;
            list.innerHTML = '<div class="wifi-popup-scanning">Scanning…</div>';
            try {
                // Reuse network info (for connectedSsid) + scan endpoint.
                const netRes = await fetch('/network/info');
                const netData = netRes.ok ? await netRes.json() : {};
                const curSsid = netData.wifi_ssid || '';

                const res = await fetch('/network/wifi/scan');
                let networks = res.ok ? await res.json() : [];
                // Retry once on empty (nmcli rescan flakes intermittently).
                if (!networks.length) {
                    await new Promise(r => setTimeout(r, 1500));
                    try {
                        const res2 = await fetch('/network/wifi/scan');
                        if (res2.ok) networks = await res2.json();
                    } catch {}
                }

                // Saved connections (for activate-without-password + forget btn)
                let saved = [];
                try {
                    const sres = await fetch('/network/wifi/saved');
                    if (sres.ok) saved = await sres.json();
                } catch {}
                const savedMap = new Map();
                saved.forEach(s => { savedMap.set(s.ssid, s); savedMap.set(s.name, s); });

                if (!networks.length) {
                    list.innerHTML = '<div class="wifi-popup-scanning">No networks found</div>';
                    return;
                }
                // Sort: currently connected first, then by signal strength.
                networks.sort((a, b) => {
                    const aCur = a.ssid === curSsid ? 0 : 1;
                    const bCur = b.ssid === curSsid ? 0 : 1;
                    if (aCur !== bCur) return aCur - bCur;
                    return b.signal - a.signal;
                });
                list.innerHTML = networks.slice(0, 10).map(n => {
                    const isConnected = n.ssid === curSsid;
                    const savedConn = savedMap.get(n.ssid);
                    const bars = n.signal > -50 ? 4 : n.signal > -60 ? 3 : n.signal > -70 ? 2 : 1;
                    const signalBars = [1,2,3,4].map(i =>
                        '<span style="height:' + (i*4) + 'px" class="' + (i <= bars ? '' : 'dim') + '"></span>'
                    ).join('');
                    const secIcon = n.security === 'open' ? '' :
                        n.security === 'enterprise' ? '<span class="security-icon" title="Enterprise">🏢</span>' :
                        '<span class="security-icon" title="Password required">🔒</span>';
                    const freqBadge = n.frequency > 5000 ? '<span style="color:#888;font-size:0.8rem;margin-right:8px;">5G</span>' : '';
                    const safeSsid = n.ssid.replace(/'/g, "\\'");
                    const forgetBtn = savedConn
                        ? '<button class="wifi-popup-forget" onclick="event.stopPropagation();forgetWifi(\'' + savedConn.name.replace(/'/g, "\\'") + '\', \'' + safeSsid + '\')" title="Forget saved network" aria-label="Forget"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-2 14a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2"/></svg></button>'
                        : '';
                    // Connected: no click. Saved: activate via stored creds. Other: password modal.
                    let clickAttr;
                    if (isConnected) {
                        clickAttr = '';
                    } else if (savedConn) {
                        const safeName = savedConn.name.replace(/'/g, "\\'");
                        clickAttr = ' onclick="certGatePickSaved(\'' + safeName + '\',\'' + safeSsid + '\')"';
                    } else {
                        clickAttr = ' onclick="certGatePick(\'' + safeSsid + '\',\'' + n.security + '\')"';
                    }
                    return '<div class="wifi-popup-item' + (isConnected ? ' connected' : '') + (savedConn ? ' saved' : '') + '"' + clickAttr + '>'
                        + (isConnected ? '<span class="connected-icon">✔</span>' : '')
                        + '<span class="ssid">' + escapeHtml(n.ssid) + '</span>'
                        + secIcon + freqBadge
                        + '<div class="signal-bars">' + signalBars + '</div>'
                        + forgetBtn
                        + '</div>';
                }).join('');
            } catch (e) {
                list.innerHTML = '<div class="wifi-popup-scanning" style="color:#ff6666">Scan failed</div>';
            }
        }

        function certGatePick(ssid, security) {
            // Hand off to the existing connect flow.  selectWifi() opens the
            // password modal (z-index higher than the gate); when wlan0 is
            // associated, certGateWifiCheck() picks it up and closes the gate.
            selectWifi(ssid, security);
        }

        // Saved network from the cert gate — activate with stored credentials
        // (no password modal).  Falls back to the password modal on 401.
        async function certGatePickSaved(name, ssid) {
            await activateSavedWifi(name, ssid);
            // Refresh the gate list so the picked one shows as connected.
            // (certGateWifiCheck will close the gate within ~3s of the SSID
            // showing up in /network/info.)
            if (_certGateOpen) certGateScan();
        }

        // Init
        loadApInfo();
        loadNetworkInfo();
        loadInternetStatus();
        loadPassword();
        loadCalibration();
        startCalibrationStream();
        loadSerial();
        startBatteryMonitor();
        renderKeyboard();
        certGateInit();

        // Hide boot splash overlay once UI has had a moment to render.
        // Browser keeps the splash visible across chromium's first-paint gap
        // so the host-side feh splash transitions seamlessly into this one.
        requestAnimationFrame(() => {
            requestAnimationFrame(() => {
                setTimeout(() => {
                    const s = document.getElementById('boot-splash');
                    if (s) {
                        s.classList.add('hidden');
                        setTimeout(() => s.remove(), 400);
                    }
                }, 400);
            });
        });
        
        // ─────────────────────────────────────────────────────────────────────
        // Battery Monitor
        // ─────────────────────────────────────────────────────────────────────
        function startBatteryMonitor() {
            updateBattery();
            setInterval(updateBattery, 5000);  // Update every 5 seconds
        }
        
        async function updateBattery() {
            try {
                const res = await fetch('/battery');
                if (!res.ok) return;
                const data = await res.json();
                
                const voltage = data.voltage || 0;
                const percent = (data.percentage != null) ? data.percentage : 0;
                
                // Update icon
                const icon = document.getElementById('battery-icon');
                if (data.charging) {
                    icon.textContent = '';
                } else if (percent > 60) {
                    icon.textContent = '';
                } else if (percent > 20) {
                    icon.textContent = '';
                } else {
                    icon.textContent = '';
                }
                
                // Show percent / voltage
                document.getElementById('battery-percent').textContent = percent + '%';
                document.getElementById('battery-volt').textContent = voltage.toFixed(1) + 'V';
                
                // Update gauge bar
                const fill = document.getElementById('battery-fill');
                fill.style.width = percent + '%';
                fill.classList.remove('high', 'medium', 'low');
                if (percent > 60) {
                    fill.classList.add('high');
                } else if (percent > 20) {
                    fill.classList.add('medium');
                } else {
                    fill.classList.add('low');
                }
            } catch (e) {
                // ignore
            }
        }
        
        // Touch/Mouse cursor handling
        let lastTouchTime = 0;
        document.addEventListener('touchstart', () => {
            lastTouchTime = Date.now();
            document.body.classList.add('touch-mode');
        }, {passive: true});
        document.addEventListener('mousemove', () => {
            if (Date.now() - lastTouchTime > 500) {
                document.body.classList.remove('touch-mode');
            }
        });

        // ─────────────────────────────────────────────────────────────────────
        // Change Device Password (kiosk)
        // ─────────────────────────────────────────────────────────────────────
        let kioskMode = 'real';
        (async function detectMode() {
            try {
                const r = await fetch('/info');
                const d = await r.json();
                kioskMode = d.mode || 'real';
                if (kioskMode === 'sim') {
                    document.title = 'PHYSICAR DEVICE (SIM)';
                    const btn = document.getElementById('btn-pw-change');
                    if (btn) {
                        btn.disabled = true;
                        btn.textContent = 'Not supported in simulation mode';
                    }
                    const resetBtn = document.getElementById('btn-pw-reset');
                    if (resetBtn) {
                        resetBtn.disabled = true;
                        resetBtn.textContent = 'Not supported in simulation mode';
                    }
                }
            } catch {}
        })();

        let kioskPwVisible = false;
        function toggleKioskPwVisible() {
            kioskPwVisible = !kioskPwVisible;
            const input = document.getElementById('kiosk-pw-new');
            if (input) input.type = kioskPwVisible ? 'text' : 'password';
        }
        function kioskPwFocus(input) {
            // Track for the on-screen keyboard.
            activeKbTarget = input;
        }
        function kioskPwSanitize(input) {
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
            refreshKioskPwUi();
        }

        // Returns {ok, message}.  Mirrors backend validation exactly.
        function validateKioskPw(pw) {
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

        function refreshKioskPwUi() {
            const input = document.getElementById('kiosk-pw-new');
            const btn = document.getElementById('btn-pw-change');
            const status = document.getElementById('kiosk-pw-status');
            if (!input) return;
            const isSim = kioskMode === 'sim';
            const v = validateKioskPw(input.value);
            if (status) {
                if (!input.value) {
                    status.textContent = '';
                    status.className = 'status-msg';
                } else if (!v.ok) {
                    status.textContent = v.message;
                    status.className = 'status-msg err';
                } else {
                    status.textContent = '';
                    status.className = 'status-msg';
                }
            }
            if (btn && !isSim) btn.disabled = !v.ok;
        }

        function openPasswordModal() {
            if (kioskMode === 'sim') {
                if (typeof showToast === 'function') showToast('Not supported in simulation mode', true);
                return;
            }
            const input = document.getElementById('kiosk-pw-new');
            const status = document.getElementById('kiosk-pw-status');
            const btn = document.getElementById('btn-pw-change');
            if (input) { input.value = ''; input.type = 'password'; }
            kioskPwVisible = false;
            if (status) { status.textContent = ''; status.className = 'status-msg'; }
            if (btn) btn.disabled = true;  // empty input → invalid → disabled
            document.getElementById('password-modal').classList.add('active');
            renderKeyboard();
            if (input) {
                input.focus();
                activeKbTarget = input;
            }
            refreshKioskPwUi();
        }
        function closePasswordModal() {
            document.getElementById('password-modal').classList.remove('active');
        }

        async function changeDevicePassword() {
            if (kioskMode === 'sim') {
                if (typeof showToast === 'function') showToast('Not supported in simulation mode', true);
                return;
            }
            const np = document.getElementById('kiosk-pw-new').value;
            const status = document.getElementById('kiosk-pw-status');
            const btn = document.getElementById('btn-pw-change');
            function setMsg(msg, kind) {
                status.textContent = msg || '';
                status.className = 'status-msg' + (msg ? ' ' + (kind || '') : '');
            }
            const v = validateKioskPw(np);
            if (!v.ok) { setMsg(v.message, 'err'); return; }
            if (!await confirmModal('This will reboot the device. Continue?')) return;
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
        async function resetDevicePassword() {
            if (kioskMode === 'sim') {
                if (typeof showToast === 'function') showToast('Not supported in simulation mode', true);
                return;
            }
            if (!await confirmModal('Reset the device password to its default and reboot now?\nAll active sessions will be logged out.')) return;
            const btn = document.getElementById('btn-pw-reset');
            if (btn) btn.disabled = true;
            if (typeof showToast === 'function') showToast('Resetting password and rebooting…');
            try {
                const res = await fetch('/auth/password', { method: 'DELETE' });
                const d = await res.json().catch(() => ({}));
                if (!res.ok) {
                    if (typeof showToast === 'function') showToast(d.detail || 'Failed to reset password.', true);
                    if (btn) btn.disabled = false;
                    return;
                }
                if (typeof showToast === 'function') showToast('Password reset. Rebooting…');
            } catch (e) {
                if (typeof showToast === 'function') showToast('Network error: ' + e.message, true);
                if (btn) btn.disabled = false;
            }
        }

        // Expose for inline onclick
        window.changeDevicePassword = changeDevicePassword;
        window.resetDevicePassword = resetDevicePassword;
        window.toggleKioskPwVisible = toggleKioskPwVisible;
        window.kioskPwFocus = kioskPwFocus;
        window.kioskPwSanitize = kioskPwSanitize;
        window.openPasswordModal = openPasswordModal;
        window.closePasswordModal = closePasswordModal;        // ─────────────────────────────────────────────────────────────────────
        // Bluetooth tab (touchscreen variant)
        // Main tab: connected devices only + Manage button.
        // Manage popup: paired list + scan list (mirrors WiFi popup pattern).
        // ─────────────────────────────────────────────────────────────────────
        let btScanning = false;
        let btBusy = false;
        let btLastScan = [];
        let btPairedList = [];
        let btManageOpen = false;
        let _btScanInterval = null;

        function btIcon(d) {
            const i = (d.icon || '').toLowerCase();
            if (i.includes('audio') || i.includes('headset') || i.includes('headphone')) return '🎧';
            if (i.includes('input-keyboard')) return '⌨️';
            if (i.includes('input-mouse')) return '🖱️';
            if (i.includes('input-gaming') || i.includes('gamepad')) return '🎮';
            if (i.includes('phone')) return '📱';
            if (i.includes('computer')) return '💻';
            return '🔵';
        }

        function btEsc(s) {
            const d = document.createElement('div'); d.textContent = s; return d.innerHTML;
        }

        // Row used in the main BT panel: paired devices, action by connection state.
        function btMainRow(d) {
            const tag = d.connected
                ? '<span class="bt-tag ok">Connected</span>'
                : '<span class="bt-tag muted">Not connected</span>';
            const action = d.connected
                ? `<button class="bt-act" data-act="disconnect" data-mac="${btEsc(d.mac)}">Disconnect</button>`
                : `<button class="bt-act danger" data-act="remove" data-mac="${btEsc(d.mac)}">Forget</button>`;
            const rowCls = d.connected ? 'bt-item' : 'bt-item bt-item-offline';
            const name = btEsc(d.name || d.mac);
            return `<div class="${rowCls}">
                <span class="bt-icon">${btIcon(d)}</span>
                <span class="bt-name">${name}${tag}</span>
                <span class="bt-actions">${action}</span>
            </div>`;
        }

        // Row used in the Add Device modal: scan results only.
        function btDeviceRow(d) {
            const name = btEsc(d.name || d.mac);
            return `<div class="bt-item">
                <span class="bt-icon">${btIcon(d)}</span>
                <span class="bt-name">${name}</span>
                <span class="bt-actions">
                    <button class="bt-act primary" data-act="pair" data-mac="${btEsc(d.mac)}">Pair</button>
                </span>
            </div>`;
        }

        function btApplyStatus(s) {
            // Status element removed from UI; kept as no-op.
            void s;
        }

        // Main BT tab: show all paired devices (connected first).
        function btApplyPaired(list) {
            btPairedList = (list || []).filter(d => d.paired);
            const c = document.getElementById('bt-paired-main');
            if (c) {
                if (!btPairedList.length) {
                    c.innerHTML = '<div class="bt-empty">No paired devices</div>';
                } else {
                    const sorted = btPairedList.slice().sort((a, b) => (b.connected ? 1 : 0) - (a.connected ? 1 : 0));
                    c.innerHTML = sorted.map(d => btMainRow(d)).join('');
                    btBindActions(c);
                }
            }
            if (btManageOpen) btRenderManage();
        }

        function btRenderManage() {
            const sc = document.getElementById('bt-scan-list');
            if (sc) {
                const macs = new Set(btPairedList.map(d => d.mac));
                const list = btLastScan.filter(d => !macs.has(d.mac));
                if (!list.length) {
                    sc.innerHTML = btScanning
                        ? '<div class="bt-empty">Scanning…</div>'
                        : '<div class="bt-empty">No new devices</div>';
                } else {
                    sc.innerHTML = list.map(d => btDeviceRow(d)).join('');
                    btBindActions(sc);
                }
            }
        }

        function btBindActions(container) {
            container.querySelectorAll('.bt-act').forEach(b => {
                b.onclick = () => btAction(b.dataset.act, b.dataset.mac, b);
            });
        }

        async function btAction(act, mac, btn) {
            if (btBusy) return;
            btBusy = true;
            const orig = btn.textContent;
            btn.disabled = true;
            btn.textContent = '…';
            try {
                const res = await fetch('/network/bluetooth/' + act, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ mac }),
                });
                if (!res.ok) {
                    const d = await res.json().catch(() => ({}));
                    showToast(act + ' failed: ' + (d.detail || res.status), 'error');
                } else {
                    showToast(act + ' ok', 'success');
                }
            } catch (e) {
                showToast('Network error: ' + e.message, 'error');
            } finally {
                btBusy = false;
                btn.textContent = orig;
                btn.disabled = false;
                btRefresh();
            }
        }

        async function btRefresh() {
            try {
                const [s, devs] = await Promise.all([
                    fetch('/network/bluetooth/status').then(r => r.ok ? r.json() : null),
                    fetch('/network/bluetooth/devices').then(r => r.ok ? r.json() : []),
                ]);
                btApplyStatus(s);
                btApplyPaired(devs);
            } catch {}
        }

        async function btScan() {
            if (btScanning) return;
            btScanning = true;
            const btn = document.getElementById('bt-rescan');
            if (btn) btn.classList.add('spinning');
            if (btManageOpen) btRenderManage();
            try {
                const res = await fetch('/network/bluetooth/scan?seconds=8');
                if (res.ok) btLastScan = await res.json();
            } catch (e) { /* keep prior list */ }
            finally {
                btScanning = false;
                if (btn) btn.classList.remove('spinning');
                if (btManageOpen) btRenderManage();
                btRefresh();
            }
        }

        function btOpenManage() {
            btManageOpen = true;
            const ov = document.getElementById('bt-manage-popup');
            if (ov) ov.classList.add('active');
            btRenderManage();
            btScan();
            if (_btScanInterval) clearInterval(_btScanInterval);
            _btScanInterval = setInterval(() => { if (btManageOpen && !btScanning) btScan(); }, 12000);
        }

        function btCloseManage() {
            btManageOpen = false;
            const ov = document.getElementById('bt-manage-popup');
            if (ov) ov.classList.remove('active');
            if (_btScanInterval) { clearInterval(_btScanInterval); _btScanInterval = null; }
        }

        window.btScan = btScan;
        window.btRefresh = btRefresh;
        window.btOpenManage = btOpenManage;
        window.btCloseManage = btCloseManage;
