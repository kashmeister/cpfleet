"""Unit tests that need no hardware: python3 -m unittest discover tests"""
import base64
import hashlib
import io
import json
import os
import socket
import struct
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

os.environ["CPFLEET_HOME"] = tempfile.mkdtemp()
os.environ["CPFLEET_CACHE"] = tempfile.mkdtemp()

from cpfleet import bundle, cli, inventory, provision, repl, settings, sync, web  # noqa: E402


class Repl(unittest.TestCase):
    BAR = "\x1b]0;\U0001f40d192.168.1.42 | 51@code.py AttributeError | 10.3.1\x1b\\"

    def test_clean_strips_status_bar_and_csi(self):
        self.assertEqual(repl.clean(self.BAR + "\x1b[2K\x1b[0Ghello\r\nworld\r\n"), "hello\nworld\n")

    def test_status_bar(self):
        self.assertEqual(repl.status_bar("x" + self.BAR), ["192.168.1.42", "51@code.py AttributeError", "10.3.1"])
        self.assertIsNone(repl.status_bar("no bar here"))

    def test_state_of(self):
        self.assertEqual(cli.state_of(["ip", "code.py", "10"]), "running code.py")
        self.assertEqual(cli.state_of(["ip", "51@code.py AttributeError", "10"]), "error code.py line 51: AttributeError")
        self.assertEqual(cli.state_of(["ip", "REPL", "10"]), "at REPL")
        self.assertEqual(cli.state_of(["ip", "Done", "10"]), "finished")


class Settings(unittest.TestCase):
    def test_toml_string_escapes(self):
        self.assertEqual(settings.toml_string('pa"ss\\word'), '"pa\\"ss\\\\word"')

    def test_set_replace_and_append(self):
        text = 'A = "1"\nB = 2\n'
        self.assertEqual(settings.set_key(text, "B", "3"), 'A = "1"\nB = 3\n')
        self.assertEqual(settings.set_key(text, "C", '"x"'), 'A = "1"\nB = 2\nC = "x"\n')
        self.assertEqual(settings.keys(text), ["A", "B"])
        self.assertEqual(settings.unset_key(text, "A"), "B = 2\n")

    def test_literal(self):
        self.assertEqual(settings.literal("42"), "42")
        self.assertEqual(settings.literal("home-wifi"), '"home-wifi"')


class Firmware(unittest.TestCase):
    def test_otadata_matches_esp_idf(self):
        data = provision.otadata(0x2000)
        self.assertEqual(len(data), 0x2000)
        self.assertEqual(data[:4], b"\x01\x00\x00\x00")
        self.assertEqual(data[28:32], bytes.fromhex("9a984347"))  # value ESP-IDF writes for sequence 1
        self.assertEqual(set(data[32:]), {0xFF})

    def test_full_image_layout(self):
        prof = provision.load_profile("magtag_2.9_grayscale")
        d = Path(tempfile.mkdtemp())
        (d / "uf2.bin").write_bytes(b"U" * 0x300000)            # TinyUF2 combined: up to past 0x2d0000
        (d / "cp.bin").write_bytes(b"C" * 0x1000 + b"\xff" * 0xF000 + b"A" * 0x100)  # otadata area blank
        (d / "fs.img").write_bytes(provision.fat12_image(prof["ffat_size"]))
        img = provision.full_image(prof, d / "uf2.bin", d / "cp.bin", d / "fs.img")
        self.assertEqual(len(img), prof["flash_size"])
        self.assertEqual(img[:1], b"C")                            # CircuitPython over TinyUF2
        self.assertEqual(img[0xe000 + 28:0xe000 + 32], bytes.fromhex("9a984347"))  # boot selector -> ota_0
        self.assertEqual(img[0x2d0000:0x2d0001], b"U")             # TinyUF2's uf2 partition kept
        self.assertEqual(img[0x310000 + 510:0x310000 + 512], b"\x55\xaa")  # CIRCUITPY file system

    def test_fat12_matches_circuitpython_geometry(self):
        img = provision.fat12_image(0xF0000)
        self.assertEqual(len(img), 0xF0000)
        bps, spc, reserved, nfats, root, total, media, fatsz = struct.unpack_from("<HBHBHHBH", img, 11)
        self.assertEqual((bps, spc, reserved, nfats, root, total, media, fatsz), (512, 1, 1, 1, 512, 1920, 0xF8, 6))
        self.assertEqual(img[510:512], b"\x55\xaa")
        root_start = (reserved + nfats * fatsz) * bps
        self.assertEqual(img[root_start:root_start + 11], b"CIRCUITPY  ")


class Inventory(unittest.TestCase):
    BOARDS = [{"name": "MagTag-1a2b", "host": "10.0.0.19", "hostname": "cpy-x-a1b2c3d41a2b", "mac": "a1b2c3d41a2b"},
              {"name": "MagTag-3c4d", "host": "10.0.0.21", "hostname": "cpy-x-a1b2c3d43c4d", "mac": "a1b2c3d43c4d"}]

    def test_resolve(self):
        r = lambda s: [b["name"] for b in inventory.resolve(s, self.BOARDS)]
        self.assertEqual(r("all"), ["MagTag-1a2b", "MagTag-3c4d"])
        self.assertEqual(r("1a2b"), ["MagTag-1a2b"])
        self.assertEqual(r("magtag-3c4d,1a2b"), ["MagTag-3c4d", "MagTag-1a2b"])
        self.assertEqual(r("10.0.0.21"), ["MagTag-3c4d"])
        self.assertEqual(r("192.168.1.5"), ["192.168.1.5"])
        with self.assertRaises(inventory.InventoryError):
            inventory.resolve("nope", self.BOARDS)

    def test_merge_by_hostname_updates_ip(self):
        boards, changed = inventory.merge(self.BOARDS, [dict(self.BOARDS[0], host="10.0.0.99")])
        self.assertEqual([c[0] for c in changed], ["updated"])
        self.assertIn("10.0.0.99", [b["host"] for b in boards])

    def test_record_from_version_json(self):
        info = {"board_name": "MagTag-1a2b", "hostname": "cpy-2.9_grayscale-a1b2c3d41a2b", "ip": "10.0.0.19",
                "board_id": "adafruit_magtag_2.9_grayscale"}
        self.assertEqual(inventory.record(info, "x")["mac"], "a1b2c3d41a2b")


