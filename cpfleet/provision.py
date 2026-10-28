"""Set up boards connected over USB: firmware if needed (TinyUF2, CircuitPython, boot selector), then the
Wi-Fi web workflow (boot.py + settings.toml).

The CIRCUITPY file system is read and written directly with esptool and mtools, so this works on Macs
that never mount the board's USB drive. Passwords are typed hidden and never printed or kept in files,
except the web workflow password, which cpfleet stores in ~/.config/cpfleet/password (mode 600).
"""
import binascii
import getpass
import glob
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from . import inventory, repl, settings

PROFILES = Path(__file__).with_name("profiles")
BOARD_FILES = Path(__file__).with_name("board_files")
CACHE = Path(os.environ.get("CPFLEET_CACHE") or (
    Path.home() / "Library" / "Caches" / "cpfleet" if sys.platform == "darwin" else Path.home() / ".cache" / "cpfleet"))
BACKUPS = inventory.HOME / "backups"


def die(msg):
    sys.exit("cpfleet: " + msg)


def load_profile(name):
    path = PROFILES / (name + ".json")
    if not path.exists():
        die("no board profile %r; available: %s" % (name, ", ".join(p.stem for p in PROFILES.glob("*.json"))))
    p = json.loads(path.read_text())
    for k in ("flash_size", "ffat_offset", "ffat_size", "otadata_offset", "otadata_size"):
        p[k] = int(p[k], 0)
    return p


def tool(name, hint):
    path = shutil.which(name) or next((p for p in ("/opt/homebrew/bin/" + name, "/usr/local/bin/" + name)
                                       if os.access(p, os.X_OK)), None)
    if not path:
        die("needs %s (%s)" % (name, hint))
    return path


def serial_ports():
    return sorted(glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/ttyACM*"))


class Esp:
    def __init__(self, chip):
        self.bin, self.chip = tool("esptool", "brew install esptool, or pipx install esptool"), chip

    def __call__(self, port, *args, check=True):
        r = subprocess.run([self.bin, "--chip", self.chip, "--port", port, "--before", "no-reset",
                            "--after", "no-reset", *args], capture_output=True, text=True)
        if check and r.returncode:
            die("esptool %s on %s failed: %s" % (args[0], port, (r.stderr or r.stdout).strip().splitlines()[-1:]))
        return r

    def mac(self, port):
        m = re.search(r"MAC:\s*([0-9a-f:]{17})", self(port, "chip-id", check=False).stdout)
        return m.group(1).replace(":", "") if m else None


# ---- firmware files -----------------------------------------------------------------------------

USER_AGENT = "cpfleet/%s (+https://github.com/kashmeister/cpfleet)"  # some sites reject Python's default


def urlopen(url, timeout=60):
    from . import __version__
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT % __version__})
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        e.close()
        die("download failed: HTTP %d from %s" % (e.code, url))
    except (urllib.error.URLError, OSError) as e:
        die("download failed: %s (%s)" % (getattr(e, "reason", e), url))


def fetch(url, dest, quiet=False):
    if dest.exists() and dest.stat().st_size:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not quiet:
        print("   downloading " + url)
    tmp = dest.with_suffix(".part")
    with urlopen(url) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    tmp.rename(dest)
    return dest


def github_latest(repo):
    with urlopen("https://api.github.com/repos/%s/releases/latest" % repo, timeout=20) as r:
        return json.load(r)


def circuitpython_bin(prof, version="latest"):
    if version == "latest":
        version = github_latest("adafruit/circuitpython")["tag_name"]
    url = prof["circuitpython_bin"].format(board_id=prof["board_id"], version=version)
    return fetch(url, CACHE / "circuitpython" / url.rsplit("/", 1)[1]), version


def tinyuf2_combined(prof):
    rel = github_latest("adafruit/tinyuf2")
    want = "tinyuf2-%s-" % prof["tinyuf2_board"]
    asset = next((a for a in rel["assets"] if a["name"].startswith(want) and a["name"].endswith(".zip")), None)
    if not asset:
        die("TinyUF2 %s has no %s*.zip" % (rel["tag_name"], want))
    z = fetch(asset["browser_download_url"], CACHE / "tinyuf2" / asset["name"])
    out = z.with_name(z.stem + "-combined.bin")
    if not out.exists():
        with zipfile.ZipFile(z) as zf:
            out.write_bytes(zf.read(next(n for n in zf.namelist() if n.endswith("combined.bin"))))
    return out, rel["tag_name"]


def otadata(size, seq=1):
    """ESP-IDF OTA selector: sequence 1 boots ota_0. CRC as ESP-IDF computes it (seq 1 -> 0x4743989a)."""
    crc = binascii.crc32(struct.pack("<I", seq), 0xFFFFFFFF) & 0xFFFFFFFF
    entry = struct.pack("<I", seq) + b"\xff" * 20 + struct.pack("<II", 0xFFFFFFFF, crc)
    return entry + b"\xff" * (size - len(entry))


