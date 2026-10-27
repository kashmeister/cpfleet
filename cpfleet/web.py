"""CircuitPython web workflow HTTP API (web_api_version 4): /cp/*.json and /fs/."""
import base64
import json
import urllib.error
import urllib.parse
import urllib.request

HINTS = {
    401: "wrong web workflow password",
    403: "no CIRCUITPY_WEB_API_PASSWORD set on the board",
    404: "not found",
    409: "the file system is in use over USB (boot.py should call storage.disable_usb_drive())",
    413: "file too large",
    417: "file too large",
}


class WebError(Exception):
    pass


def request(host, method, path, password=None, data=None, headers=None, timeout=10.0):
    """One HTTP request; returns (status, body). Raises WebError if the board can't be reached."""
    hdrs = dict(headers or {})
    if password is not None:
        hdrs["Authorization"] = "Basic " + base64.b64encode((":" + password).encode()).decode()
    url = "http://%s%s" % (host, urllib.parse.quote(path))
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        with e:
            return e.code, e.read()
    except (urllib.error.URLError, OSError) as e:
        raise WebError("unreachable (%s)" % getattr(e, "reason", e)) from None


def _ok(status, body, what):
    if status >= 300:
        raise WebError("%s: HTTP %d, %s" % (what, status, HINTS.get(status, body[:80].decode(errors="replace"))))
    return body


def version(host, timeout=5.0):
    return json.loads(_ok(*request(host, "GET", "/cp/version.json", timeout=timeout), "version"))


def devices(host, timeout=5.0):
    """Other boards this board sees over mDNS: [{hostname, instance_name, port, ip}]."""
    return json.loads(_ok(*request(host, "GET", "/cp/devices.json", timeout=timeout), "devices"))["devices"]


def diskinfo(host, timeout=5.0):
    info = json.loads(_ok(*request(host, "GET", "/cp/diskinfo.json", timeout=timeout), "diskinfo"))
    return info[0] if isinstance(info, list) else info


def listdir(host, password, path="/"):
    """Directory listing: {name: {"directory", "modified_ns", "file_size"}}, plus free space in bytes."""
    path = path if path.endswith("/") else path + "/"
    status, body = request(host, "GET", "/fs" + path, password, headers={"Accept": "application/json"})
    if status == 404:
        return None, None
    info = json.loads(_ok(status, body, "list " + path))
    files = {f["name"]: f for f in info.get("files", [])}
    return files, info.get("free", 0) * info.get("block_size", 512)


def read(host, password, path):
    return _ok(*request(host, "GET", "/fs" + path, password), "read " + path)


def write(host, password, path, data, mtime_ms=None):
    headers = {"Content-Type": "application/octet-stream"}
    if mtime_ms is not None:
        headers["X-Timestamp"] = str(int(mtime_ms))
    _ok(*request(host, "PUT", "/fs" + path, password, data=data, headers=headers, timeout=30), "write " + path)


def mkdir(host, password, path):
    path = path if path.endswith("/") else path + "/"
    status, body = request(host, "PUT", "/fs" + path, password, data=b"")
    if status not in (201, 204, 409):  # 409 here can also mean "exists"
        _ok(status, body, "mkdir " + path)


def delete(host, password, path):
    _ok(*request(host, "DELETE", "/fs" + path, password), "delete " + path)
