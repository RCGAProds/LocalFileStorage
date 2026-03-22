"""
Launch.py — LocalFileHub Launcher
===================================
Double-click to start LocalFileHub.

Architecture:
  • This launcher starts server.py as a child process.
  • server.py detects LOCALFILEHUB_LAUNCHER=1 and emits structured
    [STATUS] key=value lines instead of printing a terminal banner.
  • The launcher parses those lines to update a live dashboard.
  • HTTP requests, uploads, errors, and extension events are logged cleanly.
  • The terminal banner (QR ASCII art, ANSI boxes) is suppressed entirely
    when running through the launcher — the launcher has its own UI for that.

Requirements:
  • Python 3.8+
  • server.py in the same folder as this file.
"""

import os
import sys

# ── Hide the console window on Windows ───────────────────────────────────────
if sys.platform == "win32" and os.environ.get("LAUNCHER_NO_RELAUNCH") != "1":
    import subprocess as _sp
    _pythonw   = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    _this_file = os.path.abspath(__file__)
    _this_dir  = os.path.dirname(_this_file)
    if os.path.isfile(_pythonw):
        _env = os.environ.copy()
        _env["LAUNCHER_NO_RELAUNCH"] = "1"
        _env["LAUNCHER_ROOT"]        = _this_dir
        _sp.Popen(
            [_pythonw, _this_file],
            cwd=_this_dir,
            env=_env,
            creationflags=_sp.CREATE_NO_WINDOW,
        )
        sys.exit(0)

import subprocess
import threading
import socket
import importlib
import time
import tkinter as tk
from tkinter import messagebox

# ── Project root ──────────────────────────────────────────────────────────────
ROOT = os.environ.get("LAUNCHER_ROOT") or os.path.dirname(os.path.abspath(__file__))

# ── Config ────────────────────────────────────────────────────────────────────
DEPENDENCIES = [
    ("flask",         "flask",         True),
    ("werkzeug",      "werkzeug",      True),
    ("Pillow",        "PIL",           False),
    ("imagehash",     "imagehash",     False),
    ("flask-limiter", "flask_limiter", False),
    ("qrcode",        "qrcode",        False),
]
PORT = 5000

# ── Palette: Terminal-Luxe ────────────────────────────────────────────────────
# Deep blacks, phosphor amber, surgical whites.
BG          = "#080808"
BG2         = "#0f0f0f"
BG3         = "#161616"
BG4         = "#1d1d1d"
BG5         = "#242424"

AMBER       = "#e8a020"
AMBER_DIM   = "#7a4a08"
AMBER_GLOW  = "#f5c060"
AMBER_FAINT = "#2a1a04"

GREEN       = "#3dba6e"
GREEN_DIM   = "#183d28"
GREEN_GLOW  = "#5edd8e"

RED         = "#c94040"
RED_DIM     = "#3d1010"
RED_GLOW    = "#e05555"

BLUE        = "#4080c0"
BLUE_DIM    = "#102040"
BLUE_GLOW   = "#60a8e8"

CYAN        = "#30a8a0"
CYAN_DIM    = "#0a3030"

WHITE       = "#f0ece0"
GREY        = "#888070"
GREY_DIM    = "#3a3630"
GREY_FAINT  = "#1e1c18"

BORDER      = "#2a2820"
BORDER2     = "#3a3628"

# ── Lines to suppress from the server log ────────────────────────────────────
# Covers: [STATUS] protocol lines, all Flask/Werkzeug startup noise,
# and every element of the server.py CLI banner (box-drawing chars, ANSI
# escape sequences, QR ASCII art, URL rows, feature rows, etc.)
_SUPPRESS_FRAGMENTS = [
    # Internal protocol — never shown raw
    "[STATUS]",

    # Flask / Werkzeug startup noise
    "Serving Flask app",
    "Debug mode:",
    "WARNING: This is a development server",
    "Use a production WSGI",
    "Restarting with",
    "Debugger is active",
    "Debugger PIN",
    "Press CTRL+C to quit",
    " * Running on",
    "werkzeug",
    "Environment:",

    # server.py CLI banner — box-drawing characters
    "\u250c", "\u2510", "\u2514", "\u2518", "\u251c", "\u2524", "\u2500", "\u2502",

    # server.py CLI banner — content rows
    "LocalFileHub",
    "Media server",
    "Network ",
    "Local   ",
    "Python ",
    "uploads ",
    "disk free",
    " files  ",
    " folders",
    " tags  ",
    " rules",
    "Image thumbnails",
    "Video thumbnails",
    "Perceptual hash",
    "Rate limiter",
    "Extension hooks",
    "Automation rules",
    "Press  Ctrl+C",
    "pip install qrcode",

    # ANSI escape sequences
    "\033[",

    # QR code ASCII art block characters
    "\u2588", "\u2580", "\u2584",
]

# Log level constants
LOG_ALL      = 0
LOG_REQUESTS = 1
LOG_WARNINGS = 2
LOG_ERRORS   = 3

