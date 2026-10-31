#!/usr/bin/env python3
"""Serve a live summary of local coding-agent sessions (Claude Code, Codex, opencode) for e-ink displays.

Reads, never writes, what each tool already keeps on disk:
  claude    ~/.claude/projects/<project>/<session>.jsonl, its custom-title.json and subagents/*.meta.json;
            for sessions run in the Claude desktop app, its title and archived flag
            (~/Library/Application Support/Claude/claude-code-sessions/*/*/local_*.json)
  codex     ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl, ~/.codex/session_index.jsonl (titles) and
            ~/.codex/state_5.sqlite (archived threads, sub-thread links), opened read-only
  opencode  ~/.local/share/opencode/opencode.db (sessions, messages, parts), opened read-only
and serves GET /agents.json on the LAN, advertised over Bonjour as _cpagents._tcp so boards find it by
name. With --usb it also writes the same snapshot, one JSON line per change plus a keep-alive, to agents
displays plugged into this computer (CircuitPython's second USB serial channel; see usblink.py in
apps/agents). No settings or hooks of any of the tools are involved.

  python3 extras/agents_watch.py [--port 8766] [--hours 3] [--sources claude,codex,opencode]
                                 [--usb [--no-http]] [--no-advertise]

Set AGENTS_TOKEN in the environment to require it from HTTP clients (Authorization: Bearer <token>).

States: "question" (Claude's AskUserQuestion or plan approval, opencode's question tool), "your-turn" (the
session finished its turn), "working", "busy" (a tool call has had no result for a while: a long command,
or a permission prompt; none of the logs can tell those apart), "background" (its turn is over but it
waits for its own background command or subagent, which will wake it), "idle". Each session's "since" is its last
activity; a working or busy one also has "turn", when its current turn started. With --text, a session
also has "said", the end of the agent's latest reply, and a working one "doing", its open tool call (what
the agent wrote, never your prompts).
"""
import argparse
import calendar
import glob
import hmac
import json
import os
import re
import select
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path.home()
PROJECTS = Path(os.environ.get("CLAUDE_PROJECTS") or HOME / ".claude" / "projects")
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or HOME / ".codex")
XDG_DATA = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local" / "share")
OPENCODE_DB = Path(os.environ.get("OPENCODE_DB") or XDG_DATA / "opencode" / "opencode.db")
DESKTOP_SESSIONS = Path(os.environ.get("CLAUDE_DESKTOP_SESSIONS") or
                        HOME / "Library" / "Application Support" / "Claude" / "claude-code-sessions")
TAIL_BYTES = 512 * 1024   # transcript lines can be large; the state is in the last few entries
WORKING_SECS = 30         # file written this recently: working
BUSY_SECS = 20            # a tool call open this long without output: maybe waiting for a permission
STALE_SECS = 600          # a turn still open with nothing written for this long: idle (closed, or crashed)
RECENT_SECS = 300         # finished subagents are still listed this long
BACKGROUND_SECS = 3600    # a background task with no news for this long, its transcript quiet too: gone
ASKING_TOOLS = {"AskUserQuestion": "question", "ExitPlanMode": "plan approval"}
# Claude Code reports a background command or subagent's end as a task notification, in a queued_command
# attachment or a user message: <task-notification><task-id>…</task-id>…<status>completed</status>…
NOTIFICATION = re.compile(r"<task-id>([^<]+)</task-id>.*?<status>(\w+)</status>", re.S)
ORDER = {"question": 0, "your-turn": 1, "busy": 2, "working": 3, "background": 4, "idle": 5}
ACTIVE = ("busy", "working", "background")  # states with a turn time
WAITING = ("question", "your-turn")
# What one snapshot carries, to keep it small for the boards (about 2 MB of RAM, and 16 KB per USB line)
PER_TOOL, TITLE_LEN, SUBS, SUB_TYPE_LEN, SUB_DESC_LEN, SAID_LEN, DOING_LEN = 8, 48, 3, 12, 40, 280, 60
# Subagent types that say nothing (the boards show the description alone); a "plugin:" prefix is dropped
GENERIC_TYPES = {"general-purpose", "general", "agent", "fork", "default", "subagent", "build", "worker"}


