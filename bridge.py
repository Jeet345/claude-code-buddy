#!/usr/bin/env python3
"""The one place the buddy is an input: answering permission prompts.

The `deck-buddy` mod asks here, over a Unix socket, whenever Claude Code's own
verdict on a tool call is `ask`:

    mod  POST /ask    {session_id, id, tool, input, reason, rule, agent}
         <- {id, decision: "hold" | "ask"}      at once, never blocks
    mod  POST /wait   {id}
         <- {id, decision: "allow" | "deny" | "ask", remember}
                                                 blocks until answered
    any  POST /answer {id, decision, remember?}  the buddy's buttons, the band
    any  POST /prefs  {approve?: bool}           <- {approve}
    any  POST /cue    {cue: "wave"}

"hold" means the buddy will show Allow / Deny and the terminal dialog waits.
"ask" means: show the normal terminal dialog. Every path that is not a click on
Allow or Deny ends in "ask" - the feature off, your terminal already focused,
the prompt released because you went back to the terminal, the mod's call
being abandoned. Fail open is the whole contract: the worst this module can do
is make you answer in the terminal after all.

Only the person at this desk can answer: the socket is created mode 0600 in a
directory only they can enter, and an answer must name the exact call id the
mod is waiting on, so approving one prompt can never approve another.

No GTK here. `Approvals` is plain state behind a lock, so the tests drive it
without a display; the daemon hands it a `focused(session_id)` probe and an
`on_change` callback, and is responsible for running both on its main loop.
"""

import json
import os
import socket
import socketserver
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler

ANSWERS = ("allow", "deny", "ask")
WAIT_MAX = 600          # secs a /wait may block; the mod gives up first anyway
BODY_MAX = 64 * 1024    # bytes; a request is a few hundred


class Request:
    """One held prompt. Everything but the id is for display."""

    __slots__ = ("id", "session_id", "tool", "input", "reason", "rule", "agent", "risk",
                 "since", "decision", "remember", "done")

    def __init__(self, d, now):
        self.id = str(d.get("id") or "")
        self.session_id = str(d.get("session_id") or "")
        self.tool = str(d.get("tool") or "")
        inp = d.get("input")
        self.input = {k: str(v) for k, v in inp.items()} if isinstance(inp, dict) else {}
        self.reason = str(d.get("reason") or "")
        self.rule = str(d.get("rule") or "")
        self.agent = str(d.get("agent") or "")
        self.risk = str(d.get("risk") or "")[:60]    # the mod's guard name for it, if any
        self.since = now
        self.decision = None
        self.remember = False
        self.done = threading.Event()

    @property
    def target(self):
        i = self.input
        return (i.get("command") or i.get("file_path") or i.get("url")
                or i.get("pattern") or i.get("description") or "")

    def __repr__(self):
        return f"<Request {self.id[:12]} {self.tool} {self.target[:30]!r}>"


class Approvals:
    """Held prompts, oldest first. Thread-safe; every method is short."""

    def __init__(self, enabled=False, focused=None, on_change=None, clock=time.time):
        self.enabled = bool(enabled)
        # focused(session_id) -> True / False / None. None is "cannot tell",
        # which on Wayland is every answer - and then holding is still right,
        # because the terminal shows a band with an "Answer here" button.
        self.focused = focused or (lambda _sid: None)
        self.on_change = on_change or (lambda: None)
        self.clock = clock
        self._lock = threading.Lock()
        self._held = {}

    # -- the mod's side -----------------------------------------------------
    def ask(self, d):
        """Hold this prompt, or send it straight to the terminal."""
        req = Request(d if isinstance(d, dict) else {}, self.clock())
        if not req.id:
            return {"id": "", "decision": "ask", "why": "no call id"}
        if not self.enabled:
            return {"id": req.id, "decision": "ask", "why": "approve is off"}
        try:
            focused = self.focused(req.session_id)
        except Exception:
            focused = None
        if focused is True:
            return {"id": req.id, "decision": "ask", "why": "terminal is focused"}
        with self._lock:
            self._held[req.id] = req
        self.on_change()
        why = "terminal not focused" if focused is False else "focus unknown"
        return {"id": req.id, "decision": "hold", "why": why}

    def wait(self, rid, timeout=WAIT_MAX):
        """Block until this prompt is answered. Unknown ids answer `ask`."""
        with self._lock:
            req = self._held.get(rid)
        if req is None:
            return {"id": rid, "decision": "ask", "remember": False}
        req.done.wait(timeout)
        with self._lock:
            self._held.pop(rid, None)
        decision = req.decision if req.decision in ANSWERS else "ask"
        self.on_change()
        return {"id": rid, "decision": decision,
                "remember": bool(req.remember and decision == "allow")}

    # -- the buddy's side ---------------------------------------------------
    def answer(self, rid, decision, remember=False):
        """Settle one prompt. False when there is no such prompt held."""
        if decision not in ANSWERS:
            return False
        with self._lock:
            req = self._held.get(rid)
            if req is None or req.done.is_set():
                return False
            req.decision = decision
            req.remember = bool(remember)
            req.done.set()
        self.on_change()
        return True

    def release(self, session_id=None):
        """Send held prompts back to the terminal: one session's, or all."""
        with self._lock:
            hit = [r for r in self._held.values()
                   if not r.done.is_set() and (session_id is None or r.session_id == session_id)]
        for r in hit:
            self.answer(r.id, "ask")
        return len(hit)

    def sync(self, session_id, pending_id):
        """A session file says which prompt (if any) is still open; drop the rest.

        This is what releases a prompt whose turn you interrupted in the
        terminal: the mod's call is abandoned, the session file stops naming it,
        and the buddy should stop offering a button for it.
        """
        with self._lock:
            stale = [r for r in self._held.values()
                     if r.session_id == session_id and r.id != pending_id
                     and not r.done.is_set() and self.clock() - r.since > 3.0]
        for r in stale:
            self.answer(r.id, "ask")

    def held(self):
        with self._lock:
            return sorted((r for r in self._held.values() if not r.done.is_set()),
                          key=lambda r: r.since)

    def current(self):
        held = self.held()
        return held[0] if held else None