_LEVEL_LABELS = ["ALL", "REQUESTS", "WARN", "ERRORS"]


# ─────────────────────────────────────────────────────────────────────────────
# Process termination helpers
# ─────────────────────────────────────────────────────────────────────────────

def _kill_process_tree(proc):
    if proc is None:
        return
    pid = proc.pid
    if sys.platform == "win32":
        try:
            _cf = subprocess.CREATE_NO_WINDOW
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                creationflags=_cf, capture_output=True,
            )
            return
        except Exception:
            pass
        try:
            import psutil
            parent = psutil.Process(pid)
            for child in parent.children(recursive=True):
                try: child.kill()
                except Exception: pass
            parent.kill()
            return
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass
    else:
        import signal
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            pass
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except Exception:
                pass
            try:
                proc.kill()
            except Exception:
                pass


def _kill_port(port: int):
    if sys.platform == "win32":
        try:
            _cf = subprocess.CREATE_NO_WINDOW
            result = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, creationflags=_cf,
            )
            for line in result.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    parts = line.split()
                    pid = parts[-1]
                    if pid.isdigit():
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", pid],
                            creationflags=_cf, capture_output=True,
                        )
        except Exception:
            pass
    else:
        try:
            import signal
            result = subprocess.run(
                ["lsof", "-ti", f"tcp:{port}"],
                capture_output=True, text=True,
            )
            for pid_str in result.stdout.split():
                pid_str = pid_str.strip()
                if pid_str.isdigit():
                    try: os.kill(int(pid_str), signal.SIGKILL)
                    except Exception: pass
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Dependency helpers
# ─────────────────────────────────────────────────────────────────────────────

def check_dependencies():
    missing_req, missing_opt = [], []
    for pip_name, import_name, required in DEPENDENCIES:
        try:
            importlib.import_module(import_name)
        except ImportError:
            (missing_req if required else missing_opt).append((pip_name, import_name))
    return missing_req, missing_opt


def install_packages(packages, log_fn=print):
    pip_names = [p[0] for p in packages]
    log_fn(f"Installing: {', '.join(pip_names)} ...")
    _cf = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet"] + pip_names,
        capture_output=True, text=True, creationflags=_cf,
    )
    if result.returncode != 0:
        log_fn(f"pip error: {result.stderr.strip()}")
        return False
    log_fn("Installation complete.")
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Network / QR helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _qr_matrix(data: str):
    try:
        import qrcode as _qr
        qr = _qr.QRCode(border=1)
        qr.add_data(data)
        qr.make(fit=True)
        return qr.get_matrix()
    except ImportError:
        return None


def draw_qr(canvas, url, size=160):
    canvas.delete("all")
    canvas.configure(bg=BG3)
    matrix = _qr_matrix(url)
    if matrix is None:
        canvas.create_text(
            size // 2, size // 2,
            text="pip install qrcode\nto enable QR",
            fill=GREY, font=("Courier", 7), justify="center",
        )
        return
    rows = len(matrix)
    cell = size / rows
    for r, row in enumerate(matrix):
        for c, v in enumerate(row):
            x0, y0 = c * cell, r * cell
            color = AMBER_GLOW if v else BG3
            canvas.create_rectangle(x0, y0, x0 + cell, y0 + cell,
                                    fill=color, outline="")


# ─────────────────────────────────────────────────────────────────────────────
# Backup helper
# ─────────────────────────────────────────────────────────────────────────────

def _run_backup(log_fn):
    import shutil as _shutil, datetime
    uploads_src = os.path.join(ROOT, "uploads")
    db_src      = os.path.join(ROOT, "database.db")
    backups_dir = os.path.join(ROOT, "backups")

    if not os.path.isdir(uploads_src):
        log_fn("ERROR  uploads/ folder not found", "error")
        return False

    ts  = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M")
    dst = os.path.join(backups_dir, f"backup_{ts}")
    os.makedirs(dst, exist_ok=True)

    log_fn("COPY   uploads/ ...", "dim")
    _shutil.copytree(uploads_src, os.path.join(dst, "uploads"), dirs_exist_ok=True)
    log_fn("OK     uploads/ copied", "ok")

    if os.path.isfile(db_src):
        log_fn("COPY   database.db ...", "dim")
        _shutil.copy2(db_src, os.path.join(dst, "database.db"))
        log_fn("OK     database.db copied", "ok")
    else:
        log_fn("SKIP   database.db not found", "dim")

    all_b = sorted(
        [d for d in os.listdir(backups_dir)
         if os.path.isdir(os.path.join(backups_dir, d)) and d.startswith("backup_")],
        reverse=True,
    )
    for old in all_b[10:]:
        _shutil.rmtree(os.path.join(backups_dir, old), ignore_errors=True)
        log_fn(f"PRUNE  {old}", "dim")

    n = sum(len(fs) for _, _, fs in os.walk(dst))
    log_fn(f"DONE   {n} files backed up to backups/backup_{ts}", "ok")
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Formatting helpers
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_bytes(b):
    try:
        b = int(b)
    except Exception:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024 or unit == "TB":
            return f"{b:.1f} {unit}"
        b /= 1024


