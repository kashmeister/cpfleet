"""The agents watcher's Codex and opencode sources, the merged snapshot, and the board's views, against fake
rollouts and fake SQLite databases in a temp dir (never this computer's own)."""
import contextlib
import importlib.util
import io
import json
import os
import re
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


aw = load("agents_watch_sources", HERE.parent / "extras" / "agents_watch.py")
views = load("views", HERE.parent / "apps" / "agents" / "views.py")  # the board's side


def isotime(age):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - age)) + ".123Z"


def isolate(root):
    """Point every source at `root`, where nothing exists until a test makes it."""
    aw.PROJECTS = root / "claude-projects"
    aw.CODEX_HOME = root / "codex"
    aw.OPENCODE_DB = root / "opencode.db"
    aw._codex_meta.clear()
    aw._codex_tail.clear()
    aw._codex_index[:] = [None, {}]
    aw._last_good.clear()
    aw._last_error.clear()


# ---- Codex ---------------------------------------------------------------------------------------------

def ev(age, kind, **payload):
    return (age, "event_msg", dict(payload, type=kind))


def item(age, kind, **payload):
    return (age, "response_item", dict(payload, type=kind))


def started(age):
    return ev(age, "task_started", turn_id="t")


def complete(age):
    return ev(age, "task_complete", turn_id="t")


def call(age, cid, name="exec"):
    return item(age, "function_call", call_id=cid, name=name, arguments="{}")


def output(age, cid):
    return item(age, "function_call_output", call_id=cid, output="ok")


MSG = "message"


class CodexFixture:
    def make_codex(self):
        self.codex = aw.CODEX_HOME
        self.day = self.codex / "sessions" / "2026" / "01" / "02"
        self.day.mkdir(parents=True)

    def rollout(self, tid, entries, source="vscode", thread_source="user", cwd="/Users/me/code/myproj",
                mtime_age=None):
        meta = {"timestamp": isotime(max([e[0] for e in entries] or [0]) + 1), "type": "session_meta",
                "payload": {"id": tid, "cwd": cwd, "originator": "codex_cli_rs", "source": source,
                            "thread_source": thread_source, "base_instructions": {"text": "x" * 5000}}}
        lines = [meta]
        for age, kind, payload in entries:
            if payload.get("type") == MSG:
                payload = dict(payload, role="assistant", content=[{"type": "output_text", "text": "hi"}])
            lines.append({"timestamp": isotime(age), "type": kind, "payload": payload})
        p = self.day / ("rollout-2026-01-02T03-04-05-%s.jsonl" % tid)
        p.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
        age = min([e[0] for e in entries] or [0]) if mtime_age is None else mtime_age
        os.utime(p, (time.time() - age, time.time() - age))
        return p

    def index(self, *pairs):
        with open(self.codex / "session_index.jsonl", "a") as f:
            for tid, name in pairs:
                f.write(json.dumps({"id": tid, "thread_name": name, "updated_at": isotime(0)}) + "\n")

    def state_db(self, threads, edges=()):
        db = sqlite3.connect(str(self.codex / "state_5.sqlite"))
        db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, title TEXT NOT NULL, "
                   "archived INTEGER NOT NULL DEFAULT 0, name TEXT)")
        db.execute("CREATE TABLE thread_spawn_edges (parent_thread_id TEXT NOT NULL, "
                   "child_thread_id TEXT NOT NULL PRIMARY KEY, status TEXT NOT NULL)")
        for tid, archived, name in threads:
            db.execute("INSERT INTO threads (id, title, archived, name) VALUES (?, ?, ?, ?)",
                       (tid, "SECRET FIRST PROMPT", archived, name))
        db.executemany("INSERT INTO thread_spawn_edges VALUES (?, ?, ?)", edges)
        db.commit()
        db.close()

    def codex_world(self):
        self.make_codex()
        self.rollout("thread-busy", [started(90), item(80, MSG), call(60, "c1")])
        self.rollout("thread-done", [started(300), item(260, MSG), complete(240)])
        self.rollout("thread-work", [started(10), call(8, "c2"), output(5, "c2")])
        old = 5 * 3600  # finished long ago; the app's settings line touched it just now
        self.rollout("thread-old", [started(old + 60), complete(old), ev(0, "thread_settings_applied")])
        self.rollout("thread-exec", [started(200), complete(100)], source="exec", cwd="/Users/me/code/tools")
        self.rollout("thread-auto", [started(200), complete(150)], thread_source="automation")
        self.rollout("thread-arch", [started(200), complete(100)])
        self.rollout("thread-lead", [started(200), call(100, "w1", "wait")])
        spawn = {"subagent": {"thread_spawn": {"parent_thread_id": "thread-lead", "depth": 1,
                                               "agent_nickname": "Ada", "agent_role": "explorer"}}}
        self.rollout("thread-kid", [started(30), item(3, MSG)], source=spawn, thread_source="subagent")
        self.rollout("thread-gone", [started(30), item(3, MSG)], source=dict(spawn), thread_source="subagent")
        self.rollout("thread-guard", [started(40), complete(20)], source={"subagent": {"other": "guardian"}},
                     thread_source="guardian_review")
        self.rollout("thread-stop", [started(90), ev(50, "turn_aborted", reason="interrupted")])
        self.rollout("thread-stale", [started(1300), item(1200, MSG)])
        self.index(("thread-done", "Old name"), ("thread-busy", "Fix the parser"), ("thread-done", "Ship it"),
                   ("thread-lead", "Lead thread"), ("thread-kid", "Scan the tests"), ("thread-stop", "Stopped"),
                   ("thread-stale", "Left open"), ("thread-auto", "Daily chore"), ("thread-arch", "Archived"),
                   ("thread-guard", "Reviewer"))
        self.state_db([("thread-work", 0, "From the db"), ("thread-arch", 1, "Archived")],
                      [("thread-lead", "thread-kid", "open"), ("thread-lead", "thread-gone", "closed")])


