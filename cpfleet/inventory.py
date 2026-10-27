"""The local list of boards (~/.config/cpfleet/boards.json), the shared password, and discovery."""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from . import web

HOME = Path(os.environ.get("CPFLEET_HOME", Path.home() / ".config" / "cpfleet"))
BOARDS = HOME / "boards.json"
PASSWORD = HOME / "password"


class InventoryError(Exception):
    pass


def load():
    try:
        return json.loads(BOARDS.read_text()).get("boards", [])
    except FileNotFoundError:
        return []


def save(boards):
    HOME.mkdir(parents=True, exist_ok=True)
    os.chmod(HOME, 0o700)
    boards = sorted(boards, key=lambda b: b["name"].lower())
    BOARDS.write_text(json.dumps({"boards": boards}, indent=2) + "\n")


def password():
    """The web workflow password shared by the boards: $CPFLEET_PASSWORD, else ~/.config/cpfleet/password."""
    if os.environ.get("CPFLEET_PASSWORD"):
        return os.environ["CPFLEET_PASSWORD"]
    try:
        return PASSWORD.read_text().rstrip("\n")
    except FileNotFoundError:
        raise InventoryError("no web workflow password: run `cpfleet password` or set CPFLEET_PASSWORD") from None


def set_password(value):
    HOME.mkdir(parents=True, exist_ok=True)
    os.chmod(HOME, 0o700)
    fd = os.open(PASSWORD, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value)


def record(info, host):
    """A board entry from /cp/version.json (or a devices.json row)."""
    hostname = info.get("hostname", "")
    mac = re.search(r"([0-9a-f]{12})$", hostname)
    return {
        "name": info.get("board_name") or info.get("instance_name") or hostname,
        "host": info.get("ip") or host,
        "hostname": hostname,
        "mac": mac.group(1) if mac else "",
        "board_id": info.get("board_id", ""),
    }


def merge(boards, new):
    """Add or update entries, matched by hostname (stable) rather than IP (DHCP)."""
    by_key = {b.get("hostname") or b["host"]: b for b in boards}
    changed = []
    for entry in new:
        key = entry.get("hostname") or entry["host"]
        old = by_key.get(key)
        if old is None:
            by_key[key] = entry
            changed.append(("added", entry))
        else:
            updated = dict(old, **{k: v for k, v in entry.items() if v})
            if updated != old:
                by_key[key] = updated
                changed.append(("updated", updated))
    return list(by_key.values()), changed


def scan(seeds):
    """Find boards: ask each seed host and the boards it knows about over mDNS (devices.json)."""
    found, queue, seen = {}, list(seeds), set()
    while queue:
        host = queue.pop()
        if host in seen:
            continue
        seen.add(host)
        try:
            info = web.version(host, timeout=3)
        except web.WebError:
            continue
        entry = record(info, host)
        found[entry["hostname"] or host] = entry
        try:
            queue += [d["ip"] for d in web.devices(host, timeout=3) if d.get("ip")]
        except web.WebError:
            pass
    return list(found.values())


def mdns_names(seconds=3):
    """Instance names advertising _circuitpython._tcp (macOS dns-sd or Linux avahi-browse); best effort."""
    if shutil.which("avahi-browse"):
        cmd = ["avahi-browse", "-rtp", "_circuitpython._tcp"]
    elif shutil.which("dns-sd"):
        cmd = ["dns-sd", "-B", "_circuitpython._tcp", "local."]
    else:
        return [], []
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=seconds).stdout
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
    names, ips = set(), set()
    for line in out.splitlines():
        if cmd[0] == "dns-sd" and " Add " in line:
            names.add(line.split("_circuitpython._tcp.")[-1].strip())
        elif cmd[0] == "avahi-browse" and line.startswith("="):
            parts = line.split(";")
            if len(parts) > 7:
                names.add(parts[3])
                ips.add(parts[7])
    return sorted(names), sorted(ips)


def resolve(spec, boards=None):
    """Targets from "all", a name, a name suffix such as "1a2b", an IP or host, or a comma list of those."""
    boards = load() if boards is None else boards
    if not spec or spec == "all":
        if not boards:
            raise InventoryError("no boards yet: run `cpfleet add <ip>` with one board's address")
        return boards
    out = []
    for token in spec.split(","):
        token = token.strip()
        hits = [b for b in boards if token.lower() in (b["name"].lower(), b["host"], b.get("hostname", "").lower())]
        hits = hits or [b for b in boards if b["name"].lower().endswith(token.lower())
                        or b.get("mac", "").endswith(token.lower())]
        if len(hits) > 1:
            raise InventoryError("%r matches %s" % (token, ", ".join(b["name"] for b in hits)))
        if hits:
            out += hits
        elif re.match(r"^[\w.-]+$", token) and ("." in token or ":" in token):
            out.append({"name": token, "host": token})
        else:
            raise InventoryError("no board called %r (see `cpfleet ls`)" % token)
    return out
