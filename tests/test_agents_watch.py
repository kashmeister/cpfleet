"""The agents watcher (extras/agents_watch.py) with Claude Code transcripts, and its USB and HTTP sides."""
import importlib.util
import json
import os
import select
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


aw = load("agents_watch", HERE.parent / "extras" / "agents_watch.py")
usblink = load("usblink", HERE.parent / "apps" / "agents" / "usblink.py")  # the board's side


def assistant(*content, stop=None):
    return {"type": "assistant", "message": {"role": "assistant", "content": list(content), "stop_reason": stop}}


def tool_use(tid, name):
    return {"type": "tool_use", "id": tid, "name": name, "input": {}}


def tool_result(tid):
    return {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid}]}}


PROMPT = {"type": "user", "message": {"role": "user", "content": "do the thing"}}
TEXT = {"type": "text", "text": "done"}


class ConversationState(unittest.TestCase):
    def test_states(self):
        s = aw.conversation_state
        self.assertEqual(s([PROMPT, assistant(TEXT, stop="end_turn")]), ("your-turn", ""))
        self.assertEqual(s([PROMPT, assistant(tool_use("t1", "Bash"), stop="tool_use")]), ("tool", "Bash"))
        self.assertEqual(s([PROMPT, assistant(tool_use("t1", "Bash")), tool_result("t1")]), ("other", ""))
        self.assertEqual(s([PROMPT, assistant(tool_use("q", "AskUserQuestion"))]), ("question", "question"))
        self.assertEqual(s([PROMPT, assistant(tool_use("p", "ExitPlanMode"))]), ("question", "plan approval"))
        self.assertEqual(s([assistant(TEXT, stop="end_turn"), PROMPT]), ("other", ""))  # a new prompt: working

    def test_text_is_the_agents_own(self):
        bash = dict(tool_use("t1", "Bash"), input={"command": "make", "description": "Run the tests"})
        entries = [PROMPT, assistant({"type": "text", "text": "**Checking** the \u201cparser\u201d"}),
                   assistant(bash)]
        self.assertEqual(aw.claude_text(entries), ("**Checking** the \u201cparser\u201d", "Bash: Run the tests"))
        self.assertEqual(aw.claude_text(entries + [tool_result("t1")])[1], "")
        self.assertEqual(aw.claude_text([PROMPT]), ("", ""))  # never the prompt
        self.assertEqual(aw.plain("**Checking** the \u201cparser\u201d\n\n# Done \u2014 ok"),
                         'Checking the "parser" Done - ok')
        self.assertEqual(aw.tail_text("x" * 10 + " " + "word " * 100, 40), "..." + "word " * 6 + "word")

    def test_turn_started(self):
        def at(e, ts):
            return dict(e, timestamp="2026-01-02T03:%02d:00.000Z" % ts)
        t = aw.turn_started
        base = aw.iso_time("2026-01-02T03:00:00Z")
        entries = [at(PROMPT, 1), at(assistant(TEXT, stop="end_turn"), 2), at(PROMPT, 5),
                   at(assistant(tool_use("t1", "Bash")), 6), at(tool_result("t1"), 7)]
        self.assertEqual(t(entries), base + 5 * 60)  # the last prompt; a tool result isn't one
        self.assertEqual(t(entries[3:]), base + 6 * 60)  # the prompt is further back: the earliest entry
        self.assertIsNone(t([PROMPT]))


