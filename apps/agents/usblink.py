# The USB line protocol between extras/agents_watch.py --usb and apps/agents, on CircuitPython's second
# USB serial channel (usb_cdc.data). Plain Python, so the host's unit tests can import it too.
#   host -> board  "?cpagents"           a probe: is this an agents display?
#   board -> host  "!cpagents <name>"    yes, and its name
#   host -> board  {"generated": ...}    a snapshot, as /agents.json serves it, on one line; sent when it
#                                        changes and again every few seconds as a keep-alive
# Lines end with "\n". Anything else is ignored, so a stray or partial line does no harm.
import json

PROBE = b"?cpagents"
HELLO = b"!cpagents"
LIMIT = 16384  # longest line kept; a longer one is dropped (junk, or a stream joined mid-line)


class LineReader:
    """Splits a byte stream into lines; feed() returns the complete ones and keeps the rest."""

    def __init__(self, limit=LIMIT):
        self.rest = b""
        self.limit = limit
        self.skip = False  # dropping the rest of a line that was too long

    def feed(self, data):
        parts = (self.rest + bytes(data)).split(b"\n")
        self.rest = parts.pop()
        if self.skip and parts:
            parts.pop(0)
            self.skip = False
        if len(self.rest) > self.limit:
            self.rest, self.skip = b"", True
        return [p.strip() for p in parts if p.strip() and len(p) <= self.limit]


def parse(line):
    """("probe", None), ("snapshot", dict) or (None, None) for one line."""
    if line == PROBE:
        return "probe", None
    if line[:1] == b"{":
        try:
            d = json.loads(line.decode())
        except (ValueError, UnicodeError):
            return None, None
        if isinstance(d, dict) and "sessions" in d:
            return "snapshot", d
    return None, None


def hello(name):
    return HELLO + b" " + name.encode() + b"\n"