def fat12_image(size, label="CIRCUITPY"):
    """An empty FAT12 file system with the geometry CircuitPython itself formats (512-byte sectors,
    1 sector per cluster, 1 FAT, 512 root entries)."""
    bps, spc, reserved, nfats, root_entries = 512, 1, 1, 1, 512
    total, root_secs, fat_secs = size // bps, root_entries * 32 // bps, 1
    while ((total - reserved - nfats * fat_secs - root_secs) // spc + 2) * 3 // 2 + 1 > fat_secs * bps:
        fat_secs += 1
    boot = bytearray(bps)
    boot[0:11] = b"\xeb\xfe\x90MSDOS5.0"
    struct.pack_into("<HBHBHHBHHHII", boot, 11, bps, spc, reserved, nfats, root_entries,
                     total if total < 65536 else 0, 0xF8, fat_secs, 63, 255, 1, total if total >= 65536 else 0)
    struct.pack_into("<BBBI11s8s", boot, 36, 0x80, 0, 0x29, int.from_bytes(os.urandom(4), "little"),
                     label.ljust(11).encode(), b"FAT12   ")
    boot[510:512] = b"\x55\xaa"
    fat = bytearray(fat_secs * bps)
    fat[0:3] = b"\xf8\xff\xff"
    root = bytearray(root_secs * bps)
    root[0:11], root[11] = label.ljust(11).encode(), 0x08
    img = bytes(boot) + bytes(fat) * nfats + bytes(root)
    return img + b"\x00" * (total * bps - len(img))


# ---- boards ---------------------------------------------------------------------------------------

PROBE = ("import sys, board, wifi\nprint(sys.implementation.version[0], board.board_id,"
         " ''.join('%02x' % b for b in wifi.radio.mac_address))\n")
TO_ROM = "import microcontroller\nmicrocontroller.on_next_reset(microcontroller.RunMode.BOOTLOADER)\nmicrocontroller.reset()\n"


def wifi_twin(port):
    """The inventory board on this USB port: the port name contains the board's UID (version.json "UID").
    Used when a board's USB console stops taking input (it happens; a reset clears it) but Wi-Fi works."""
    for b in inventory.load():
        if b.get("uid") and b["uid"] in port:
            return b
    return None


def transport(port, via_wifi):
    return repl.WebSocket(via_wifi["host"], inventory.password()) if via_wifi else repl.Serial(port)


def circuitpython_board(port):
    """(major version, board_id, MAC, via_wifi) if CircuitPython answers on this port (over USB, else over
    Wi-Fi for a known board), else None. Asking stops code.py, so it's restarted before returning."""
    for via_wifi in (None, wifi_twin(port)):
        if via_wifi is False:
            continue
        try:
            with transport(port, via_wifi) as t:
                if via_wifi is None:
                    repl.enter_raw(t, attempts=2)  # a stuck USB console: give up quickly, Wi-Fi is next
                out, err = repl.run(t, PROBE, 5)
                repl.reload(t)
        except (repl.ReplError, OSError, ConnectionError, inventory.InventoryError):
            if via_wifi is None and wifi_twin(port) is None:
                return None
            continue
        parts = out.split()
        if len(parts) == 3 and not err.strip():
            return int(parts[0]), parts[1], parts[2], via_wifi
    return None


def to_rom_bootloader(port, via_wifi=None, wait=20):
    """Ask CircuitPython to reboot into the ROM bootloader: over USB, else over Wi-Fi for a known board
    (retried while a board that just rebooted rejoins Wi-Fi). Returns True once a console took the command."""
    routes = [via_wifi] if via_wifi else [None] + ([wifi_twin(port)] if wifi_twin(port) else [])
    deadline = time.monotonic() + wait
    while True:
        for via in routes:
            try:
                with transport(port, via) as t:
                    repl.enter_raw(t, attempts=2)
                    t.write(TO_ROM.encode() + b"\x04")
                    time.sleep(0.5)
                return True
            except (repl.ReplError, OSError, ConnectionError, inventory.InventoryError):
                continue
        if time.monotonic() > deadline:
            return False
        time.sleep(2)


def find_boards(esp, prof, reinstall, only=None):
    """Probe the boards on USB without changing anything (code.py is restarted after asking).
    Returns [{port, mac, keep, via, in_rom}]; `only`: MAC suffixes to limit it to."""
    wanted = lambda mac: not only or any(mac.endswith(s.lower()) for s in only)
    found, probed = [], set()
    for port in serial_ports():
        twin = wifi_twin(port)
        if twin and twin.get("mac") and not wanted(twin["mac"]):
            continue  # a known board that --only excludes: don't touch it
        info = circuitpython_board(port)
        if info is None:
            continue
        probed.add(port)
        major, board_id, mac, via = info
        if via:
            print("%s: USB console didn't answer; using %s over Wi-Fi" % (port, via["name"]))
        if board_id != prof["board_id"]:
            print("%s: %s, not a %s board; leaving it alone" % (port, board_id, prof["board_id"]))
        elif wanted(mac):
            found.append({"port": port, "mac": mac, "keep": major >= 10 and not reinstall, "via": via,
                          "in_rom": False})
    for port in serial_ports():
        if port not in probed:
            mac = esp.mac(port)
            if mac and wanted(mac):
                found.append({"port": port, "mac": mac, "keep": False, "via": None, "in_rom": True})
    return found


def into_rom(esp, board, circuitpython_ports):
    """Switch a board into ROM download mode and return its ROM port; the next esptool command must start
    at once.

    A board switched by software doesn't stay in download mode: it reboots a few seconds after each esptool
    session ends (and after a chip-id). So every esptool step is its own session, started right after a
    switch (see rom_session). The ROM port is recognised as the one new port that appears, not with chip-id."""
    if board["in_rom"]:
        return board["port"]
    deadline = time.monotonic() + 20  # a board that just left download mode is still rebooting
    while board["port"] not in serial_ports() and time.monotonic() < deadline:
        time.sleep(0.5)
    before = set(serial_ports())
    if not to_rom_bootloader(board["port"], board["via"]):
        die("couldn't reach %s over USB or Wi-Fi to switch it to download mode" % board["mac"])
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        time.sleep(0.5)
        new = [p for p in serial_ports() if p not in before and p not in circuitpython_ports]
        if len(new) == 1:
            time.sleep(0.5)  # let the port settle
            return new[0]
    die("%s didn't come up in download mode; hold Boot, press and release Reset, release Boot, and run "
        "this again" % board["mac"])


def rom_session(esp, board, circuitpython_ports, *args):
    """One esptool command in download mode, switching the board in first (again, if it has left)."""
    for attempt in range(3):
        port = into_rom(esp, board, circuitpython_ports)
        r = esp(port, *args, check=False)
        if r.returncode == 0:
            return r
        if board["in_rom"]:
            break
        time.sleep(3)  # it left download mode; let it reboot into CircuitPython, then switch it again
    die("esptool %s failed on %s: %s" % (args[0], board["mac"], (r.stderr or r.stdout).strip().splitlines()[-3:]))


def full_image(prof, uf2_bin, cp_bin, files_img):
    """The whole flash in one image, written in one esptool session: TinyUF2 (bootloader, partition table,
    uf2 partition), CircuitPython over it (same layout), the boot selector, and the CIRCUITPY file system."""
    img = bytearray(b"\xff" * prof["flash_size"])
    for offset, data in ((0, uf2_bin.read_bytes()), (0, cp_bin.read_bytes()),
                         (prof["otadata_offset"], otadata(prof["otadata_size"])),
                         (prof["ffat_offset"], files_img.read_bytes())):
        img[offset:offset + len(data)] = data
    return bytes(img)


def show_plan(found):
    print("Boards:")
    for b in found:
        print("  %-30s %s  %s" % (b["port"], b["mac"], "keep firmware, rewrite boot.py and settings.toml"
                                  if b["keep"] else "back up flash, install TinyUF2 + CircuitPython, write files"))


def ask_settings():
    ssid = input("Wi-Fi name: ").strip()
    wifi_pw = getpass.getpass("Wi-Fi password: ")
    try:
        web_pw = inventory.password()
        print("Using the web workflow password from %s." % inventory.PASSWORD)
    except inventory.InventoryError:
        web_pw = getpass.getpass("New web workflow password for the boards: ")
        if web_pw != getpass.getpass("Repeat: "):
            die("passwords don't match")
    if not (ssid and wifi_pw and web_pw):
        die("Wi-Fi name and passwords can't be empty")
    return ssid, wifi_pw, web_pw


def write_files(img, prof, name, ssid, wifi_pw, web_pw, mcopy, mtype):
    boot_py = (BOARD_FILES / "boot.py").read_text().replace(
        'PIN = "BUTTON_D"', 'PIN = %s' % settings.toml_string(prof.get("usb_drive_pin", "")))
    subprocess.run([mcopy, "-i", str(img), "-o", "-", "::/boot.py"], input=boot_py.encode(), check=True)
    old = subprocess.run([mtype, "-i", str(img), "::/settings.toml"], capture_output=True).stdout.decode(errors="replace")
    text = old
    for key, value in (("CIRCUITPY_WIFI_SSID", ssid), ("CIRCUITPY_WIFI_PASSWORD", wifi_pw),
                       ("CIRCUITPY_WEB_API_PASSWORD", web_pw), ("CIRCUITPY_WEB_INSTANCE_NAME", name)):
        text = settings.set_key(text, key, settings.toml_string(value))
    subprocess.run([mcopy, "-i", str(img), "-o", "-", "::/settings.toml"], input=text.encode(), check=True)


def main(a):
    prof = load_profile(a.profile)
    esp = Esp(prof["chip"])
    mcopy = tool("mcopy", "brew install mtools")
    mtype = tool("mtype", "brew install mtools")
    found = find_boards(esp, prof, a.reinstall, a.only)
    if not found:
        die("no board in download mode or running CircuitPython on USB. Put one in download mode "
            "(MagTag: hold Boot, press and release Reset, release Boot) and run this again.")
    show_plan(found)
    if a.dry_run or (not a.yes and input("Provision them? [y/N] ").strip().lower() != "y"):
        return
    ssid, wifi_pw, web_pw = ask_settings()
    CACHE.mkdir(parents=True, exist_ok=True)
    if not all(b["keep"] for b in found):  # download before touching any board
        cp_bin, cp_ver = circuitpython_bin(prof)
        uf2_bin, uf2_ver = tinyuf2_combined(prof)
        print("   CircuitPython %s, TinyUF2 %s" % (cp_ver, uf2_ver))
    cp_ports = {b["port"] for b in found if not b["in_rom"]}
    for b in found:
        mac = b["mac"]
        name = a.name.format(prefix=prof["name_prefix"], mac4=mac[-4:], mac=mac)
        print("== %s" % name)
        # These temporary files hold the passwords; they're blanked and deleted afterwards.
        with tempfile.NamedTemporaryFile(dir=CACHE, suffix=".img") as files, \
                tempfile.NamedTemporaryFile(dir=CACHE, suffix=".bin") as whole:
            img = Path(files.name)
            if b["keep"]:
                rom_session(esp, b, cp_ports, "read-flash", hex(prof["ffat_offset"]), hex(prof["ffat_size"]), str(img))
                write_files(img, prof, name, ssid, wifi_pw, web_pw, mcopy, mtype)
                r = rom_session(esp, b, cp_ports, "write-flash", hex(prof["ffat_offset"]), str(img))
            else:
                BACKUPS.mkdir(parents=True, exist_ok=True)
                backup = BACKUPS / ("flash-%s.bin" % mac)
                if not backup.exists():
                    print("   backing up flash to " + str(backup))
                    rom_session(esp, b, cp_ports, "read-flash", "0", hex(prof["flash_size"]), str(backup))
                img.write_bytes(fat12_image(prof["ffat_size"]))
                write_files(img, prof, name, ssid, wifi_pw, web_pw, mcopy, mtype)
                Path(whole.name).write_bytes(full_image(prof, uf2_bin, cp_bin, img))
                print("   writing TinyUF2, CircuitPython, boot selector and files (one session, about a minute)")
                r = rom_session(esp, b, cp_ports, "write-flash", "0x0", whole.name)
            print("   done, verified" if "verified" in r.stdout else "   WRITE NOT VERIFIED")
            for f in (img, Path(whole.name)):
                f.write_bytes(b"\0" * f.stat().st_size)
    inventory.set_password(web_pw)
    print("Done. Boards cpfleet switched restart by themselves; press Reset on any you put in download mode by "
          "hand. Once they're on Wi-Fi, run `cpfleet scan` (or `cpfleet add <ip>`).")


def firmware(a):
    """Update CircuitPython on USB boards and keep their files: CircuitPython's .bin with the boot selector
    patched in, written in one session (the .bin covers otadata with 0xFF otherwise)."""
    prof = load_profile(a.profile)
    esp = Esp(prof["chip"])
    found = find_boards(esp, prof, False, a.only)
    if not found:
        die("no board found on USB")
    cp_bin, cp_ver = circuitpython_bin(prof, a.cp_version)
    print("Install CircuitPython %s on: %s" % (cp_ver, ", ".join(b["mac"] for b in found)))
    if a.dry_run or (not a.yes and input("Go ahead? [y/N] ").strip().lower() != "y"):
        return
    data = bytearray(cp_bin.read_bytes())
    ota = otadata(prof["otadata_size"])
    data[prof["otadata_offset"]:prof["otadata_offset"] + len(ota)] = ota
    patched = CACHE / (cp_bin.stem + "-otadata.bin")
    patched.write_bytes(bytes(data))
    cp_ports = {b["port"] for b in found if not b["in_rom"]}
    for b in found:
        rom_session(esp, b, cp_ports, "write-flash", "0x0", str(patched))
        print("%s: CircuitPython %s written" % (b["mac"], cp_ver))
    print("Boards cpfleet switched restart by themselves; press Reset on any you put in download mode by hand.")
