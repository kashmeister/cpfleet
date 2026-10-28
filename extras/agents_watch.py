#!/usr/bin/env python3
"""Serve a live summary of local Claude Code sessions and subagents for e-ink displays (apps/agents).

Reads, never writes, the transcripts Claude Code keeps in ~/.claude/projects:
  <project>/<session>.jsonl                      the session, appended as it works
  <project>/<session>/custom-title.json          its title as the app shows it
  <project>/<session>/subagents/agent-*.json(l)  its subagents (meta: type, description)
and serves GET /agents.json on the LAN. No Claude Code settings or hooks are involved.

  python3 extras/agents_watch.py [--port 8766] [--hours 3]

States: "question" (an AskUserQuestion or plan approval is open), "your-turn" (the session finished its
turn), "working", "busy" (a tool call has had no result for a while: a long command, or a permission
prompt; transcripts can't tell those apart), "idle".
"""
import argparse
import glob
import json
import os
import socket
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


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] not in ("/agents.json", "/"):
            self.send_response(404)
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


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--hours", type=float, default=3, help="sessions active within this many hours")
    ap.add_argument("--every", type=float, default=2, help="seconds between scans")
    ap.add_argument("--once", action="store_true", help="print one snapshot and exit")
    a = ap.parse_args()
    if a.once:
        print(json.dumps(snapshot(a.hours), indent=2))
        return

    def scan():
        while True:
            try:
                STATE["snapshot"] = snapshot(a.hours)
            except Exception as e:  # keep serving the last good snapshot
                print("scan failed:", e, flush=True)
            time.sleep(a.every)
    STATE["snapshot"] = snapshot(a.hours)
    threading.Thread(target=scan, daemon=True).start()
    print("serving http://%s:%d/agents.json" % (a.bind, a.port), flush=True)
    ThreadingHTTPServer((a.bind, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
