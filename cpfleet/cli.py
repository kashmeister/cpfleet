"""cpfleet: manage several CircuitPython boards over Wi-Fi (web workflow), one short command at a time."""
import argparse
import getpass
import json
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import __version__, inventory, repl, settings, sync, web

JSON = False


def emit(rows, fmt):
    """Print one line per board (or JSON with --json)."""
    if JSON:
        print(json.dumps(rows, indent=None if len(rows) > 1 else 2))
        return
    for r in rows:
        print(fmt(r))


def each(boards, fn):
    """Run fn(board) for every board in parallel; returns rows in board order, errors captured."""
    def wrap(b):
        try:
            return dict({"name": b["name"], "host": b["host"]}, **(fn(b) or {}))
        except (web.WebError, repl.ReplError, ConnectionError, OSError, TimeoutError) as e:
            return {"name": b["name"], "host": b["host"], "error": str(e) or e.__class__.__name__}
    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(wrap, boards))


def line(r, ok):
    return "%-14s %-15s %s" % (r["name"], r["host"], ("error: " + r["error"]) if "error" in r else ok(r))


def state_of(bar):
    """CircuitPython status bar [ip, state, version] -> short state."""
    if not bar or len(bar) < 2:
        return "unknown"
    s = bar[1]
    if s == "code.py" or s.endswith(".py") and "@" not in s:
        return "running " + s
    if "@" in s:  # e.g. "51@code.py AttributeError"
        where, _, err = s.partition(" ")
        lineno, _, f = where.partition("@")
        return "error %s line %s: %s" % (f, lineno, err)
    return {"Done": "finished", "REPL": "at REPL"}.get(s, s)


def ws(b):
    return repl.WebSocket(b["host"], inventory.password())


# ---- commands -------------------------------------------------------------------------------

def cmd_ls(a):
    boards = inventory.load()
    if not boards:
        print("no boards yet: `cpfleet add <ip>` with one board's address (it finds the others)")
        return
    emit(boards, lambda b: "%-14s %-15s %-12s %s" % (b["name"], b["host"], b.get("mac", ""), b.get("board_id", "")))


def cmd_add(a):
    found = inventory.scan(a.hosts)
    if not found:
        sys.exit("no CircuitPython web workflow board answered at " + ", ".join(a.hosts))
    _scan_save(found)


def cmd_scan(a):
    boards = inventory.load()
    names, ips = inventory.mdns_names()
    found = inventory.scan([b["host"] for b in boards] + ips)
    _scan_save(found)
    known = {b["name"] for b in found}
    missing = [n for n in names if n not in known]
    if missing:
        print("also advertised on mDNS but not reached: " + ", ".join(missing))


def _scan_save(found):
    boards, changed = inventory.merge(inventory.load(), found)
    inventory.save(boards)
    for what, b in changed:
        print("%-7s %-14s %-15s %s" % (what, b["name"], b["host"], b.get("board_id", "")))
    if not changed:
        print("no changes (%d boards)" % len(boards))


def cmd_password(a):
    pw = getpass.getpass("Web workflow password (CIRCUITPY_WEB_API_PASSWORD): ")
    if not pw or pw != getpass.getpass("Repeat: "):
        sys.exit("passwords empty or don't match")
    inventory.set_password(pw)
    print("saved to %s (mode 600)" % inventory.PASSWORD)