import re as _re
_ANSI_RE = _re.compile(r'\033\[[0-9;]*[mKHJABCDsuhrfnlp]')


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub('', s)


def _classify_line(line: str):
    """
    Returns (tag, keep, display_line) for a raw server output line.
    tag:          tkinter text tag name
    keep:         bool — whether to store this entry at all
    display_line: cleaned string to display
    """
    # Strip ANSI escape codes before any matching — catches banner lines that
    # reach here despite PYTHONIOENCODING=utf-8 / NO_COLOR being set.
    line     = _strip_ansi(line)
    stripped = line.strip()
    if not stripped:
        return "", False, ""

    if stripped.startswith("[STATUS]"):
        return "", False, ""

    # Check against every suppression fragment (case-sensitive substring match)
    for frag in _SUPPRESS_FRAGMENTS:
        if frag in stripped:
            return "", False, ""

    # Also suppress lines that are purely whitespace + box-drawing / block chars
    # (catches QR art rows and banner borders that slip past fragment matching)
    _printable = stripped.encode("ascii", errors="ignore").decode()
    if not _printable.strip():
        return "", False, ""

    lo = stripped.lower()

    # HTTP requests — reformat cleanly
    for method in ("GET ", "POST ", "PUT ", "DELETE ", "PATCH ", "HEAD "):
        if method in stripped:
            m = _re.search(
                r'"(GET|POST|PUT|DELETE|PATCH|HEAD)\s+(\S+)\s+HTTP[^"]*"\s+(\d+)',
                stripped,
            )
            if m:
                verb   = m.group(1).ljust(6)
                path   = m.group(2)
                status = m.group(3)
                s_int  = int(status)
                if s_int >= 500:
                    tag = "error"
                elif s_int >= 400:
                    tag = "warn"
                else:
                    tag = "request"
                return tag, True, f"{verb} {status}  {path}"
            return "request", True, stripped

    if any(k in lo for k in ("error", "traceback", "exception")):
        return "error", True, stripped

    if any(k in lo for k in ("[warn]", "warning")):
        return "warn", True, stripped

    if any(k in stripped for k in ("[EXT]", "[HOOK]", "[THUMB]")):
        clean = stripped
        for tok in ("[EXT]", "[HOOK]", "[THUMB]", "✅", "⚠️", "❌"):
            clean = clean.replace(tok, "")
        clean = clean.strip().lstrip(" \t")
        return "ext", True, f"EXT    {clean}"

    return "dim", True, stripped


# ─────────────────────────────────────────────────────────────────────────────
# Pulsing activity oscilloscope
# ─────────────────────────────────────────────────────────────────────────────

class ActivityBar:
    W    = 200
    H    = 28
    COLS = 50

    def __init__(self, parent):
        self._samples = [0.0] * self.COLS
        self._decay   = 0.0
        self.canvas   = tk.Canvas(
            parent, width=self.W, height=self.H,
            bg=BG3, highlightthickness=0,
        )
        self._draw()
        self._tick()

    def pulse(self, intensity=1.0):
        self._decay = min(1.0, self._decay + intensity)

    def _tick(self):
        self._decay = max(0.0, self._decay - 0.12)
        self._samples.pop(0)
        self._samples.append(self._decay)
        self._draw()
        self.canvas.after(60, self._tick)

    def _draw(self):
        c = self.canvas
        c.delete("all")
        c.create_rectangle(0, 0, self.W, self.H, fill=BG3, outline="")
        mid   = self.H / 2
        col_w = self.W / self.COLS
        c.create_line(0, mid, self.W, mid, fill=GREY_DIM, width=1)
        pts = []
        for i, v in enumerate(self._samples):
            x = i * col_w + col_w / 2
            y = mid - v * (mid - 3)
            pts.extend([x, y])
        if len(pts) >= 4:
            c.create_line(*pts, fill=AMBER_DIM, width=3, smooth=True)
            c.create_line(*pts, fill=AMBER,     width=1, smooth=True)


# ─────────────────────────────────────────────────────────────────────────────
# Stat tile widget
# ─────────────────────────────────────────────────────────────────────────────

class StatTile:
    def __init__(self, parent, label: str, var: tk.StringVar, accent=AMBER):
        self.frame = tk.Frame(parent, bg=BG3, padx=12, pady=8)
        tk.Label(
            self.frame, text=label,
            bg=BG3, fg=GREY, font=("Courier", 7),
        ).pack(anchor="w")
        tk.Label(
            self.frame, textvariable=var,
            bg=BG3, fg=accent, font=("Courier", 14, "bold"),
        ).pack(anchor="w")


# ─────────────────────────────────────────────────────────────────────────────
# Main launcher GUI
# ─────────────────────────────────────────────────────────────────────────────