SHARE_TEXT = [False]  # --text: the agents' latest replies and tool calls go in the snapshot too
# What the boards' font can't draw, in ASCII; and markdown that would only clutter a few lines of text
ASCII = {"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-",
         "\u2026": "...", "\u2192": "->", "\u2022": "-", "\u00a0": " ", "\u2713": "v", "\u00d7": "x"}
MARKUP = re.compile(r"\*\*|__|`+|^#+\s*", re.M)
TOOL_HINT = ("description", "command", "cmd", "file_path", "filePath", "path", "pattern", "url", "query")


def plain(text):
    """Text as the boards' font can draw it: ASCII, markdown emphasis dropped, on one line."""
    text = MARKUP.sub("", str(text or ""))
    text = "".join(ASCII.get(ch, ch) for ch in text).encode("ascii", "ignore").decode()
    return " ".join(text.split())


def tail_text(text, n=SAID_LEN):
    """The end of text in at most n characters, from the start of a word, with "..." where it's cut."""
    t = plain(text)
    if len(t) <= n:
        return t
    t = t[-(n - 3):]
    cut = t.find(" ")
    return "..." + (t[cut + 1:] if 0 <= cut < 20 else t)


def doing_text(name, args):
    """A tool call as "Bash: Run the tests": its description, else its command, path or the like."""
    hint = ""
    if isinstance(args, dict):
        for k in TOOL_HINT:
            v = args.get(k)
            if isinstance(v, list):
                v = " ".join(str(x) for x in v)
            if v:
                hint = str(v)
                break
    return clip(plain(name + (": " + hint if hint else "")), DOING_LEN)


def clip(s, n):
    s = " ".join(str(s or "").split())  # one line
    return s if len(s) <= n else s[:n - 1].rstrip() + "~"


def iso_time(ts):
    """Epoch seconds from the tools' UTC timestamps (2026-01-02T03:04:05.678Z), or None."""
    try:
        return calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return None


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


def first_entry(path, limit=1024 * 1024):
    """The first JSON line of a file, or None (Codex's session_meta line is tens of KB)."""
    try:
        with open(path, "rb") as f:
            line = f.readline(limit)
        return json.loads(line)
    except (OSError, ValueError):
        return None


def ro_connect(path):
    """A read-only connection to a SQLite database that its app may be writing (WAL): never written to."""
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=0.5)


def ro_query(path, fn, tries=3):
    """fn(connection) on a read-only connection, retried while the database is locked or busy."""
    for i in range(tries):
        db = None
        try:
            db = ro_connect(path)
            return fn(db)
        except sqlite3.OperationalError as e:
            if i == tries - 1 or not ("locked" in str(e) or "busy" in str(e)):
                raise
            time.sleep(0.1)
        finally:
            if db is not None:
                db.close()


# ---- Claude Code ---------------------------------------------------------------------------------------

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


def claude_text(entries):
    """(the latest reply's text, its open tool call) from a transcript's entries: what the agent wrote,
    never your prompts."""
    said, pending = "", {}
    for e in entries:
        content = (e.get("message") or {}).get("content")
        if e.get("type") == "assistant" and isinstance(content, list):
            texts = [c.get("text") or "" for c in content if isinstance(c, dict) and c.get("type") == "text"]
            if any(t.strip() for t in texts):
                said = " ".join(texts)
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    pending[c.get("id")] = doing_text(c.get("name") or "tool", c.get("input"))
        elif e.get("type") == "user":
            results = [c for c in content if isinstance(c, dict) and c.get("type") == "tool_result"] \
                if isinstance(content, list) else []
            if not results:
                pending.clear()  # a new prompt
            for c in results:
                pending.pop(c.get("tool_use_id"), None)
    return said, (list(pending.values())[-1] if pending else "")


def turn_started(entries):
    """When the current turn began: the last prompt (a user entry that isn't only tool results), or, with
    no prompt in the tail, its earliest entry (the turn began before that). None without timestamps."""
    start, first = None, None
    for e in entries:
        ts = iso_time(e.get("timestamp"))
        first = first or ts
        if e.get("type") != "user" or e.get("isMeta"):
            continue
        content = (e.get("message") or {}).get("content")
        if isinstance(content, list) and any(isinstance(c, dict) and c.get("type") == "tool_result"
                                             for c in content):
            continue
        if isinstance(content, str) and content.lstrip().startswith("<task-notification>"):
            continue  # a background task's news, not your prompt
        start = ts or start
    return start or first


