#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
Discord Quest Fake — настольное приложение (Windows 10).

Пустышка — копия оконного системного exe с именем exe реальной игры.
Discord сканирует процессы, видит совпадение имени -> ставит активность.
Плюс автовыполнение квестов через API: видео, игра, активность.

Запуск:  py quest_app.py
"""

import base64
import datetime
import json
import os
import platform
import queue
import random
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
except ImportError:
    print("Нет tkinter.\n  Windows: переустановите Python с галкой 'tcl/tk'.\n"
          "  Linux:   sudo apt install python3-tk")
    raise

APP_NAME = "Наебщик Дискорда"
APP_VER = "4.1"

IS_WIN = os.name == "nt"
IS_MAC = platform.system() == "Darwin"

DETECTABLE_URLS = (
    "https://discord.com/api/v10/applications/detectable",
    "https://discord.com/api/applications/detectable",
    "https://discord.com/api/v9/applications/detectable",
)
API_BASE = "https://discord.com/api/v9"

CACHE_FILE = Path(tempfile.gettempdir()) / "discord-quest-fake-games.json"
BUILD_CACHE = Path(tempfile.gettempdir()) / "discord-quest-fake-build.json"
CONFIG_FILE = Path.home() / ".discord-quest-fake.json"

BAD_TOKENS = ("launcher", "test", "beta", "demo", "server", "crash",
              "unins", "setup", "helper", "benchmark", "shipping")
CREATE_NO_WINDOW = 0x08000000

# что умеем и в каком порядке предпочитаем, если у квеста несколько задач
TASK_ORDER = ("WATCH_VIDEO", "WATCH_VIDEO_ON_MOBILE", "PLAY_ON_DESKTOP",
              "PLAY_ACTIVITY", "STREAM_ON_DESKTOP")
VIDEO_TASKS = ("WATCH_VIDEO", "WATCH_VIDEO_ON_MOBILE")
LIGHT_TASKS = VIDEO_TASKS + ("PLAY_ACTIVITY", "STREAM_ON_DESKTOP")

C = {
    "bg":   "#1e1f22",
    "bg2":  "#2b2d31",
    "bg3":  "#313338",
    "fg":   "#dbdee1",
    "fg2":  "#949ba4",
    "line": "#3f4147",
    "accent": "#5865f2",
    "accent2": "#4752c4",
    "green": "#23a55a",
    "red": "#da373c",
    "yellow": "#f0b232",
}


# =========================================================================== #
#  БЭКЕНД
# =========================================================================== #
def os_tag():
    s = platform.system().lower()
    if s.startswith("win"):
        return "win32"
    if s == "darwin":
        return "darwin"
    return "linux"


def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def sanitize(s):
    s = re.sub(r'[\\/:*?"<>|]', "", s).strip()
    return s or "game"


def default_root():
    if IS_WIN:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    elif IS_MAC:
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "discord-quest-fake" / "games"


def norm_token(t):
    t = (t or "").strip().strip('"').strip("'")
    if t.lower().startswith("bearer "):
        t = t[7:].strip()
    return t


def parse_iso(s):
    if not s:
        return 0.0
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def jitter(lo, hi):
    return random.uniform(lo, hi)


def http_json(url, token=None, timeout=30):
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; discord-quest-fake/4.1)",
        "Accept": "application/json",
    }
    if token:
        headers["Authorization"] = token
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_detectable(force=False):
    if not force and CACHE_FILE.exists() and time.time() - CACHE_FILE.stat().st_mtime < CACHE_TTL:
        try:
            return json.loads(CACHE_FILE.read_text("utf-8"))
        except Exception:
            pass
    err = None
    for url in DETECTABLE_URLS:
        try:
            data = http_json(url)
            if isinstance(data, list) and data:
                try:
                    CACHE_FILE.write_text(json.dumps(data), "utf-8")
                except Exception:
                    pass
                return data
        except Exception as e:
            err = e
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text("utf-8"))
        except Exception:
            pass
    raise RuntimeError(f"Не удалось получить список игр Discord: {err}")


def exe_score(e):
    n = (e.get("name") or "").lower()
    return (1 if e.get("is_launcher") else 0,
            sum(t in n for t in BAD_TOKENS),
            len(n))


def pick_exe(app, tag):
    exes = [e for e in (app.get("executables") or [])
            if e.get("name") and e.get("os") == tag]
    if not exes:
        return None
    return sorted(exes, key=exe_score)[0]["name"]


def search_games(games, query, tag, limit=80):
    q = norm(query)
    out = [g for g in games if q in norm(g.get("name") or "") and pick_exe(g, tag)]
    out.sort(key=lambda g: (0 if norm(g.get("name")) == q else
                            1 if norm(g.get("name")).startswith(q) else 2,
                            len(g.get("name") or "")))
    return out[:limit]


def find_game_by_id(games, app_id):
    for g in games:
        if str(g.get("id")) == str(app_id) and pick_exe(g, os_tag()):
            return g
    return None


# на Windows 10 всё это — настоящие оконные приложения
WIN_WINDOWED = ("System32/notepad.exe", "System32/mspaint.exe",
                "System32/charmap.exe", "System32/osk.exe")
WIN_PLAIN = ("System32/ping.exe",)


def stub_candidates(windowed=True):
    if not IS_WIN:
        return [Path(c) for c in ("/bin/sleep", "/usr/bin/sleep") if Path(c).exists()]
    root = os.environ.get("SystemRoot", r"C:\Windows")
    names = WIN_WINDOWED + WIN_PLAIN if windowed else WIN_PLAIN
    out = []
    for rel in names:
        p = Path(root) / rel
        if p.exists():
            out.append(p)
    return out


def kill_processes(names):
    if not IS_WIN:
        return
    for n in names:
        try:
            subprocess.run(["taskkill", "/F", "/IM", n],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=CREATE_NO_WINDOW)
        except Exception:
            pass


def tasklist_has(exe_name):
    if not IS_WIN:
        return True
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {exe_name}", "/NH"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW).stdout
        return exe_name.lower() in out.decode("cp866", "ignore").lower()
    except Exception:
        return False


def file_in_use(p):
    p = Path(p)
    if not p.exists():
        return False
    try:
        with open(p, "r+b"):
            return False
    except PermissionError:
        return True
    except OSError:
        return False


def find_window(pid, timeout=2.5):
    """HWND первого видимого окна процесса, либо None."""
    if not IS_WIN or not pid:
        return None
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        t0 = time.time()
        while True:
            found = []

            def cb(h, l):
                p = wintypes.DWORD()
                u.GetWindowThreadProcessId(h, ctypes.byref(p))
                if p.value == pid and u.IsWindowVisible(h):
                    found.append(h)
                    return False
                return True

            u.EnumWindows(CB(cb), 0)
            if found:
                return found[0]
            if time.time() - t0 >= timeout:
                return None
            time.sleep(0.15)
    except Exception:
        return None


def set_window_title(pid, title):
    h = find_window(pid, 3.0)
    if h:
        try:
            import ctypes
            ctypes.windll.user32.SetWindowTextW(h, str(title))
        except Exception:
            pass


# --------------------------------------------------------------------------- #
#  MUI: переименованный системный exe не находит <имя>.exe.mui в языковых
#  подпапках и падает с "Не удалось создать новый документ". Копируем их рядом.
# --------------------------------------------------------------------------- #
LANG_DIR_RE = re.compile(r"^[a-z]{2}(?:-[A-Za-z0-9]{2,8})*$")
_LANG_CACHE = {}


def _lang_dirs(root):
    key = str(root)
    if key in _LANG_CACHE:
        return _LANG_CACHE[key]
    try:
        res = sorted((d for d in Path(root).iterdir()
                      if d.is_dir() and LANG_DIR_RE.match(d.name)),
                     key=lambda d: d.name)
    except OSError:
        res = []
    _LANG_CACHE[key] = res
    return res


def copy_mui(src_exe, dst_exe):
    """Разложить <dst>.exe.mui по языковым папкам рядом с пустышкой."""
    src_exe, dst_exe = Path(src_exe), Path(dst_exe)
    src_mui_name = src_exe.name + ".mui"
    dst_mui_name = dst_exe.name + ".mui"
    done = []
    for d in _lang_dirs(src_exe.parent):
        s = d / src_mui_name
        if not s.is_file():
            continue
        try:
            out = dst_exe.parent / d.name
            out.mkdir(parents=True, exist_ok=True)
            t = out / dst_mui_name
            if not t.exists() or t.stat().st_size != s.stat().st_size:
                shutil.copyfile(s, t)
            done.append(d.name)
        except OSError:
            pass
    return done


class FakeGame:
    def __init__(self, root, exe_name, game_name, seconds, log=None, windowed=True):
        self.root = Path(root)
        self.exe_name = exe_name
        self.game_name = game_name
        self.seconds = seconds
        self.log = log or (lambda *a, **k: None)
        self.windowed = windowed
        self.sources = stub_candidates(windowed)
        self.src_i = 0
        self.folder = self.root / sanitize(self.game_name)
        self.stub = self.folder / self.exe_name.replace("\\", "/")
        self.proc = None
        self.deadline = 0.0
        self._stop = False
        self.early_deaths = 0
        self._mui_logged = False

    # ---------------------------------------------------------------- #
    def _want_window(self):
        return IS_WIN and not self._is_ping()

    def _is_ping(self):
        return "ping" in self.sources[self.src_i].name.lower()

    def start(self):
        if not self.sources:
            raise RuntimeError("нет системного exe для пустышки")
        self.deadline = time.time() + self.seconds
        self.log(f"    {self.stub.name}: готовлю пустышку "
                 f"(порядок: {', '.join(s.name for s in self.sources)})", "dim")

        for i in range(len(self.sources)):
            self.src_i = i
            name = self.sources[i].name
            try:
                self._make()
            except Exception as e:
                self.log(f"    {name}: файл не подготовился: {e}", "err")
                continue
            try:
                self._spawn()
            except Exception as e:
                self.log(f"    {name}: не подошёл: {e}", "err")
                continue

            seen = tasklist_has(self.stub.name)
            self.log(f"    {self.stub.name}: PID {self.proc.pid}, источник {name}, "
                     f"виден в системе: {'да' if seen else 'НЕТ'}", "ok" if seen else "warn")
            if self._want_window():
                threading.Thread(target=set_window_title,
                                 args=(self.proc.pid, self.game_name), daemon=True).start()
            threading.Thread(target=self._supervise, daemon=True).start()
            return

        self.log(f"    {self.stub.name}: ничего не удержалось — надзор продолжит попытки",
                 "warn")
        threading.Thread(target=self._supervise, daemon=True).start()

    # ---------------------------------------------------------------- #
    @staticmethod
    def _writable(p):
        try:
            if p.exists():
                os.chmod(p, 0o666)
        except OSError:
            pass

    def _kill_by_name(self):
        if IS_WIN:
            kill_processes([self.stub.name])

    @staticmethod
    def _place(src, dst):
        """Хардлинк, если можно — иначе копия."""
        try:
            if dst.exists():
                dst.unlink()
            os.link(src, dst)
            return
        except OSError:
            pass
        shutil.copyfile(src, dst)

    def _make(self):
        src = self.sources[self.src_i]
        self.folder.mkdir(parents=True, exist_ok=True)
        self.stub.parent.mkdir(parents=True, exist_ok=True)

        ready = False
        try:
            ready = self.stub.exists() and self.stub.stat().st_size == src.stat().st_size
        except OSError:
            ready = False

        if not ready:
            if file_in_use(self.stub):
                self.log(f"    {self.stub.name}: убираю хвост прошлого запуска", "warn")
                self._kill_by_name()
                for _ in range(20):
                    if not file_in_use(self.stub):
                        break
                    time.sleep(0.25)

            self._writable(self.stub)
            last = None
            for _ in range(6):
                try:
                    self._place(src, self.stub)
                    if not IS_WIN:
                        self.stub.chmod(0o755)
                    ready = True
                    break
                except OSError as e:
                    last = e
                    time.sleep(0.3)
            if not ready:
                if self.stub.exists() and self.stub.stat().st_size > 0:
                    self.log(f"    {self.stub.name}: файл занят ({last}), беру как есть",
                             "dim")
                else:
                    raise RuntimeError(f"не смог подготовить {self.stub.name}: {last}")

        langs = copy_mui(src, self.stub)
        if langs and not self._mui_logged:
            self._mui_logged = True
            self.log(f"    {self.stub.name}: языковые файлы на месте "
                     f"({', '.join(langs)})", "dim")

    def _doc_file(self):
        """Пустой txt рядом с пустышкой: notepad не создаёт 'новый документ'."""
        doc = self.folder / (sanitize(self.game_name) + ".txt")
        try:
            if not doc.exists():
                doc.write_text("", "utf-8")
        except OSError:
            return None
        return doc if doc.exists() else None

    def _argv(self, remaining):
        if IS_WIN and not self._is_ping():
            src_name = self.sources[self.src_i].name.lower()
            if src_name.startswith("notepad"):
                doc = self._doc_file()
                if doc:
                    return [str(self.stub), str(doc)]
            return [str(self.stub)]
        if IS_WIN and self._is_ping():
            return [str(self.stub), "-n", str(max(2, min(remaining, 9999))), "127.0.0.1"]
        return [str(self.stub)]

    def _spawn(self):
        remaining = int(self.deadline - time.time())
        win = self._want_window()
        kw = {} if win else ({"creationflags": CREATE_NO_WINDOW} if IS_WIN else {})
        try:
            self.proc = subprocess.Popen(
                self._argv(remaining), cwd=str(self.stub.parent),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
        except Exception as e:
            self.proc = None
            raise RuntimeError(f"Popen не сработал: {e}")

        time.sleep(0.4)
        if self.proc.poll() is not None:
            code = self.proc.returncode
            self.proc = None
            raise RuntimeError(f"процесс вышел сразу (код {code})")

        # для оконных источников окно обязательно должно реально появиться
        if win and not find_window(self.proc.pid, 2.5):
            try:
                self.proc.terminate()
            except Exception:
                pass
            self.proc = None
            raise RuntimeError("окно так и не появилось")

    def _alive(self):
        return self.proc is not None and self.proc.poll() is None

    def _supervise(self):
        born = time.time()
        while not self._stop and time.time() < self.deadline:
            time.sleep(1)
            if self._alive():
                continue
            if self.proc is not None:
                lived = time.time() - born
                self.log(f"    {self.stub.name}: процесс пропал через {lived:.1f} c", "warn")
                # быстрая смерть = источник не подходит, меняем сразу
                if lived < 4:
                    self.early_deaths += 1
                    if self.early_deaths >= 1 and self.src_i + 1 < len(self.sources):
                        self.src_i += 1
                        self.log(f"    перехожу на {self.sources[self.src_i].name}", "warn")
                        self.early_deaths = 0
                        self._kill_by_name()
                else:
                    self.early_deaths = 0
            try:
                self._make()
                self._spawn()
                if self._want_window():
                    threading.Thread(target=set_window_title,
                                     args=(self.proc.pid, self.game_name),
                                     daemon=True).start()
            except Exception as e:
                self.log(f"    {self.stub.name}: {e}", "err")
                self.proc = None
            born = time.time()
        if not self._stop:
            self.stop()

    def alive(self):
        return not self._stop

    def pid(self):
        return self.proc.pid if self.proc else 0

    def stop(self):
        if self._stop:
            return
        self._stop = True
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass


def ipc_candidates():
    if IS_WIN:
        return [rf"\\.\pipe\discord-ipc-{i}" for i in range(10)]
    dirs = []
    for d in (os.environ.get("XDG_RUNTIME_DIR"), os.environ.get("TMPDIR"),
              tempfile.gettempdir(), "/tmp"):
        if d and d not in dirs:
            dirs.append(d)
    if hasattr(os, "getuid"):
        d = f"/run/user/{os.getuid()}"
        if d not in dirs:
            dirs.append(d)
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        dirs.append(os.path.join(xdg, "app", "com.discordapp.Discord"))
    return [os.path.join(d, f"discord-ipc-{i}") for d in dirs for i in range(10)]


def discord_running():
    for path in ipc_candidates():
        try:
            if IS_WIN:
                f = open(path, "r+b", buffering=0)
                f.close()
            else:
                import socket
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(1)
                s.connect(path)
                s.close()
            return True
        except Exception:
            continue
    return False


class DiscordRPC(threading.Thread):
    def __init__(self, app_id, activity, pid):
        super().__init__(daemon=True)
        self.app_id, self.activity, self.pid = str(app_id), activity, pid
        self.sock = None

    def _connect(self):
        for path in ipc_candidates():
            try:
                if IS_WIN:
                    self.sock = open(path, "r+b", buffering=0)
                else:
                    import socket
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.settimeout(3)
                    s.connect(path)
                    self.sock = s
                return True
            except Exception:
                continue
        return False

    def _read(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.read(n - len(buf)) if IS_WIN else self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("ipc closed")
            buf += chunk
        return buf

    def _write(self, b):
        if IS_WIN:
            self.sock.write(b)
        else:
            self.sock.sendall(b)

    def _send(self, op, payload):
        data = json.dumps(payload).encode("utf-8")
        self._write(struct.pack("<II", op, len(data)) + data)

    def _recv(self):
        op, ln = struct.unpack("<II", self._read(8))
        payload = json.loads(self._read(ln).decode("utf-8")) if ln else {}
        return op, payload

    def run(self):
        if not self._connect():
            return
        try:
            self._send(0, {"v": 1, "client_id": self.app_id})
            while True:
                op, payload = self._recv()
                if op == 3:
                    self._send(4, payload)
                elif op == 1 and payload.get("evt") == "READY":
                    break
            self._send(1, {"cmd": "SET_ACTIVITY",
                           "args": {"pid": self.pid, "activity": self.activity},
                           "nonce": str(uuid.uuid4())})
            while True:
                op, payload = self._recv()
                if op == 3:
                    self._send(4, payload)
        except Exception:
            pass

    def close(self):
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass


# =========================================================================== #
#  API DISCORD (квесты)
# =========================================================================== #
class DiscordApiError(Exception):
    def __init__(self, code, payload=None, raw=""):
        self.code = code
        self.payload = payload or {}
        self.raw = raw
        super().__init__(f"{code} {self.describe()}")

    def describe(self):
        msg = self.payload.get("message") or ""
        errs = self.payload.get("errors") or {}
        if errs:
            try:
                msg += " " + json.dumps(errs, ensure_ascii=False)
            except Exception:
                pass
        return msg.strip() or "ошибка"


def _b64(obj):
    return base64.b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode()


def _fetch_build_number():
    """Настоящий build клиента: html discord.com/app, затем публичный снапшот."""
    try:
        req = urllib.request.Request("https://discord.com/app", headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Accept": "text/html"})
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read().decode("utf-8", "ignore")
        for pat in (r'"BUILD_NUMBER"\s*:\s*"?(\d{4,})',
                    r'build_number["\']?\s*[:=]\s*["\']?(\d{4,})'):
            m = re.search(pat, html)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    try:
        data = http_json("https://raw.githubusercontent.com/MFDI-hub/"
                         "discord_build_number_scrapper/main/data/latest.json")
        return int((data.get("stable") or {}).get("build_number") or 0)
    except Exception:
        return 0


def build_number():
    try:
        if BUILD_CACHE.exists() and time.time() - BUILD_CACHE.stat().st_mtime < CACHE_TTL:
            v = int(json.loads(BUILD_CACHE.read_text("utf-8")).get("build_number") or 0)
            if v:
                return v
    except Exception:
        pass
    v = _fetch_build_number()
    if v:
        try:
            BUILD_CACHE.write_text(json.dumps({"build_number": v}), "utf-8")
        except Exception:
            pass
    return v or 0


def _locale():
    try:
        import locale as _l
        loc = _l.getlocale()[0] or ""
    except Exception:
        loc = ""
    return (loc or "ru-RU").replace("_", "-")


def super_properties(build=0):
    if IS_WIN:
        os_name, os_arch = "Windows", "x64"
        v = platform.version() or ""
        os_ver = v if v.count(".") >= 2 else "10.0.19045"
    elif IS_MAC:
        os_name, os_arch = "Mac OS X", "arm64"
        os_ver = platform.mac_ver()[0] or "14.0.0"
    else:
        os_name, os_arch = "Linux", "x64"
        os_ver = platform.release() or "6.5.0"
    return _b64({
        "os": os_name,
        "browser": "Discord Client",
        "release_channel": "stable",
        "client_version": "1.0.9178",
        "os_version": os_ver,
        "os_arch": os_arch,
        "app_arch": os_arch,
        "system_locale": _locale(),
        "client_build_number": int(build or 0),
        "native_build_number": 0,
        "client_event_source": None,
        "design_id": 0,
    })


class DiscordApi:
    def __init__(self, token):
        self.token = norm_token(token)
        self.build = build_number()
        self.ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) discord/1.0.9178 Chrome/128.0.6613.186 "
                   "Electron/32.2.7 Safari/537.36")

    def _headers(self, with_body):
        h = {
            "Authorization": self.token,
            "User-Agent": self.ua,
            "Accept": "*/*",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
            "Origin": "https://discord.com",
            "Referer": "https://discord.com/channels/@me",
            "X-Discord-Locale": "ru",
            "X-Super-Properties": super_properties(self.build),
        }
        if with_body:
            h["Content-Type"] = "application/json"
        return h

    def request(self, method, path, body=None, retries=3):
        url = path if path.startswith("http") else API_BASE + path
        data = None if body is None else json.dumps(body).encode("utf-8")
        last = None
        for attempt in range(retries + 1):
            req = urllib.request.Request(url, data=data,
                                         headers=self._headers(data is not None),
                                         method=method)
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    raw = r.read().decode("utf-8")
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                raw = e.read().decode("utf-8", "ignore")
                try:
                    payload = json.loads(raw) if raw else {}
                except Exception:
                    payload = {}
                if e.code == 429:
                    wait = payload.get("retry_after")
                    try:
                        wait = float(wait)
                    except (TypeError, ValueError):
                        wait = 1.5
                    time.sleep(min(30.0, wait + 0.3))
                    last = DiscordApiError(e.code, payload, raw)
                    continue
                if e.code >= 500 and attempt < retries:
                    time.sleep(1.0 + attempt)
                    last = DiscordApiError(e.code, payload, raw)
                    continue
                raise DiscordApiError(e.code, payload, raw)
            except urllib.error.URLError as e:
                last = DiscordApiError(0, {}, str(e))
                if attempt < retries:
                    time.sleep(1.0 + attempt)
                    continue
                raise last
        raise last or DiscordApiError(0, {}, "не дошло")

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, body=None):
        return self.request("POST", path, body if body is not None else {})


# --------------------------------------------------------------------------- #
#  Разбор квестов
# --------------------------------------------------------------------------- #
def quest_tasks(q):
    """tasks из task_config_v2, с откатом на legacy task_config."""
    cfg = q.get("config") or {}
    for key in ("task_config_v2", "task_config"):   # v2 приоритетнее, как в клиенте
        tasks = ((cfg.get(key) or {}).get("tasks")) or {}
        if tasks:
            return tasks
    return {}


def task_kind(key, task):
    """Тип задачи по полю type, иначе по ключу. Префиксы ловят варианты
    вида PLAY_ON_DESKTOP_V2 и всё новое, что Discord допишет."""
    raw = str((task or {}).get("type") or key or "").upper()
    if not raw:
        return None
    if raw.startswith("WATCH_VIDEO"):
        return "WATCH_VIDEO"
    if raw.startswith("PLAY_ON_DESKTOP"):
        return "PLAY_ON_DESKTOP"
    if raw.startswith("PLAY_ACTIVITY"):
        return "PLAY_ACTIVITY"
    if raw.startswith("STREAM_ON_DESKTOP"):
        return "STREAM_ON_DESKTOP"
    if raw.startswith("ACHIEVEMENT_IN_GAME"):
        return "ACHIEVEMENT_IN_GAME"
    if raw.startswith("ACHIEVEMENT"):
        return "ACHIEVEMENT_IN_ACTIVITY"
    if raw.startswith("PLAY_ON_XBOX"):
        return "PLAY_ON_XBOX"
    if raw.startswith("PLAY_ON_PLAYSTATION"):
        return "PLAY_ON_PLAYSTATION"
    return raw


def pick_task(tasks):
    """(наш тип, ключ задачи в конфиге, задача) — самая приоритетная поддержанная.
    Ключ возвращаем отдельно: прогресс на сервере лежит под ключом, не под типом."""
    best = None
    for key, task in (tasks or {}).items():
        if not isinstance(task, dict):
            continue
        kind = task_kind(key, task)
        if kind in TASK_ORDER:
            rank = TASK_ORDER.index(kind)
            if best is None or rank < best[0]:
                best = (rank, kind, key, task)
    if not best:
        return None, None, None
    return best[1], best[2], best[3]


def quest_app_id(q, tasks):
    # с июля 2026 id приложения лежит в задаче, старый путь — фолбэк
    for t in (tasks or {}).values():
        for a in (t.get("applications") or []):
            if a.get("id"):
                return str(a["id"])
    app = (q.get("config") or {}).get("application") or {}
    return str(app.get("id") or "")


def quest_progress(q, task_key):
    if not task_key:
        return 0.0
    st = q.get("user_status") or {}
    v = (st.get("progress") or {}).get(task_key)
    if isinstance(v, dict) and v.get("value") is not None:
        try:
            return float(v["value"])
        except (TypeError, ValueError):
            pass
    if st.get("stream_progress_seconds") is not None:
        try:
            return float(st["stream_progress_seconds"])
        except (TypeError, ValueError):
            pass
    return 0.0


def fetch_quests(token):
    api = DiscordApi(token)
    data = api.get("/quests/@me")
    out = []
    for q in (data.get("quests") or []):
        cfg = q.get("config") or {}
        msgs = cfg.get("messages") or {}
        tasks = quest_tasks(q)
        st = q.get("user_status") or {}
        targets = [int(t.get("target") or 0) for t in tasks.values() if isinstance(t, dict)]
        out.append({
            "app_id": quest_app_id(q, tasks),
            "quest_id": str(q.get("id") or ""),
            "name": (msgs.get("game_title") or msgs.get("quest_name")
                     or (cfg.get("application") or {}).get("name") or "?"),
            "tasks": ", ".join(sorted(tasks.keys())) or "?",
            "target": max(targets or [0]),
            "expires": parse_iso(cfg.get("expires_at")),
            "enrolled": bool(st.get("enrolled_at")),
            "completed": bool(st.get("completed_at")),
            "claimed": bool(st.get("claimed_at")),
        })
    return out, data


class Session:
    """Одна сессия: N пустышек, живут одновременно до общего дедлайна."""

    def __init__(self, items, total, root, use_rpc, clean, log, windowed=True):
        self.items, self.total, self.root = items, total, root
        self.use_rpc, self.clean, self.log = use_rpc, clean, log
        self.windowed = windowed
        self.runners, self.rpcs = [], []
        self.running = False
        self._done = False
        self.started = 0.0

    def start(self):
        self.running = True
        self.started = time.time()
        for it in self.items:
            try:
                r = FakeGame(self.root, it["exe"], it["name"], self.total,
                             self.log, self.windowed)
                r.start()
            except Exception as e:
                self.log(f"  ! {it['name']}: {e}", "err")
                continue
            self.runners.append(r)
            self.log(f"  запущено: {it['name']}  ->  {r.stub.name}", "ok")
            if self.use_rpc and it.get("app_id"):
                time.sleep(0.2)
                act = {"details": "Сессия", "state": "Discord Quest",
                       "timestamps": {"start": int(time.time() * 1000)}}
                rpc = DiscordRPC(it["app_id"], act, r.pid())
                rpc.start()
                self.rpcs.append(rpc)
        if not self.runners:
            self.running = False
            self._done = True
            return
        threading.Thread(target=self._wait, daemon=True).start()

    def _wait(self):
        while any(r.alive() for r in self.runners):
            time.sleep(0.5)
        self._shutdown()
        self.log("Сессия завершена. Проверьте прогресс квеста в Discord.", "ok")

    def remaining(self):
        return max(0, int(self.total - (time.time() - self.started)))

    def cancel(self):
        self.log("Остановлено вручную.", "warn")
        self._shutdown()

    def _shutdown(self):
        if self._done:
            return
        self._done = True
        self.running = False
        for r in self.runners:
            r.stop()
        for rpc in self.rpcs:
            rpc.close()
        if self.clean:
            shutil.rmtree(self.root, ignore_errors=True)


# =========================================================================== #
#  АВТОВЫПОЛНЕНИЕ КВЕСТОВ
# =========================================================================== #
class AutoRunner(threading.Thread):
    """Принимает квесты, крутит видео, отбивает heartbeat, забирает награды.
    Режимы: api (тихо, без окон), hybrid (тихо + окна), process (только окна).
    Что не умеет — пропускает с записью в лог."""

    def __init__(self, api, games, log, root, opts, on_finish):
        super().__init__(daemon=True)
        self.api = api
        self.games = list(games or [])
        self.log = log
        self.root = Path(root)
        self.opts = opts or {}
        self.on_finish = on_finish
        self.mode = self.opts.get("mode", "api")
        self.fast_video = bool(self.opts.get("fast_video", False))
        self.stealth = self.mode != "process"
        self.stopped = False
        self.rows = {}
        self.lock = threading.Lock()
        self.threads = []
        self.fakes = []
        self.user_id = ""
        self.channel_id = None

    # ---------------------------------------------------------------- #
    def set_status(self, qid, text, name=None, task=None, target=None):
        with self.lock:
            row = self.rows.setdefault(qid, {"name": "", "task": "", "target": 0,
                                             "status": ""})
            if name:
                row["name"] = name
            if task:
                row["task"] = task
            if target:
                row["target"] = target
            row["status"] = text

    def stop(self):
        self.stopped = True
        for f in list(self.fakes):
            try:
                f.stop()
            except Exception:
                pass

    def _sleep(self, sec):
        end = time.time() + max(0.0, sec)
        while time.time() < end and not self.stopped:
            time.sleep(min(0.4, max(0.05, end - time.time())))

    # ---------------------------------------------------------------- #
    def run(self):
        try:
            self._main()
        except DiscordApiError as e:
            if e.code == 401:
                self.log("Автоквесты: токен не принят (401). Возьмите свежий "
                         "пользовательский токен.", "err")
            else:
                self.log(f"Автоквесты: ошибка API {e.code} {e.describe()}", "err")
        except Exception as e:
            self.log(f"Автоквесты: {e}", "err")
        finally:
            for f in list(self.fakes):
                try:
                    f.stop()
                except Exception:
                    pass
            self.log("Автоквесты: закончил.", "ok")
            self.on_finish()

    # ---------------------------------------------------------------- #
    def _main(self):
        me = self.api.get("/users/@me")
        self.user_id = str(me.get("id") or "")
        self.log(f"Автоквесты: аккаунт {me.get('username', '?')} (id {self.user_id}), "
                 f"режим {self.mode}, build {self.api.build}", "ok")

        data = self.api.get("/quests/@me")
        quests = data.get("quests") or []
        excluded = data.get("excluded_quests") or []
        self.log(f"Автоквесты: /quests/@me -> quests {len(quests)}, "
                 f"excluded {len(excluded)}, ключи ответа: "
                 f"{', '.join(sorted(data.keys())) or '—'}", "dim")

        blocked = data.get("quest_enrollment_blocked_until")
        susp = data.get("quest_access_suspended_until")
        if blocked:
            self.log(f"Автоквесты: приём квестов придержан до {blocked}.", "warn")
        if susp:
            self.log(f"Автоквесты: аккаунт помечен за автоматизацию, "
                     f"квесты заблокированы до {susp}.", "warn")

        if not quests:
            if excluded:
                self.log(f"Автоквесты: все квесты ушли в excluded_quests "
                         f"({len(excluded)}) — аккаунт по ним не проходит.", "warn")
            else:
                self.log("Автоквесты: список пуст. Проверьте, что токен от того "
                         "аккаунта, где видны квесты.", "warn")

        if not self.games:
            try:
                self.games = fetch_detectable()
            except Exception as e:
                self.log(f"Автоквесты: база игр не загрузилась ({e}).", "warn")

        now = time.time()
        plan, ready, reasons = [], [], {}
        for q in quests:
            qid = str(q.get("id") or "")
            if not qid:
                continue
            cfg = q.get("config") or {}
            name = ((cfg.get("messages") or {}).get("game_title")
                    or (cfg.get("application") or {}).get("name") or "?")
            st = q.get("user_status") or {}
            tasks = quest_tasks(q)
            keys = ", ".join(sorted(tasks.keys())) or "нет задач"

            exp = parse_iso(cfg.get("expires_at"))
            if exp and exp < now and not st.get("completed_at"):
                self.set_status(qid, "истёк", name=name)
                reasons["истёк"] = reasons.get("истёк", 0) + 1
                continue

            kind, task_key, task = pick_task(tasks)
            if not kind:
                self.set_status(qid, f"не умею: {keys}", name=name)
                reasons["не умею"] = reasons.get("не умею", 0) + 1
                self.log(f"Автоквесты: пропускаю «{name}» — ключи задач: {keys}", "warn")
                continue

            target = int((task or {}).get("target") or 0)
            app_id = quest_app_id(q, tasks)
            exe_name = None
            if self.games:
                g = find_game_by_id(self.games, app_id)
                if g:
                    exe_name = pick_exe(g, os_tag())
            it = {"q": q, "qid": qid, "name": name, "kind": kind, "task_key": task_key,
                  "target": target, "app_id": app_id, "status": st,
                  "exe_name": exe_name or (sanitize(name) + ".exe")}
            self.set_status(qid, "в очереди", name=name, task=kind, target=target)
            (ready if st.get("completed_at") else plan).append(it)

        if reasons:
            self.log("Автоквесты: пропуски — " +
                     ", ".join(f"{k}: {v}" for k, v in sorted(reasons.items())), "dim")

        for it in ready:
            if self.stopped:
                break
            if self.opts.get("claim") and not it["status"].get("claimed_at"):
                self._claim(it)
            else:
                self.set_status(it["qid"], "уже пройден")

        if not plan:
            self.log(f"Автоквесты: поддерживаемых незакрытых квестов нет "
                     f"(всего {len(quests)}).", "warn")
            return

        random.shuffle(plan)
        light = [it for it in plan if it["kind"] in LIGHT_TASKS]
        heavy = [it for it in plan if it["kind"] not in LIGHT_TASKS]
        self.log(f"Автоквесты: в работе {len(plan)} квест(ов) "
                 f"(видео/активности {len(light)}, игры {len(heavy)}).", "ok")

        # лёгкие и тяжёлые идут параллельно, но внутри группы — по одному
        for group in (light, heavy):
            if group:
                t = threading.Thread(target=self._run_group, args=(group,), daemon=True)
                t.start()
                self.threads.append(t)
                self._sleep(jitter(0.5, 3.0))

        for t in self.threads:
            while t.is_alive() and not self.stopped:
                t.join(1)

        if self.opts.get("claim") and not self.stopped:
            self._sleep(jitter(3, 10))
            fresh = {}
            try:
                fresh = {str(x.get("id")): x
                         for x in (self.api.get("/quests/@me").get("quests") or [])}
            except Exception:
                pass
            for it in plan:
                f = fresh.get(it["qid"])
                if not f:
                    continue
                st = f.get("user_status") or {}
                if st.get("completed_at") and not st.get("claimed_at"):
                    self._claim(it, f)

    # ---------------------------------------------------------------- #
    def _run_group(self, items):
        for i, it in enumerate(items):
            if self.stopped:
                return
            if i:
                self._sleep(jitter(4, 18))
            if not self._prepare(it):
                continue
            self._work(it)

    def _prepare(self, it):
        st = it["status"]
        if st.get("enrolled_at"):
            return True
        if not self.opts.get("enroll"):
            self.log(f"Автоквесты: «{it['name']}» не принят, а авто-enroll выключен.",
                     "warn")
            return True
        self._sleep(jitter(2, 10))
        loc = random.choice((11, 10, 2))
        try:
            it["status"] = self.api.post(f"/quests/{it['qid']}/enroll",
                                         self._sealed(it["q"], {"location": loc})) or {}
        except DiscordApiError as e:
            self.log(f"Автоквесты: «{it['name']}» не принялся ({e.code} {e.describe()})",
                     "warn")
            self.set_status(it["qid"], "принять не удалось")
            return False
        self.log(f"Автоквесты: принял «{it['name']}» (location {loc})", "dim")
        return True

    def _work(self, it):
        try:
            kind = it["kind"]
            if kind in VIDEO_TASKS:
                self._video(it)
            elif kind == "PLAY_ACTIVITY":
                self._heartbeat(it, activity=True)
            elif kind == "STREAM_ON_DESKTOP":
                self.log(f"Автоквесты: «{it['name']}» — стрим. Отбиваю heartbeat, "
                         f"но зачёт может потребовать реального стрима в войсе.", "warn")
                self._heartbeat(it, activity=True)
            else:
                self._play(it)
        except Exception as e:
            self.log(f"Автоквесты: «{it['name']}» сломался: {e}", "err")
            self.set_status(it["qid"], "ошибка")

    # ---------------------------------------------------------------- #
    @staticmethod
    def _sealed(q, body):
        for key in ("traffic_metadata_sealed", "metadata_sealed"):
            if q.get(key):
                body[key] = q[key]
        return body

    @staticmethod
    def _progress(res, task_key, fallback=0.0):
        st = res or {}
        v = (st.get("progress") or {}).get(task_key)
        if isinstance(v, dict) and v.get("value") is not None:
            try:
                return float(v["value"])
            except (TypeError, ValueError):
                pass
        if st.get("stream_progress_seconds") is not None:
            try:
                return float(st["stream_progress_seconds"])
            except (TypeError, ValueError):
                pass
        return fallback

    def _channel(self):
        if self.channel_id:
            return self.channel_id
        try:
            for c in (self.api.get("/channels") or []):
                if c.get("type") in (1, 3):
                    self.channel_id = str(c["id"])
                    return self.channel_id
        except Exception:
            pass
        try:
            for g in (self.api.get("/users/@me/guilds") or [])[:5]:
                for c in (self.api.get(f"/guilds/{g['id']}/channels") or []):
                    if c.get("type") == 2:
                        self.channel_id = str(c["id"])
                        return self.channel_id
        except Exception:
            pass
        return None

    def _exec_path(self, it, stub=None):
        if stub:
            return str(stub)
        return f"C:\\Program Files\\{sanitize(it['name'])}\\{it['exe_name']}"

    def _launch_fake(self, it):
        if not self.games:
            return None
        g = find_game_by_id(self.games, it["app_id"])
        if not g:
            self.log(f"Автоквесты: «{it['name']}» нет в базе детекта — без окна.", "dim")
            return None
        exe = pick_exe(g, os_tag())
        if not exe:
            return None
        life = it["target"] + jitter(15, 120)   # не закрываем ровно на цели
        try:
            fg = FakeGame(self.root, exe, g.get("name") or it["name"], life,
                          self.log, self.opts.get("windowed", True))
            fg.start()
        except Exception as e:
            self.log(f"Автоквесты: пустышка для «{it['name']}» не поднялась: {e}", "warn")
            return None
        self.fakes.append(fg)
        self.log(f"Автоквесты: поднял «{fg.game_name}» -> {fg.stub.name} на {int(life)} c",
                 "ok")
        return fg

    def _play(self, it):
        fg = None
        if self.mode in ("hybrid", "process"):
            fg = self._launch_fake(it)
        path = self._exec_path(it, fg.stub if fg else None)
        if self.mode == "process":
            # клиент детектит процесс и отбивает heartbeat сам — не дублируем
            if self._wait_own_progress(it, 90):
                return
            self.log(f"Автоквесты: «{it['name']}» сам не зачёлся — подключаю heartbeat.",
                     "warn")
        self._heartbeat(it, exe_path=path)

    def _wait_own_progress(self, it, seconds):
        qid, target = it["qid"], it["target"]
        prog = quest_progress(it["q"], it["task_key"])
        end = time.time() + seconds
        while time.time() < end and not self.stopped:
            self._sleep(10)
            try:
                fresh = {str(x.get("id")): x
                         for x in (self.api.get("/quests/@me").get("quests") or [])}
            except Exception:
                continue
            f = fresh.get(qid)
            if not f:
                return False
            p = quest_progress(f, it["task_key"])
            if p > prog:
                it["q"], prog = f, p
            self.set_status(qid, f"{int(prog)}/{target} c")
            if prog > 0 and time.time() - end + seconds > 20:
                return True
        return prog > 0

    def _heartbeat(self, it, activity=False, exe_path=None):
        qid, target = it["qid"], it["target"]
        key = it["task_key"]
        app_id = it["app_id"]
        extra = {}
        if exe_path:
            extra["executable_path"] = exe_path
        if activity:
            ch = self._channel()
            if ch:
                extra["stream_key"] = f"call:{ch}:{self.user_id}"
        prog = quest_progress(it["q"], key)
        deadline = time.time() + target + 300
        misses = 0
        while prog < target and not self.stopped and time.time() < deadline:
            body = {"application_id": app_id, "terminal": False}
            body.update(extra)
            try:
                res = self.api.post(f"/quests/{qid}/heartbeat", body)
                misses = 0
                prog = max(prog, self._progress(res, key, prog))
            except DiscordApiError as e:
                misses += 1
                if e.code in (401, 403, 404) or misses >= 4:
                    self.log(f"Автоквесты: heartbeat «{it['name']}» не идёт "
                             f"({e.code} {e.describe()})", "warn")
                    break
            self.set_status(qid, f"{int(prog)}/{target} c")
            if prog >= target:
                break
            self._sleep(jitter(20, 30))
        if prog >= target and not self.stopped:
            body = {"application_id": app_id, "terminal": True}
            body.update(extra)
            try:
                self.api.post(f"/quests/{qid}/heartbeat", body)
            except DiscordApiError:
                pass
            self.set_status(qid, "пройден")
            self.log(f"Автоквесты: «{it['name']}» пройден ({it['kind']}).", "ok")
        elif not self.stopped:
            self.set_status(qid, f"недобор {int(prog)}/{target}")
            self.log(f"Автоквесты: «{it['name']}» не добрал прогресс.", "warn")

    def _video(self, it):
        qid, target = it["qid"], it["target"]
        key = it["task_key"]
        prog = quest_progress(it["q"], key)
        deadline = time.time() + target * 3 + 420
        done = False
        last = time.time()
        while not done and not self.stopped and time.time() < deadline:
            now = time.time()
            step = now - last
            last = now
            if self.fast_video:
                step = max(step, 8.0)          # ускоренно, ~4x
            ts = min(float(target), prog + step)
            if ts <= prog:
                self._sleep(1.0)
                continue
            try:
                res = self.api.post(f"/quests/{qid}/video-progress",
                                    {"timestamp": round(ts + random.uniform(0.0, 0.9), 3)})
            except DiscordApiError as e:
                if e.code in (401, 403):
                    self.log(f"Автоквесты: видео «{it['name']}» отбито "
                             f"({e.code} {e.describe()})", "warn")
                    break
                self._sleep(2)
                continue
            if res.get("completed_at"):
                done = True
                prog = float(target)
                break
            prog = max(prog, self._progress(res, key, prog))
            self.set_status(qid, f"{int(prog)}/{target} c")
            # 7–9.5 с — это цикл самого клиента Discord
            self._sleep(1.5 if self.fast_video else jitter(7.0, 9.5))
        for _ in range(30):
            if done or self.stopped:
                break
            try:
                if self.api.post(f"/quests/{qid}/video-progress",
                                 {"timestamp": float(target)}).get("completed_at"):
                    done = True
                    break
            except DiscordApiError:
                pass
            self._sleep(jitter(4, 7))
        if done:
            self.set_status(qid, "пройден")
            self.log(f"Автоквесты: видео «{it['name']}» просмотрено.", "ok")
        elif not self.stopped:
            self.set_status(qid, "видео не добито")
            self.log(f"Автоквесты: видео «{it['name']}» не добито.", "warn")

    def _claim(self, it, q=None):
        qid = it["qid"]
        src = q or it["q"]
        if self.stealth:
            self._sleep(jitter(30, 240))
        plat = 0
        try:
            plats = ((src.get("config") or {}).get("rewards_config") or {}).get("platforms")
            if plats:
                plat = int(plats[0])
        except Exception:
            plat = 0
        body = self._sealed(src, {"location": 2, "platform": plat})
        try:
            res = self.api.post(f"/quests/{qid}/claim-reward", body)
        except DiscordApiError as e:
            if e.code in (400, 403):
                try:
                    res = self.api.post(f"/quests/{qid}/claim-reward",
                                        self._sealed(src, {"location": 2}))
                except DiscordApiError as e2:
                    self.log(f"Автоквесты: награда «{it['name']}» не забралась "
                             f"({e2.code} {e2.describe()})", "warn")
                    self.set_status(qid, "награда не забрана")
                    return
            else:
                self.log(f"Автоквесты: награда «{it['name']}» не забралась "
                         f"({e.code} {e.describe()})", "warn")
                self.set_status(qid, "награда не забрана")
                return
        if (res or {}).get("errors"):
            self.log(f"Автоквесты: «{it['name']}» — Discord вернул ошибку награды, "
                     f"скорее всего капча. Заберите вручную в Gift Inventory.", "warn")
            self.set_status(qid, "награда вручную")
        else:
            self.log(f"Автоквесты: награда за «{it['name']}» получена.", "ok")
            self.set_status(qid, "награда получена")


def open_folder(path):
    path = str(path)
    try:
        if IS_WIN:
            os.startfile(path)
        elif IS_MAC:
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


# =========================================================================== #
#  ИНТЕРФЕЙС
# =========================================================================== #
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} {APP_VER}")
        self.geometry("1160x800")
        self.minsize(980, 660)
        self.configure(bg=C["bg"])

        self.games = []
        self.games_loading = False
        self.queue_items = []
        self.quest_rows = {}
        self.session = None
        self.session_active = False
        self.auto = None
        self.logq = queue.Queue()
        self.cfg = self.load_cfg()

        self._style()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.after(80, self._drain_log)
        self.after(300, self._tick)
        threading.Thread(target=self._load_db, kwargs={"silent": True}, daemon=True).start()
        self._check_discord()

    # ------------------------------------------------------------------ #
    def load_cfg(self):
        try:
            return json.loads(CONFIG_FILE.read_text("utf-8"))
        except Exception:
            return {}

    def save_cfg(self):
        cfg = {
            "minutes": self.var_min.get(),
            "tail": self.var_tail.get(),
            "root": self.var_root.get(),
            "rpc": self.var_rpc.get(),
            "clean": self.var_clean.get(),
            "windowed": self.var_win.get(),
            "token": self.var_token.get(),
            "auto_mode": self.var_auto_mode.get(),
            "auto_enroll": self.var_auto_enroll.get(),
            "auto_claim": self.var_auto_claim.get(),
            "auto_fast": self.var_auto_fast.get(),
            "geometry": self.geometry(),
        }
        try:
            CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    def _style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure(".", background=C["bg"], foreground=C["fg"],
                        fieldbackground=C["bg3"], bordercolor=C["line"],
                        font=("Segoe UI", 10) if IS_WIN else ("Helvetica", 10))
        style.configure("TFrame", background=C["bg"])
        style.configure("Card.TFrame", background=C["bg2"])
        style.configure("TLabel", background=C["bg"], foreground=C["fg"])
        style.configure("Card.TLabel", background=C["bg2"], foreground=C["fg"])
        style.configure("Dim.TLabel", background=C["bg"], foreground=C["fg2"],
                        font=("Segoe UI", 9) if IS_WIN else ("Helvetica", 9))
        style.configure("DimCard.TLabel", background=C["bg2"], foreground=C["fg2"],
                        font=("Segoe UI", 9) if IS_WIN else ("Helvetica", 9))
        style.configure("H1.TLabel", background=C["bg"], foreground=C["fg"],
                        font=("Segoe UI Semibold", 15) if IS_WIN else ("Helvetica", 15, "bold"))
        style.configure("H2.TLabel", background=C["bg2"], foreground=C["fg"],
                        font=("Segoe UI Semibold", 11) if IS_WIN else ("Helvetica", 11, "bold"))

        style.configure("TNotebook", background=C["bg"], borderwidth=0, tabmargins=(0, 0, 0, 0))
        style.configure("TNotebook.Tab", background=C["bg"], foreground=C["fg2"],
                        padding=(16, 9), borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", C["bg2"])],
                  foreground=[("selected", C["fg"])])

        style.configure("Treeview", background=C["bg2"], fieldbackground=C["bg2"],
                        foreground=C["fg"], rowheight=26, borderwidth=0)
        style.map("Treeview", background=[("selected", C["accent"])],
                  foreground=[("selected", "#ffffff")])
        style.configure("Treeview.Heading", background=C["bg3"], foreground=C["fg2"],
                        relief="flat", borderwidth=0, padding=(6, 6))
        style.map("Treeview.Heading", background=[("active", C["bg3"])])

        style.configure("Accent.TButton", background=C["accent"], foreground="#ffffff",
                        borderwidth=0, focusthickness=0, padding=(14, 9))
        style.map("Accent.TButton",
                  background=[("active", C["accent2"]), ("disabled", C["line"])],
                  foreground=[("disabled", C["fg2"])])

        style.configure("Ghost.TButton", background=C["bg3"], foreground=C["fg"],
                        borderwidth=0, padding=(12, 8))
        style.map("Ghost.TButton", background=[("active", C["line"])])

        style.configure("Danger.TButton", background=C["red"], foreground="#ffffff",
                        borderwidth=0, padding=(12, 8))
        style.map("Danger.TButton", background=[("active", "#b32d31")])

        style.configure("TEntry", fieldbackground=C["bg3"], foreground=C["fg"],
                        bordercolor=C["line"], insertcolor=C["fg"], padding=6)
        style.configure("TCheckbutton", background=C["bg2"], foreground=C["fg"])
        style.map("TCheckbutton", background=[("active", C["bg2"])])
        style.configure("Tab.TCheckbutton", background=C["bg"], foreground=C["fg"])
        style.map("Tab.TCheckbutton", background=[("active", C["bg"])])

        style.configure("Bar.Horizontal.TProgressbar", background=C["accent"],
                        troughcolor=C["bg3"], borderwidth=0, thickness=8)

    # ------------------------------------------------------------------ #
    def _build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        head = ttk.Frame(self, padding=(16, 12, 16, 8))
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(1, weight=1)

        ttk.Label(head, text=APP_NAME, style="H1.TLabel").grid(row=0, column=0, sticky="w")
        self.lbl_discord = ttk.Label(head, text="Discord: проверяю...", style="Dim.TLabel")
        self.lbl_discord.grid(row=0, column=2, sticky="e")

        main = ttk.Frame(self, padding=(16, 0, 16, 0))
        main.grid(row=1, column=0, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(0, weight=1)

        self.nb = ttk.Notebook(main)
        self.nb.grid(row=0, column=0, sticky="nsew")

        self._tab_search()
        self._tab_quests()
        self._tab_auto()
        self._tab_queue()
        self._tab_log()

        self._sidebar(main)

        bottom = ttk.Frame(self, padding=(16, 10, 16, 14))
        bottom.grid(row=2, column=0, sticky="ew")
        bottom.columnconfigure(0, weight=1)

        self.bar = ttk.Progressbar(bottom, style="Bar.Horizontal.TProgressbar",
                                   mode="determinate", maximum=100)
        self.bar.grid(row=0, column=0, sticky="ew")

        row = ttk.Frame(bottom)
        row.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        row.columnconfigure(0, weight=1)

        self.lbl_status = ttk.Label(row, text="Готово к запуску.", foreground=C["fg2"])
        self.lbl_status.grid(row=0, column=0, sticky="w")

        self.lbl_timer = ttk.Label(row, text="00:00", foreground=C["accent"],
                                   font=("Consolas", 13, "bold"))
        self.lbl_timer.grid(row=0, column=1, padx=14)

        self.btn_start = ttk.Button(row, text="СТАРТ", style="Accent.TButton",
                                    command=self.on_start)
        self.btn_start.grid(row=0, column=2)
        self.btn_stop = ttk.Button(row, text="СТОП", style="Danger.TButton",
                                   command=self.on_stop, state="disabled")
        self.btn_stop.grid(row=0, column=3, padx=(8, 0))

        self.bind("<Control-Return>", lambda e: self.on_start())

    # ------------------------------------------------------------------ #
    def _tab_search(self):
        f = ttk.Frame(self.nb, padding=12)
        self.nb.add(f, text="  Поиск игр  ")
        f.columnconfigure(0, weight=1)
        f.rowconfigure(2, weight=1)

        bar = ttk.Frame(f)
        bar.grid(row=0, column=0, sticky="ew")
        bar.columnconfigure(0, weight=1)

        self.var_search = tk.StringVar()
        e = ttk.Entry(bar, textvariable=self.var_search)
        e.grid(row=0, column=0, sticky="ew")
        e.bind("<Return>", lambda ev: self.on_search())
        ttk.Button(bar, text="Найти", style="Accent.TButton",
                   command=self.on_search).grid(row=0, column=1, padx=(8, 0))

        ttk.Label(f, text="Enter — искать, двойной клик — добавить в очередь. "
                          "Названия только латиницей, как в базе Discord.",
                  style="Dim.TLabel").grid(row=1, column=0, sticky="w", pady=(6, 8))

        wrap = ttk.Frame(f)
        wrap.grid(row=2, column=0, sticky="nsew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)

        self.tv_search = ttk.Treeview(wrap, columns=("exe",), show="tree headings",
                                      selectmode="browse")
        self.tv_search.heading("#0", text="Игра")
        self.tv_search.heading("exe", text="Исполняемый файл")
        self.tv_search.column("#0", width=420)
        self.tv_search.column("exe", width=220)
        self.tv_search.grid(row=0, column=0, sticky="nsew")

        sb = ttk.Scrollbar(wrap, command=self.tv_search.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.tv_search.configure(yscrollcommand=sb.set)
        self.tv_search.bind("<Double-1>", lambda e: self.add_from_search())

        btns = ttk.Frame(f)
        btns.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(btns, text="Добавить в очередь", style="Ghost.TButton",
                   command=self.add_from_search).pack(side="left")
        ttk.Button(btns, text="Обновить базу игр", style="Ghost.TButton",
                   command=lambda: threading.Thread(target=self._load_db,
                                                    kwargs={"force": True},
                                                    daemon=True).start()
                   ).pack(side="left", padx=8)

    # ------------------------------------------------------------------ #
    def _tab_quests(self):
        f = ttk.Frame(self.nb, padding=12)
        self.nb.add(f, text="  Активные квесты  ")
        f.columnconfigure(0, weight=1)
        f.rowconfigure(3, weight=1)

        ttk.Label(f, text="Нужен пользовательский токен Discord "
                          "(Ctrl+Shift+I → Network → запрос quests/@me → заголовок Authorization).",
                  style="Dim.TLabel", wraplength=700).grid(row=0, column=0, sticky="w")

        bar = ttk.Frame(f)
        bar.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        bar.columnconfigure(0, weight=1)

        self.var_token = tk.StringVar(value=self.cfg.get("token", ""))
        self.ent_token = ttk.Entry(bar, textvariable=self.var_token, show="•")
        self.ent_token.grid(row=0, column=0, sticky="ew")
        ttk.Button(bar, text="Показать", style="Ghost.TButton",
                   command=self.toggle_token).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(bar, text="Загрузить", style="Accent.TButton",
                   command=self.on_load_quests).grid(row=0, column=2, padx=(8, 0))
        ttk.Button(bar, text="Дамп", style="Ghost.TButton",
                   command=self.dump_quests).grid(row=0, column=3, padx=(8, 0))

        ttk.Label(f, text="Двойной клик по квесту — добавить игру в очередь.",
                  style="Dim.TLabel").grid(row=2, column=0, sticky="w", pady=(6, 8))

        wrap = ttk.Frame(f)
        wrap.grid(row=3, column=0, sticky="nsew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)

        self.tv_quests = ttk.Treeview(wrap, columns=("tasks", "time", "state"),
                                      show="tree headings", selectmode="browse")
        self.tv_quests.heading("#0", text="Квест")
        self.tv_quests.heading("tasks", text="Тип задачи")
        self.tv_quests.heading("time", text="Нужно времени")
        self.tv_quests.heading("state", text="Статус")
        self.tv_quests.column("#0", width=320)
        self.tv_quests.column("tasks", width=220)
        self.tv_quests.column("time", width=100)
        self.tv_quests.column("state", width=130)
        self.tv_quests.grid(row=0, column=0, sticky="nsew")

        sb = ttk.Scrollbar(wrap, command=self.tv_quests.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.tv_quests.configure(yscrollcommand=sb.set)
        self.tv_quests.bind("<Double-1>", lambda e: self.add_from_quests())

    # ------------------------------------------------------------------ #
    def _tab_auto(self):
        f = ttk.Frame(self.nb, padding=12)
        self.nb.add(f, text="  Автоквесты  ")
        f.columnconfigure(0, weight=1)
        f.rowconfigure(4, weight=1)

        ttk.Label(f, style="Dim.TLabel", wraplength=700, justify="left", text=(
            "Токен берётся с вкладки «Активные квесты». Программа сама принимает квесты, "
            "крутит видео, поднимает пустышки для игровых квестов, отбивает heartbeat для "
            "активностей и забирает награды. Чего не умеет (достижения, консоль) — пропускает.\n"
            "Discord с апреля 2026 придирается к автоматизации: возможна временная "
            "блокировка квестов на аккаунте."),
        ).grid(row=0, column=0, sticky="w")

        self.var_auto_mode = tk.StringVar(value=self.cfg.get("auto_mode", "api — тихо, без окон (только API)"))
        self.var_auto_enroll = tk.BooleanVar(value=bool(self.cfg.get("auto_enroll", True)))
        self.var_auto_claim = tk.BooleanVar(value=bool(self.cfg.get("auto_claim", True)))
        self.var_auto_fast = tk.BooleanVar(value=bool(self.cfg.get("auto_fast", False)))

        opts = ttk.Frame(f)
        opts.grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Label(opts, text="Способ зачёта игровых квестов:", style="Dim.TLabel"
                  ).pack(side="left")
        ttk.Combobox(opts, textvariable=self.var_auto_mode, width=36, state="readonly",
                     values=("api — тихо, без окон (только API)",
                             "hybrid — тихо + окна-пустышки",
                             "process — только окна, как раньше")).pack(side="left", padx=6)

        opts2 = ttk.Frame(f)
        opts2.grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Checkbutton(opts2, text="Принимать квесты",
                        variable=self.var_auto_enroll,
                        style="Tab.TCheckbutton").pack(side="left")
        ttk.Checkbutton(opts2, text="Забирать награды с задержкой",
                        variable=self.var_auto_claim,
                        style="Tab.TCheckbutton").pack(side="left", padx=12)
        ttk.Checkbutton(opts2, text="Ускорять видео (4x, быстрее но заметнее)",
                        variable=self.var_auto_fast,
                        style="Tab.TCheckbutton").pack(side="left")

        bar = ttk.Frame(f)
        bar.grid(row=3, column=0, sticky="ew", pady=(10, 8))
        self.btn_auto = ttk.Button(bar, text="ВЫПОЛНИТЬ ВСЁ", style="Accent.TButton",
                                   command=self.on_auto)
        self.btn_auto.pack(side="left")
        self.btn_auto_stop = ttk.Button(bar, text="СТОП", style="Danger.TButton",
                                        command=self.on_auto_stop, state="disabled")
        self.btn_auto_stop.pack(side="left", padx=8)
        self.lbl_auto = ttk.Label(bar, text="Простой: ничего не делаю.", foreground=C["fg2"])
        self.lbl_auto.pack(side="left", padx=10)

        wrap = ttk.Frame(f)
        wrap.grid(row=4, column=0, sticky="nsew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)

        self.tv_auto = ttk.Treeview(wrap, columns=("task", "target", "status"),
                                    show="tree headings", selectmode="browse")
        self.tv_auto.heading("#0", text="Квест")
        self.tv_auto.heading("task", text="Тип задачи")
        self.tv_auto.heading("target", text="Цель")
        self.tv_auto.heading("status", text="Статус")
        self.tv_auto.column("#0", width=320)
        self.tv_auto.column("task", width=180)
        self.tv_auto.column("target", width=90)
        self.tv_auto.column("status", width=220)
        self.tv_auto.grid(row=0, column=0, sticky="nsew")

        sb = ttk.Scrollbar(wrap, command=self.tv_auto.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.tv_auto.configure(yscrollcommand=sb.set)

    # ------------------------------------------------------------------ #
    def _tab_queue(self):
        f = ttk.Frame(self.nb, padding=12)
        self.nb.add(f, text="  Очередь  ")
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)

        ttk.Label(f, text="Игры, которые будут запущены одновременно. "
                          "1–2 за раз выглядит естественнее, чем пять.",
                  style="Dim.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))

        wrap = ttk.Frame(f)
        wrap.grid(row=1, column=0, sticky="nsew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)

        self.tv_queue = ttk.Treeview(wrap, columns=("exe",), show="tree headings",
                                     selectmode="extended")
        self.tv_queue.heading("#0", text="Игра")
        self.tv_queue.heading("exe", text="Исполняемый файл")
        self.tv_queue.column("#0", width=420)
        self.tv_queue.column("exe", width=260)
        self.tv_queue.grid(row=0, column=0, sticky="nsew")

        sb = ttk.Scrollbar(wrap, command=self.tv_queue.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.tv_queue.configure(yscrollcommand=sb.set)

        btns = ttk.Frame(f)
        btns.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(btns, text="Убрать выбранное", style="Ghost.TButton",
                   command=self.remove_selected).pack(side="left")
        ttk.Button(btns, text="Очистить", style="Ghost.TButton",
                   command=self.clear_queue).pack(side="left", padx=8)

    # ------------------------------------------------------------------ #
    def _tab_log(self):
        f = ttk.Frame(self.nb, padding=12)
        self.nb.add(f, text="  Журнал  ")
        f.columnconfigure(0, weight=1)
        f.rowconfigure(0, weight=1)

        self.txt_log = tk.Text(f, bg=C["bg2"], fg=C["fg"], insertbackground=C["fg"],
                               relief="flat", wrap="word", padx=10, pady=8,
                               font=("Consolas", 9), state="disabled")
        self.txt_log.grid(row=0, column=0, sticky="nsew")

        sb = ttk.Scrollbar(f, command=self.txt_log.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.txt_log.configure(yscrollcommand=sb.set)

        self.txt_log.tag_config("ok", foreground=C["green"])
        self.txt_log.tag_config("warn", foreground=C["yellow"])
        self.txt_log.tag_config("err", foreground=C["red"])
        self.txt_log.tag_config("dim", foreground=C["fg2"])

    # ------------------------------------------------------------------ #
    def _sidebar(self, parent):
        side = ttk.Frame(parent, style="Card.TFrame", padding=14, width=290)
        side.grid(row=0, column=1, sticky="ns", padx=(12, 0))
        side.grid_propagate(False)

        ttk.Label(side, text="Настройки", style="H2.TLabel").pack(anchor="w")

        def row(label, widget):
            fr = ttk.Frame(side, style="Card.TFrame")
            fr.pack(fill="x", pady=(10, 0))
            ttk.Label(fr, text=label, style="Card.TLabel").pack(anchor="w")
            widget(fr)

        def spin(var, lo, hi, step):
            def make(fr):
                tk.Spinbox(fr, from_=lo, to=hi, increment=step, textvariable=var,
                           bg=C["bg3"], fg=C["fg"], buttonbackground=C["line"],
                           relief="flat", highlightthickness=0, justify="center"
                           ).pack(fill="x", pady=(4, 0))
            return make

        self.var_min = tk.IntVar(value=int(self.cfg.get("minutes", 15) or 15))
        self.var_tail = tk.IntVar(value=int(self.cfg.get("tail", 60) or 60))

        row("Длительность, минут", spin(self.var_min, 1, 600, 1))
        row("Случайный довесок, сек", spin(self.var_tail, 0, 600, 10))

        def root_row(fr):
            fr2 = ttk.Frame(fr, style="Card.TFrame")
            fr2.pack(fill="x", pady=(4, 0))
            fr2.columnconfigure(0, weight=1)
            ttk.Entry(fr2, textvariable=self.var_root).grid(row=0, column=0, sticky="ew")
            ttk.Button(fr2, text="…", width=3, style="Ghost.TButton",
                       command=self.pick_root).grid(row=0, column=1, padx=(6, 0))

        self.var_root = tk.StringVar(value=self.cfg.get("root") or str(default_root()))
        row("Папка для пустышек", root_row)

        self.var_rpc = tk.BooleanVar(value=bool(self.cfg.get("rpc", False)))
        self.var_clean = tk.BooleanVar(value=bool(self.cfg.get("clean", False)))
        self.var_win = tk.BooleanVar(value=bool(self.cfg.get("windowed", True)))

        ttk.Checkbutton(side, text="Пустышка с окном (как в оригинале)",
                        variable=self.var_win).pack(anchor="w", pady=(12, 0))
        ttk.Checkbutton(side, text="Rich Presence (косметика)",
                        variable=self.var_rpc).pack(anchor="w", pady=(4, 0))
        ttk.Checkbutton(side, text="Удалять пустышки после выхода",
                        variable=self.var_clean).pack(anchor="w", pady=(4, 0))

        ttk.Separator(side).pack(fill="x", pady=14)

        ttk.Button(side, text="Открыть папку игр", style="Ghost.TButton",
                   command=lambda: open_folder(self.var_root.get() or default_root())
                   ).pack(fill="x")
        ttk.Button(side, text="Сбросить кэш игр", style="Ghost.TButton",
                   command=self.clear_cache).pack(fill="x", pady=(6, 0))

        ttk.Separator(side).pack(fill="x", pady=14)
        ttk.Label(side, text="Как пользоваться", style="H2.TLabel").pack(anchor="w")
        ttk.Label(side, style="DimCard.TLabel", justify="left", wraplength=250, text=(
            "1. Вставь токен, жми «Дамп» — убедись, что квесты видны.\n"
            "2. На вкладке «Автоквесты» жми «ВЫПОЛНИТЬ ВСЁ».\n"
            "3. Для игровых квестов включи «Отображать текущую активность».\n"
            "4. Не закрывай приложение, пока идёт отсчёт.\n\n"
            "Режим «api» ничего не запускает, всё делает запросами."
        )).pack(anchor="w", pady=(6, 0))

    # ------------------------------------------------------------------ #
    #  Действия
    # ------------------------------------------------------------------ #
    def pick_root(self):
        d = filedialog.askdirectory(initialdir=self.var_root.get() or str(Path.home()))
        if d:
            self.var_root.set(d)

    def toggle_token(self):
        self._token_shown = not getattr(self, "_token_shown", False)
        for e in (getattr(self, "ent_token", None), getattr(self, "ent_token_auto", None)):
            if e is not None:
                try:
                    e.configure(show="" if self._token_shown else "•")
                except Exception:
                    pass

    def log(self, msg, tag="dim"):
        self.logq.put((msg, tag))

    def _drain_log(self):
        try:
            while True:
                msg, tag = self.logq.get_nowait()
                ts = time.strftime("%H:%M:%S")
                self.txt_log.configure(state="normal")
                self.txt_log.insert("end", f"[{ts}] {msg}\n", tag)
                self.txt_log.see("end")
                self.txt_log.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(80, self._drain_log)

    # ------------------------------------------------------------------ #
    def _load_db(self, force=False, silent=False):
        if self.games_loading:
            return
        self.games_loading = True
        if not silent:
            self.log("Загружаю список игр Discord...", "dim")
        try:
            self.games = fetch_detectable(force)
            self.log(f"База игр загружена: {len(self.games)} записей (ОС: {os_tag()}).", "ok")
            if self.var_search.get().strip():
                self.after(0, self.on_search)
        except Exception as e:
            self.log(f"Ошибка загрузки базы: {e}", "err")
        finally:
            self.games_loading = False

    def on_search(self):
        q = self.var_search.get().strip()
        if not q:
            return
        if not self.games:
            self.log("База ещё грузится, подожди пару секунд.", "warn")
            return
        self.tv_search.delete(*self.tv_search.get_children())
        hits = search_games(self.games, q, os_tag())
        for g in hits:
            exe = pick_exe(g, os_tag())
            self.tv_search.insert("", "end", iid=str(g.get("id")), text=g.get("name"),
                                  values=(exe,))
        self.log(f"Поиск «{q}»: найдено {len(hits)}.", "dim" if hits else "warn")

    def add_from_search(self):
        sel = self.tv_search.selection()
        if not sel:
            return
        iid = sel[0]
        g = next((x for x in self.games if str(x.get("id")) == iid), None)
        if not g:
            return
        self.enqueue(g.get("name"), pick_exe(g, os_tag()), g.get("id"))

    def on_load_quests(self):
        token = norm_token(self.var_token.get())
        if not token:
            messagebox.showinfo(APP_NAME, "Вставьте пользовательский токен Discord.")
            return
        self.save_cfg()
        threading.Thread(target=self._load_quests, args=(token,), daemon=True).start()

    def _load_quests(self, token):
        self.log("Запрашиваю активные квесты...", "dim")
        try:
            quests, data = fetch_quests(token)
        except DiscordApiError as e:
            if e.code == 401:
                self.log("/quests/@me: 401. Токен точно пользовательский и свежий?", "err")
            else:
                self.log(f"/quests/@me вернул {e.code}: {e.describe()}", "err")
            return
        except Exception as e:
            self.log(f"Не смог получить квесты: {e}", "err")
            return
        self.after(0, lambda: self._fill_quests(quests, data))

    def _fill_quests(self, quests, data=None):
        self.tv_quests.delete(*self.tv_quests.get_children())
        self.quest_rows = {}
        data = data or {}
        self.log(f"Ответ /quests/@me: quests {len(quests)}, "
                 f"excluded {len(data.get('excluded_quests') or [])}, "
                 f"blocked {data.get('quest_enrollment_blocked_until') or '—'}, "
                 f"suspended {data.get('quest_access_suspended_until') or '—'}", "dim")
        if not quests:
            self.log("Активных квестов не видно. Жми «Дамп» и смотри файл.", "warn")
            return
        for q in quests:
            tgt = f"{q['target'] // 60} мин" if q.get("target") else "—"
            state = ("награда забрана" if q["claimed"] else
                     "пройден" if q["completed"] else
                     "принят" if q["enrolled"] else "не принят")
            iid = q["quest_id"] or q["app_id"] or q["name"]
            self.tv_quests.insert("", "end", iid=iid, text=q["name"],
                                  values=(q["tasks"], tgt, state))
            self.quest_rows[iid] = q
        self.log(f"Активных квестов: {len(quests)}.", "ok")

    def dump_quests(self):
        token = norm_token(self.var_token.get())
        if not token:
            messagebox.showinfo(APP_NAME, "Сначала вставьте токен.")
            return

        def work():
            try:
                raw = DiscordApi(token).get("/quests/@me")
                out = Path(tempfile.gettempdir()) / "quests_dump.json"
                out.write_text(json.dumps(raw, ensure_ascii=False, indent=2), "utf-8")
                n = len(raw.get("quests") or [])
                self.log(f"Дамп квестов: {out} (квестов {n}, ключи ответа: "
                         f"{', '.join(sorted(raw.keys()))})", "ok")
                if not n:
                    self.log("Дамп: список пуст — смотрите в файле excluded_quests "
                             "и quest_enrollment_blocked_until.", "warn")
            except Exception as e:
                self.log(f"Дамп не получился: {e}", "err")

        threading.Thread(target=work, daemon=True).start()

    def add_from_quests(self):
        sel = self.tv_quests.selection()
        if not sel:
            return
        row = self.quest_rows.get(sel[0])
        if not row:
            return
        if not self.games:
            self.log("База игр ещё грузится.", "warn")
            return
        g = find_game_by_id(self.games, row["app_id"])
        if not g:
            self.log(f"«{row['name']}» нет в базе детекта — возможно, это квест типа "
                     f"{row['tasks']}.", "warn")
            return
        self.enqueue(g.get("name"), pick_exe(g, os_tag()), row["app_id"])

    def enqueue(self, name, exe, app_id):
        if not exe:
            self.log(f"«{name}»: нет exe для текущей ОС.", "warn")
            return
        if any(x["app_id"] == app_id for x in self.queue_items):
            self.log(f"«{name}» уже в очереди.", "warn")
            return
        self.queue_items.append({"name": name, "exe": exe, "app_id": str(app_id or "")})
        self.refresh_queue()
        self.log(f"В очередь: {name}  ({exe})", "ok")
        self.nb.select(3)

    def refresh_queue(self):
        self.tv_queue.delete(*self.tv_queue.get_children())
        for i, it in enumerate(self.queue_items):
            self.tv_queue.insert("", "end", iid=str(i), text=it["name"], values=(it["exe"],))
        self.nb.tab(3, text=f"  Очередь ({len(self.queue_items)})  ")

    def remove_selected(self):
        for iid in sorted((int(x) for x in self.tv_queue.selection()), reverse=True):
            self.queue_items.pop(iid)
        self.refresh_queue()

    def clear_queue(self):
        self.queue_items.clear()
        self.refresh_queue()

    def clear_cache(self):
        try:
            CACHE_FILE.unlink(missing_ok=True)
            BUILD_CACHE.unlink(missing_ok=True)
            self.log("Кэш игр сброшен.", "ok")
        except Exception as e:
            self.log(f"Не смог сбросить кэш: {e}", "err")

    # ------------------------------------------------------------------ #
    def on_start(self):
        if self.session and self.session.running:
            return
        if not self.queue_items:
            messagebox.showinfo(APP_NAME, "Очередь пуста. Найдите игру и добавьте её.")
            self.nb.select(0)
            return
        if not discord_running():
            self.log("Discord не запущен — детект работать не будет.", "err")
            if not messagebox.askyesno(APP_NAME, "Discord, похоже, не запущен.\n"
                                                 "Всё равно запустить?"):
                return

        total = self.var_min.get() * 60 + (random.uniform(0, self.var_tail.get())
                                           if self.var_tail.get() else 0)
        self.save_cfg()
        self.log("=" * 54, "dim")
        self.log(f"Старт сессии: {len(self.queue_items)} игр(ы), "
                 f"{int(total // 60)} мин {int(total % 60)} с.", "ok")

        self.session = Session(list(self.queue_items), total,
                               Path(self.var_root.get() or default_root()),
                               self.var_rpc.get(), self.var_clean.get(), self.log,
                               self.var_win.get())
        self.session.start()

        if not self.session.running:
            self.log("Ничего не удалось запустить.", "err")
            self.session = None
            self.session_active = False
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")
            return

        self.session_active = True
        self.bar["value"] = 0
        self.lbl_timer.configure(text=f"{int(total // 60):02d}:{int(total % 60):02d}")
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.lbl_status.configure(text=f"Идёт сессия: {len(self.session.runners)} процесс(ов).")
        self.log("Не закрывайте приложение, пока идёт отсчёт.", "warn")

    def on_stop(self):
        if self.session and self.session.running:
            self.session.cancel()

    # ------------------------------------------------------------------ #
    def on_auto(self):
        if self.auto and self.auto.is_alive():
            return
        token = norm_token(self.var_token.get())
        if not token:
            messagebox.showinfo(APP_NAME, "Вставьте пользовательский токен Discord "
                                          "на вкладке «Активные квесты».")
            self.nb.select(1)
            return
        if not discord_running():
            if not messagebox.askyesno(APP_NAME, "Discord не запущен — игровые квесты "
                                                 "зачтутся только через heartbeat.\n"
                                                 "Всё равно запустить?"):
                return
        self.save_cfg()
        mode = (self.var_auto_mode.get() or "api").split(" ")[0].strip() or "api"
        self.tv_auto.delete(*self.tv_auto.get_children())
        self.auto = AutoRunner(
            DiscordApi(token), self.games, self.log,
            Path(self.var_root.get() or default_root()),
            {"mode": mode,
             "fast_video": self.var_auto_fast.get(),
             "enroll": self.var_auto_enroll.get(),
             "claim": self.var_auto_claim.get(),
             "windowed": self.var_win.get()},
            on_finish=lambda: self.after(0, self._auto_finish_ui))
        self.btn_auto.configure(state="disabled")
        self.btn_auto_stop.configure(state="normal")
        self.lbl_auto.configure(text="Работаю...")
        self.auto.start()
        self.after(500, self._auto_tick)

    def on_auto_stop(self):
        if self.auto and self.auto.is_alive():
            self.auto.stop()

    def _auto_refresh(self):
        a = self.auto
        if not a:
            return
        with a.lock:
            rows = dict(a.rows)
        seen = set()
        for qid, row in rows.items():
            seen.add(qid)
            vals = (row.get("task", ""), f"{row.get('target', 0)} c", row.get("status", ""))
            if self.tv_auto.exists(qid):
                self.tv_auto.item(qid, text=row.get("name", ""), values=vals)
            else:
                self.tv_auto.insert("", "end", iid=qid, text=row.get("name", ""), values=vals)
        for iid in self.tv_auto.get_children():
            if iid not in seen:
                self.tv_auto.delete(iid)

    def _auto_tick(self):
        if self.auto and self.auto.is_alive():
            self._auto_refresh()
            self.after(800, self._auto_tick)
        else:
            self._auto_refresh()

    def _auto_finish_ui(self):
        self.btn_auto.configure(state="normal")
        self.btn_auto_stop.configure(state="disabled")
        self.lbl_auto.configure(text="Закончил.")
        self._auto_refresh()

    # ------------------------------------------------------------------ #
    def _tick(self):
        s = self.session
        if s and s.running:
            rem = s.remaining()
            total = max(1, int(s.total))
            self.bar["value"] = (total - rem) / total * 100
            self.lbl_timer.configure(text=f"{rem // 60:02d}:{rem % 60:02d}")
        elif self.session_active:
            self.session_active = False
            self.session = None
            self.bar["value"] = 0
            self.lbl_timer.configure(text="00:00")
            self.lbl_status.configure(text="Сессия завершена.")
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")
        if self.auto and self.auto.is_alive():
            with self.auto.lock:
                rows = dict(self.auto.rows)
            done = sum(1 for r in rows.values()
                       if r.get("status") in ("пройден", "награда получена"))
            self.lbl_status.configure(text=f"Автоквесты: {done}/{len(rows)} готово.")
        self.after(400, self._tick)

    def _check_discord(self):
        ok = discord_running()
        self.lbl_discord.configure(
            text="Discord: запущен" if ok else "Discord: не найден",
            foreground=C["green"] if ok else C["red"])
        self.after(8000, self._check_discord)

    # ------------------------------------------------------------------ #
    def on_close(self):
        if self.session and self.session.running:
            if not messagebox.askyesno(APP_NAME, "Сессия ещё идёт. Остановить и выйти?"):
                return
            self.session.cancel()
        if self.auto and self.auto.is_alive():
            self.auto.stop()
        self.save_cfg()
        self.destroy()


def main():
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    app = App()
    app.log(f"{APP_NAME} {APP_VER} — запущено.", "dim")
    app.log(f"ОС: {platform.system()} / Python {platform.python_version()}", "dim")
    if not discord_running():
        app.log("Discord не найден. Откройте клиент перед стартом сессии.", "warn")
    app.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        try:
            input("\nEnter — закрыть...")
        except EOFError:
            pass