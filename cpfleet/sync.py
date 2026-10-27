"""Upload files and folders to a board over the web workflow, skipping what's already there."""
import os
from pathlib import Path, PurePosixPath

from . import web

SKIP_NAMES = {".DS_Store", "__pycache__", ".git", ".gitignore", "README.md"}
PROTECTED = {"/settings.toml", "/boot_out.txt"}
FAT_SLACK_MS = 2000  # FAT stores modification times in 2 s steps


def plan(paths, dest="/"):
    """[(local Path, remote path)] for files and (recursively) folders. A folder's contents go into dest."""
    dest = "/" + dest.strip("/")
    out = []
    for p in map(Path, paths):
        if p.is_dir():
            for root, dirs, files in os.walk(p):
                dirs[:] = sorted(d for d in dirs if d not in SKIP_NAMES and not d.startswith("."))
                for name in sorted(files):
                    if name in SKIP_NAMES or name.startswith("._"):
                        continue
                    local = Path(root) / name
                    out.append((local, str(PurePosixPath(dest) / local.relative_to(p).as_posix())))
        elif p.is_file():
            out.append((p, str(PurePosixPath(dest) / p.name)))
        else:
            raise FileNotFoundError(p)
    # code.py last, so the board's auto-reload runs it with its libraries already in place
    return sorted(out, key=lambda lr: (PurePosixPath(lr[1]).name in ("code.py", "main.py"), lr[1]))


def push(host, password, files, allow_settings=False, dry_run=False):
    """Upload [(local, remote)]; returns (uploaded, skipped, protected) lists of remote paths."""
    listings = {}

    def listing(remote_dir):
        if remote_dir not in listings:
            listings[remote_dir] = web.listdir(host, password, remote_dir)
        return listings[remote_dir]

    todo, skipped, protected = [], [], []
    for local, remote in files:
        if remote in PROTECTED and not allow_settings:
            protected.append(remote)
            continue
        rdir, name = str(PurePosixPath(remote).parent), PurePosixPath(remote).name
        entries, _ = listing(rdir)
        st = local.stat()
        have = (entries or {}).get(name)
        if have and have["file_size"] == st.st_size and \
                abs(have["modified_ns"] // 1_000_000 - int(st.st_mtime * 1000)) <= FAT_SLACK_MS:
            skipped.append(remote)
        else:
            todo.append((local, remote, st, have))

    need = sum(st.st_size - (have or {}).get("file_size", 0) for _, _, st, have in todo)
    _, free = listing("/")
    if free is not None and need > free:
        raise web.WebError("needs %d KB more than the board's %d KB free" % (need // 1024 + 1, free // 1024))
    if dry_run:
        return [r for _, r, _, _ in todo], skipped, protected

    made = set()
    for local, remote, st, _ in todo:
        parent = PurePosixPath(remote).parent
        for d in [p for p in list(reversed(parent.parents)) + [parent] if str(p) != "/"]:  # create /a, then /a/b
            key = str(d)
            if key in made:
                continue
            up, name = str(d.parent), d.name
            entries, _ = listing(up)
            if not entries or name not in entries:
                web.mkdir(host, password, key)
                listings.pop(up, None)
            made.add(key)
        web.write(host, password, remote, local.read_bytes(), mtime_ms=st.st_mtime * 1000)
    return [r for _, r, _, _ in todo], skipped, protected
