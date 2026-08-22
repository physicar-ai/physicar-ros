"""Wake tickets for PhysiCar chat tools — one concept, in-memory, in-server.

A wake starts an automatic turn in the chat session that reserved it. The
ONLY primitive is the one-shot ticket:

    reserve(message)      -> wake_id   (binds to the calling chat session;
                                        the message says WHY it was reserved)
    redeem(wake_id, note) -> bool      (fires the wake; a second redeem is a
                                        no-op, so retries cannot spam the chat)

Redemption works from anywhere: in-process (`from pcwake import redeem`) or
over HTTP — POST http://localhost/wake/<id> (nginx proxies it here,
loopback-only). The AI reserves via the utils_wake_reserve tool; custom tool
code can reserve directly during a call and hand the id to its background
thread:

    def watch_lidar():
        \"\"\"Start watching; wakes the AI when something is closer than 0.5 m.\"\"\"
        from pcwake import reserve, redeem
        wid = reserve("Lidar: obstacle within 0.5 m")   # bound to THIS chat
        def run():
            while True:
                if _min_distance() < 0.5:
                    redeem(wid)
                    break
                time.sleep(0.5)
        threading.Thread(target=run, daemon=True).start()
        return "watching"

Pending wakes and tickets live in this process only — they vanish on server
restart, consistently with the background threads that would redeem them.
"""
import threading
import time


class _Session(threading.local):
    value = ""


_session = _Session()     # set per tool call by tools_server

_lock = threading.Condition()
_pending = {}             # session id -> [{"message", "at"}]
_MAX_PER_SESSION = 50

_tickets = {}             # wake_id -> {"session", "message", "created"}
_redeemed = {}   # wake_id -> redeemed_at — lets status() distinguish 'fired' from 'expired'
_TICKET_TTL = 24 * 3600
_MAX_TICKETS = 200


def _enqueue(session, message, wake_id=""):
    if not session:
        return False
    with _lock:
        q = _pending.setdefault(session, [])
        if len(q) >= _MAX_PER_SESSION:
            del q[0]   # oldest out — nothing may grow unbounded
        q.append({"message": str(message)[:2000], "wake_id": str(wake_id), "at": time.time()})
        _lock.notify_all()
    return True


def reserve(message, session=None):
    """Issue a one-shot wake ticket for the current (or given) chat session."""
    import uuid
    sid = session if session is not None else _session.value
    if not sid:
        return None
    now = time.time()
    with _lock:
        for k in [k for k, v in _tickets.items() if now - v["created"] > _TICKET_TTL]:
            _tickets.pop(k, None)
        while len(_tickets) >= _MAX_TICKETS:
            _tickets.pop(min(_tickets, key=lambda k: _tickets[k]["created"]), None)
        wid = uuid.uuid4().hex
        _tickets[wid] = {"session": sid, "message": str(message)[:500], "created": now}
    return wid


def redeem(wake_id, note=""):
    """Consume a ticket and wake its chat. False when unknown/expired/used."""
    wid = str(wake_id or "")
    with _lock:
        t = _tickets.pop(wid, None)
        if t:
            while len(_redeemed) >= _MAX_TICKETS:
                _redeemed.pop(min(_redeemed, key=_redeemed.get), None)
            _redeemed[wid] = time.time()
    if not t:
        return False
    msg = t["message"] + ((" — " + str(note)[:500]) if note else "")
    return _enqueue(t["session"], msg, wid)   # id included — the LLM knows which reservation fired


def status(wake_id):
    """One ticket's state: pending / redeemed / unknown."""
    wid = str(wake_id or "")
    now = time.time()
    with _lock:
        t = _tickets.get(wid)
        if t:
            return {"state": "pending", "message": t["message"],
                    "age_seconds": round(now - t["created"]),
                    "expires_in_seconds": round(_TICKET_TTL - (now - t["created"]))}
        r = _redeemed.get(wid)
        if r:
            return {"state": "redeemed", "redeemed_seconds_ago": round(now - r)}
    return {"state": "unknown", "hint": "expired, never existed, or the tool server restarted"}


def tickets(session=None):
    """Outstanding (un-redeemed) tickets of a chat session."""
    sid = session if session is not None else _session.value
    now = time.time()
    with _lock:
        return [{"wake_id": k, "message": v["message"], "age_seconds": round(now - v["created"])}
                for k, v in _tickets.items() if v["session"] == sid]


def drain(session, wait=0.0):
    """Take (and remove) pending wakes for a session, blocking up to `wait`
    seconds for the first one. Used by the server's long-poll endpoint."""
    deadline = time.time() + max(0.0, float(wait))
    with _lock:
        while True:
            q = _pending.pop(session, None)
            if q:
                return q
            remaining = deadline - time.time()
            if remaining <= 0:
                return []
            _lock.wait(timeout=min(remaining, 5.0))