class CodexSource(CodexFixture, unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        isolate(self.root)
        self.codex_world()

    def test_states_titles_and_subthreads(self):
        got = {s["title"]: s for s in aw.codex_sessions(3, time.time())}
        self.assertEqual({t: s["state"] for t, s in got.items()}, {
            "Fix the parser": "busy",      # a tool call open for a minute: a long command or an approval
            "Ship it": "your-turn",        # the latest index line wins
            "From the db": "working",      # no index line: the app's thread name, never its first prompt
            "tools": "idle",               # codex exec, done: nobody's turn; titled after its folder
            "Daily chore": "idle",         # an automation run, done
            "Lead thread": "working",      # waiting on its sub-thread
            "Stopped": "your-turn",        # turn_aborted
            "Left open": "idle",           # a turn open with nothing written for 20 minutes
        })
        self.assertEqual(got["Fix the parser"]["detail"], "exec")
        kid, gone = sorted(got["Lead thread"]["subagents"], key=lambda x: x["done"])  # and one just closed
        self.assertEqual((kid["type"], kid["desc"], kid["done"]), ("explorer", "Scan the tests", False))
        self.assertAlmostEqual(kid["since"], time.time() - 31, delta=3)  # when it started
        self.assertTrue(gone["done"])
        self.assertNotIn("SECRET FIRST PROMPT", json.dumps(list(got.values())))
        self.assertAlmostEqual(got["Ship it"]["since"], time.time() - 240, delta=3)

    def test_turn_parsing(self):
        entries = [{"timestamp": isotime(a), "type": k, "payload": p} for a, k, p in
                   [started(30), call(20, "a"), call(20, "b", "js"), output(10, "a")]]
        turn, calls, last, error = aw.codex_turn(entries)
        self.assertEqual((turn, calls), ("started", {"b": "js"}))
        self.assertAlmostEqual(last, time.time() - 10, delta=2)
        entries.append({"timestamp": isotime(5), "type": "event_msg",
                        "payload": {"type": "task_complete", "error": "boom"}})
        self.assertEqual(aw.codex_turn(entries)[:2], ("task_complete", {}))
        self.assertTrue(aw.codex_turn(entries)[3])

    def test_locked_database_is_skipped_not_fatal(self):
        db = sqlite3.connect(str(self.codex / "state_5.sqlite"), timeout=0)
        db.execute("BEGIN EXCLUSIVE")
        try:
            titles = {s["title"] for s in aw.codex_sessions(3, time.time())}
        finally:
            db.rollback()
            db.close()
        self.assertIn("Fix the parser", titles)
        self.assertIn("myproj", titles)  # thread-work's name was in the database: its folder instead

    def test_no_database_and_no_index(self):
        (self.codex / "state_5.sqlite").unlink()
        (self.codex / "session_index.jsonl").unlink()
        self.assertEqual({s["title"] for s in aw.codex_sessions(3, time.time())}, {"myproj", "tools"})


# ---- opencode ------------------------------------------------------------------------------------------

def ms(age):
    return int((time.time() - age) * 1000)


class OpencodeFixture:
    def make_opencode(self):
        db = sqlite3.connect(str(aw.OPENCODE_DB))
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, parent_id TEXT, "
                   "slug TEXT NOT NULL, directory TEXT NOT NULL, title TEXT NOT NULL, version TEXT NOT NULL, "
                   "permission TEXT, time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, "
                   "time_compacting INTEGER, time_archived INTEGER)")
        db.execute("CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, "
                   "time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL)")
        db.execute("CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT NOT NULL, session_id TEXT NOT NULL,"
                   " time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL)")
        db.execute("CREATE TABLE permission (project_id TEXT PRIMARY KEY, time_created INTEGER NOT NULL, "
                   "time_updated INTEGER NOT NULL, data TEXT NOT NULL)")
        self.oc = db
        self.n = 0

    def oc_session(self, sid, title, age, parent=None, archived=False):
        self.oc.execute("INSERT INTO session (id, project_id, parent_id, slug, directory, title, version, "
                        "time_created, time_updated, time_archived) "
                        "VALUES (?, 'p', ?, 's', '/Users/me/code/app', ?, '1.3.3', ?, ?, ?)",
                        (sid, parent, title, ms(age + 600), ms(age), ms(age) if archived else None))

    def oc_message(self, sid, age, role="assistant", completed=True, finish="stop", tools=(), **extra):
        self.n += 1
        mid = "msg_%03d" % self.n
        t = {"created": ms(age + 5)}
        if completed:
            t["completed"] = ms(age)
        data = dict({"role": role, "time": t, "agent": extra.pop("agent", "build")}, **extra)
        if role == "assistant" and completed:
            data["finish"] = finish
        self.oc.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?)", (mid, sid, ms(age + 5), ms(age),
                                                                       json.dumps(data)))
        for i, (tool, status) in enumerate(tools):
            part = {"type": "tool", "tool": tool, "callID": "c%d" % i,
                    "state": {"status": status, "input": {}, "time": {"start": ms(age)}}}
            self.oc.execute("INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                            ("prt_%03d_%d" % (self.n, i), mid, sid, ms(age), ms(age), json.dumps(part)))
        return mid

    def opencode_world(self):
        self.make_opencode()
        s = self.oc_session
        s("ses_done", "Plan the release", 120)
        self.oc_message("ses_done", 120)
        s("ses_bash", "Run the build", 60)
        self.oc_message("ses_bash", 60, completed=False, tools=[("read", "completed"), ("bash", "running")])
        s("ses_ask", "Pick a name", 30)
        self.oc_message("ses_ask", 30, completed=False, tools=[("question", "running")])
        s("ses_lead", "Refactor the parser", 100)
        self.oc_message("ses_lead", 100, completed=False, tools=[("task", "running")])
        s("ses_kid", "find tests (@explore subagent)", 5, parent="ses_lead")
        self.oc_message("ses_kid", 5, completed=False, agent="explore", tools=[("grep", "running")])
        s("ses_lead2", "Write the docs", 200)
        self.oc_message("ses_lead2", 200, completed=False, tools=[("task", "running")])
        s("ses_kid2", "ask about style (@general subagent)", 50, parent="ses_lead2")
        self.oc_message("ses_kid2", 50, completed=False, agent="general", tools=[("question", "pending")])
        s("ses_stop", "Stopped one", 90)
        self.oc_message("ses_stop", 90, completed=True, error={"name": "MessageAbortedError"})
        s("ses_new", "New prompt", 5)
        self.oc_message("ses_new", 5, role="user")
        s("ses_arch", "Archived one", 10, archived=True)
        self.oc_message("ses_arch", 10)
        s("ses_old", "Old one", 5 * 3600)
        self.oc_message("ses_old", 5 * 3600)
        self.oc.commit()


class OpencodeSource(OpencodeFixture, unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        isolate(self.root)
        self.opencode_world()

    def tearDown(self):
        self.oc.close()

    def test_states_titles_and_subagents(self):
        got = {s["title"]: s for s in aw.opencode_sessions(3, time.time())}  # its writer still open (WAL)
        self.assertEqual({t: s["state"] for t, s in got.items()}, {
            "Plan the release": "your-turn",
            "Run the build": "busy",
            "Pick a name": "question",          # the question tool is open
            "Refactor the parser": "working",   # its subagent runs
            "Write the docs": "question",       # its subagent asks you
            "Stopped one": "your-turn",
            "New prompt": "working",
        })
        self.assertEqual(got["Run the build"]["detail"], "bash")
        sub = got["Refactor the parser"]["subagents"][0]
        self.assertEqual((sub["type"], sub["desc"], sub["done"]), ("explore", "find tests", False))
        self.assertAlmostEqual(sub["since"], time.time() - 605, delta=3)  # the child session's creation
        sub = got["Write the docs"]["subagents"][0]
        self.assertEqual((sub["type"], sub["desc"]), ("general", "ask about style"))
        self.assertEqual(aw.compact_sub(sub)["type"], "")  # generic: the boards show the description

    def test_unreadable_database_keeps_the_last_sessions(self):
        self.oc.close()
        first = aw.collect("opencode", 3, time.time())
        self.assertEqual(len(first), 7)
        aw.OPENCODE_DB.write_bytes(b"not a database" * 100)
        for extra in ("-wal", "-shm"):
            try:
                Path(str(aw.OPENCODE_DB) + extra).unlink()
            except FileNotFoundError:
                pass
        self.oc = sqlite3.connect(":memory:")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(aw.collect("opencode", 3, time.time()), first)
        self.assertIn("opencode:", err.getvalue())
        self.assertTrue(aw._last_error["opencode"])


# ---- the merged snapshot -------------------------------------------------------------------------------

class MergedSnapshot(CodexFixture, OpencodeFixture, unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        isolate(self.root)

    def tearDown(self):
        if getattr(self, "oc", None):
            self.oc.close()

    def claude_session(self, title):
        proj = aw.PROJECTS / "-Users-me-code-myproj"
        proj.mkdir(parents=True, exist_ok=True)
        sid = "c%d" % len(list(proj.glob("*.jsonl")))
        (proj / (sid + ".jsonl")).write_text(json.dumps(
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "x"}],
                                              "stop_reason": "end_turn"}}) + "\n")
        (proj / sid).mkdir()
        (proj / sid / "custom-title.json").write_text(json.dumps({"customTitle": title}))

    def test_all_tools(self):
        self.claude_session("A Claude session with a title that goes on and on and on, far too long")
        self.codex_world()
        self.opencode_world()
        snap = aw.snapshot(3)
        self.assertEqual(snap["tools"], {"claude": {"n": 1, "waiting": 1}, "codex": {"n": 8, "waiting": 2},
                                         "opencode": {"n": 7, "waiting": 4}})
        self.assertEqual(snap["waiting"], 7)
        by_tool = {}
        for s in snap["sessions"]:
            by_tool.setdefault(s["tool"], []).append(s)
        self.assertEqual({t: len(v) for t, v in by_tool.items()}, {"claude": 1, "codex": 8, "opencode": 7})
        self.assertEqual([s["state"] for s in snap["sessions"]][:3], ["question", "question", "your-turn"])
        claude = by_tool["claude"][0]
        self.assertLessEqual(len(claude["title"]), aw.TITLE_LEN)
        self.assertTrue(claude["title"].endswith("~"))
        self.assertNotIn("detail", claude)      # empty fields are left out
        self.assertNotIn("subagents", claude)
        self.assertLessEqual(len(aw.encode(snap)), 6000)

    def test_caps(self):
        self.make_codex()
        for i in range(12):
            self.rollout("thread-%02d" % i, [started(100 + i), complete(50 + i)])
        self.rollout("thread-lead", [started(200), call(100, "w1", "wait")])
        for i in range(5):
            spawn = {"subagent": {"thread_spawn": {"parent_thread_id": "thread-lead", "agent_role": "worker"}}}
            self.rollout("kid-%d" % i, [started(30), item(3, MSG)], source=spawn)
        self.index(("kid-0", "a very long description of what this sub-thread is doing right now"))
        snap = aw.snapshot(3, ["codex"])
        self.assertEqual(snap["tools"], {"codex": {"n": 13, "waiting": 12}})
        self.assertEqual(len(snap["sessions"]), aw.PER_TOOL)
        self.assertEqual(snap["waiting"], 12)  # counts beyond what's sent
        lead = [s for s in aw.codex_sessions(3, time.time()) if s["title"] == "myproj" and s["subagents"]][0]
        small = aw.compact(lead, "codex")
        self.assertEqual((len(small["subagents"]), small["nsub"]), (aw.SUBS, 5))
        self.assertTrue(all(len(x["desc"]) <= aw.SUB_DESC_LEN for x in small["subagents"]))

    def test_missing_tools_and_sources(self):
        self.claude_session("Only Claude here")
        snap = aw.snapshot(3)
        self.assertEqual(list(snap["tools"]), ["claude"])  # no Codex or opencode on this computer
        self.assertEqual([s["title"] for s in snap["sessions"]], ["Only Claude here"])
        self.codex_world()
        self.assertEqual(list(aw.snapshot(3, ["codex"])["tools"]), ["codex"])
        self.assertEqual(aw.snapshot(3, ["opencode"])["sessions"], [])