class Snapshot(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        aw.PROJECTS = self.root
        aw.CODEX_HOME = self.root / "no-codex"  # this computer's own Codex and opencode stay out of it
        aw.OPENCODE_DB = self.root / "no-opencode.db"
        aw.DESKTOP_SESSIONS = self.root / "desktop"
        self.proj = self.root / "-Users-me-code-myproj"
        self.proj.mkdir()

    def session(self, sid, entries, title=None, age=0):
        p = self.proj / (sid + ".jsonl")
        p.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        os.utime(p, (time.time() - age, time.time() - age))
        if title:
            (self.proj / sid).mkdir(exist_ok=True)
            (self.proj / sid / "custom-title.json").write_text(json.dumps({"customTitle": title}))
        return p

    def test_titles_desktop_then_latest_rename_then_file(self):
        rename = {"type": "custom-title", "customTitle": "New name"}
        self.session("r", [PROMPT, rename, assistant(TEXT, stop="end_turn")], title="Old name")  # stale file
        self.session("f", [PROMPT, assistant(TEXT, stop="end_turn")], title="From the file")
        self.session("d", [PROMPT, rename, assistant(TEXT, stop="end_turn")], title="Old name")
        self.session("x", [PROMPT, assistant(TEXT, stop="end_turn")], title="Archived")
        app = self.root / "desktop" / "acct" / "org"
        app.mkdir(parents=True)
        (app / "local_1.json").write_text(json.dumps({"cliSessionId": "d", "title": "Desktop name"}))
        (app / "local_2.json").write_text(json.dumps({"cliSessionId": "x", "title": "Gone", "isArchived": True}))
        got = sorted(s["title"] for s in aw.snapshot(hours=3)["sessions"])
        self.assertEqual(got, ["Desktop name", "From the file", "New name"])

    def test_background_task_keeps_it_working(self):
        launch = assistant(dict(tool_use("bg", "Bash"), input={"command": "make", "description": "Build it",
                                                               "run_in_background": True}))
        started = dict(tool_result("bg"), toolUseResult={"backgroundTaskId": "b123", "stdout": ""})
        done = assistant(TEXT, stop="end_turn")
        news = {"type": "attachment", "attachment": {"type": "queued_command", "prompt":
                "<task-notification>\n<task-id>b123</task-id>\n<status>completed</status>\n</task-notification>"}}
        agent = dict(tool_result("ag"), toolUseResult={"agentId": "a9", "isAsync": True, "status": "async_launched"})
        self.session("w", [PROMPT, launch, started, done], title="Waits on a build")
        self.session("d", [PROMPT, launch, started, done, news], title="Build finished")
        self.session("a", [PROMPT, assistant(tool_use("ag", "Agent")), agent, done], title="Waits on an agent")
        self.session("s", [PROMPT, launch, started, done], title="Stale", age=2 * 3600)
        got = {s["title"]: s["state"] for s in aw.snapshot(hours=3)["sessions"]}
        self.assertEqual(got, {"Waits on a build": "background", "Build finished": "your-turn",
                               "Waits on an agent": "background", "Stale": "your-turn"})
        self.assertEqual(aw.background_tasks([PROMPT, launch, started]), {"b123": "Bash: Build it"})
        user_news = {"type": "user", "message": {"role": "user", "content":
                     "<task-notification><task-id>b123</task-id><status>failed</status></task-notification>"}}
        self.assertEqual(aw.background_tasks([PROMPT, launch, started, user_news]), {})

    def test_background_launch_far_back(self):
        # The launch is further back than the tail read for the state: the whole file is followed
        launch = assistant(dict(tool_use("bg", "Bash"), input={"command": "make", "run_in_background": True}))
        started = dict(tool_result("bg"), toolUseResult={"backgroundTaskId": "b7"})
        bulk = [assistant({"type": "text", "text": "x" * 4000}, stop="end_turn")] * 200  # ~800 KB
        p = self.session("far", [PROMPT, launch, started] + bulk, title="Far back")
        self.assertEqual(aw.snapshot(hours=3)["sessions"][0]["state"], "background")
        with open(p, "a") as f:  # the news arrives; only the new bytes are read
            f.write(json.dumps({"type": "attachment", "attachment": {"type": "queued_command", "prompt":
                    "<task-notification><task-id>b7</task-id><status>completed</status></task-notification>"}})
                    + "\n")
        self.assertEqual(aw.snapshot(hours=3)["sessions"][0]["state"], "your-turn")

    def test_running_subagent_after_the_turn_is_background(self):
        s = {"state": "your-turn", "subagents": [{"done": False}]}
        aw.busy_with_subagents(s)
        self.assertEqual(s["state"], "background")
        s = {"state": "idle", "subagents": [{"done": False}]}
        aw.busy_with_subagents(s)
        self.assertEqual(s["state"], "working")

    def test_task_news_is_not_a_prompt(self):
        def at(e, minute):
            return dict(e, timestamp="2026-01-02T03:%02d:00.000Z" % minute)
        news = {"type": "user", "message": {"role": "user", "content": "<task-notification>...</task-notification>"}}
        base = aw.iso_time("2026-01-02T03:00:00Z")
        self.assertEqual(aw.turn_started([at(PROMPT, 1), at(news, 5)]), base + 60)

    def test_states_titles_subagents_and_order(self):
        self.session("a", [PROMPT, assistant(TEXT, stop="end_turn")], title="Finished one", age=120)
        self.session("b", [PROMPT, assistant(tool_use("t", "Agent"))], title="Busy one", age=0)
        sub = self.proj / "b" / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-x.meta.json").write_text(json.dumps({"agentType": "Explore", "description": "find tests"}))
        (sub / "agent-x.jsonl").write_text(json.dumps(PROMPT) + "\n")
        self.session("c", [PROMPT, assistant(TEXT, stop="end_turn")], age=5 * 3600)  # too old
        self.session("d", [PROMPT, assistant(tool_use("t", "Bash"))], age=60)  # long tool call
        snap = aw.snapshot(hours=3)
        got = [(s["title"], s["state"]) for s in snap["sessions"]]
        # waiting on you first, then "busy" (maybe a permission prompt), then working
        self.assertEqual(got, [("Finished one", "your-turn"), ("myproj", "busy"), ("Busy one", "working")])
        self.assertEqual(snap["waiting"], 1)
        working = snap["sessions"][2]
        sub = working["subagents"][0]
        self.assertEqual((sub["type"], sub["desc"], "done" in sub), ("Explore", "find tests", False))
        self.assertAlmostEqual(sub["since"], time.time(), delta=5)


SNAP = {"generated": 100, "clock": "12:00", "host": "mac", "waiting": 0,
        "sessions": [{"id": "a", "title": "two\nlines", "state": "idle", "detail": "", "since": 90,
                      "subagents": []}]}


class LineFraming(unittest.TestCase):
    def test_encode_is_one_ascii_line_the_board_parses(self):
        line = aw.encode(SNAP)
        self.assertTrue(line.endswith(b"\n"))
        self.assertEqual(line.count(b"\n"), 1)  # the newline in the title is escaped
        line.decode("ascii")
        self.assertEqual(usblink.parse(line.strip()), ("snapshot", SNAP))

    def test_reader_splits_joins_and_drops(self):
        r = usblink.LineReader(limit=50)
        self.assertEqual(r.feed(b"?cpag"), [])
        self.assertEqual(r.feed(b"ents\r\n\n{\"a\""), [b"?cpagents"])
        self.assertEqual(r.feed(b":1}\n"), [b'{"a":1}'])
        self.assertEqual(r.feed(b"x" * 60), [])  # too long with no end: dropped
        self.assertEqual(r.feed(b"tail\nok\n"), [b"ok"])  # its end too: no half lines

    def test_parse(self):
        self.assertEqual(usblink.parse(b"?cpagents"), ("probe", None))
        self.assertEqual(usblink.parse(b'{"broken'), (None, None))
        self.assertEqual(usblink.parse(b'{"no":"sessions"}'), (None, None))
        self.assertEqual(usblink.parse(b">>> junk"), (None, None))
        self.assertEqual(usblink.hello("MagTag-1a2b"), b"!cpagents MagTag-1a2b\n")


class FeedLogic(unittest.TestCase):
    def test_sends_changes_at_once_and_keepalives_otherwise(self):
        f = aw.Feed(keepalive=5)
        self.assertIsNotNone(f.line(SNAP, 0))  # first: at once
        later = dict(SNAP, generated=102, clock="12:01")
        self.assertIsNone(f.line(later, 2))  # only the clock moved
        self.assertIsNotNone(f.line(later, 5))  # keep-alive
        changed = dict(later, waiting=1)
        self.assertIsNotNone(f.line(changed, 6))  # a change: at once
        self.assertIsNone(f.line(changed, 7))


class DataPorts(unittest.TestCase):
    def test_second_port_of_each_device(self):
        ports = ["/dev/cu.usbmodemC7AA1", "/dev/cu.usbmodemC7AA3", "/dev/cu.usbmodem14101",
                 "/dev/cu.usbmodemB2BB1", "/dev/cu.usbmodemB2BB4"]
        self.assertEqual(aw.data_ports(ports), ["/dev/cu.usbmodemB2BB4", "/dev/cu.usbmodemC7AA3"])


class Auth(unittest.TestCase):
    def test_token(self):
        self.assertTrue(aw.authorized(None, None))
        self.assertTrue(aw.authorized("Bearer s3cret", "s3cret"))
        self.assertFalse(aw.authorized("Bearer nope", "s3cret"))
        self.assertFalse(aw.authorized(None, "s3cret"))


class HttpToken(unittest.TestCase):
    """The HTTP handler on the loopback interface only."""

    def test_token_required_when_set(self):
        import urllib.error
        import urllib.request
        from http.server import ThreadingHTTPServer
        server = ThreadingHTTPServer(("127.0.0.1", 0), aw.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = "http://127.0.0.1:%d/agents.json" % server.server_address[1]
        aw.STATE["snapshot"], aw.TOKEN[0] = SNAP, "s3cret"
        try:
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(url, timeout=5)
            self.assertEqual(e.exception.code, 401)
            e.exception.close()
            req = urllib.request.Request(url, headers={"Authorization": "Bearer s3cret"})
            with urllib.request.urlopen(req, timeout=5) as r:
                self.assertEqual(json.load(r), SNAP)
        finally:
            aw.TOKEN[0] = None
            server.shutdown()
            server.server_close()


class FakeBoard(threading.Thread):
    """The master end of a pty, acting as apps/agents does with usblink: answers probes, collects
    snapshots. `answer=False` plays a board that isn't running the app."""

    def __init__(self, answer=True):
        super().__init__(daemon=True)
        self.master, self.slave = os.openpty()
        self.path = os.ttyname(self.slave)
        self.answer, self.snaps, self.probes = answer, [], 0
        self.stop = threading.Event()
        self.start()

    def run(self):
        reader = usblink.LineReader()
        while not self.stop.is_set():
            if not select.select([self.master], [], [], 0.05)[0]:
                continue
            try:
                data = os.read(self.master, 4096)
            except OSError:
                return
            for line in reader.feed(data):
                kind, value = usblink.parse(line)
                if kind == "probe":
                    self.probes += 1
                    if self.answer:
                        os.write(self.master, b"noise\n" + usblink.hello("MagTag-1a2b"))
                elif kind == "snapshot":
                    self.snaps.append(value)

    def close(self):
        self.stop.set()
        self.join(1)
        os.close(self.master)
        os.close(self.slave)


def wait_for(cond, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not cond():
        time.sleep(0.02)
    return cond()


class UsbHandshake(unittest.TestCase):
    def test_handshake(self):
        board = FakeBoard()
        try:
            port = aw.Port(board.path)
            self.assertEqual(aw.handshake(port), "MagTag-1a2b")
            port.close()
        finally:
            board.close()

    def test_no_answer(self):
        board = FakeBoard(answer=False)
        try:
            port = aw.Port(board.path)
            self.assertIsNone(aw.handshake(port, timeout=0.3))
            port.close()
        finally:
            board.close()


class UsbLinkLifecycle(unittest.TestCase):
    def test_feeds_boards_and_copes_with_ports_coming_and_going(self):
        board, mute = FakeBoard(), FakeBoard(answer=False)
        present = [board.path, mute.path]
        snap = dict(SNAP)
        logs = []
        link = aw.UsbLink(lambda: snap, ports=lambda: list(present), keepalive=5, retry=10, log=logs.append)
        try:
            link.step(now=0)
            self.assertEqual(list(link.boards), [board.path])
            self.assertTrue(wait_for(lambda: len(board.snaps) == 1))
            self.assertEqual(board.snaps[0], SNAP)
            self.assertEqual(mute.probes, 1)

            link.step(now=1)  # nothing new: nothing sent, the mute port isn't probed again yet
            snap["waiting"] = 3
            link.step(now=2)  # a change: sent at once
            self.assertTrue(wait_for(lambda: len(board.snaps) == 2))
            self.assertEqual(board.snaps[1]["waiting"], 3)
            link.step(now=7)  # keep-alive
            self.assertTrue(wait_for(lambda: len(board.snaps) == 3))
            link.step(now=11)  # retry the mute port
            self.assertTrue(wait_for(lambda: mute.probes == 2))

            present.remove(board.path)  # unplugged
            link.step(now=12)
            self.assertEqual(link.boards, {})
            present.append(board.path)  # plugged in again: probed and fed at once
            link.step(now=13)
            self.assertEqual(list(link.boards), [board.path])
            self.assertTrue(wait_for(lambda: len(board.snaps) == 4))
            self.assertTrue(any("unplugged" in m for m in logs))
        finally:
            for p, _, _ in link.boards.values():
                p.close()
            board.close()
            mute.close()


if __name__ == "__main__":
    unittest.main()
