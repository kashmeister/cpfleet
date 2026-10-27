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

def circuitpython_board(port):
    """(major version, board_id, MAC) if CircuitPython answers on this port, else None.
    Asking stops code.py, so it's restarted before returning."""
    try:
        with repl.Serial(port) as t:
            out, err = repl.run(t, "import sys, board, wifi\nprint(sys.implementation.version[0], board.board_id,"
                                   " ''.join('%02x' % b for b in wifi.radio.mac_address))\n", 5)
            repl.reload(t)
    except (repl.ReplError, OSError):
        return None
    parts = out.split()
    return (int(parts[0]), parts[1], parts[2]) if len(parts) == 3 and not err.strip() else None


def to_rom_bootloader(port):
    try:
        with repl.Serial(port) as t:
            repl.run(t, "import microcontroller\nmicrocontroller.on_next_reset(microcontroller.RunMode.BOOTLOADER)\n"
                        "microcontroller.reset()\n", 2)
    except (repl.ReplError, OSError):
        pass


def find_boards(esp, prof, reinstall, only=None, dry_run=False):
    """Find boards on USB and put them into ROM download mode (not on a dry run).
    Returns {port: (mac, keep_firmware)}. `only`: MAC suffixes to limit it to."""
    wanted = lambda mac: not only or any(mac.endswith(s.lower()) for s in only)
    keep, boards = {}, {}
    for port in serial_ports():
        info = circuitpython_board(port)
        if info is None:
            continue
        major, board_id, mac = info
        if board_id != prof["board_id"]:
            print("%s: %s, not a %s board; leaving it alone" % (port, board_id, prof["board_id"]))
            continue
        if not wanted(mac):
            continue
        keep[mac] = major >= 10 and not reinstall
        if dry_run:
            boards[port] = (mac, keep[mac])
        else:
            to_rom_bootloader(port)
    if keep and not dry_run:
        time.sleep(4)
    for port in serial_ports():
        if port in boards:
            continue
        mac = esp.mac(port)
        if mac and wanted(mac):
            boards[port] = (mac, keep.get(mac, False))
    return boards


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
    boards = find_boards(esp, prof, a.reinstall, a.only, a.dry_run)
    if not boards:
        die("no board in download mode or running CircuitPython on USB. Put one in download mode "
            "(MagTag: hold Boot, press and release Reset, release Boot) and run this again.")
    print("Boards:")
    for port, (mac, keep) in boards.items():
        print("  %-28s %s  %s" % (port, mac, "keep firmware, rewrite boot.py and settings.toml" if keep else
                                  "back up flash, install TinyUF2 + CircuitPython, write files"))
    if a.dry_run:
        return
    if not a.yes and input("Provision them? [y/N] ").strip().lower() != "y":
        return
    ssid, wifi_pw, web_pw = ask_settings()
    needs_fw = [p for p, (_, keep) in boards.items() if not keep]
    if needs_fw:
        cp_bin, cp_ver = circuitpython_bin(prof)
        uf2_bin, uf2_ver = tinyuf2_combined(prof)
        ota = CACHE / "otadata-ota0.bin"
        ota.write_bytes(otadata(prof["otadata_size"]))
        print("   CircuitPython %s, TinyUF2 %s" % (cp_ver, uf2_ver))
    CACHE.mkdir(parents=True, exist_ok=True)
    for port, (mac, keep) in boards.items():
        name = a.name.format(prefix=prof["name_prefix"], mac4=mac[-4:], mac=mac)
        print("== %s (%s)" % (name, port))
        with tempfile.NamedTemporaryFile(dir=CACHE, suffix=".img") as tmp:  # holds passwords; deleted after
            img = Path(tmp.name)
            if keep:
                esp(port, "read-flash", hex(prof["ffat_offset"]), hex(prof["ffat_size"]), str(img))
            else:
                BACKUPS.mkdir(parents=True, exist_ok=True)
                backup = BACKUPS / ("flash-%s.bin" % mac)
                if not backup.exists():
                    print("   backing up flash to " + str(backup))
                    esp(port, "read-flash", "0", hex(prof["flash_size"]), str(backup))
                print("   installing firmware")
                esp(port, "erase-flash")
                esp(port, "write-flash", "0x0", str(uf2_bin))
                esp(port, "write-flash", "0x0", str(cp_bin))
                esp(port, "write-flash", hex(prof["otadata_offset"]), str(ota))
                img.write_bytes(fat12_image(prof["ffat_size"]))
            write_files(img, prof, name, ssid, wifi_pw, web_pw, mcopy, mtype)
            r = esp(port, "write-flash", hex(prof["ffat_offset"]), str(img))
            print("   files written" if "verified" in r.stdout else "   WRITE NOT VERIFIED")
            img.write_bytes(b"\0" * img.stat().st_size)
    inventory.set_password(web_pw)
    print("Done. Press Reset on each board; once they're on Wi-Fi, run `cpfleet scan` (or `cpfleet add <ip>`).")


def firmware(a):
    """Update CircuitPython on USB boards and keep their files: write the new .bin, then the boot selector."""
    prof = load_profile(a.profile)
    esp = Esp(prof["chip"])
    boards = find_boards(esp, prof, False, a.only, a.dry_run)
    if not boards:
        die("no board found on USB")
    cp_bin, cp_ver = circuitpython_bin(prof, a.cp_version)
    print("Install CircuitPython %s on: %s" % (cp_ver, ", ".join("%s (%s)" % (m, p) for p, (m, _) in boards.items())))
    if a.dry_run or (not a.yes and input("Go ahead? [y/N] ").strip().lower() != "y"):
        return
    ota = CACHE / "otadata-ota0.bin"
    ota.write_bytes(otadata(prof["otadata_size"]))
    for port, (mac, _) in boards.items():
        esp(port, "write-flash", "0x0", str(cp_bin))
        esp(port, "write-flash", hex(prof["otadata_offset"]), str(ota))
        print("%s: CircuitPython %s written" % (mac, cp_ver))
    print("Press Reset on each board.")
