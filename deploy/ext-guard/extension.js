// SPDX-License-Identifier: LicenseRef-PhysiCar-Community-1.0
// Copyright (c) 2026 AICASTLE Inc.
// Licensed under the PhysiCar Community License 1.0 (see LICENSE).
//
// PhysiCar Guard — keeps physicar.physicar-ext enabled. VS Code has no "cannot
// disable" flag: even a built-in shows a Disable button, and a student who clicks
// it loses the app.physicar panel, the chat agent and the startup layout. Built-ins
// cannot be uninstalled, so "absent from the registry" means "disabled": re-enable
// all, then reload once it is back. Ships only as a built-in (deploy/ext-guard ->
// lib/vscode/extensions), never published.
// The "disabled" list lives in the browser's IndexedDB (code-server does not move
// that state to disk), so nothing on the server can scrub it: this guard is the only
// recovery, and a browser in which the guard itself was disabled too stays that way
// (the workbench option `enabledExtensions` would make Disable impossible, but
// code-server does not expose it). Two rules keep the window from ever looping:
// never reload while physicar-ext is still absent (offline first boot, missing
// built-in, incompatible release), and the reload limiter is kept in globalState,
// which survives the very reload that would reset an in-memory one.
const vscode = require('vscode');

const TARGET = 'physicar.physicar-ext';
const CHECK_MS = 10000;
const SETTLE_MS = 3000; // time for the workbench to add the re-enabled extension to this host
const RELOAD_EVERY_MS = 120000;
const LAST_RELOAD_KEY = 'lastReloadAt';

const present = () => !!vscode.extensions.getExtension(TARGET);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const RELOAD_MAX_STREAK = 3;   // give up (until the next boot) after this many reloads without physicar-ext coming back
const STREAK_KEY = 'reloadStreak';

function activate(context) {
  let busy = false;
  if (present()) { context.globalState.update(STREAK_KEY, 0); }
  async function check() {
    if (busy) return;
    busy = true;
    try {
      // A Disable click only marks the extension in the workbench — the running
      // host keeps it loaded until a reload, so "present()" cannot see the click.
      // Re-enable every tick instead (a no-op when nothing is disabled): the mark
      // never survives long enough for a reload to apply it.
      for (const cmd of ['workbench.extensions.action.enableAll', 'workbench.extensions.action.enableAllWorkspace']) {
        try { await vscode.commands.executeCommand(cmd); } catch (_) { /* command missing in this build */ }
      }
      if (present()) { await context.globalState.update(STREAK_KEY, 0); return; }
      // Absent from the host: a reload already applied a Disable. It is re-enabled
      // now — reload so it loads again, rate-limited and capped so an extension
      // that truly cannot load (offline first boot, incompatible release) never loops.
      await sleep(SETTLE_MS);
      if (present()) { await context.globalState.update(STREAK_KEY, 0); return; }
      const now = Date.now();
      if (now - context.globalState.get(LAST_RELOAD_KEY, 0) < RELOAD_EVERY_MS) return;
      const streak = context.globalState.get(STREAK_KEY, 0);
      if (streak >= RELOAD_MAX_STREAK) return;
      await context.globalState.update(LAST_RELOAD_KEY, now);
      await context.globalState.update(STREAK_KEY, streak + 1);
      vscode.window.showInformationMessage('PhysiCar extension is required — it has been re-enabled. Reloading…');
      await sleep(2500);
      await vscode.commands.executeCommand('workbench.action.reloadWindow');
    } finally {
      busy = false;
    }
  }
  const timer = setInterval(() => check().catch(() => {}), CHECK_MS);
  context.subscriptions.push({ dispose: () => clearInterval(timer) });
  check().catch(() => {});
}

function deactivate() {}

module.exports = { activate, deactivate };
