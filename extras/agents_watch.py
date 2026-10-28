#!/usr/bin/env python3
"""Serve a live summary of local Claude Code sessions and subagents for e-ink displays (apps/agents).

Reads, never writes, the transcripts Claude Code keeps in ~/.claude/projects:
  <project>/<session>.jsonl                      the session, appended as it works
  <project>/<session>/custom-title.json          its title as the app shows it
  <project>/<session>/subagents/agent-*.json(l)  its subagents (meta: type, description)
and serves GET /agents.json on the LAN, advertised over Bonjour as _cpagents._tcp so boards find it by
name. With --usb it also writes the same snapshot, one JSON line per change plus a keep-alive, to agents
displays plugged into this computer (CircuitPython's second USB serial channel; see usblink.py in
apps/agents). No Claude Code settings or hooks are involved.

  python3 extras/agents_watch.py [--port 8766] [--hours 3] [--usb [--no-http]] [--no-advertise]

Set AGENTS_TOKEN in the environment to require it from HTTP clients (Authorization: Bearer <token>).

States: "question" (an AskUserQuestion or plan approval is open), "your-turn" (the session finished its
turn), "working", "busy" (a tool call has had no result for a while: a long command, or a permission
prompt; transcripts can't tell those apart), "idle".
"""
import argparse
import glob
import hmac
import json
import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECTS = Path(os.environ.get("CLAUDE_PROJECTS", Path.home() / ".claude" / "projects"))
TAIL_BYTES = 512 * 1024   # transcript lines can be large; the state is in the last few entries
WORKING_SECS = 30         # file written this recently: working
BUSY_SECS = 20            # a tool call open this long without output: maybe waiting for a permission
ASKING_TOOLS = {"AskUserQuestion": "question", "ExitPlanMode": "plan approval"}
ORDER = {"question": 0, "your-turn": 1, "busy": 2, "working": 3, "idle": 4}


