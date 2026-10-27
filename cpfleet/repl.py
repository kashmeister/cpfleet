"""Talk to a CircuitPython REPL over the web workflow websocket (/cp/serial/) or USB serial.

Both transports share one raw-REPL implementation, so code is sent verbatim: typing multi-line
code into the friendly REPL loses newlines and characters, raw REPL doesn't.
"""
import base64
import os
import re
import select
import socket
import struct
import time

# OSC sequences (CircuitPython's status bar, e.g. "\x1b]0;🐍192.168.1.42 | code.py | 10.3.1\x1b\\")
# and CSI sequences such as "\x1b[2K".
_OSC = re.compile(r"\x1b\](.*?)(?:\x07|\x1b\\)", re.S)
_CSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def clean(text):
    """Strip terminal escapes and carriage returns from console output."""
    text = _CSI.sub("", _OSC.sub("", text))
    return text.replace("\r\n", "\n").replace("\r", "")


def status_bar(text):
    """The last CircuitPython status bar in the output, e.g. ("192.168.1.42", "code.py", "10.3.1")."""
    bars = [m.group(1) for m in _OSC.finditer(text) if m.group(1).startswith("0;")]
    if not bars:
        return None
    parts = [p.strip() for p in bars[-1][2:].lstrip("\U0001f40d").split("|")]
    return parts


class ReplError(Exception):
    pass


class Transport:
    """Byte stream to a REPL. Subclasses implement write(), _read_some(timeout) and close()."""

    def read_until(self, marker, timeout):
        """Read until marker appears; returns (data, found)."""
        data, deadline = b"", time.monotonic() + timeout
        while marker not in data:
            left = deadline - time.monotonic()
            if left <= 0:
                return data, False
            data += self._read_some(left)
        return data, True

    def read_for(self, seconds):
        data, deadline = b"", time.monotonic() + seconds
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return data
            data += self._read_some(left)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class WebSocket(Transport):
    """Minimal RFC 6455 client for CircuitPython's /cp/serial/ (one client at a time per board)."""

    def __init__(self, host, password, port=80, timeout=5.0):
        self.sock = socket.create_connection((host, port), timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        auth = base64.b64encode((":" + password).encode()).decode()
        request = (f"GET /cp/serial/ HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
                   f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                   f"Authorization: Basic {auth}\r\n\r\n")
        self.sock.sendall(request.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(1024)
            if not chunk:
                raise ConnectionError("connection closed during websocket handshake")
            head += chunk
        head, _, self.pending = head.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 101 " not in status + " ":
            code = status.split(" ")[1] if " " in status else status
            hint = {"401": "wrong web workflow password", "403": "no CIRCUITPY_WEB_API_PASSWORD set on the board",
                    "409": "another client already has the board's serial console open"}.get(code, status)
            self.sock.close()
            raise ConnectionError(hint)

    def _recv_exact(self, n, deadline):
        while len(self.pending) < n:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError
            self.sock.settimeout(left)
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                raise TimeoutError from None
            if not chunk:
                raise ConnectionError("board closed the websocket")
            self.pending += chunk
        out, self.pending = self.pending[:n], self.pending[n:]
        return out

    def _read_some(self, timeout):
        deadline = time.monotonic() + timeout
        try:
            b0, b1 = self._recv_exact(2, deadline)
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2, deadline))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8, deadline))[0]
            mask = self._recv_exact(4, deadline) if b1 & 0x80 else None
            payload = self._recv_exact(length, deadline)
        except TimeoutError:
            return b""
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        opcode = b0 & 0x0F
        if opcode == 0x8:
            raise ConnectionError("board closed the websocket")
        if opcode == 0x9:
            self._send(0xA, payload)
            return b""
        return payload if opcode in (0x0, 0x1, 0x2) else b""

    def _send(self, opcode, payload):
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            head = struct.pack(">BB", 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            head = struct.pack(">BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            head = struct.pack(">BBQ", 0x80 | opcode, 0x80 | 127, n)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def write(self, data):
        self._send(0x1, data)

    def close(self):
        try:
            self._send(0x8, struct.pack(">H", 1000))
        except OSError:
            pass
        self.sock.close()


class Serial(Transport):
    """USB serial port (/dev/cu.usbmodem*), raw mode, no flow control."""

    def __init__(self, port):
        import termios
        self.fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        attrs = termios.tcgetattr(self.fd)
        attrs[0] = attrs[1] = attrs[3] = 0
        attrs[2] |= termios.CLOCAL | termios.CREAD
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)

    def _read_some(self, timeout):
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return b""
        try:
            return os.read(self.fd, 4096)
        except BlockingIOError:
            return b""

    def write(self, data):
        while data:
            try:
                n = os.write(self.fd, data)
                data = data[n:]
            except BlockingIOError:
                time.sleep(0.01)

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


RAW_PROMPT = b"raw REPL; CTRL-B to exit\r\n>"


def interrupt(t):
    """Stop code.py and get to the friendly REPL prompt."""
    t.write(b"\r\x03")
    time.sleep(0.1)
    t.write(b"\x03")
    t.read_until(b">>> ", 3)


def enter_raw(t, attempts=4):
    """Get to the raw REPL prompt. Retries, because a board that is mid-reload (e.g. right after a
    file write triggered auto-reload) ignores the first interrupts."""
    for _ in range(attempts):
        interrupt(t)
        t.write(b"\x01")
        _, found = t.read_until(RAW_PROMPT, 2)
        if found:
            return
        t.write(b"\x02")
        time.sleep(0.5)
    raise ReplError("board didn't enter raw REPL")


def run(t, code, timeout=15.0):
    """Run code via raw REPL; returns (stdout, stderr), escapes removed. Leaves the board at the REPL."""
    enter_raw(t)
    data = code.encode()
    for i in range(0, len(data), 128):  # small chunks: the REPL's input buffer is small
        t.write(data[i:i + 128])
        time.sleep(0.01)
    t.write(b"\x04")
    reply, found = t.read_until(b"\x04>", timeout)
    t.write(b"\x02")  # back to the friendly REPL
    if b"OK" not in reply:
        raise ReplError("board didn't accept the code")
    body = reply.split(b"OK", 1)[1]
    if not found:
        return clean(body.decode(errors="replace")), "timed out after %gs" % timeout
    out, err = (body.split(b"\x04") + [b""])[:2]
    return clean(out.decode(errors="replace")), clean(err.decode(errors="replace"))


def reload(t, attempts=3):
    """Soft reload: restart code.py. Waits until the board confirms, so closing the connection right
    after doesn't drop the Ctrl-D."""
    for _ in range(attempts):
        interrupt(t)
        t.write(b"\x04")
        _, found = t.read_until(b"soft reboot", 3)
        if found:
            return
    raise ReplError("board didn't reload")