# ------------------------------------------------------------------ the server
class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def _handler(bridge):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_a):          # the default wants a TCP address
            pass

        def _reply(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802 - http.server's spelling
            try:
                n = int(self.headers.get("content-length") or 0)
            except ValueError:
                n = 0
            if n > BODY_MAX:
                return self._reply(413, {"error": "too large"})
            try:
                d = json.loads(self.rfile.read(n) or b"{}") if n else {}
            except ValueError:
                return self._reply(400, {"error": "bad json"})
            if not isinstance(d, dict):
                return self._reply(400, {"error": "bad json"})
            try:
                out = bridge.route(self.path, d)
            except Exception as exc:          # never take the server down
                print(f"bridge: {self.path} failed: {exc}", flush=True)
                return self._reply(500, {"error": "failed"})
            if out is None:
                return self._reply(404, {"error": "no such route"})
            return self._reply(200, out)

        def do_GET(self):  # noqa: N802
            return self.do_POST()

    return Handler


class Bridge:
    """The socket. `prefs(d)` and `cue(d)` are the daemon's to supply."""

    def __init__(self, path, approvals, prefs=None, cue=None):
        self.path = str(path)
        self.approvals = approvals
        self.prefs = prefs or (lambda d: {"approve": approvals.enabled})
        self.cue = cue or (lambda d: {"ok": False})
        self._server = None

    def route(self, path, d):
        if path == "/ask":
            out = self.approvals.ask(d)
            print(f"ask {d.get('tool', '?')} {str(d.get('id', ''))[:16]} -> "
                  f"{out['decision']} ({out.get('why', '')})", flush=True)
            return out
        if path == "/wait":
            return self.approvals.wait(str(d.get("id") or ""))
        if path == "/answer":
            ok = self.approvals.answer(str(d.get("id") or ""), d.get("decision"),
                                       bool(d.get("remember")))
            return {"ok": ok}
        if path == "/prefs":
            return self.prefs(d)
        if path == "/cue":
            return self.cue(d)
        return None

    def start(self):
        """Bind and serve on a thread. Returns self, or None if it could not."""
        directory = os.path.dirname(self.path)
        try:
            os.makedirs(directory, mode=0o700, exist_ok=True)
            os.chmod(directory, 0o700)
        except OSError as exc:
            print(f"bridge: cannot prepare {directory}: {exc}", flush=True)
            return None
        if os.path.exists(self.path):
            if _alive(self.path):
                print(f"bridge: {self.path} is served by someone else", flush=True)
                return None
            try:
                os.unlink(self.path)
            except OSError:
                pass
        old = os.umask(0o177)                 # the socket is born 0600
        try:
            self._server = _Server(self.path, _handler(self))
        except OSError as exc:
            print(f"bridge: cannot bind {self.path}: {exc}", flush=True)
            return None
        finally:
            os.umask(old)
        try:
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        threading.Thread(target=self._server.serve_forever, name="bridge",
                         daemon=True).start()
        print(f"bridge: listening on {self.path}", flush=True)
        return self

    def stop(self):
        if self._server is not None:
            self.approvals.release()
            self._server.shutdown()
            self._server.server_close()
            self._server = None
            try:
                os.unlink(self.path)
            except OSError:
                pass


def _alive(path):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(path)
        return True
    except OSError:
        return False
    finally:
        s.close()