def cmd_status(a):
    def one(b):
        v = web.version(b["host"])
        d = web.diskinfo(b["host"])
        row = {"version": v.get("version"), "board_id": v.get("board_id"),
               "free_kb": d.get("free", 0) * d.get("block_size", 512) // 1024}
        try:
            with ws(b) as t:
                row["state"] = state_of(repl.status_bar(t.read_for(a.wait).decode(errors="replace")))
                if row["state"] == "finished" and a.why:
                    # The board doesn't keep the error, so run code.py again and catch it.
                    repl.reload(t)
                    out = repl.clean(t.read_for(a.why_seconds).decode(errors="replace")).splitlines()
                    errors = [l.strip() for l in out if "Error" in l and ":" in l]
                    where = [l.strip() for l in out if l.strip().startswith('File "')]
                    if errors:
                        row["state"] = "crashed: %s%s" % (errors[-1], (" (%s)" % where[-1]) if where else "")
        except ConnectionError as e:
            row["state"] = "console: %s" % e
        if row["state"] == "finished":
            row["state"] = "ended (crashed or returned; --why)"
        return row
    emit(each(inventory.resolve(a.target), one),
         lambda r: line(r, lambda r: "%-7s %-28s %d KB free" % (r["version"], r["state"], r["free_kb"])))


def cmd_push(a):
    files = sync.plan(a.paths, a.dest)
    pw = inventory.password()

    def one(b):
        up, skipped, prot = sync.push(b["host"], pw, files, a.settings, a.dry_run)
        return {"uploaded": up, "skipped": skipped, "protected": prot}
    rows = each(inventory.resolve(a.to), one)
    emit(rows, lambda r: line(r, lambda r: "%s %d, unchanged %d%s" % (
        "would upload" if a.dry_run else "uploaded", len(r["uploaded"]), len(r["skipped"]),
        ", kept %s (use --settings to replace)" % " ".join(r["protected"]) if r["protected"] else "")))
    if any("error" in r for r in rows):
        sys.exit(1)


def cmd_cat(a):
    b = inventory.resolve(a.target)
    if len(b) != 1:
        sys.exit("cat needs one board")
    sys.stdout.buffer.write(web.read(b[0]["host"], inventory.password(), a.path))


def cmd_rm(a):
    pw = inventory.password()

    def one(b):
        for path in a.paths:
            web.delete(b["host"], pw, path)
    emit(each(inventory.resolve(a.target), one), lambda r: line(r, lambda r: "removed " + " ".join(a.paths)))


def cmd_run(a):
    code = Path(a.file).read_text() if a.file else a.code
    if code is None:
        sys.exit("give code or -f file.py")

    def one(b):
        with (repl.Serial(a.usb) if a.usb else ws(b)) as t:
            out, err = repl.run(t, code, a.timeout)
            if not a.stay:
                repl.reload(t)
        return {"out": out, "err": err}
    boards = [{"name": a.usb, "host": "usb"}] if a.usb else inventory.resolve(a.target)
    rows = each(boards, one)
    if JSON:
        return emit(rows, None)
    failed = False
    for r in rows:
        prefix = "" if len(rows) == 1 else r["name"] + ": "
        if "error" in r:
            print(prefix + "error: " + r["error"], file=sys.stderr)
            failed = True
            continue
        for l in r["out"].rstrip("\n").splitlines():
            print(prefix + l)
        if r["err"].strip():
            print(prefix + r["err"].strip().splitlines()[-1], file=sys.stderr)
            failed = True
    sys.exit(1 if failed else 0)


def cmd_log(a):
    def one(b):
        with ws(b) as t:
            if a.restart:
                repl.reload(t)
            raw = t.read_for(a.seconds).decode(errors="replace")
        text = [l for l in repl.clean(raw).splitlines() if l.strip()]
        return {"state": state_of(repl.status_bar(raw)), "lines": text[-a.lines:]}
    rows = each(inventory.resolve(a.target), one)
    if JSON:
        return emit(rows, None)
    for r in rows:
        print("== %s (%s)" % (r["name"], r.get("state", r.get("error"))))
        for l in r.get("lines", []):
            print(l)


def cmd_reload(a):
    def one(b):
        with ws(b) as t:
            repl.reload(t)
        return {}
    emit(each(inventory.resolve(a.target), one), lambda r: line(r, lambda r: "reloaded"))


def _reset(target, code, done):
    def one(b):
        with ws(b) as t:
            try:
                repl.run(t, code, timeout=3)
            except (repl.ReplError, ConnectionError, OSError):
                pass  # the board drops the connection as it resets
        return {}
    emit(each(inventory.resolve(target), one), lambda r: line(r, lambda r: done))


def cmd_reset(a):
    _reset(a.target, "import microcontroller\nmicrocontroller.reset()\n", "resetting")


def cmd_bootloader(a):
    mode = "UF2" if a.uf2 else "BOOTLOADER"
    _reset(a.target, "import microcontroller\nmicrocontroller.on_next_reset(microcontroller.RunMode.%s)\n"
                     "microcontroller.reset()\n" % mode,
           "rebooting into the %s (off Wi-Fi until reset)" % ("UF2 bootloader" if a.uf2 else "ROM bootloader"))


def cmd_settings(a):
    pw = inventory.password()
    value = a.value
    if a.action == "set" and value is None:
        value = getpass.getpass("Value for %s (not shown): " % a.key)

    def one(b):
        try:
            text = web.read(b["host"], pw, "/settings.toml").decode()
        except web.WebError as e:
            if "404" not in str(e):
                raise
            text = ""
        if a.action == "keys":
            return {"keys": settings.keys(text)}
        new = settings.set_key(text, a.key, settings.literal(value)) if a.action == "set" \
            else settings.unset_key(text, a.key)
        web.write(b["host"], pw, "/settings.toml", new.encode())
        return {}
    rows = each(inventory.resolve(a.target), one)
    done = {"keys": lambda r: " ".join(r["keys"]) or "(empty)",
            "set": lambda r: "set %s (applies after `cpfleet reset`)" % a.key,
            "unset": lambda r: "removed %s (applies after `cpfleet reset`)" % a.key}[a.action]
    emit(rows, lambda r: line(r, done))


def cmd_lib(a):
    circup = shutil.which("circup")
    if not circup:
        sys.exit("needs circup: pipx install circup (or pip install circup)")
    pw = inventory.password()
    for b in inventory.resolve(a.to):
        print("== " + b["name"])
        # circup only takes the password as an argument
        subprocess.run([circup, "--host", b["host"], "--password", pw, a.action, *a.names])


def cmd_bundle(a):
    from . import bundle
    bundle.main(a, emit, each, line)


def cmd_provision(a):
    from . import provision
    provision.main(a)


def cmd_firmware(a):
    from . import provision
    provision.firmware(a)


def main(argv=None):
    global JSON
    p = argparse.ArgumentParser(prog="cpfleet", description=__doc__)
    p.add_argument("--version", action="version", version="cpfleet " + __version__)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")

    def cmd(name, fn, help, target=True):
        """target=True: optional board spec (default all); "required": must be given (commands with more arguments)."""
        s = sub.add_parser(name, help=help, description=help)
        s.set_defaults(fn=fn)
        spec = "all, a name, a name suffix such as 1a2b, an IP, or a comma list"
        if target == "required":
            s.add_argument("target", help=spec)
        elif target:
            s.add_argument("target", nargs="?", default="all", help=spec + " (default all)")
        return s

    cmd("ls", cmd_ls, "list known boards", target=False)
    s = cmd("add", cmd_add, "add a board by IP or hostname, plus every board it sees", target=False)
    s.add_argument("hosts", nargs="+")
    cmd("scan", cmd_scan, "refresh the board list (new boards, changed IPs)", target=False)
    cmd("password", cmd_password, "save the boards' web workflow password locally", target=False)
    s = cmd("status", cmd_status, "version, state (running / error / REPL) and free space; doesn't interrupt code")
    s.add_argument("--wait", type=float, default=1.2, help="seconds to wait for the status bar")
    s.add_argument("--why", action="store_true", help="for boards whose code.py ended, run it again to show the error")
    s.add_argument("--why-seconds", type=float, default=6)
    s = sub.add_parser("push", help="upload files or folders over Wi-Fi, skipping unchanged ones")
    s.set_defaults(fn=cmd_push)
    s.add_argument("paths", nargs="+")
    s.add_argument("--to", default="all")
    s.add_argument("--dest", default="/")
    s.add_argument("--settings", action="store_true", help="allow replacing settings.toml")
    s.add_argument("-n", "--dry-run", action="store_true")
    s = cmd("cat", cmd_cat, "print a file from one board", target="required")
    s.add_argument("path")
    s = cmd("rm", cmd_rm, "delete files or folders", target="required")
    s.add_argument("paths", nargs="+")
    s = cmd("run", cmd_run, "run Python on the board(s) and print only its output; restarts code.py afterwards",
            target="required")
    s.add_argument("code", nargs="?")
    s.add_argument("-f", "--file")
    s.add_argument("--stay", action="store_true", help="leave the board at the REPL instead of restarting code.py")
    s.add_argument("--usb", metavar="PORT", help="use a USB serial port instead of Wi-Fi")
    s.add_argument("--timeout", type=float, default=15)
    s = cmd("log", cmd_log, "console output for a few seconds, escape codes removed")
    s.add_argument("--restart", action="store_true", help="restart code.py first, to catch startup errors")
    s.add_argument("--seconds", type=float, default=3)
    s.add_argument("--lines", type=int, default=20)
    cmd("reload", cmd_reload, "restart code.py (soft reload)")
    cmd("reset", cmd_reset, "hard reset (needed after settings.toml changes)")
    s = cmd("bootloader", cmd_bootloader, "reboot into the ROM bootloader (for esptool) or --uf2")
    s.add_argument("--uf2", action="store_true")
    s = sub.add_parser("settings", help="read or change settings.toml without printing values")
    s.set_defaults(fn=cmd_settings)
    s.add_argument("target")
    s.add_argument("action", choices=["keys", "set", "unset"])
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?", help="omit to type it hidden")
    s = sub.add_parser("lib", help="install or update libraries with circup over Wi-Fi")
    s.set_defaults(fn=cmd_lib)
    s.add_argument("action", choices=["install", "update", "list", "freeze"])
    s.add_argument("names", nargs="*")
    s.add_argument("--to", default="all")
    s = sub.add_parser("bundle", help="install an Adafruit Learn guide's Project Bundle (zip or URL)")
    s.set_defaults(fn=cmd_bundle)
    s.add_argument("source")
    s.add_argument("--to", default="all")
    s.add_argument("-n", "--dry-run", action="store_true")
    s = sub.add_parser("provision", help="set up boards on USB: firmware if needed, Wi-Fi and web workflow")
    s.set_defaults(fn=cmd_provision)
    s.add_argument("--profile", default="magtag_2.9_grayscale")
    s.add_argument("--reinstall", action="store_true", help="reinstall firmware even on CircuitPython boards")
    s.add_argument("--name", default="{prefix}-{mac4}", help="instance name pattern ({prefix}, {mac4}, {mac})")
    s.add_argument("--only", nargs="+", metavar="MAC", help="only boards whose MAC ends with these")
    s.add_argument("-n", "--dry-run", action="store_true")
    s.add_argument("-y", "--yes", action="store_true")
    s = sub.add_parser("firmware", help="update CircuitPython on boards connected over USB, keeping their files")
    s.set_defaults(fn=cmd_firmware)
    s.add_argument("--profile", default="magtag_2.9_grayscale")
    s.add_argument("--version", dest="cp_version", default="latest")
    s.add_argument("--only", nargs="+", metavar="MAC", help="only boards whose MAC ends with these")
    s.add_argument("-n", "--dry-run", action="store_true")
    s.add_argument("-y", "--yes", action="store_true")

    a = p.parse_args(argv)
    JSON = a.json
    if a.cmd == "settings" and a.action in ("set", "unset") and not a.key:
        p.error("settings %s needs a KEY" % a.action)
    try:
        a.fn(a)
    except (inventory.InventoryError, web.WebError, FileNotFoundError) as e:
        sys.exit("cpfleet: %s" % e)
    except KeyboardInterrupt:
        sys.exit(130)