class LauncherApp:

    def __init__(self, root: tk.Tk):
        self.root            = root
        self.server_proc     = None
        self._running        = False
        self._backup_running = False
        self._network_url    = ""
        self._log_filter     = LOG_ALL
        self._request_count  = 0

        # All log entries as (tag, ts, msg, level) tuples
        self._log_entries: list = []

        # Live stats
        self._stat_files   = tk.StringVar(value="—")
        self._stat_folders = tk.StringVar(value="—")
        self._stat_tags    = tk.StringVar(value="—")
        self._stat_uploads = tk.StringVar(value="—")
        self._stat_free    = tk.StringVar(value="—")
        self._stat_reqs    = tk.StringVar(value="0")

        self._feat_vars = {}

        self._build_ui()
        self._check_server_file()
        self.root.after(120, self._startup_sequence)

    # ── UI build ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        r = self.root
        r.title("LocalFileHub")
        r.configure(bg=BG)
        r.resizable(False, False)
        r.geometry("740x800")
        try:
            r.iconbitmap(default="")
        except Exception:
            pass

        # Top accent stripe
        tk.Frame(r, bg=AMBER, height=2).pack(fill="x")

        # ── Header ───────────────────────────────────────────────────────────
        hdr = tk.Frame(r, bg=BG, padx=24, pady=18)
        hdr.pack(fill="x")

        left_hdr = tk.Frame(hdr, bg=BG)
        left_hdr.pack(side="left")

        tk.Label(
            left_hdr, text="LOCAL",
            bg=BG, fg=AMBER, font=("Courier", 22, "bold"),
        ).pack(side="left")
        tk.Label(
            left_hdr, text="FILEHUB",
            bg=BG, fg=WHITE, font=("Courier", 22, "bold"),
        ).pack(side="left")

        self.version_lbl = tk.Label(
            left_hdr, text="",
            bg=BG, fg=GREY, font=("Courier", 8),
        )
        self.version_lbl.pack(side="left", padx=(10, 0), pady=(8, 0))

        rh = tk.Frame(hdr, bg=BG)
        rh.pack(side="right", anchor="center")

        self._open_btn = self._mk_btn(
            rh, "OPEN BROWSER",
            bg=BG4, fg=GREY, active_bg=BG5,
            command=self._open_browser, state="disabled",
            padx=10, pady=5, font_size=8,
        )
        self._open_btn.pack(side="right")

        # ── Status row ───────────────────────────────────────────────────────
        sr = tk.Frame(r, bg=BG3, padx=24, pady=0)
        sr.pack(fill="x")

        sl = tk.Frame(sr, bg=BG3, pady=10)
        sl.pack(side="left")

        self._status_dot = tk.Label(
            sl, text="◉",
            bg=BG3, fg=AMBER_DIM, font=("Courier", 13),
        )
        self._status_dot.pack(side="left")

        self._status_lbl = tk.Label(
            sl, text="INITIALISING",
            bg=BG3, fg=GREY, font=("Courier", 10, "bold"),
        )
        self._status_lbl.pack(side="left", padx=(8, 0))

        sr_right = tk.Frame(sr, bg=BG3, pady=6)
        sr_right.pack(side="right", padx=(0, 6))

        tk.Label(
            sr_right, text="ACTIVITY",
            bg=BG3, fg=GREY_DIM, font=("Courier", 6),
        ).pack(anchor="e")
        self._activity = ActivityBar(sr_right)
        self._activity.canvas.pack()

        # ── Two-column body ───────────────────────────────────────────────────
        body = tk.Frame(r, bg=BG, padx=24, pady=14)
        body.pack(fill="x")

        left_col = tk.Frame(body, bg=BG)
        left_col.pack(side="left", fill="both", expand=True)

        right_col = tk.Frame(body, bg=BG)
        right_col.pack(side="right", padx=(14, 0), anchor="n")

        # URL section
        url_frame = tk.Frame(left_col, bg=BG3, padx=14, pady=12)
        url_frame.pack(fill="x")

        tk.Label(
            url_frame, text="ACCESS POINTS",
            bg=BG3, fg=GREY, font=("Courier", 7),
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))

        tk.Label(url_frame, text="NET ", bg=BG3, fg=GREY_DIM,
                 font=("Courier", 8)).grid(row=1, column=0, sticky="w")
        self.url_network = tk.Label(
            url_frame, text="—",
            bg=BG3, fg=AMBER, font=("Courier", 10, "bold"), cursor="hand2",
        )
        self.url_network.grid(row=1, column=1, sticky="w", padx=(8, 0))
        self.url_network.bind("<Button-1>",
                              lambda e: self._open_url(self._network_url))

        tk.Label(url_frame, text="LCL ", bg=BG3, fg=GREY_DIM,
                 font=("Courier", 8)).grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.url_local = tk.Label(
            url_frame, text="—",
            bg=BG3, fg=AMBER, font=("Courier", 10, "bold"), cursor="hand2",
        )
        self.url_local.grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(4, 0))
        self.url_local.bind("<Button-1>",
                            lambda e: self._open_url(f"http://localhost:{PORT}"))

        # Stats grid
        stats_outer = tk.Frame(left_col, bg=BG, pady=0)
        stats_outer.pack(fill="x", pady=(8, 0))

        stat_defs = [
            ("FILES",     self._stat_files,   AMBER),
            ("FOLDERS",   self._stat_folders, AMBER),
            ("TAGS",      self._stat_tags,    AMBER),
            ("UPLOADS",   self._stat_uploads, CYAN),
            ("DISK FREE", self._stat_free,    CYAN),
            ("REQUESTS",  self._stat_reqs,    GREEN),
        ]
        for i, (lbl, var, accent) in enumerate(stat_defs):
            col = i % 3
            row = i // 3
            tile = StatTile(stats_outer, lbl, var, accent)
            tile.frame.grid(
                row=row, column=col,
                padx=(0 if col == 0 else 4, 0),
                pady=(0 if row == 0 else 4, 0),
                sticky="nsew",
            )
        for c in range(3):
            stats_outer.columnconfigure(c, weight=1)

        # Feature pills
        pills_outer = tk.Frame(left_col, bg=BG, pady=0)
        pills_outer.pack(fill="x", pady=(10, 0))

        tk.Label(
            pills_outer, text="MODULES",
            bg=BG, fg=GREY_DIM, font=("Courier", 7),
        ).pack(anchor="w", pady=(0, 4))

        pills_row = tk.Frame(pills_outer, bg=BG)
        pills_row.pack(anchor="w")

        feat_defs = [
            ("pillow",    "PILLOW"),
            ("imagehash", "PHASH"),
            ("limiter",   "LIMITER"),
            ("ffmpeg",    "FFMPEG"),
            ("hooks",     "HOOKS"),
        ]
        for name, label in feat_defs:
            var = tk.StringVar(value="?")
            self._feat_vars[name] = var
            pill = tk.Frame(pills_row, bg=BG4, padx=8, pady=4)
            pill.pack(side="left", padx=(0, 5))
            dot_lbl = tk.Label(
                pill, textvariable=var,
                bg=BG4, fg=GREY_DIM, font=("Courier", 8, "bold"),
            )
            dot_lbl.pack(side="left")
            self._feat_vars[f"_{name}_dot"] = dot_lbl
            tk.Label(
                pill, text=f" {label}",
                bg=BG4, fg=GREY, font=("Courier", 8),
            ).pack(side="left")

        # QR code
        tk.Label(
            right_col, text="SCAN TO OPEN",
            bg=BG, fg=GREY_DIM, font=("Courier", 7),
        ).pack(pady=(0, 4))
        self.qr_canvas = tk.Canvas(
            right_col, width=160, height=160,
            bg=BG3, highlightthickness=1,
            highlightbackground=BORDER2,
        )
        self.qr_canvas.pack()

        # ── Divider ───────────────────────────────────────────────────────────
        tk.Frame(r, bg=BORDER, height=1).pack(fill="x", padx=24, pady=(10, 0))

        # ── Log section ───────────────────────────────────────────────────────
        log_hdr = tk.Frame(r, bg=BG, padx=24, pady=6)
        log_hdr.pack(fill="x")

        tk.Label(
            log_hdr, text="SERVER LOG",
            bg=BG, fg=GREY, font=("Courier", 7, "bold"),
        ).pack(side="left")

        # Filter buttons
        self._filter_btns = {}
        filter_row = tk.Frame(log_hdr, bg=BG)
        filter_row.pack(side="left", padx=(14, 0))

        for lvl, lbl in enumerate(_LEVEL_LABELS):
            is_active = (lvl == LOG_ALL)
            btn = tk.Button(
                filter_row, text=lbl,
                bg=AMBER_DIM if is_active else BG4,
                fg=AMBER_GLOW if is_active else GREY,
                activebackground=BG5, activeforeground=AMBER,
                font=("Courier", 7), relief="flat",
                padx=7, pady=2, cursor="hand2",
                command=lambda l=lvl: self._set_filter(l),
            )
            btn.pack(side="left", padx=(0, 3))
            self._filter_btns[lvl] = btn

        clear_btn = tk.Button(
            log_hdr, text="CLEAR",
            bg=BG, fg=GREY_DIM, activebackground=BG3,
            activeforeground=GREY,
            font=("Courier", 7), relief="flat",
            padx=7, pady=2, cursor="hand2",
            command=self._clear_log,
        )
        clear_btn.pack(side="right")

        # Log text area
        log_outer = tk.Frame(r, bg=BG, padx=24)
        log_outer.pack(fill="both", expand=True)

        log_inner = tk.Frame(log_outer, bg=BG3)
        log_inner.pack(fill="both", expand=True)

        scrollbar = tk.Scrollbar(
            log_inner, bg=BG3, troughcolor=BG2,
            activebackground=GREY_DIM, width=8,
        )
        scrollbar.pack(side="right", fill="y")

        self.log_text = tk.Text(
            log_inner,
            height=10,
            bg=BG2, fg=GREY,
            font=("Courier", 8),
            relief="flat",
            state="disabled",
            wrap="none",
            insertbackground=AMBER,
            yscrollcommand=scrollbar.set,
            selectbackground=BG4,
            selectforeground=WHITE,
            padx=10, pady=6,
            spacing1=1, spacing3=1,
        )
        self.log_text.pack(fill="both", expand=True)
        scrollbar.config(command=self.log_text.yview)

        self.log_text.tag_config("error",   foreground=RED_GLOW)
        self.log_text.tag_config("warn",    foreground=AMBER)
        self.log_text.tag_config("ext",     foreground=BLUE_GLOW)
        self.log_text.tag_config("ok",      foreground=GREEN_GLOW)
        self.log_text.tag_config("request", foreground=CYAN)
        self.log_text.tag_config("dim",     foreground=GREY_DIM)
        self.log_text.tag_config("ts",      foreground=GREY_DIM)

        # ── Divider ───────────────────────────────────────────────────────────
        tk.Frame(r, bg=BORDER, height=1).pack(fill="x", padx=24, pady=(6, 0))

        # ── Control buttons ───────────────────────────────────────────────────
        ctrl = tk.Frame(r, bg=BG, padx=24, pady=10)
        ctrl.pack(fill="x")

        self.stop_btn = self._mk_btn(
            ctrl, "■  STOP",
            bg=RED_DIM, fg=RED, active_bg="#2a0a0a",
            command=self._stop_server, state="disabled",
            padx=18, pady=9,
        )
        self.stop_btn.pack(side="right")

        self.start_btn = self._mk_btn(
            ctrl, "▶  START",
            bg=GREEN_DIM, fg=GREEN, active_bg="#0a2018",
            command=self._start_server, state="disabled",
            padx=18, pady=9,
        )
        self.start_btn.pack(side="right", padx=(0, 8))

        # ── Backup row ────────────────────────────────────────────────────────
        bk = tk.Frame(r, bg=BG, padx=24)
        bk.pack(fill="x", pady=(0, 16))

        self.backup_btn = self._mk_btn(
            bk, "▣  BACKUP",
            bg=BLUE_DIM, fg=BLUE, active_bg="#0a1828",
            command=self._run_backup,
            padx=18, pady=9,
        )
        self.backup_btn.pack(side="left")

        self._backup_lbl = tk.Label(
            bk, text="",
            bg=BG, fg=GREY, font=("Courier", 8),
        )
        self._backup_lbl.pack(side="left", padx=(12, 0))

        r.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── Button factory ────────────────────────────────────────────────────────

    def _mk_btn(self, parent, text, bg, fg, active_bg,
                command, state="normal", padx=14, pady=7, font_size=9):
        return tk.Button(
            parent, text=text,
            bg=bg, fg=fg,
            activebackground=active_bg, activeforeground=fg,
            font=("Courier", font_size, "bold"),
            relief="flat", padx=padx, pady=pady,
            cursor="hand2", command=command, state=state, bd=0,
        )

    # ── Status helpers ────────────────────────────────────────────────────────

    def _set_status(self, text, color=AMBER):
        self._status_dot.configure(fg=color)
        self._status_lbl.configure(text=text, fg=color)

    def _crt_on_animation(self):
        """CRT screen power-on flicker effect."""
        frames = [
            (AMBER_FAINT, "STARTING"),
            (AMBER_DIM,   "STARTING"),
            (AMBER,       "STARTING ."),
            (AMBER_DIM,   "STARTING ."),
            (AMBER,       "STARTING .."),
            (AMBER_DIM,   "STARTING .."),
            (AMBER_GLOW,  "STARTING ..."),
        ]

        def _step(i=0):
            if i < len(frames):
                col, txt = frames[i]
                self._set_status(txt, col)
                self.root.after(80, _step, i + 1)

        _step()

    # ── Log helpers ───────────────────────────────────────────────────────────

    def _tag_to_level(self, tag: str) -> int:
        if tag == "error":
            return LOG_ERRORS
        if tag == "warn":
            return LOG_WARNINGS
        if tag == "request":
            return LOG_REQUESTS
        return LOG_ALL

    def _log(self, msg: str, tag: str = ""):
        import datetime
        ts  = datetime.datetime.now().strftime("%H:%M:%S")
        lvl = self._tag_to_level(tag)
        self._log_entries.append((tag, ts, msg, lvl))
        if len(self._log_entries) > 1000:
            self._log_entries = self._log_entries[-800:]
        if lvl >= self._log_filter:
            self._append_log_line(tag, ts, msg)

    def _append_log_line(self, tag: str, ts: str, msg: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", ts + "  ", "ts")
        if tag:
            self.log_text.insert("end", msg + "\n", tag)
        else:
            self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_filter(self, level: int):
        self._log_filter = level
        for lvl, btn in self._filter_btns.items():
            if lvl == level:
                btn.configure(bg=AMBER_DIM, fg=AMBER_GLOW)
            else:
                btn.configure(bg=BG4, fg=GREY)
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        for (tag, ts, msg, lvl) in self._log_entries:
            if lvl >= level:
                self._append_log_line(tag, ts, msg)

    def _clear_log(self):
        self._log_entries = []
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _log_server_line(self, raw: str):
        tag, keep, display = _classify_line(raw)
        if not keep:
            return
        if tag == "request":
            self._request_count += 1
            self._stat_reqs.set(str(self._request_count))
            self._activity.pulse(0.7)
        elif tag == "error":
            self._activity.pulse(1.0)
        self._log(display, tag)

    # ── Misc helpers ─────────────────────────────────────────────────────────

    def _open_url(self, url: str):
        if not url:
            return
        import webbrowser
        webbrowser.open(url)

    def _open_browser(self):
        self._open_url(f"http://localhost:{PORT}")

    def _check_server_file(self):
        path = os.path.join(ROOT, "server.py")
        if not os.path.isfile(path):
            self._set_status("SERVER.PY NOT FOUND", RED)
            messagebox.showerror(
                "Missing file",
                f"server.py was not found in:\n{ROOT}\n\n"
                "Make sure Launch.py is in the same folder as server.py.",
            )
            return False
        return True

    def _detect_version(self):
        try:
            with open(os.path.join(ROOT, "server.py"), encoding="utf-8") as f:
                for line in f:
                    if "__version__" in line or "VERSION" in line:
                        parts = line.strip().split("=")
                        if len(parts) == 2:
                            v = parts[1].strip().strip('"').strip("'")
                            if v:
                                self.version_lbl.configure(text=f"v{v}")
                                return
        except Exception:
            pass

    def _update_feat_pill(self, name: str, active: bool, label: str):
        var = self._feat_vars.get(name)
        if var:
            var.set(label)
        dot = self._feat_vars.get(f"_{name}_dot")
        if dot:
            dot.configure(fg=GREEN if active else RED_DIM)

    # ── Startup sequence ──────────────────────────────────────────────────────

    def _startup_sequence(self):
        self._detect_version()
        missing_req, missing_opt = check_dependencies()

        if missing_req:
            self._log(
                f"Missing required: {', '.join(p[0] for p in missing_req)}", "warn"
            )
            if not messagebox.askyesno(
                "Missing dependencies",
                "Required packages are not installed:\n\n"
                + "\n".join(f"  • {p[0]}" for p in missing_req)
                + "\n\nInstall them now?",
            ):
                self._set_status("MISSING DEPS", RED)
                return
            self._set_status("INSTALLING", AMBER)
            if not install_packages(missing_req, log_fn=self._log):
                self._set_status("INSTALL FAILED", RED)
                messagebox.showerror(
                    "Installation failed",
                    "Could not install packages.\nTry:  pip install flask werkzeug",
                )
                return

        if missing_opt:
            self._log(
                f"Optional not installed: {', '.join(p[0] for p in missing_opt)}", "dim"
            )
            if messagebox.askyesno(
                "Optional packages",
                "These optional packages add features:\n\n"
                + "\n".join(f"  • {p[0]}" for p in missing_opt)
                + "\n\nInstall them? (skippable)",
                default="no",
            ):
                install_packages(missing_opt, log_fn=self._log)

        self._set_status("READY", GREY)
        self.start_btn.configure(state="normal")
        self._start_server()

    # ── Server control ────────────────────────────────────────────────────────

    def _start_server(self):
        if self._running:
            return

        self._crt_on_animation()
        self.start_btn.configure(state="disabled")
        self._log("Checking port for orphaned processes...", "dim")
        _kill_port(PORT)

        server_path = os.path.join(ROOT, "server.py")
        env = os.environ.copy()
        env["NO_COLOR"]                 = "1"
        env["TERM"]                     = "dumb"
        env["PYTHONIOENCODING"]         = "utf-8"
        env["PYTHONLEGACYWINDOWSSTDIO"] = "0"
        env["PYTHONUNBUFFERED"]         = "1"
        env["LOCALFILEHUB_LAUNCHER"]    = "1"
        env["LAUNCHER_ROOT"]            = ROOT

        if sys.platform == "win32":
            exe_dir    = os.path.dirname(sys.executable)
            python_exe = os.path.join(exe_dir, "python.exe")
            if not os.path.isfile(python_exe):
                python_exe = sys.executable
        else:
            python_exe = sys.executable

        _cflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        _kwargs = {}
        if sys.platform != "win32":
            _kwargs["start_new_session"] = True

        try:
            self.server_proc = subprocess.Popen(
                [python_exe, server_path],
                cwd=ROOT, env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=_cflags,
                **_kwargs,
            )
        except Exception as e:
            self._log(f"Failed to start server: {e}", "error")
            self._set_status("START FAILED", RED)
            self.start_btn.configure(state="normal")
            return

        self._running = True
        self.stop_btn.configure(state="normal")

        ip = get_local_ip()
        self._network_url = f"http://{ip}:{PORT}"
        self.url_network.configure(text=self._network_url)
        self.url_local.configure(text=f"http://localhost:{PORT}")
        draw_qr(self.qr_canvas, self._network_url)

        self._open_btn.configure(state="normal")
        threading.Thread(target=self._read_server_output, daemon=True).start()
        self.root.after(2000, self._poll_server)

    def _stop_server(self):
        if not self._running or self.server_proc is None:
            return
        self._log("Sending stop signal...", "dim")
        self._set_status("STOPPING", AMBER)

        proc = self.server_proc
        self.server_proc = None
        self._running    = False

        _kill_process_tree(proc)
        try:
            proc.wait(timeout=8)
        except Exception:
            pass

        self._set_status("OFFLINE", GREY)
        self.stop_btn.configure(state="disabled")
        self.start_btn.configure(state="normal")
        self._open_btn.configure(state="disabled")
        self.url_network.configure(text="—")
        self.url_local.configure(text="—")
        self.qr_canvas.delete("all")
        self._log("Server stopped.", "dim")

    def _read_server_output(self):
        try:
            for line in self.server_proc.stdout:
                line = line.rstrip()
                if not line:
                    continue
                if line.startswith("[STATUS]"):
                    self.root.after(0, self._parse_status, line)
                else:
                    self.root.after(0, self._log_server_line, line)
        except Exception as e:
            self.root.after(0, self._log, f"output reader: {e}", "error")

    def _parse_status(self, line: str):
        try:
            kv          = line[len("[STATUS]"):].strip()
            key, _, val = kv.partition("=")
            key = key.strip()
            val = val.strip()
        except Exception:
            return

        if key == "files":
            self._stat_files.set(val)
        elif key == "folders":
            self._stat_folders.set(val)
        elif key == "tags":
            self._stat_tags.set(val)
        elif key == "uploads_bytes":
            self._stat_uploads.set(_fmt_bytes(val))
        elif key == "disk_free_bytes":
            self._stat_free.set(_fmt_bytes(val))
        elif key == "ready":
            self._set_status("RUNNING", GREEN)
        elif key.startswith("feat_"):
            feat = key[5:]
            if feat in self._feat_vars:
                if feat == "hooks":
                    active = int(val) > 0
                    label  = f"{val}x" if active else "—"
                else:
                    active = val == "1"
                    label  = "ON" if active else "—"
                self._update_feat_pill(feat, active, label)

    def _poll_server(self):
        if not self._running:
            return
        if self.server_proc and self.server_proc.poll() is not None:
            self._running    = False
            self.server_proc = None
            self.root.after(0, self._on_server_died)
        else:
            self.root.after(2000, self._poll_server)

    def _on_server_died(self):
        self._set_status("CRASHED", RED)
        self.stop_btn.configure(state="disabled")
        self.start_btn.configure(state="normal")
        self._open_btn.configure(state="disabled")
        self._log("Server process exited unexpectedly.", "error")

    # ── Backup ────────────────────────────────────────────────────────────────

    def _run_backup(self):
        if self._backup_running:
            return
        self._backup_running = True
        self.backup_btn.configure(state="disabled", text="▣  BACKING UP...")
        self._backup_lbl.configure(text="running...", fg=AMBER)
        self._log("Backup started", "dim")
        threading.Thread(target=self._backup_worker, daemon=True).start()

    def _backup_worker(self):
        try:
            ok  = _run_backup(self._log_ts)
            msg = "Backup complete." if ok else "Backup failed — see log."
            self.root.after(0, self._backup_done, ok, msg)
        except Exception as e:
            self.root.after(0, self._backup_done, False, str(e))

    def _backup_done(self, success: bool, message: str):
        self._backup_running = False
        self.backup_btn.configure(state="normal", text="▣  BACKUP")
        if success:
            self._backup_lbl.configure(text=message, fg=GREEN)
        else:
            self._backup_lbl.configure(text=f"ERROR: {message}", fg=RED)
        self.root.after(8000, lambda: self._backup_lbl.configure(text=""))

    def _log_ts(self, msg: str, tag: str = "dim"):
        self.root.after(0, self._log, msg, tag)

    # ── Close ─────────────────────────────────────────────────────────────────

    def _on_close(self):
        if self._backup_running:
            if not messagebox.askyesno("Backup in progress",
                                        "A backup is running.\nQuit anyway?"):
                return
        if self._running:
            if not messagebox.askyesno("Quit",
                                        "The server is still running.\nStop it and quit?"):
                return
            self._stop_server()

        if self.server_proc is not None:
            _kill_process_tree(self.server_proc)
            self.server_proc = None

        self.root.destroy()


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    if sys.version_info < (3, 8):
        import tkinter as _tk
        _tk.Tk().withdraw()
        messagebox.showerror(
            "Python version",
            f"LocalFileHub requires Python 3.8+.\n"
            f"You are running {sys.version.split()[0]}.\n\n"
            "Download at https://python.org",
        )
        sys.exit(1)

    root = tk.Tk()
    LauncherApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
