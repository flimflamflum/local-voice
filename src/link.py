"""Linking the Android app: server token/port, this PC's addresses, the QR code, starting the server."""
import ipaddress
import json
import os
import secrets
import socket
import subprocess
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "server_config.json"
LOG_PATH = ROOT / "server.log"
DEFAULT_PORT = 8765
_TAILSCALE = ipaddress.ip_network("100.64.0.0/10")


def load_config():
    """{"token", "port"}; creates server_config.json with a fresh token the first time."""
    cfg = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        except ValueError:
            cfg = {}
    changed = False
    if not cfg.get("token"):
        cfg["token"] = secrets.token_urlsafe(12)  # 16 chars
        changed = True
    if not cfg.get("port"):
        cfg["port"] = DEFAULT_PORT
        changed = True
    if changed:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return cfg


def local_addresses():
    """[(ip, label)] the phone could use, Tailscale first."""
    ips = set()
    host = socket.gethostname()
    try:
        ips.update(info[4][0] for info in socket.getaddrinfo(host, None, socket.AF_INET))
    except OSError:
        pass
    try:
        ips.update(socket.gethostbyname_ex(host)[2])
    except OSError:
        pass

    out = []
    for ip in ips:
        addr = ipaddress.ip_address(ip)
        if addr.is_loopback or addr.is_link_local:
            continue
        if addr in _TAILSCALE:
            out.append((0, ip, "Tailscale · works anywhere"))
        elif addr.is_private:
            out.append((1 if ip.startswith(("192.168.", "10.")) else 2, ip, "Home Wi-Fi only"))
    return [(ip, label) for _, ip, label in sorted(out)]


def link_url(host, port, token):
    """What the QR code holds; the Android app understands jarvis://link?..."""
    return "jarvis://link?" + urllib.parse.urlencode({"host": host, "port": port, "token": token})


def qr_svg(text):
    import segno

    return segno.make(text, error="m").svg_inline(dark="#06070c", light="#ffffff", border=2, omitsize=True)


def server_running(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def start_server():
    """Launch server.py in the background (no console window); output goes to server.log."""
    exe = Path(sys.executable)
    console = exe.with_name("python.exe")  # the desktop app runs under pythonw.exe
    if console.exists():
        exe = console
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    log = open(LOG_PATH, "ab")  # noqa: SIM115 - handed to the child process
    try:
        subprocess.Popen(
            [str(exe), str(ROOT / "server.py")],
            cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    finally:
        log.close()