def tail_entries(path, size=TAIL_BYTES):
    """JSON entries from the end of a .jsonl file (the first, partial line is dropped)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            n = f.tell()
            f.seek(max(0, n - size))
            data = f.read()
    except OSError:
        return []
    lines = data.split(b"\n")
    if n > size:
        lines = lines[1:]
    out = []
    for line in lines:
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def conversation_state(entries):
    """(state, detail) from the last user/assistant messages of a transcript."""
    pending, last = {}, None
    for e in entries:
        if e.get("type") not in ("user", "assistant"):
            continue
        msg = e.get("message") or {}
        content = msg.get("content")
        if e["type"] == "assistant" and isinstance(content, list):
            for c in content:
                if c.get("type") == "tool_use":
                    pending[c.get("id")] = c.get("name", "")
            last = ("assistant", msg.get("stop_reason"))
        elif e["type"] == "user":
            if isinstance(content, list):
                for c in content:
                    if c.get("type") == "tool_result":
                        pending.pop(c.get("tool_use_id"), None)
                if not any(c.get("type") == "tool_result" for c in content):
                    pending.clear()
            else:
                pending.clear()
            last = ("user", None)
    asking = [ASKING_TOOLS[n] for n in pending.values() if n in ASKING_TOOLS]
    if asking:
        return "question", asking[0]
    if pending:
        return "tool", sorted(set(pending.values()))[0]
    if last == ("assistant", "end_turn"):
        return "your-turn", ""
    return "other", ""


def title_of(session_dir, entries, project_dir):
    try:
        t = json.loads((session_dir / "custom-title.json").read_text()).get("customTitle")
        if t:
            return t
    except (OSError, ValueError):
        pass
    for e in reversed(entries):
        for key in ("customTitle", "agentName"):
            if e.get(key):
                return e[key]
    return project_dir.name.rsplit("-", 1)[-1] or project_dir.name


def subagents(session_dir, now):
    out = []
    for meta in glob.glob(str(session_dir / "subagents" / "agent-*.meta.json")):
        log = meta[: -len(".meta.json")] + ".jsonl"
        try:
            age = now - os.path.getmtime(log)
            info = json.loads(Path(meta).read_text())
        except (OSError, ValueError):
            continue
        state, _ = conversation_state(tail_entries(log, 64 * 1024))
        if state != "your-turn" and age < 120:  # still running
            out.append({"type": info.get("agentType", "agent"), "desc": info.get("description", "")})
    return out


def snapshot(hours):
    now = time.time()
    sessions = []
    for path in glob.glob(str(PROJECTS / "*" / "*.jsonl")):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if now - mtime > hours * 3600:
            continue
        p = Path(path)
        session_dir = p.with_suffix("")
        entries = tail_entries(p)
        conv, detail = conversation_state(entries)
        age = now - mtime
        if conv == "question":
            state = "question"
        elif conv == "your-turn":
            state = "your-turn"
        elif conv == "tool":
            state = "busy" if age > BUSY_SECS else "working"
        else:
            state = "working" if age < WORKING_SECS else "idle"
        subs = subagents(session_dir, now)
        if subs and state in ("busy", "idle"):
            state = "working"  # waiting on its own subagents, not on you
        sessions.append({"id": p.stem, "title": title_of(session_dir, entries, p.parent), "state": state,
                         "detail": detail, "since": int(mtime), "subagents": subs})
    sessions.sort(key=lambda s: (ORDER[s["state"]], -s["since"]))
    return {"generated": int(now), "clock": time.strftime("%H:%M"), "host": socket.gethostname(),
            "waiting": sum(s["state"] in ("question", "your-turn") for s in sessions), "sessions": sessions}


# ---- USB: CircuitPython's data channel ---------------------------------------------------------------

PROBE = b"?cpagents\n"  # the board's side is apps/agents/usblink.py
HELLO = b"!cpagents"
PORT_GLOBS = ("/dev/cu.usbmodem*",)  # macOS
LINUX_BY_ID = "/dev/serial/by-id/*-if0[1-9]*"  # Linux names interfaces; the console is if00


def data_ports(ports):
    """The data channel among serial ports: on macOS a device's ports differ only in the last digit (its
    interface), so for each device with several, every port but the first. The first is the console, which
    is never touched: a probe typed into a REPL would run as code."""
    devices = {}
    for p in sorted(ports):
        devices.setdefault(p[:-1], []).append(p)
    return [p for group in devices.values() if len(group) > 1 for p in group[1:]]


def find_ports():
    ports = [p for g in PORT_GLOBS for p in glob.glob(g)]
    return data_ports(ports) + sorted(glob.glob(LINUX_BY_ID))


def encode(snap):
    """One line: compact JSON, ASCII only (newlines inside strings are escaped, so it can't break a line)."""
    return json.dumps(snap, separators=(",", ":")).encode() + b"\n"


class Feed:
    """What to send one board: the snapshot when it changes (ignoring the clock), else the same again every
    `keepalive` seconds, so the board knows the link is alive."""

    def __init__(self, keepalive=5):
        self.keepalive, self.key, self.sent = keepalive, None, None

    def line(self, snap, now):
        key = {k: v for k, v in snap.items() if k not in ("generated", "clock")}
        if key != self.key or self.sent is None or now - self.sent >= self.keepalive:
            self.key, self.sent = key, now
            return encode(snap)
        return None


class Port:
    """A serial port in raw mode, non-blocking, writes with a deadline (like cpfleet/repl.py's Serial)."""

    def __init__(self, path):
        import termios
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            attrs = termios.tcgetattr(self.fd)
            attrs[0] = attrs[1] = attrs[3] = 0
            attrs[2] |= termios.CLOCAL | termios.CREAD
            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
            termios.tcflush(self.fd, termios.TCIOFLUSH)
        except termios.error as e:
            os.close(self.fd)
            raise OSError(str(e))

    def read(self):
        try:
            return os.read(self.fd, 4096)
        except BlockingIOError:
            return b""

    def write(self, data, timeout=2.0):
        """Raises OSError if the port is gone or the board stops reading for `timeout` seconds."""
        deadline = time.monotonic() + timeout
        while data:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([], [self.fd], [], left)[1]:
                raise OSError("write timed out")
            try:
                data = data[os.write(self.fd, data):]
            except BlockingIOError:
                time.sleep(0.01)

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


def handshake(port, timeout=1.5):
    """Send a probe; the board's name if it answers as an agents display within `timeout`, else None."""
    port.write(b"\n" + PROBE, timeout)  # the newline ends any partial line left from before
    buf, deadline = b"", time.monotonic() + timeout
    while time.monotonic() < deadline:
        if select.select([port.fd], [], [], max(0, deadline - time.monotonic()))[0]:
            buf += port.read()
            for line in buf.split(b"\n")[:-1]:
                line = line.strip()
                if line.startswith(HELLO):
                    return line[len(HELLO):].strip().decode(errors="replace") or "board"
    return None


class UsbLink:
    """Finds agents displays on USB data ports and keeps each one fed; copes with ports coming and going.
    A port that doesn't answer the probe is tried again every `retry` seconds (a board may still be
    starting code.py)."""

    def __init__(self, get_snapshot, ports=find_ports, opener=Port, keepalive=5, retry=10, log=print):
        self.get_snapshot, self.ports, self.opener = get_snapshot, ports, opener
        self.keepalive, self.retry, self.log = keepalive, retry, log
        self.boards = {}  # path -> (port, feed, name)
        self.tried = {}   # path -> when it last failed the probe

    def drop(self, path, why):
        port, _, name = self.boards.pop(path)
        port.close()
        self.tried[path] = time.monotonic()
        self.log("usb: %s on %s: %s" % (name, path, why))

    def step(self, now=None):
        now = time.monotonic() if now is None else now
        present = set(self.ports())
        for path in list(self.boards):
            if path not in present:
                self.drop(path, "unplugged")
        for path in list(self.tried):
            if path not in present:
                del self.tried[path]  # plugged in again later: probe at once
        for path in sorted(present - set(self.boards)):
            if path in self.tried and now - self.tried[path] < self.retry:
                continue
            port, name = None, None
            try:
                port = self.opener(path)
                name = handshake(port)
            except OSError:
                pass
            if name:
                self.boards[path] = (port, Feed(self.keepalive), name)
                self.tried.pop(path, None)
                self.log("usb: %s on %s" % (name, path))
            else:
                if port:
                    port.close()
                self.tried[path] = now
        snap = self.get_snapshot()
        for path, (port, feed, name) in list(self.boards.items()):
            port.read()  # discard anything the board sent, so its buffer never fills
            line = feed.line(snap, now) if snap else None
            if line:
                try:
                    port.write(line)
                except OSError as e:
                    self.drop(path, e)

    def run(self, every=0.5):
        while True:
            try:
                self.step()
            except Exception as e:  # keep going; a port may have vanished mid-step
                self.log("usb: %s" % e)
            time.sleep(every)


# ---- HTTP and Bonjour ---------------------------------------------------------------------------------

SERVICE = "_cpagents._tcp"


def authorized(header, token):
    """True if no token is set, or the Authorization header carries it."""
    if not token:
        return True
    return hmac.compare_digest((header or "").encode(), ("Bearer " + token).encode())


def advertise(port, token):
    """Register the HTTP service with macOS's Bonjour (dns-sd -R) while we run; the process, or None."""
    exe = shutil.which("dns-sd")
    if not exe:
        print("no dns-sd here, so boards won't find this by name; give them AGENTS_URL", flush=True)
        return None
    name = "Claude agents on %s" % socket.gethostname().split(".")[0]
    txt = ["path=/agents.json"] + (["auth=token"] if token else [])
    proc = subprocess.Popen([exe, "-R", name, SERVICE, "local", str(port)] + txt,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("advertising %r as %s" % (name, SERVICE), flush=True)
    return proc


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] not in ("/agents.json", "/"):
            self.send_response(404)
            self.end_headers()
            return
        if not authorized(self.headers.get("Authorization"), TOKEN[0]):
            self.send_response(401)
            self.end_headers()
            return
        body = json.dumps(STATE["snapshot"], separators=(",", ":")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


STATE = {"snapshot": {}}
TOKEN = [None]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--hours", type=float, default=3, help="sessions active within this many hours")
    ap.add_argument("--every", type=float, default=2, help="seconds between scans")
    ap.add_argument("--once", action="store_true", help="print one snapshot and exit")
    ap.add_argument("--usb", action="store_true", help="also feed agents displays plugged in over USB")
    ap.add_argument("--no-http", action="store_true", help="don't serve HTTP (USB only)")
    ap.add_argument("--no-advertise", action="store_true", help="don't advertise over Bonjour")
    a = ap.parse_args()
    if a.once:
        print(json.dumps(snapshot(a.hours), indent=2))
        return
    if a.no_http and not a.usb:
        ap.error("--no-http needs --usb, or there's nothing to do")
    TOKEN[0] = os.environ.get("AGENTS_TOKEN") or None
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # so the finally below stops dns-sd

    def scan():
        while True:
            try:
                STATE["snapshot"] = snapshot(a.hours)
            except Exception as e:  # keep serving the last good snapshot
                print("scan failed:", e, flush=True)
            time.sleep(a.every)
    STATE["snapshot"] = snapshot(a.hours)
    threading.Thread(target=scan, daemon=True).start()
    usb = UsbLink(lambda: STATE["snapshot"], log=lambda m: print(m, flush=True)) if a.usb else None
    if a.no_http:
        print("feeding agents displays on USB", flush=True)
        try:
            usb.run()
        except KeyboardInterrupt:
            pass
        return
    if usb:
        threading.Thread(target=usb.run, daemon=True).start()
    server = ThreadingHTTPServer((a.bind, a.port), Handler)
    dns_sd = None
    if not a.no_advertise and not a.bind.startswith("127."):
        dns_sd = advertise(a.port, TOKEN[0])
    print("serving http://%s:%d/agents.json%s%s" % (a.bind, a.port, " (token required)" if TOKEN[0] else "",
                                                   ", and USB" if usb else ""), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if dns_sd:
            dns_sd.terminate()


if __name__ == "__main__":
    main()
