# The agents screens of apps/agents, from a snapshot of extras/agents_watch.py: four cards side by side,
# each under one of the MagTag's four LEDs and above one of its four buttons, and a page for one session. A card is an icon for the state and how long it has been in it, the title, its subagents
# (+ running, a check mark finished) and the tool:
#   ? 3m    a question or choice open for you, 3 minutes   > 12m   working on this turn for 12 minutes
#   || 9m   paused: finished its turn 9 minutes ago        ~ 2m    busy: a long command or a permission
#   * 12m   its turn is over, but it waits on its own              prompt
#           background command or subagent                 z 2h    idle for 2 hours
# (code.py draws the icons: a question mark, a pause sign, a gear, a play triangle, an hourglass, a Z)
# Cards that wait for you (and haven't been looked at) get a black bar along their top, under their lit LED.
# A button opens a page for its column's session: what it's doing, its subagents, and how its latest reply
# ends (a frozen frame; the board goes back to the grid by itself).
# Plain Python, so the host's unit tests can import it too. CircuitPython's str has no ljust/rjust/center:
# use pad() and rjust().
SHORT = {"claude": "Claude", "codex": "Codex", "opencode": "opencode"}
LONG = {"claude": "Claude Code", "codex": "Codex", "opencode": "opencode"}
WAITING = ("question", "your-turn")
STATES = ("question", "your-turn", "busy", "working", "background", "idle")
ACTIVE = ("busy", "working", "background")  # timed from the turn's prompt
WORDS = {"question": "QUESTION FOR YOU", "your-turn": "YOUR TURN", "busy": "BUSY", "working": "WORKING",
         "background": "BACKGROUND", "idle": "IDLE"}
# Subagent types that say nothing: the line shows the description alone (old watchers send these)
GENERIC = ("general-purpose", "general", "agent", "fork", "default", "subagent", "build", "worker")
TYPE_LEN = 12
FRESH = 3600    # a session waiting longer than this no longer counts as new
CHECK = "\x01"  # terminalio's font has no check mark, so code.py draws one where this character is


def pad(s, n):
    return s[:n] + " " * max(0, n - len(s))


def rjust(s, n):
    return " " * max(0, n - len(s)) + s


def tool_of(s):
    return s.get("tool") or "claude"  # watchers before Codex and opencode sent no tool


def key(s):
    return tool_of(s) + ":" + str(s.get("id", ""))


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


def elapsed(s, gen):
    """How long the session has been in its state: working (busy, in the background) since the turn's
    prompt, otherwise since it last did anything. "" for a working session from a watcher that sends no
    turn times."""
    if s.get("state") in ACTIVE:
        return age(gen - s["turn"]) if s.get("turn") else ""
    return age(gen - s.get("since", gen))


def needs_you(s, gen, acked):
    """Waiting for you, recently, and not yet looked at (acked holds (id, since) of those that were)."""
    return (s.get("state") in WAITING and gen - s.get("since", gen) < FRESH
            and (s.get("id"), s.get("since")) not in acked)


def ranked(d, acked):
    """The sessions in the order the cards take them: waiting for you, then busy, working and in the
    background, then the rest (looked at, or idle); the most recently active first within each."""
    gen = d.get("generated", 0)

    def rank(s):
        if needs_you(s, gen, acked):
            r = 0
        elif s.get("state") in ACTIVE:
            r = 1
        else:
            r = 2
        return (r, -s.get("since", 0))
    return sorted([s for s in d.get("sessions", []) if s.get("state") in STATES], key=rank)


def assign(slots, keys, n):
    """The cards' places: the first n of keys, those already on screen (slots, None for an empty place)
    staying where they were, so a card doesn't jump to another column (and LED) as others change."""
    want = keys[:n]
    out = [k if k in want else None for k in (list(slots) + [None] * n)[:n]]
    for k in want:
        if k not in out:
            out[out.index(None)] = k
    return out


