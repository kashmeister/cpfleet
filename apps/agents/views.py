# The agents screens of apps/agents: one view per tool (Claude Code, Codex, opencode), from a snapshot of
# extras/agents_watch.py. A row is a state symbol, the title and how long it has been in that state:
#   > Port the board app          run 12m   working (or busy) for 12 minutes, since your prompt
#   ? Release notes                ask 3m   asking you something for 3 minutes
#   ! Fix the parser              idle 9m   finished its turn 9 minutes ago (idle: likewise)
#     + Explore: find tests            2m   a running subagent, and for how long
#     v write the summary                              one that finished in the last few minutes (drawn as a
#                                                      check mark: CHECK in the text marks where)
# Plain Python, so the host's unit tests can import it too. CircuitPython's str has no ljust/rjust/center:
# use pad() and rjust().
TOOLS = ("claude", "codex", "opencode")
CYCLE = TOOLS + ("quote",)         # button A's order
SHORT = {"claude": "Claude", "codex": "Codex", "opencode": "opencode"}
LONG = {"claude": "Claude Code", "codex": "Codex", "opencode": "opencode"}
WAITING = ("question", "your-turn")
SYMBOL = {"question": "?", "your-turn": "!", "busy": "~", "working": ">", "idle": "."}
# Subagent types that say nothing: the line shows the description alone (old watchers send these)
GENERIC = ("general-purpose", "general", "agent", "fork", "default", "subagent", "build", "worker")
TYPE_LEN = 12
SUBS = 3  # subagent lines per session
CHECK = "\x01"  # terminalio's font has no check mark, so code.py draws one where this character is


def pad(s, n):
    return s[:n] + " " * max(0, n - len(s))


def rjust(s, n):
    return " " * max(0, n - len(s)) + s


def tool_of(s):
    return s.get("tool") or "claude"  # watchers before Codex and opencode sent no tool


def waiting_by_tool(d):
    """{tool: sessions waiting for you}, from the watcher's totals if it sends them (they count sessions
    beyond those it sends), else from the sessions."""
    out = {}
    for s in d.get("sessions", []):
        if s.get("state") in WAITING:
            t = tool_of(s)
            out[t] = out.get(t, 0) + 1
    tools = d.get("tools")
    if isinstance(tools, dict):
        for t, v in tools.items():
            if isinstance(v, dict) and "waiting" in v:
                out[t] = v["waiting"]
    return out


def views(d):
    """The views button A cycles through: every tool the watcher reads (its "tools" field lists them, even
    with no sessions, so a quiet tool still has a view), then the quote. An older watcher without that
    field: Claude, plus any tool that has sessions."""
    if isinstance(d.get("tools"), dict) and d["tools"]:
        have = set(d["tools"]) | set(tool_of(s) for s in d.get("sessions", []))
    else:
        have = set(["claude"]) | set(tool_of(s) for s in d.get("sessions", []))
    return [v for v in CYCLE if v == "quote" or v in have]


def next_view(view, d):
    vs = views(d)
    i = CYCLE.index(view) if view in CYCLE else -1
    for k in range(1, len(CYCLE) + 1):
        v = CYCLE[(i + k) % len(CYCLE)]
        if v in vs:
            return v
    return "claude"


def age(seconds):
    """Coarse, so the screen doesn't redraw every minute: <1m, 1-9m, 10m steps, then hours."""
    m = seconds // 60
    if m < 1:
        return "<1m"
    if m < 10:
        return "%dm" % m
    if m < 60:
        return "%dm" % (m // 10 * 10)
    return "%dh" % (m // 60)


def timing(s, gen):
    """The right-hand column of a session's row: how long it has worked on this turn (from the prompt; a
    watcher from before "turn" doesn't say), has been asking you, or has done nothing (its turn over)."""
    state = s.get("state")
    if state in ("working", "busy"):
        return "run " + age(gen - s["turn"]) if s.get("turn") else ""
    since = s.get("since", gen)
    return ("ask " if state == "question" else "idle ") + age(gen - since)


def header(tool, waiting, offline, clock, width):
    """The view's first line, left part: the longest wording that fits in width."""
    name, short = LONG.get(tool, tool).upper(), SHORT.get(tool, tool).upper()
    if offline:
        options = ["WATCHER OFFLINE since " + clock, "OFFLINE since " + clock, "OFFLINE"]
    elif waiting:
        options = ["%s  %d waiting" % (name, waiting), "%s  %d waiting" % (short, waiting),
                   "%s %d wait" % (short, waiting)]
    else:
        options = [name, short]
    for h in options:
        if len(h) <= width:
            return h
    return options[-1]


def row(mark, text, right, cols):
    """mark + text, with `right` (an age, or "") flush right, in cols - 1 characters."""
    width = cols - 1 - len(mark)
    if right:
        return mark + pad(text, width - len(right) - 1) + " " + right
    return mark + pad(text, width)


def sub_text(sub):
    """ "Explore: find tests", or the description alone when the type is generic or long."""
    t = (sub.get("type") or "").split(":")[-1]
    desc = sub.get("desc") or ""
    if t and t.lower() not in GENERIC and len(t) <= TYPE_LEN:
        return t + ": " + desc if desc else t
    return desc or t


def agent_lines(d, tool, cols, rows, offline, source):
    """The text of one tool's view, at most `rows` lines of at most cols - 1 characters (the last column
    stays blank, as a margin)."""
    sessions = [s for s in d.get("sessions", []) if tool_of(s) == tool]
    gen = d.get("generated", 0)
    head = header(tool, waiting_by_tool(d).get(tool), offline, d.get("clock", "?"), cols - 12)
    tail = (source + " " if source else "") + d.get("clock", "")
    lines = [pad(head, cols - 12) + rjust(tail, 11)]  # the tool's name heads the view; no tab line
    if not sessions:
        lines.append("no recent %s sessions" % LONG.get(tool, tool))
        lines.append("(watcher --hours: how far back)")
    for s in sessions:
        state = s.get("state")
        if state not in SYMBOL:
            continue
        lines.append(row(SYMBOL[state] + " ", s.get("title", ""), timing(s, gen), cols))
        subs = s.get("subagents") or []
        for sub in subs[:SUBS]:
            done = sub.get("done")
            right = "" if done or not sub.get("since") else age(gen - sub["since"])
            lines.append(row("  " + CHECK + " " if done else "  + ", sub_text(sub), right, cols))
        more = max(len(subs), s.get("nsub", 0)) - SUBS
        if more > 0:
            lines.append("    +%d more" % more)
    if len(lines) > rows:
        lines = lines[:rows - 1] + ["... %d more lines" % (len(lines) - rows + 1)]
    return lines