class FakeBoard(BaseHTTPRequestHandler):
    """Just enough of the web workflow /fs/ API for sync."""
    files = {}

    def log_message(self, *a):
        pass

    def _auth(self):
        if self.headers.get("Authorization") != "Basic " + base64.b64encode(b":pw").decode():
            self.send_response(401)
            self.end_headers()
            return False
        return True

    def do_GET(self):
        if not self._auth():
            return
        path = self.path[3:]
        if path.endswith("/"):
            names = {}
            for p, (data, mtime) in self.files.items():
                if p.startswith(path) and p != path:
                    rest = p[len(path):]
                    name, is_dir = rest.split("/")[0], "/" in rest.rstrip("/")
                    names[name] = {"name": name, "directory": is_dir or p.endswith("/"),
                                   "modified_ns": mtime * 1_000_000, "file_size": len(data)}
            if path != "/" and path not in self.files:
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps({"free": 1000, "block_size": 512, "files": list(names.values())}).encode()
        else:
            body = self.files[path][0]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self):
        if not self._auth():
            return
        data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        path = self.path[3:]
        existed = path in self.files
        self.files[path] = (data, int(self.headers.get("X-Timestamp", "0")))
        self.send_response(204 if existed else 201)
        self.end_headers()


class Sync(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeBoard)
        cls.host = "127.0.0.1:%d" % cls.server.server_port
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_push_uploads_then_skips_and_protects_settings(self):
        d = Path(tempfile.mkdtemp())
        (d / "lib").mkdir()
        (d / "lib" / "helper.py").write_text("x = 1\n")
        (d / "code.py").write_text("import helper\n")
        (d / "settings.toml").write_text("A = 1\n")
        (d / ".DS_Store").write_text("junk")
        files = sync.plan([d])
        self.assertEqual([r for _, r in files][-1], "/code.py")  # code.py goes last
        self.assertNotIn("/.DS_Store", [r for _, r in files])
        up, skipped, prot = sync.push(self.host, "pw", files)
        self.assertEqual(sorted(up), ["/code.py", "/lib/helper.py"])
        self.assertEqual(prot, ["/settings.toml"])
        self.assertIn("/lib/", FakeBoard.files)
        up, skipped, _ = sync.push(self.host, "pw", files)
        self.assertEqual((up, sorted(skipped)), ([], ["/code.py", "/lib/helper.py"]))

    def test_wrong_password(self):
        with self.assertRaises(web.WebError) as e:
            web.listdir(self.host, "wrong", "/")
        self.assertIn("password", str(e.exception))


class WebSocketRepl(unittest.TestCase):
    """A fake board that speaks the websocket handshake and the raw REPL protocol."""

    def serve(self, conn):
        req = b""
        while b"\r\n\r\n" not in req:
            req += conn.recv(1024)
        key = [l.split(b": ")[1] for l in req.split(b"\r\n") if l.lower().startswith(b"sec-websocket-key")][0]
        accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")
        send = lambda data: conn.sendall(struct.pack(">BB", 0x81, len(data)) + data)
        send(b"\x1b]0;\xf0\x9f\x90\x8d10.0.0.1 | code.py | 10.3.1\x1b\\")
        code = b""
        while True:
            head = conn.recv(2)
            if len(head) < 2:
                return
            n, mask = head[1] & 0x7F, conn.recv(4)
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(conn.recv(n)))
            if head[0] & 0x0F == 8:
                return
            for ch in data:
                c = bytes([ch])
                if c == b"\x03":
                    send(b"\r\n>>> ")
                elif c == b"\x01":
                    send(b"raw REPL; CTRL-B to exit\r\n>")
                    code = b""
                elif c == b"\x04":
                    send(b"OK" + (b"2\r\n" if b"1 + 1" in code else b"") + b"\x04\x04>")
                elif c != b"\x02":
                    code += c

    def test_status_bar_and_run(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        threading.Thread(target=lambda: self.serve(srv.accept()[0]), daemon=True).start()
        with repl.WebSocket("127.0.0.1", "pw", port=port) as t:
            self.assertEqual(repl.status_bar(t.read_for(0.3).decode()), ["10.0.0.1", "code.py", "10.3.1"])
            self.assertEqual(repl.run(t, "print(1 + 1)\n", timeout=3), ("2\n", ""))
        srv.close()


class Bundle(unittest.TestCase):
    def test_versions_and_pick(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for v in (9, 10):
                z.writestr("Proj/CircuitPython %d.x/code.py" % v, "print(%d)" % v)
                z.writestr("Proj/CircuitPython %d.x/lib/x.mpy" % v, "x")
        found = bundle.versions(zipfile.ZipFile(buf))
        self.assertEqual(found, {9: "Proj/CircuitPython 9.x/", 10: "Proj/CircuitPython 10.x/"})
        self.assertEqual(bundle.pick(found, 10), 10)
        self.assertEqual(bundle.pick(found, 11), 10)
        self.assertEqual(bundle.pick(found, 8), 10)

    def test_top_level_code_py(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("code.py", "")
        self.assertEqual(bundle.versions(zipfile.ZipFile(buf)), {0: ""})


if __name__ == "__main__":
    unittest.main()