def wrap(text, width, rows):
    """text in at most rows lines of at most width characters; a word longer than a line is cut, and text
    that doesn't fit ends in ~ on the last line."""
    lines, line = [], ""
    for word in text.split():
        while len(word) > width:
            if line:
                lines.append(line)
                line = ""
            lines.append(word[:width])
            word = word[width:]
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = line + " " + word if line else word
    if line:
        lines.append(line)
    if len(lines) > rows:  # the last line takes as much of the rest as fits
        lines = lines[:rows - 1] + [" ".join(lines[rows - 1:])[:width - 1] + "~"]
    return lines


def sub_text(sub):
    """ "Explore: find tests", or the description alone when the type is generic or long."""
    t = (sub.get("type") or "").split(":")[-1]
    desc = sub.get("desc") or ""
    if t and t.lower() not in GENERIC and len(t) <= TYPE_LEN:
        return t + ": " + desc if desc else t
    return desc or t


def sub_line(sub, gen, width):
    """A subagent in width characters: + running (and for how long) or a check mark finished, its text."""
    right = "" if sub.get("done") or not sub.get("since") else " " + age(gen - sub["since"])
    return (CHECK if sub.get("done") else "+") + pad(sub_text(sub), width - 1 - len(right)) + right


def card(s, gen, acked):
    """What one card shows, as a tuple the board compares to tell when to redraw:
    (state, elapsed, title lines, subagent lines, footer, marked: waiting for you).
    The title takes 4 lines of 8 (the larger font); with subagents, 2 lines, then up to 3 subagents in 12."""
    subs = s.get("subagents") or []
    nsub = max(len(subs), s.get("nsub", 0))
    title = s.get("title", "") or "?"
    if subs:
        lines = wrap(title, 8, 2)
        shown = [sub_line(x, gen, 12) for x in subs[:3]]
    else:
        lines, shown = wrap(title, 8, 4), []
    footer = SHORT.get(tool_of(s), tool_of(s)).lower()
    if nsub > len(shown):
        footer += " +%d" % (nsub - len(shown))
    return (s.get("state"), elapsed(s, gen), tuple(lines), tuple(shown), footer, needs_you(s, gen, acked))


def page(s, gen, head_cols, body_cols, body_rows):
    """One session's page, frozen when it opens: (head, body).
    head: 2 lines of the larger font, head_cols - 1 wide (the first two characters are left for code.py's
    icon): the state, for how long and the tool; the title. body: up to body_rows lines of the small font,
    body_cols - 1 wide: what it's doing now, its subagents, and the end of its latest reply ("said", from a
    watcher run with --text), as much of it as fits."""
    width = head_cols - 1
    state = s.get("state")
    words = WORDS.get(state, state or "")
    left = "  " + words + "  " + elapsed(s, gen)
    tool = LONG.get(tool_of(s), tool_of(s)).upper()
    if len(left) + 1 + len(tool) > width:
        tool = SHORT.get(tool_of(s), tool_of(s)).upper()
    head = [pad(left, width - len(tool)) + tool, wrap(s.get("title", ""), width, 1)[0] if s.get("title") else ""]

    bw = body_cols - 1
    body = []
    doing = s.get("doing") or (s.get("detail") if state == "busy" else "")
    if doing:
        body.append(("> " + doing)[:bw])
    subs = s.get("subagents") or []
    nsub = max(len(subs), s.get("nsub", 0))
    body += [sub_line(x, gen, bw) for x in subs[:3]]
    if nsub > 3:
        body.append("+%d more" % (nsub - 3))
    said = s.get("said") or ""
    room = body_rows - len(body) - (1 if body and said else 0)
    if said and room > 0:
        if body:
            body.append("")
        body += wrap(said, bw, 1000)[-room:]  # the end of the reply: where it got to
    if not body:
        body.append("Nothing more to show. To see how each")
        body.append("agent's latest reply ends, run the watcher")
        body.append("with --text.")
    return head, body[:body_rows]