# ---- the board's views ---------------------------------------------------------------------------------

COLS, ROWS = 296 // 6, 128 // 12  # the MagTag with terminalio's font

OLD = {"generated": 1000, "clock": "12:00", "host": "mac", "waiting": 1,  # a watcher from before Codex
       "sessions": [{"id": "a1b2c3d4-0000", "title": "Old format", "state": "your-turn", "detail": "",
                     "since": 900, "subagents": [{"type": "Explore", "desc": "find tests"}]}]}

NEW = {"generated": 1000, "clock": "12:00", "host": "mac", "waiting": 3,
       "tools": {"claude": {"n": 1, "waiting": 0}, "codex": {"n": 2, "waiting": 2},
                 "opencode": {"n": 0, "waiting": 0}},
       "sessions": [{"id": "c1", "tool": "codex", "title": "Ship it", "state": "your-turn", "since": 990},
                    {"id": "c2", "tool": "codex", "title": "x" * 60, "state": "question", "since": 980,
                     "subagents": [{"type": "worker", "desc": "y" * 40}] * 3, "nsub": 5},
                    {"id": "a1", "tool": "claude", "title": "Claude one", "state": "working", "since": 999}]}


class BoardViews(unittest.TestCase):
    def check_fits(self, lines):
        self.assertLessEqual(len(lines), ROWS)
        for line in lines:
            self.assertLess(len(line), COLS, line)  # Terminal wraps a full-width line

    def test_old_watcher_means_claude(self):
        self.assertEqual(views.views(OLD), ["claude", "quote"])
        self.assertEqual(views.next_view("claude", OLD), "quote")
        self.assertEqual(views.next_view("quote", OLD), "claude")
        lines = views.agent_lines(OLD, "claude", COLS, ROWS, False, "WiFi")
        self.check_fits(lines)
        self.assertTrue(lines[0].startswith("CLAUDE CODE  1 waiting"))
        self.assertEqual(lines[1], "")  # no other tools: no tab line
        self.assertTrue(lines[2].startswith("! Old format"))
        self.assertEqual(lines[3].rstrip(), "  + Explore: find tests")  # an old watcher: no start time

    def test_views_cycle_every_tool_the_watcher_reads(self):
        # opencode has no sessions in NEW but the watcher reads it, so it still gets a view
        self.assertEqual(views.views(NEW), ["claude", "codex", "opencode", "quote"])
        self.assertEqual(views.next_view("claude", NEW), "codex")
        self.assertEqual(views.next_view("codex", NEW), "opencode")
        self.assertEqual(views.next_view("opencode", NEW), "quote")
        empty = views.agent_lines(NEW, "opencode", COLS, ROWS, False, "USB")
        self.assertIn("no recent opencode sessions", empty)
        self.check_fits(empty)
        only_claude = dict(NEW, tools={"claude": {"n": 1, "waiting": 1}})
        self.assertEqual(views.views(only_claude), ["claude", "codex", "quote"])  # codex: it has sessions
        self.assertEqual(views.views({}), ["claude", "quote"])

    def test_headers_mark_the_views_that_wait(self):
        claude = views.agent_lines(NEW, "claude", COLS, ROWS, False, "USB")
        self.assertTrue(claude[0].startswith("CLAUDE CODE "))
        self.assertNotIn("waiting", claude[0])
        self.assertEqual(claude[1], "[Claude]  Codex 2!  opencode")
        codex = views.agent_lines(NEW, "codex", COLS, ROWS, False, "USB")
        self.assertTrue(codex[0].startswith("CODEX  2 waiting"))
        self.assertTrue(codex[0].endswith("USB 12:00"))
        self.assertEqual(codex[1], "Claude  [Codex 2!]  opencode")
        self.assertTrue(codex[2].startswith("! Ship it"))
        self.assertTrue(codex[3].startswith("? xxxx"))
        self.assertEqual(codex[4].rstrip(), "  + " + "y" * 40)  # worker: generic, so no type
        self.assertIn("    +2 more", codex)
        self.check_fits(claude)
        self.check_fits(codex)
        empty = views.agent_lines(NEW, "opencode", COLS, ROWS, True, "WiFi")
        self.assertTrue(empty[0].startswith("WATCHER OFFLINE since 12:00"))
        self.assertIn("no recent opencode sessions", empty)

    def test_many_sessions_fit(self):
        d = dict(NEW, sessions=[dict(NEW["sessions"][1], id=str(i)) for i in range(8)])
        lines = views.agent_lines(d, "codex", COLS, ROWS, False, "WiFi")
        self.check_fits(lines)
        self.assertTrue(lines[-1].startswith("... "))

    def test_rows(self):
        d = {"generated": 10000, "sessions": [
            {"id": "a", "title": "Fix the parser", "state": "your-turn", "since": 10000 - 540, "subagents": [
                {"type": "Explore", "desc": "find tests", "since": 10000 - 130},
                {"type": "general-purpose", "desc": "Find Project Bundle", "since": 10000 - 20},
                {"type": "codex:codex-rescue", "desc": "second opinion", "since": 9000, "done": 1},
                {"type": "claude-code-guide", "desc": "hooks docs", "since": 9000}]},
            {"id": "b", "title": "t" * 80, "state": "working", "since": 9990}]}
        lines = views.agent_lines(d, "claude", COLS, ROWS, False, "")
        self.check_fits(lines)
        self.assertEqual(lines[2:], [
            "! Fix the parser" + " " * 30 + "9m",
            "  + Explore: find tests" + " " * 23 + "2m",
            "  + Find Project Bundle" + " " * 22 + "<1m",
            "  " + views.CHECK + " codex-rescue: second opinion" + " " * 16,  # drawn as a check mark
            "    +1 more",
            "> " + "t" * 46])
        self.assertEqual({len(l) for l in lines[2:4]}, {COLS - 1})

    def test_no_string_methods_circuitpython_lacks(self):
        for name in ("views.py", "code.py"):
            src = (HERE.parent / "apps" / "agents" / name).read_text()
            self.assertIsNone(re.search(r"\.(ljust|rjust|center)\(", src), name)


if __name__ == "__main__":
    unittest.main()
