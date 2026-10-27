"""Install an Adafruit Learn guide's Project Bundle on boards over Wi-Fi.

A Project Bundle (the "Download Project Bundle" button on a guide's code page) is a zip with one folder
per CircuitPython major version, e.g. "MagTag_Quotes/CircuitPython 10.x/" holding code.py, lib/ and any
fonts or images. cpfleet picks the folder matching each board's version and pushes it. settings.toml is
never overwritten; the keys the project expects are listed so they can be set with `cpfleet settings`.
"""
import hashlib
import re
import shutil
import zipfile
from pathlib import Path

from . import inventory, settings, sync, web
from . import provision
from .provision import CACHE

_VERSION_DIR = re.compile(r"^(.*?CircuitPython (\d+)\.x)/")


def fetch(source):
    if not re.match(r"^https?://", source):
        return Path(source)
    return provision.fetch(source, CACHE / "bundles" / (hashlib.sha256(source.encode()).hexdigest()[:16] + ".zip"),
                           quiet=True)


def versions(zf):
    """{major: folder prefix inside the zip}; {0: ""} when the zip has code.py at its top level."""
    found = {}
    for name in zf.namelist():
        m = _VERSION_DIR.match(name)
        if m:
            found[int(m.group(2))] = m.group(1) + "/"
    if not found and any(n.split("/")[-1] == "code.py" for n in zf.namelist()):
        top = min((n for n in zf.namelist() if n.endswith("code.py")), key=len)
        found[0] = top[: -len("code.py")]
    return found


def pick(found, major):
    if major in found:
        return major
    older = [v for v in found if v <= major]
    return max(older) if older else max(found)


def extract(zf, prefix, dest):
    if dest.exists():
        shutil.rmtree(dest)
    for name in zf.namelist():
        if name.startswith(prefix) and not name.endswith("/"):
            out = dest / name[len(prefix):]
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(zf.read(name))
    return dest


def main(a, emit, each, line):
    path = fetch(a.source)
    zf = zipfile.ZipFile(path)
    found = versions(zf)
    if not found:
        raise web.WebError("%s has no 'CircuitPython N.x' folder or code.py" % path.name)
    pw = inventory.password()

    def one(b):
        major = int(web.version(b["host"])["version"].split(".")[0])
        v = pick(found, major)
        folder = extract(zf, found[v], CACHE / "bundles" / ("%s-cp%d" % (path.stem, v)))
        wanted = []
        if (folder / "settings.toml").exists():
            wanted = settings.keys((folder / "settings.toml").read_text())
        up, skipped, _ = sync.push(b["host"], pw, sync.plan([folder]), dry_run=a.dry_run)
        return {"bundle_version": v, "uploaded": up, "skipped": skipped, "needs_settings": wanted,
                "mismatch": v not in (major, 0)}
    rows = each(inventory.resolve(a.to), one)
    emit(rows, lambda r: line(r, lambda r: "%s %d, unchanged %d from the CircuitPython %s folder%s%s" % (
        "would upload" if a.dry_run else "uploaded", len(r["uploaded"]), len(r["skipped"]),
        "%d.x" % r["bundle_version"] if r["bundle_version"] else "top-level",
        " (no folder for the board's version)" if r["mismatch"] else "",
        "; set with `cpfleet settings %s set KEY`: %s" % (r["name"], " ".join(r["needs_settings"]))
        if r["needs_settings"] else "")))