def background_tasks(entries):
    """Background commands and subagents (run_in_background) the session started and hasn't heard the end
    of: {task id: the tool call, as doing_text}."""
    return background_update(entries, {}, {})


_background = {}  # transcript -> [bytes read, {task id: doing}, {tool_use id: doing}]
BACKGROUND_MARKS = (b"backgroundTaskId", b"isAsync", b"async_launched", b"task-notification", b'"tool_use"')


def background_in(path):
    """background_tasks() of a whole transcript (a launch can be much further back than the tail we read
    for the state), reading only what was added since the last scan."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return {}
    st = _background.get(path)
    if st is None or size < st[0]:
        st = _background[path] = [0, {}, {}]
    if size > st[0]:
        try:
            with open(path, "rb") as f:
                f.seek(st[0])
                data = f.read(size - st[0])
        except OSError:
            return st[1]
        end = data.rfind(b"\n") + 1  # whole lines only; a line being written is read next time
        entries = []
        for line in data[:end].split(b"\n"):
            if any(m in line for m in BACKGROUND_MARKS):
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    pass
        st[0] += end
        background_update(entries, st[1], st[2])
        if len(st[2]) > 500:  # labels of old tool calls: keep the newest
            for k in list(st[2])[:-200]:
                del st[2][k]
    return st[1]


def background_update(entries, running, uses):
    """Adds the background tasks started in entries to running, and removes those that ended."""
    for e in entries:
        content = (e.get("message") or {}).get("content")
        if e.get("type") == "assistant" and isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    uses[c.get("id")] = doing_text(c.get("name") or "tool", c.get("input"))
        r = e.get("toolUseResult")
        if isinstance(r, dict):
            tid = r.get("backgroundTaskId") or (r.get("agentId") if r.get("isAsync") or
                                                r.get("status") == "async_launched" else None)
            if tid:
                use = [c.get("tool_use_id") for c in content if isinstance(c, dict) and
                       c.get("type") == "tool_result"] if isinstance(content, list) else []
                running[str(tid)] = uses.get(use[0] if use else None, "")
        news = content if isinstance(content, str) else ""
        if isinstance(e.get("attachment"), dict):
            news += json.dumps(e["attachment"])
        if "task-notification" in news:
            for tid, status in NOTIFICATION.findall(news):
                if status not in ("running", "started"):
                    running.pop(tid, None)
    return running


_desktop = {}  # desktop session file -> (mtime, cliSessionId, title, archived)


def desktop_sessions():
    """{Claude Code session id: (title, archived)} for sessions run in the Claude desktop app, whose own
    title (the one in its sidebar, renamed there) is the one to show. Files are re-read when they change."""
    out, seen = {}, set()
    for f in glob.glob(str(DESKTOP_SESSIONS / "*" / "*" / "local_*.json")):
        seen.add(f)
        try:
            mtime = os.path.getmtime(f)
            hit = _desktop.get(f)
            if not hit or hit[0] != mtime:
                d = json.loads(Path(f).read_text())
                hit = _desktop[f] = (mtime, d.get("cliSessionId"), d.get("title"), bool(d.get("isArchived")))
        except (OSError, ValueError, AttributeError):
            continue
        if hit[1]:
            out[hit[1]] = (hit[2], hit[3])
    for f in set(_desktop) - seen:
        del _desktop[f]
    return out


def title_of(session_dir, entries, project_dir, desktop_title=None):
    """The desktop app's title, else the latest rename in the transcript (/rename writes custom-title and
    agent-name entries; custom-title.json can be left behind), else custom-title.json, else the folder."""
    if desktop_title:
        return desktop_title
    for e in reversed(entries):
        for key in ("customTitle", "agentName"):
            if e.get(key):
                return e[key]
    try:
        t = json.loads((session_dir / "custom-title.json").read_text()).get("customTitle")
        if t:
            return t
    except (OSError, ValueError):
        pass
    return project_dir.name.rsplit("-", 1)[-1] or project_dir.name


_claude_started = {}  # subagent log -> when it started (its first entry; never changes)


def subagents(session_dir, now):
    """Running subagents, and those that finished in the last RECENT_SECS ("done")."""
    out = []
    for meta in glob.glob(str(session_dir / "subagents" / "agent-*.meta.json")):
        log = meta[: -len(".meta.json")] + ".jsonl"
        try:
            age = now - os.path.getmtime(log)
            if age >= RECENT_SECS:
                continue  # finished, or silent, too long ago
            info = json.loads(Path(meta).read_text())
        except (OSError, ValueError):
            continue
        state, _ = conversation_state(tail_entries(log, 64 * 1024))
        running = state != "your-turn" and age < 120
        if log not in _claude_started:
            _claude_started[log] = iso_time((first_entry(log) or {}).get("timestamp")) or now - age
        out.append({"type": info.get("agentType", "agent"), "desc": info.get("description", ""),
                    "since": int(_claude_started[log]), "done": not running})
    return out


def busy_with_subagents(s):
    """A session waiting on its own running subagents is working, not waiting on you; one that has finished
    its turn waits for them in the background (they report back to it)."""
    if any(not x.get("done") for x in s["subagents"]):
        if s["state"] in ("busy", "idle"):
            s["state"] = "working"
        elif s["state"] == "your-turn":
            s["state"] = "background"


def claude_sessions(hours, now):
    sessions = []
    desktop = desktop_sessions()
    for path in glob.glob(str(PROJECTS / "*" / "*.jsonl")):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if now - mtime > hours * 3600:
            continue
        p = Path(path)
        desktop_title, archived = desktop.get(p.stem, (None, False))
        if archived:
            continue  # archived in the desktop app
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
        s = {"id": p.stem, "title": title_of(session_dir, entries, p.parent, desktop_title), "state": state,
             "detail": detail, "since": int(mtime), "turn": turn_started(entries),
             "subagents": subagents(session_dir, now)}
        if SHARE_TEXT[0]:
            s["said"], s["doing"] = claude_text(entries)
        background = background_in(path) if age < BACKGROUND_SECS else {}
        if background and s["state"] in ("your-turn", "idle"):
            # Its turn is over but it waits for a background command or subagent, which will wake it
            s["state"], s["detail"] = "background", ""
            if SHARE_TEXT[0] and not s.get("doing"):
                s["doing"] = list(background.values())[-1] or "background task"
        busy_with_subagents(s)
        sessions.append(s)
    return sessions


# ---- Codex ---------------------------------------------------------------------------------------------
# A rollout is one thread: a session_meta line, then turns (event_msg task_started ... task_complete or
# turn_aborted) holding response_items, tool calls among them (function_call / custom_tool_call, answered by
# *_output with the same call_id). The desktop app appends thread_settings_applied to old rollouts when it
# starts, so neither the file's mtime nor those entries say a thread is active. Approvals leave no trace.

CODEX_WAIT_TOOLS = {"wait", "sleep"}  # the agent waiting on its own work (subagents, a timer), not on you
CODEX_UNATTENDED = ("exec", "mcp")    # `codex exec` and Codex as an MCP server: nobody's turn when done
CODEX_UNATTENDED_THREADS = ("automation",)  # the app's scheduled runs: likewise
CODEX_IGNORED = {"thread_settings_applied"}
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_codex_meta, _codex_tail, _codex_index = {}, {}, [None, {}]


def codex_turn(entries):
    """(turn, open tool calls {call_id: name}, last activity, error, start) from a rollout's entries. turn
    is "started", "task_complete", "turn_aborted" or None if the entries hold no turn event; start is when
    the last turn started, or the earliest entry if none started in these entries."""
    turn, calls, last, error, start, first = None, {}, None, False, None, None
    for e in entries:
        p = e.get("payload")
        if not isinstance(p, dict) or p.get("type") in CODEX_IGNORED:
            continue
        ts = iso_time(e.get("timestamp"))
        if ts:
            last = ts
            first = first or ts
        kind, t = e.get("type"), p.get("type")
        if kind == "event_msg":
            if t == "task_started":
                turn, error, start = "started", False, ts
                calls.clear()
            elif t in ("task_complete", "turn_aborted"):
                turn, error = t, bool(p.get("error"))
                calls.clear()
        elif kind == "response_item":
            if t in ("function_call", "custom_tool_call"):
                calls[p.get("call_id")] = p.get("name") or "tool"
            elif t in ("function_call_output", "custom_tool_call_output"):
                calls.pop(p.get("call_id"), None)
    return turn, calls, last, error, start or first


def codex_text(entries):
    """(the latest reply's text, its open tool call) from a rollout's entries: what the agent wrote."""
    said, calls = "", {}
    for e in entries:
        p = e.get("payload")
        if e.get("type") != "response_item" or not isinstance(p, dict):
            continue
        t = p.get("type")
        if t == "message" and p.get("role") == "assistant":
            texts = [c.get("text") or "" for c in p.get("content") or []
                     if isinstance(c, dict) and c.get("type") == "output_text"]
            if any(x.strip() for x in texts):
                said = " ".join(texts)
        elif t in ("function_call", "custom_tool_call"):
            args = p.get("arguments")
            try:
                args = json.loads(args) if isinstance(args, str) else args
            except ValueError:
                args = None
            calls[p.get("call_id")] = doing_text(p.get("name") or "tool", args)
        elif t in ("function_call_output", "custom_tool_call_output"):
            calls.pop(p.get("call_id"), None)
    return said, (list(calls.values())[-1] if calls else "")


def codex_meta(path):
    """The session_meta payload of a rollout (cached: it never changes), or {}."""
    if path not in _codex_meta:
        e = first_entry(path) or {}
        meta = e.get("payload") if e.get("type") == "session_meta" else None
        meta = dict(meta) if isinstance(meta, dict) else {}
        meta.setdefault("timestamp", e.get("timestamp"))  # when the thread started
        _codex_meta[path] = meta
    return _codex_meta[path]


def codex_tail(path, size, mtime):
    """codex_turn() of the rollout's tail, and codex_text() with --text; cached until the file changes."""
    key = (size, mtime, SHARE_TEXT[0])
    hit = _codex_tail.get(path)
    if not hit or hit[0] != key:
        entries = tail_entries(path)
        hit = _codex_tail[path] = (key, codex_turn(entries) + (codex_text(entries) if SHARE_TEXT[0] else ("", "")))
    return hit[1]


def codex_titles():
    """{thread id: name} from session_index.jsonl, where the latest line for an id wins."""
    path = CODEX_HOME / "session_index.jsonl"
    try:
        st = os.stat(path)
    except OSError:
        return {}
    key = (st.st_size, st.st_mtime)
    if _codex_index[0] != key:
        names = {}
        try:
            with open(path, "rb") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(d, dict) and d.get("id") and d.get("thread_name"):
                        names[d["id"]] = d["thread_name"]
        except OSError:
            return _codex_index[1]
        _codex_index[:] = [key, names]
    return _codex_index[1]


def codex_threads(ids):
    """({id: (archived, name)}, {child: (parent, status)}) from state_5.sqlite, or empty if it can't be read
    (missing, locked, or a schema we don't know). threads.title is the first prompt: never read it."""
    db_path = CODEX_HOME / "state_5.sqlite"
    if not ids or not db_path.exists():
        return {}, {}

    def read(db):
        marks = ",".join("?" * len(ids))
        threads = {r[0]: (r[1], r[2]) for r in
                   db.execute("SELECT id, archived, name FROM threads WHERE id IN (%s)" % marks, ids)}
        edges = {r[1]: (r[0], r[2]) for r in db.execute(
            "SELECT parent_thread_id, child_thread_id, status FROM thread_spawn_edges "
            "WHERE child_thread_id IN (%s) OR parent_thread_id IN (%s)" % (marks, marks), ids + ids)}
        return threads, edges
    try:
        return ro_query(db_path, read)
    except sqlite3.Error:
        return {}, {}


def codex_sessions(hours, now):
    cutoff = now - hours * 3600
    found = []
    for path in glob.glob(str(CODEX_HOME / "sessions" / "*" / "*" / "*" / "rollout-*.jsonl")):
        try:
            st = os.stat(path)
        except OSError:
            continue
        if st.st_mtime < cutoff:
            continue
        turn, calls, last, error, start, said, doing = codex_tail(path, st.st_size, st.st_mtime)
        if not last or last < cutoff:  # only touched by the app's settings, not active
            continue
        meta = codex_meta(path)
        tid = meta.get("id") or (UUID.findall(Path(path).stem) or [Path(path).stem])[0]
        found.append((tid, meta, turn, calls, last, error, start, said, doing))
    ids = [f[0] for f in found]
    threads, edges = codex_threads(ids)
    names = codex_titles()
    top, children = {}, []
    for tid, meta, turn, calls, last, error, started, said, doing in found:
        if threads.get(tid, (0,))[0]:
            continue  # archived in the app
        age = now - last
        open_tools = sorted(set(n for n in calls.values() if n not in CODEX_WAIT_TOOLS))
        detail = ""
        if open_tools:
            state = "busy" if age > BUSY_SECS else "working"
            detail = open_tools[0]
        elif calls:
            state = "working"
        elif turn in ("task_complete", "turn_aborted"):
            state = "your-turn"
        else:
            state = "working" if age < STALE_SECS else "idle"
        source = meta.get("source")
        if state == "your-turn" and (source in CODEX_UNATTENDED or
                                     meta.get("thread_source") in CODEX_UNATTENDED_THREADS):
            state = "idle"
        title = names.get(tid) or (threads.get(tid) or (0, None))[1] or Path(meta.get("cwd") or "codex").name
        s = {"id": tid, "title": title, "state": state, "detail": detail, "since": int(last), "turn": started,
             "said": said, "doing": doing, "subagents": []}
        sub = source.get("subagent") if isinstance(source, dict) else None
        if sub is not None or tid in edges:
            spawn = sub.get("thread_spawn") if isinstance(sub, dict) else None
            parent = (spawn or {}).get("parent_thread_id") or edges.get(tid, (None,))[0]
            if parent:
                spawn = spawn or {}
                done = state not in ("working", "busy") or edges.get(tid, (None, "open"))[1] != "open"
                desc = names.get(tid) or spawn.get("agent_nickname") or spawn.get("agent_path") or ""
                start = iso_time(meta.get("timestamp")) or last
                if not done or age < RECENT_SECS:
                    children.append((parent, {"type": spawn.get("agent_role") or "", "desc": desc,
                                              "since": int(start), "done": done}))
            continue  # a sub-thread (or the app's own reviewer), never a session of its own
        top[tid] = s
    for parent, sub in children:
        if parent in top:
            top[parent]["subagents"].append(sub)
    for s in top.values():
        busy_with_subagents(s)
    return list(top.values())


# ---- opencode ------------------------------------------------------------------------------------------
# session rows (parent_id set: a subagent's child session), message rows (data: role, time.created /
# completed, finish, error, agent) and part rows (data.type "tool": tool, state.status pending / running /
# completed / error). Its `permission` table holds saved rules, not pending requests: a permission prompt
# shows only as a tool part left running, as with the others.

OPENCODE_ASKING = {"question": "question"}
SUBAGENT_SUFFIX = re.compile(r"\s*\(@([\w.-]+) subagent\)\s*$")


def opencode_read(db, cutoff_ms):
    """[(session row, last message data or None, its open tool calls [tool name], activity ms,
    the last prompt's time ms or None, with --text the latest reply's text and the open tool call)]"""
    rows = db.execute("SELECT id, parent_id, title, directory, time_updated, time_created FROM session "
                      "WHERE time_archived IS NULL AND time_updated >= ?", (cutoff_ms,)).fetchall()
    out = []
    for row in rows:
        activity = row[4]
        m = db.execute("SELECT id, time_updated, data FROM message WHERE session_id = ? "
                       "ORDER BY time_created DESC, id DESC LIMIT 1", (row[0],)).fetchone()
        msg, tools, doing = None, [], ""
        if m:
            activity = max(activity, m[1])
            try:
                msg = json.loads(m[2])
            except ValueError:
                msg = {}
            if msg.get("role") == "assistant" and not (msg.get("time") or {}).get("completed"):
                for part_time, data in db.execute("SELECT time_updated, data FROM part WHERE message_id = ?",
                                                  (m[0],)):
                    activity = max(activity, part_time)
                    if '"tool"' not in data:
                        continue
                    try:
                        part = json.loads(data)
                    except ValueError:
                        continue
                    st = part.get("state") or {}
                    if part.get("type") == "tool" and st.get("status") in ("pending", "running"):
                        tools.append(part.get("tool") or "tool")
                        hint = st.get("input") if isinstance(st.get("input"), dict) else {}
                        if st.get("title") and not hint.get("description"):
                            hint = dict(hint, description=st["title"])
                        doing = doing_text(part.get("tool") or "tool", hint)
        prompt = db.execute("SELECT MAX(time_created) FROM message WHERE session_id = ? AND "
                            "(data LIKE '%\"role\":\"user\"%' OR data LIKE '%\"role\": \"user\"%')",
                            (row[0],)).fetchone()[0]
        said = ""
        if SHARE_TEXT[0] and not row[1]:  # the latest text the agent wrote (a text part of a reply)
            r = db.execute("SELECT p.data FROM part p JOIN message m ON p.message_id = m.id "
                           "WHERE m.session_id = ? AND (m.data LIKE '%\"role\":\"assistant\"%' OR "
                           "m.data LIKE '%\"role\": \"assistant\"%') AND (p.data LIKE '%\"type\":\"text\"%' OR "
                           "p.data LIKE '%\"type\": \"text\"%') ORDER BY p.time_created DESC, p.id DESC LIMIT 1",
                           (row[0],)).fetchone()
            try:
                said = (json.loads(r[0]).get("text") or "") if r else ""
            except (ValueError, AttributeError):
                said = ""
        out.append((row, msg, tools, activity, prompt, said, doing))
    return out


def opencode_state(msg, tools, age):
    """(state, detail) for one opencode session."""
    if msg is None:
        return ("working" if age < WORKING_SECS else "idle"), ""
    if msg.get("role") != "assistant":
        return ("working" if age < STALE_SECS else "idle"), ""  # a prompt, the reply not started yet
    if msg.get("error"):
        return "your-turn", ""
    if (msg.get("time") or {}).get("completed"):
        if msg.get("finish") == "tool-calls" and age < WORKING_SECS:
            return "working", ""  # between steps
        return "your-turn", ""
    asking = sorted(OPENCODE_ASKING[t] for t in tools if t in OPENCODE_ASKING)
    if asking:
        return "question", asking[0]
    others = sorted(t for t in tools if t != "task")  # task: a subagent running
    if others:
        return ("busy" if age > BUSY_SECS else "working"), others[0]
    if tools:
        return "working", ""
    return ("working" if age < STALE_SECS else "idle"), ""


def opencode_sessions(hours, now):
    if not OPENCODE_DB.exists():
        return []
    rows = ro_query(OPENCODE_DB, lambda db: opencode_read(db, int((now - hours * 3600) * 1000)))
    top, children = {}, []
    for row, msg, tools, activity, prompt, said, doing in rows:
        sid, parent, title, directory = row[:4]
        age = now - activity / 1000.0
        state, detail = opencode_state(msg, tools, age)
        if parent:
            m = SUBAGENT_SUFFIX.search(title or "")
            kind = (msg or {}).get("agent") or (m.group(1) if m else "subagent")
            done = state not in ("question", "working", "busy")
            if not done or age < RECENT_SECS:
                sub = {"type": kind, "desc": SUBAGENT_SUFFIX.sub("", title or ""), "since": int(row[5] / 1000),
                       "done": done}
                children.append((parent, state, detail, sub))
            continue
        top[sid] = {"id": sid, "title": title or Path(directory or "opencode").name, "state": state,
                    "detail": detail, "since": int(activity / 1000),
                    "turn": prompt / 1000.0 if prompt else None, "said": said, "doing": doing,
                    "subagents": []}
    for parent, state, detail, sub in children:
        s = top.get(parent)
        if not s:
            continue
        s["subagents"].append(sub)
        if state == "question":  # a subagent asking you: the session waits for you
            s["state"], s["detail"] = "question", detail
    for s in top.values():
        busy_with_subagents(s)
    return list(top.values())


# ---- the snapshot --------------------------------------------------------------------------------------

SOURCES = {
    "claude": (claude_sessions, lambda: PROJECTS.is_dir()),
    "codex": (codex_sessions, lambda: (CODEX_HOME / "sessions").is_dir()),
    "opencode": (opencode_sessions, lambda: OPENCODE_DB.exists()),
}
_last_good, _last_error = {}, {}


def compact(s, tool):
    """A session as the boards get it: short id, clipped strings, empty fields left out."""
    out = {"id": s["id"][-8:], "tool": tool, "title": clip(s["title"], TITLE_LEN), "state": s["state"],
           "since": s["since"]}
    if s.get("detail"):
        out["detail"] = clip(s["detail"], 20)
    if s["state"] in ACTIVE and s.get("turn"):
        out["turn"] = int(s["turn"])  # how long it has been at it
    if SHARE_TEXT[0]:
        if s.get("said"):
            out["said"] = tail_text(s["said"])
        if s["state"] in ACTIVE and s.get("doing"):
            out["doing"] = s["doing"]
    subs = sorted(s.get("subagents") or [], key=lambda x: (bool(x.get("done")), -x.get("since", 0)))
    if subs:
        out["subagents"] = [compact_sub(x) for x in subs[:SUBS]]  # running first, newest first
        if len(subs) > SUBS:
            out["nsub"] = len(subs)
    return out


def sub_type(t):
    """A subagent's type if it says something and is short (Explore, Plan, web-fetch), else ""."""
    t = str(t or "").split(":")[-1]
    return t if t.lower() not in GENERIC_TYPES and len(t) <= SUB_TYPE_LEN else ""


def compact_sub(x):
    out = {"type": sub_type(x.get("type")), "desc": clip(x.get("desc"), SUB_DESC_LEN)}  # old boards need type
    if x.get("since"):
        out["since"] = int(x["since"])
    if x.get("done"):
        out["done"] = 1
    return out


def collect(name, hours, now):
    """One source's sessions; if it fails, the last ones it gave (and the error, once, on stderr)."""
    fn, _ = SOURCES[name]
    try:
        sessions = fn(hours, now)
        _last_good[name], _last_error[name] = sessions, None
    except Exception as e:  # one tool's trouble must not hide the others
        sessions = _last_good.get(name, [])
        if _last_error.get(name) != str(e):
            _last_error[name] = str(e)
            print("%s: %s" % (name, e), file=sys.stderr, flush=True)
    return sessions


def snapshot(hours, sources=None):
    """sources: names to read (default: every one whose data is on this computer)."""
    now = time.time()
    names = [n for n in (sources or SOURCES) if n in SOURCES and (sources or SOURCES[n][1]())]
    out, tools, waiting = [], {}, 0
    for name in names:
        sessions = collect(name, hours, now)
        sessions.sort(key=lambda s: (ORDER[s["state"]], -s["since"]))
        w = sum(s["state"] in WAITING for s in sessions)
        tools[name] = {"n": len(sessions), "waiting": w}
        waiting += w
        out += [compact(s, name) for s in sessions[:PER_TOOL]]
    out.sort(key=lambda s: (ORDER[s["state"]], -s["since"]))
    return {"generated": int(now), "clock": time.strftime("%H:%M"), "host": socket.gethostname(),
            "waiting": waiting, "tools": tools, "sessions": out}


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
    name = "Agents on %s" % socket.gethostname().split(".")[0]
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
    ap.add_argument("--sources", help="comma-separated, from %s (default: all found)" % ",".join(SOURCES))
    ap.add_argument("--once", action="store_true", help="print one snapshot and exit")
    ap.add_argument("--usb", action="store_true", help="also feed agents displays plugged in over USB")
    ap.add_argument("--no-http", action="store_true", help="don't serve HTTP (USB only)")
    ap.add_argument("--no-advertise", action="store_true", help="don't advertise over Bonjour")
    ap.add_argument("--text", action="store_true",
                    help="also send the end of each session's latest reply and its current tool call, for the "
                         "boards' session page (they reach anything that can reach the port)")
    a = ap.parse_args()
    sources = None
    if a.sources:
        sources = [s.strip() for s in a.sources.split(",") if s.strip()]
        unknown = [s for s in sources if s not in SOURCES]
        if unknown or not sources:
            ap.error("--sources: unknown %s (choose from %s)"
                     % (",".join(unknown) or "none", ",".join(SOURCES)))
    SHARE_TEXT[0] = a.text
    if a.once:
        print(json.dumps(snapshot(a.hours, sources), indent=2))
        return
    if a.no_http and not a.usb:
        ap.error("--no-http needs --usb, or there's nothing to do")
    TOKEN[0] = os.environ.get("AGENTS_TOKEN") or None
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # so the finally below stops dns-sd

    def scan():
        while True:
            try:
                STATE["snapshot"] = snapshot(a.hours, sources)
            except Exception as e:  # keep serving the last good snapshot
                print("scan failed:", e, flush=True)
            time.sleep(a.every)
    STATE["snapshot"] = snapshot(a.hours, sources)
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
