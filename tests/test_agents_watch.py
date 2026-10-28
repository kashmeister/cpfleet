"""The Claude Code agents watcher (extras/agents_watch.py), against fake transcripts."""
import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("agents_watch", HERE.parent / "extras" / "agents_watch.py")
aw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(aw)


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


class Snapshot(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        aw.PROJECTS = self.root
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
        self.assertEqual(working["subagents"], [{"type": "Explore", "desc": "find tests"}])


if __name__ == "__main__":
    unittest.main()
