/* PhysiCar session helper — auto-injected by nginx into every /myapp/ HTML
 * page. Student pages need zero setup: just call `physicarSession.token()`.
 *
 * The source of truth is the "physicar_session" COOKIE (set by the App page
 * on Chat sign-in):
 *  - Cookies ride automatically on every same-origin request (pages, fetch,
 *    WebSocket handshakes), so a backend can simply read
 *    request.cookies["physicar_session"] — per browser = per user, so multiple
 *    users on the same robot never mix accounts.
 *  - This helper is for when PAGE JS needs the token (display, direct API
 *    calls, etc.).
 *
 * localStorage("physicar_session") is a fallback for JS inside the App page
 * iframe (same storage partition).
 */
(function () {
  var KEY = 'physicar_session';
  function fromCookie() {
    try {
      var m = document.cookie.match(/(?:^|;\s*)physicar_session=([^;]+)/);
      return m ? decodeURIComponent(m[1]) : null;
    } catch (e) { return null; }
  }
  window.physicarSession = {
    token: function () {
      var t = fromCookie();
      if (t) return t;
      try { return localStorage.getItem(KEY) || null; } catch (e) { return null; }
    },
    clear: function () {
      try {
        document.cookie = KEY + '=; Path=/; Max-Age=0'
          + (location.protocol === 'https:' ? '; SameSite=None; Secure' : '; SameSite=Lax');
      } catch (e) {}
      try { localStorage.removeItem(KEY); } catch (e) {}
    }
  };
})();
