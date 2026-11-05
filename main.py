"""
Minecraft Server Manager - create, run, update and downgrade a local
Minecraft Java Edition server (Vanilla / Paper / Fabric) without a command line.

Install:  pip install customtkinter requests
Run:      python main.py
"""
import glob
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import zipfile
from datetime import datetime
from tkinter import filedialog, messagebox

import customtkinter as ctk
import requests

try:
    from PIL import Image  # optional: auto-resizes server icons and shows a preview
except ImportError:
    Image = None
# --------------------------------------------------------------------------
# Constants / configuration
# --------------------------------------------------------------------------
MOJANG_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
PAPER_API = "https://api.papermc.io/v2/projects/paper"
PAPER_FILL_API = "https://fill.papermc.io/v3/projects/paper"
FABRIC_API = "https://meta.fabricmc.net/v2/versions"
MODRINTH_API = "https://api.modrinth.com/v2"
HEADERS = {"User-Agent": "MCServerManager/1.0"}
TYPES = ["Vanilla", "Paper", "Fabric"]
META_FILE = "mcsm_server.json"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "MCServerManager")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

DEFAULT_PROPS = {
    "gamemode": "survival",
    "difficulty": "easy",
    "max-players": "20",
    "online-mode": "true",
    "view-distance": "10",
    "motd": "A Minecraft Server",
}


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Remote APIs
# --------------------------------------------------------------------------
def http_json(url):
    r = requests.get(url, headers=HEADERS, timeout=25)
    r.raise_for_status()
    return r.json()


def fetch_vanilla_catalog():
    data = http_json(MOJANG_MANIFEST)
    return {"latest": data["latest"], "versions": data["versions"]}


def fetch_paper_catalog():
    try:
        data = http_json(PAPER_API)
        return {"versions": list(reversed(data["versions"]))}  # newest first
    except Exception:
        # v2 API is deprecated/removed: fall back to PaperMC Fill v3
        data = http_json(PAPER_FILL_API)
        vers = []
        for group in data["versions"].values():
            vers.extend(group)
        return {"versions": vers}  # Fill lists newest first


def fetch_fabric_catalog():
    data = http_json(FABRIC_API)
    return {"game": data["game"], "loader": data["loader"], "installer": data["installer"]}


def resolve_vanilla(version, catalog):
    entry = next((v for v in catalog["versions"] if v["id"] == version), None)
    if not entry:
        raise RuntimeError(f"Vanilla version '{version}' was not found in the Mojang manifest.")
    meta = http_json(entry["url"])
    server = meta.get("downloads", {}).get("server")
    if not server:
        raise RuntimeError(f"Mojang does not provide a server.jar for {version}.")
    return {"url": server["url"], "version": version, "build": "",
            "java": (meta.get("javaVersion") or {}).get("majorVersion")}


def resolve_paper(version):
    try:
        data = http_json(f"{PAPER_API}/versions/{version}/builds")
        builds = data.get("builds", [])
        if not builds:
            raise RuntimeError("no builds")
        stable = [b for b in builds if b.get("channel") == "default"] or builds
        best = max(stable, key=lambda b: b["build"])
        name = best["downloads"]["application"]["name"]
        url = f"{PAPER_API}/versions/{version}/builds/{best['build']}/downloads/{name}"
        return {"url": url, "version": version, "build": str(best["build"]), "java": None}
    except Exception:
        pass
    # Fill v3 fallback
    builds = http_json(f"{PAPER_FILL_API}/versions/{version}/builds")
    if not builds:
        raise RuntimeError(f"No Paper builds exist for {version}.")
    stable = [b for b in builds if str(b.get("channel", "")).upper() == "STABLE"] or builds
    best = max(stable, key=lambda b: b["id"])
    dl = best["downloads"].get("server:default") or next(iter(best["downloads"].values()))
    return {"url": dl["url"], "version": version, "build": str(best["id"]), "java": None}


def resolve_fabric(version, catalog):
    if not any(g["version"] == version for g in catalog["game"]):
        raise RuntimeError(f"Fabric does not support Minecraft {version}.")
    loader = next((l for l in catalog["loader"] if l.get("stable")), catalog["loader"][0])["version"]
    installer = next((i for i in catalog["installer"] if i.get("stable")), catalog["installer"][0])["version"]
    url = f"{FABRIC_API}/loader/{version}/{loader}/{installer}/server/jar"
    return {"url": url, "version": version, "build": loader, "java": None}


# --------------------------------------------------------------------------
# Java helpers
# --------------------------------------------------------------------------
def required_java(version):
    """Minimum Java major version required by a Minecraft version id."""
    m = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", version or "")
    if m:
        a, b, c = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
        if a >= 26:
            return 25
        if a == 1:
            if b >= 21 or (b == 20 and c >= 5):
                return 21
            if b >= 18:
                return 17
            if b == 17:
                return 16
        return 8
    s = re.match(r"^(\d\d)w(\d\d)", version or "")
    if s:
        key = (int(s.group(1)), int(s.group(2)))
        if key >= (26, 1):
            return 25
        if key >= (24, 14):
            return 21
        if key >= (21, 44):
            return 17
        if key >= (21, 19):
            return 16
    return 8


def probe_java(path):
    """Return the major version of a java executable, or None."""
    try:
        out = subprocess.run([path, "-version"], capture_output=True, text=True,
                             timeout=10, creationflags=NO_WINDOW)
        text = (out.stderr or "") + (out.stdout or "")
        m = re.search(r'version "(\d+)(?:\.(\d+))?', text)
        if not m:
            return None
        major = int(m.group(1))
        if major == 1 and m.group(2):
            major = int(m.group(2))
        return major
    except Exception:
        return None


def find_java_installs():
    candidates = []
    pf = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
          os.environ.get("ProgramW6432"), r"C:\Program Files", r"D:\Program Files"]
    vendors = ["Java", "Eclipse Adoptium", "Eclipse Foundation", "Microsoft", "Zulu", "BellSoft",
               "Amazon Corretto", "Semeru", "AdoptOpenJDK", "OpenJDK", "RedHat"]
    for base in filter(None, pf):
        for v in vendors:
            candidates += glob.glob(os.path.join(base, v, "*", "bin", "java.exe"))
    home = os.path.expanduser("~")
    candidates += glob.glob(os.path.join(home, ".jdks", "*", "bin", "java.exe"))
    appdata = os.environ.get("APPDATA", "")
    candidates += glob.glob(os.path.join(appdata, ".minecraft", "runtime", "*", "*", "*", "bin", "java.exe"))
    local = os.environ.get("LOCALAPPDATA", "")
    candidates += glob.glob(os.path.join(local, "Packages", "Microsoft.4297127D64EC6_*", "LocalCache", "Local",
                                         "runtime", "*", "*", "*", "bin", "java.exe"))
    jh = os.environ.get("JAVA_HOME")
    if jh:
        candidates.append(os.path.join(jh, "bin", "java.exe"))
    w = shutil.which("java")
    if w:
        candidates.append(w)
    seen, result = set(), []
    for c in candidates:
        key = os.path.normcase(os.path.abspath(c))
        if key in seen or not os.path.isfile(c):
            continue
        seen.add(key)
        major = probe_java(c)
        if major:
            result.append((major, os.path.abspath(c)))
    result.sort(key=lambda x: -x[0])
    return result


# --------------------------------------------------------------------------
# server.properties helpers
# --------------------------------------------------------------------------
def _unescape(v):
    return re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), v)


def _escape(v):
    return "".join(c if ord(c) < 128 else "\\u%04x" % ord(c) for c in v)


def read_properties(path):
    props = {}
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\r\n")
                if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                props[k.strip()] = _unescape(v)
    return props


def write_properties(path, updates):
    lines = []
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    remaining = dict(updates)
    out = []
    for line in lines:
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            k = line.split("=", 1)[0].strip()
            if k in remaining:
                out.append(f"{k}={_escape(remaining.pop(k))}")
                continue
        out.append(line)
    for k, v in remaining.items():
        out.append(f"{k}={_escape(v)}")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")


def safe_name(s):
    return re.sub(r"[^A-Za-z0-9._-]", "_", s or "unknown")


# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.title("Minecraft Server Manager")
        self.geometry("1180x800")
        self.minsize(1000, 680)

        self.cfg = load_config()
        self.uiq = queue.Queue()
        self.catalog = {}
        self.busy = False
        self.proc = None
        self.restart_pending = False
        self.java_map = {}
        self.java_ctx_version = None
        self.meta = {}

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(40, self.poll)
        self.refresh_network()
        self.load_catalogs()
        self.detect_java()
        last = self.cfg.get("last_folder")
        if not (last and os.path.isdir(last)):
            last = os.path.join(os.path.expanduser("~"), "MinecraftServer")
        self.folder_var.set(last)
        if os.path.isdir(last):
            self.load_server()
        self.show_page("Console" if os.path.isfile(self.jar_path()) else "Create Server")

    # ---------------- infrastructure ----------------
    def ui(self, fn):
        self.uiq.put(fn)

    def poll(self):
        for _ in range(300):
            try:
                fn = self.uiq.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                pass
        self.after(40, self.poll)

    def set_status(self, text):
        self.ui(lambda: self.status_lbl.configure(text=text))

    def set_progress(self, frac, text=None):
        def f():
            self.progress.set(max(0.0, min(1.0, frac)))
            if text:
                self.status_lbl.configure(text=text)
        self.ui(f)

    def run_task(self, fn, label):
        if self.busy:
            messagebox.showinfo("Busy", "Another operation is still running. Please wait.")
            return
        self.busy = True
        self.progress.set(0)
        self.status_lbl.configure(text=label)

        def work():
            try:
                fn()
            except Exception as e:
                self.ui(lambda e=e: self._fail(e))
            finally:
                self.ui(self._task_done)
        threading.Thread(target=work, daemon=True).start()

    def _fail(self, e):
        self.status_lbl.configure(text=f"Error: {e}")
        messagebox.showerror("Error", str(e))

    def _task_done(self):
        self.busy = False
        self.refresh_buttons()

    def download(self, url, dest, label):
        with requests.get(url, headers=HEADERS, stream=True, timeout=30) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            done = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(65536):
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        self.set_progress(done / total, f"{label}: {done / 1e6:.1f} / {total / 1e6:.1f} MB")

    # ---------------- UI construction ----------------
    def _build_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # ----- sidebar -----
        side = ctk.CTkFrame(self, width=210, corner_radius=0)
        side.grid(row=0, column=0, sticky="nsew")
        side.grid_propagate(False)
        side.grid_rowconfigure(8, weight=1)
        ctk.CTkLabel(side, text="Minecraft\nServer Manager", font=ctk.CTkFont(size=20, weight="bold"),
                     justify="left").grid(row=0, column=0, sticky="w", padx=18, pady=(20, 4))
        self.state_lbl = ctk.CTkLabel(side, text="\u25cf Stopped", text_color="#e05252",
                                      font=ctk.CTkFont(size=13, weight="bold"))
        self.state_lbl.grid(row=1, column=0, sticky="w", padx=18, pady=(0, 14))

        self.page_names = ["Console", "Create Server", "Versions", "Settings", "Plugins"]
        self.nav_buttons = {}
        for i, name in enumerate(self.page_names):
            b = ctk.CTkButton(side, text=name, anchor="w", height=38, corner_radius=8,
                              fg_color="transparent", hover_color=("gray70", "gray28"),
                              command=lambda n=name: self.show_page(n))
            b.grid(row=2 + i, column=0, sticky="ew", padx=12, pady=2)
            self.nav_buttons[name] = b

        self.releases_var = ctk.BooleanVar(value=self.cfg.get("releases_only", True))
        ctk.CTkSwitch(side, text="Releases only", variable=self.releases_var,
                      command=self.on_filter_toggle).grid(row=9, column=0, sticky="w", padx=18, pady=(6, 4))
        ctk.CTkLabel(side, text="Off = include snapshots\nand old betas", text_color="gray",
                     justify="left", font=ctk.CTkFont(size=11)).grid(row=10, column=0, sticky="w", padx=18)
        ctk.CTkButton(side, text="Reload versions", height=32, command=self.load_catalogs
                      ).grid(row=11, column=0, sticky="ew", padx=14, pady=(8, 16))

        # ----- main area -----
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.grid(row=0, column=1, sticky="nsew", padx=14, pady=12)
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(1, weight=1)

        top = ctk.CTkFrame(main, corner_radius=10)
        top.grid(row=0, column=0, sticky="ew")
        top.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(top, text="Server folder", font=ctk.CTkFont(weight="bold")).grid(row=0, column=0, padx=(14, 8), pady=(12, 4))
        self.folder_var = ctk.StringVar()
        ctk.CTkEntry(top, textvariable=self.folder_var, height=32).grid(row=0, column=1, sticky="ew", padx=4, pady=(12, 4))
        ctk.CTkButton(top, text="Browse", width=80, height=32, command=self.browse_folder).grid(row=0, column=2, padx=4, pady=(12, 4))
        ctk.CTkButton(top, text="Load", width=70, height=32, command=self.load_server).grid(row=0, column=3, padx=(4, 14), pady=(12, 4))
        self.server_info = ctk.CTkLabel(top, text="No server loaded", anchor="w")
        self.server_info.grid(row=1, column=0, columnspan=4, sticky="ew", padx=14)
        self.java_warn = ctk.CTkLabel(top, text="", anchor="w", justify="left", wraplength=720)
        self.java_warn.grid(row=2, column=0, columnspan=4, sticky="ew", padx=14, pady=(0, 10))

        self.page_host = ctk.CTkFrame(main, fg_color="transparent")
        self.page_host.grid(row=1, column=0, sticky="nsew", pady=10)
        self.page_host.grid_columnconfigure(0, weight=1)
        self.page_host.grid_rowconfigure(0, weight=1)

        bottom = ctk.CTkFrame(main, corner_radius=10)
        bottom.grid(row=2, column=0, sticky="ew")
        bottom.grid_columnconfigure(0, weight=1)
        self.status_lbl = ctk.CTkLabel(bottom, text="Ready", anchor="w")
        self.status_lbl.grid(row=0, column=0, sticky="ew", padx=14, pady=(8, 2))
        self.progress = ctk.CTkProgressBar(bottom, height=8)
        self.progress.set(0)
        self.progress.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 10))

        self.pages = {}
        scroll_pages = {"Create Server", "Versions", "Settings"}
        for name in self.page_names:
            if name in scroll_pages:
                p = ctk.CTkScrollableFrame(self.page_host, corner_radius=10)
            else:
                p = ctk.CTkFrame(self.page_host, corner_radius=10)
            p.grid(row=0, column=0, sticky="nsew")
            p.grid_remove()
            self.pages[name] = p
        self._build_create(self.pages["Create Server"])
        self._build_update(self.pages["Versions"])
        self._build_config(self.pages["Settings"])
        self._build_plugins(self.pages["Plugins"])
        self._build_control(self.pages["Console"])
        self.show_page("Console")

    def show_page(self, name):
        for n, p in self.pages.items():
            if n == name:
                p.grid()
            else:
                p.grid_remove()
        for n, b in self.nav_buttons.items():
            b.configure(fg_color=("#3a7ebf", "#1f538d") if n == name else "transparent")

    def _build_create(self, t):
        t.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(t, text="Create a new server in the folder selected above.",
                     font=ctk.CTkFont(size=15, weight="bold")).grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=10)
        ctk.CTkLabel(t, text="Server type:").grid(row=1, column=0, sticky="w", padx=10, pady=6)
        self.create_type = ctk.CTkOptionMenu(t, values=TYPES, command=lambda _: self.refresh_version_lists())
        self.create_type.grid(row=1, column=1, sticky="w", padx=10)
        ctk.CTkLabel(t, text="Version:").grid(row=2, column=0, sticky="w", padx=10, pady=6)
        self.create_ver = ctk.CTkComboBox(t, values=["Loading..."], width=260, command=self.on_version_pick)
        self.create_ver.grid(row=2, column=1, sticky="w", padx=10)
        self.create_eula = ctk.CTkCheckBox(t, text="I accept the Minecraft EULA (writes eula=true)")
        self.create_eula.grid(row=3, column=0, columnspan=2, sticky="w", padx=10, pady=10)
        self.create_btn = ctk.CTkButton(t, text="Download && Create Server", command=self.create_server)
        self.create_btn.grid(row=4, column=0, columnspan=2, sticky="w", padx=10, pady=10)
        ctk.CTkLabel(t, text="Tip: you can also type a version id directly into the box.",
                     text_color="gray").grid(row=5, column=0, columnspan=3, sticky="w", padx=10)

    def _build_update(self, t):
        t.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(t, text="Change / Update Server Version",
                     font=ctk.CTkFont(size=15, weight="bold")).grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=10)
        self.upd_current = ctk.CTkLabel(t, text="Current: (no server loaded)", anchor="w")
        self.upd_current.grid(row=1, column=0, columnspan=3, sticky="w", padx=10)
        ctk.CTkLabel(t, text="Server type:").grid(row=2, column=0, sticky="w", padx=10, pady=6)
        self.upd_type = ctk.CTkOptionMenu(t, values=TYPES, command=lambda _: self.refresh_version_lists())
        self.upd_type.grid(row=2, column=1, sticky="w", padx=10)
        self.upd_auto_btn = ctk.CTkButton(t, text="Auto-Update to Latest", command=self.auto_update)
        self.upd_auto_btn.grid(row=3, column=0, columnspan=2, sticky="w", padx=10, pady=10)
        ctk.CTkLabel(t, text="Switch to version:").grid(row=4, column=0, sticky="w", padx=10, pady=6)
        self.upd_ver = ctk.CTkComboBox(t, values=["Loading..."], width=260, command=self.on_version_pick)
        self.upd_ver.grid(row=4, column=1, sticky="w", padx=10)
        self.upd_switch_btn = ctk.CTkButton(t, text="Switch to Selected Version", command=self.switch_version)
        self.upd_switch_btn.grid(row=5, column=0, columnspan=2, sticky="w", padx=10, pady=10)
        ctk.CTkLabel(t, justify="left", text_color="gray",
                     text=("Before every change the world folders are zipped into <server>/backups/ as\n"
                           "world_backup_<old>_to_<new>_<timestamp>.zip. server.properties, configs and\n"
                           "worlds are never touched. The old jar is kept in backups/ too.\n"
                           "Warning: downgrading a world can still corrupt chunks - restore the backup if needed.")
                     ).grid(row=6, column=0, columnspan=3, sticky="w", padx=10, pady=10)

    def _build_config(self, t):
        t.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(t, text="Java", font=ctk.CTkFont(size=15, weight="bold")).grid(row=0, column=0, sticky="w", padx=10, pady=(10, 4))
        jf = ctk.CTkFrame(t, fg_color="transparent")
        jf.grid(row=1, column=0, columnspan=3, sticky="ew", padx=10)
        jf.grid_columnconfigure(0, weight=1)
        self.java_combo = ctk.CTkComboBox(jf, values=["Detecting Java..."], command=self.on_java_pick)
        self.java_combo.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ctk.CTkButton(jf, text="Browse java.exe", width=120, command=self.browse_java).grid(row=0, column=1, padx=3)
        ctk.CTkButton(jf, text="Re-scan", width=80, command=self.detect_java).grid(row=0, column=2, padx=3)

        ctk.CTkLabel(t, text="server.properties", font=ctk.CTkFont(size=15, weight="bold")).grid(
            row=2, column=0, sticky="w", padx=10, pady=(16, 4))
        self.p_gamemode = ctk.CTkOptionMenu(t, values=["survival", "creative", "adventure", "spectator"])
        self.p_difficulty = ctk.CTkOptionMenu(t, values=["peaceful", "easy", "normal", "hard"])
        self.p_maxplayers = ctk.CTkEntry(t, width=100)
        self.p_online = ctk.CTkSwitch(t, text="Online Mode (off = cracked / offline)")
        self.view_frame = ctk.CTkFrame(t, fg_color="transparent")
        self.p_view_lbl = ctk.CTkLabel(self.view_frame, text="10", width=30)
        self.p_view = ctk.CTkSlider(self.view_frame, from_=2, to=32, number_of_steps=30, width=250,
                                    command=lambda v: self.p_view_lbl.configure(text=str(int(v))))
        self.p_motd = ctk.CTkEntry(t)
        rows = [("Gamemode", self.p_gamemode), ("Difficulty", self.p_difficulty),
                ("Max Players", self.p_maxplayers), ("Online Mode", self.p_online), ("Server Name (MOTD)", self.p_motd)]
        r = 3
        for label, w in rows:
            ctk.CTkLabel(t, text=label).grid(row=r, column=0, sticky="w", padx=10, pady=5)
            w.grid(row=r, column=1, sticky="ew" if w is self.p_motd else "w", padx=10)
            r += 1
        ctk.CTkLabel(t, text="View Distance").grid(row=r, column=0, sticky="w", padx=10, pady=5)
        self.view_frame.grid(row=r, column=1, sticky="w", padx=10)
        self.p_view.pack(side="left")
        self.p_view_lbl.pack(side="left", padx=8)
        r += 1
        ctk.CTkLabel(t, text="Server Icon").grid(row=r, column=0, sticky="w", padx=10, pady=5)
        iconf = ctk.CTkFrame(t, fg_color="transparent")
        iconf.grid(row=r, column=1, sticky="w", padx=10)
        self.icon_preview = ctk.CTkLabel(iconf, text="(none)", width=56)
        self.icon_preview.pack(side="left")
        ctk.CTkButton(iconf, text="Choose image...", width=120, command=self.choose_icon).pack(side="left", padx=6)
        ctk.CTkButton(iconf, text="Remove", width=80, command=self.remove_icon).pack(side="left")
        r += 1
        self.cfg_eula = ctk.CTkCheckBox(t, text="Accept Minecraft EULA (eula=true)")
        self.cfg_eula.grid(row=r, column=0, columnspan=2, sticky="w", padx=10, pady=10)
        r += 1
        self.save_cfg_btn = ctk.CTkButton(t, text="Save Configuration", command=self.save_configuration)
        self.save_cfg_btn.grid(row=r, column=0, columnspan=2, sticky="w", padx=10, pady=6)
        self._set_props_widgets(DEFAULT_PROPS)

    def _build_control(self, t):
        t.grid_columnconfigure(0, weight=1)
        t.grid_rowconfigure(2, weight=1)
        ctrl = ctk.CTkFrame(t)
        ctrl.grid(row=0, column=0, sticky="ew", pady=(4, 4))
        ctrl.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(ctrl, text="RAM (-Xms / -Xmx):").grid(row=0, column=0, padx=10, pady=8)
        self.ram_slider = ctk.CTkSlider(ctrl, from_=2, to=16, number_of_steps=14, command=self.on_ram)
        self.ram_slider.set(self.cfg.get("ram", 4))
        self.ram_slider.grid(row=0, column=1, sticky="ew", padx=8)
        self.ram_lbl = ctk.CTkLabel(ctrl, text=f"{int(self.cfg.get('ram', 4))} GB", width=60)
        self.ram_lbl.grid(row=0, column=2, padx=6)
        self.start_btn = ctk.CTkButton(ctrl, text="Start", width=90, fg_color="#2e8b57", hover_color="#236b43", command=self.start_server)
        self.stop_btn = ctk.CTkButton(ctrl, text="Stop", width=90, fg_color="#b03a3a", hover_color="#8a2d2d", command=self.stop_server)
        self.restart_btn = ctk.CTkButton(ctrl, text="Restart", width=90, command=self.restart_server)
        self.start_btn.grid(row=0, column=3, padx=4)
        self.stop_btn.grid(row=0, column=4, padx=4)
        self.restart_btn.grid(row=0, column=5, padx=(4, 10))

        net = ctk.CTkFrame(t)
        net.grid(row=1, column=0, sticky="ew", pady=4)
        self.local_ip_lbl = ctk.CTkLabel(net, text="Local IP: ...")
        self.local_ip_lbl.grid(row=0, column=0, padx=10, pady=6)
        ctk.CTkButton(net, text="Copy", width=60, command=lambda: self.copy_text(self.local_ip)).grid(row=0, column=1, padx=4)
        self.public_ip_lbl = ctk.CTkLabel(net, text="Public IP: ...")
        self.public_ip_lbl.grid(row=0, column=2, padx=(24, 10))
        ctk.CTkButton(net, text="Copy", width=60, command=lambda: self.copy_text(self.public_ip)).grid(row=0, column=3, padx=4)
        ctk.CTkButton(net, text="Refresh", width=70, command=self.refresh_network).grid(row=0, column=4, padx=8)
        self.local_ip = ""
        self.public_ip = ""

        self.console = ctk.CTkTextbox(t, font=ctk.CTkFont(family="Consolas", size=12), wrap="none")
        self.console.grid(row=2, column=0, sticky="nsew", pady=4)
        self.console.configure(state="disabled")

        cmdf = ctk.CTkFrame(t, fg_color="transparent")
        cmdf.grid(row=3, column=0, sticky="ew")
        cmdf.grid_columnconfigure(0, weight=1)
        self.cmd_entry = ctk.CTkEntry(cmdf, placeholder_text="Type a command (op <name>, gamemode creative <name>, stop ...) and press Enter")
        self.cmd_entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.cmd_entry.bind("<Return>", lambda e: self.send_command())
        ctk.CTkButton(cmdf, text="Send", width=80, command=self.send_command).grid(row=0, column=1)

        bf = ctk.CTkFrame(t)
        bf.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        bf.grid_columnconfigure(0, weight=1)
        self.bc_entry = ctk.CTkEntry(bf, placeholder_text="Message to all players (e.g. We need to restart the server)")
        self.bc_entry.grid(row=0, column=0, sticky="ew", padx=(8, 6), pady=6)
        self.bc_entry.bind("<Return>", lambda e: self.broadcast())
        ctk.CTkButton(bf, text="Broadcast", width=90, command=self.broadcast).grid(row=0, column=1, padx=3)
        self.bc_presets = {
            "Presets...": "",
            "Restart soon": "We need to restart the server soon!",
            "Restart in 5 min": "The server will restart in 5 minutes.",
            "Updating": "The server is being updated. Back in a few minutes!",
            "Lag warning": "Server is lagging, we are working on it.",
        }
        ctk.CTkOptionMenu(bf, values=list(self.bc_presets), width=130, command=self.pick_preset).grid(row=0, column=2, padx=3)
        ctk.CTkButton(bf, text="Warn & Restart (30s)", width=150, fg_color="#c27c1a", hover_color="#9a6214",
                      command=self.warn_restart).grid(row=0, column=3, padx=(3, 8))
        self.refresh_buttons()

    # ---------------- helpers: state ----------------
    @property
    def folder(self):
        return self.folder_var.get().strip()

    def jar_path(self):
        return os.path.join(self.folder, "server.jar")

    def refresh_buttons(self):
        running = self.proc is not None and self.proc.poll() is None
        self.start_btn.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")
        self.restart_btn.configure(state="normal" if running else "disabled")
        if hasattr(self, "state_lbl"):
            self.state_lbl.configure(text="\u25cf Running" if running else "\u25cf Stopped",
                                     text_color="#4cc47c" if running else "#e05252")

    def copy_text(self, text):
        if not text:
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status_lbl.configure(text=f"Copied: {text}")

    def browse_folder(self):
        d = filedialog.askdirectory(title="Select server folder (any drive)")
        if d:
            self.folder_var.set(os.path.normpath(d))
            self.load_server()

    # ---------------- version catalogs ----------------
    def load_catalogs(self):
        def work():
            self.set_status("Fetching live version lists...")
            errors = []
            for name, fn in (("Vanilla", fetch_vanilla_catalog), ("Paper", fetch_paper_catalog), ("Fabric", fetch_fabric_catalog)):
                try:
                    self.catalog[name] = fn()
                except Exception as e:
                    errors.append(f"{name}: {e}")
            self.ui(self.refresh_version_lists)
            self.set_status("Version lists loaded." if not errors else "Some lists failed: " + "; ".join(errors))
        threading.Thread(target=work, daemon=True).start()

    def list_versions(self, typ):
        cat = self.catalog.get(typ)
        if not cat:
            return []
        rel = self.releases_var.get()
        if typ == "Vanilla":
            return [v["id"] for v in cat["versions"] if not rel or v["type"] == "release"]
        if typ == "Paper":
            return [v for v in cat["versions"] if not rel or "-" not in v]
        return [g["version"] for g in cat["game"] if not rel or g.get("stable")]

    def refresh_version_lists(self):
        for typ_menu, combo in ((self.create_type, self.create_ver), (self.upd_type, self.upd_ver)):
            vers = self.list_versions(typ_menu.get())
            combo.configure(values=vers or ["(unavailable)"])
            if combo.get() not in vers:
                combo.set(vers[0] if vers else "(unavailable)")

    def on_filter_toggle(self):
        self.cfg["releases_only"] = self.releases_var.get()
        save_config(self.cfg)
        self.refresh_version_lists()

    def on_version_pick(self, version):
        self.java_ctx_version = version
        self.update_java_warning()

    def resolve(self, typ, version):
        if typ == "Vanilla":
            return resolve_vanilla(version, self.catalog["Vanilla"])
        if typ == "Paper":
            return resolve_paper(version)
        return resolve_fabric(version, self.catalog["Fabric"])

    def latest_version(self, typ):
        cat = self.catalog.get(typ)
        if not cat:
            raise RuntimeError(f"{typ} version list is not loaded. Click 'Reload versions'.")
        if typ == "Vanilla":
            return cat["latest"]["release"]
        if typ == "Paper":
            return next(v for v in cat["versions"] if "-" not in v)
        return next(g["version"] for g in cat["game"] if g.get("stable"))

    # ---------------- Java ----------------
    def detect_java(self):
        def work():
            self.set_status("Scanning for Java installations...")
            found = find_java_installs()
            self.ui(lambda: self._apply_java(found))
        threading.Thread(target=work, daemon=True).start()

    def _apply_java(self, found):
        self.java_map = {}
        for major, path in found:
            self.java_map[f"Java {major}  -  {path}"] = path
        saved = self.cfg.get("java_path")
        if saved and os.path.isfile(saved) and saved not in self.java_map.values():
            major = probe_java(saved)
            if major:
                self.java_map[f"Java {major}  -  {saved}"] = saved
        values = list(self.java_map) or ["No Java found - use Browse java.exe"]
        self.java_combo.configure(values=values)
        pick = next((k for k, v in self.java_map.items() if v == saved), values[0])
        self.java_combo.set(pick)
        self.on_java_pick(pick)
        self.status_lbl.configure(text=f"Found {len(found)} Java installation(s).")

    def browse_java(self):
        p = filedialog.askopenfilename(title="Select java.exe", filetypes=[("java.exe", "java.exe"), ("Executables", "*.exe")])
        if not p:
            return
        major = probe_java(p)
        if not major:
            messagebox.showerror("Invalid Java", "That file could not be run as Java.")
            return
        label = f"Java {major}  -  {os.path.normpath(p)}"
        self.java_map[label] = os.path.normpath(p)
        self.java_combo.configure(values=list(self.java_map))
        self.java_combo.set(label)
        self.on_java_pick(label)

    def on_java_pick(self, label):
        path = self.java_map.get(label)
        if path:
            self.cfg["java_path"] = path
            save_config(self.cfg)
        self.update_java_warning()

    def selected_java(self):
        return self.java_map.get(self.java_combo.get())

    def update_java_warning(self):
        ver = self.java_ctx_version or self.meta.get("version")
        path = self.selected_java()
        if not ver or ver.startswith("("):
            self.java_warn.configure(text="")
            return
        if not path:
            self.java_warn.configure(text="No Java selected. Choose one in Settings.", text_color="orange")
            return
        have = probe_java(path) or 0
        need = required_java(ver)
        if have < need:
            self.java_warn.configure(
                text=f"WARNING: Minecraft {ver} needs Java {need}+ but the selected Java is {have}. "
                     f"Select a newer Java in Settings.", text_color="#ff6b6b")
        elif need <= 8 and have >= 17:
            self.java_warn.configure(
                text=f"Note: Minecraft {ver} is an old version (Java 8 recommended); Java {have} may cause problems.",
                text_color="orange")
        else:
            self.java_warn.configure(text=f"Java {have} is compatible with Minecraft {ver} (needs {need}+).",
                                     text_color="#5fd38d")

    # ---------------- load / meta ----------------
    def load_server(self):
        folder = self.folder
        if not folder:
            return
        self.cfg["last_folder"] = folder
        save_config(self.cfg)
        self.meta = {}
        mp = os.path.join(folder, META_FILE)
        if os.path.isfile(mp):
            try:
                with open(mp, "r", encoding="utf-8") as f:
                    self.meta = json.load(f)
            except Exception:
                self.meta = {}
        has_jar = os.path.isfile(self.jar_path())
        if self.meta:
            build = f" (build {self.meta['build']})" if self.meta.get("build") else ""
            txt = f"{self.meta.get('type')} {self.meta.get('version')}{build}"
            if self.meta.get("type") in TYPES:
                self.upd_type.set(self.meta["type"])
                self.refresh_version_lists()
        elif has_jar:
            txt = "server.jar found (type/version unknown - pick a type on the Versions page)"
        else:
            txt = "No server.jar in this folder yet - use 'Create Server'"
        self.server_info.configure(text=f"Server: {txt}")
        self.upd_current.configure(text=f"Current: {txt}")
        props = dict(DEFAULT_PROPS)
        props.update(read_properties(os.path.join(folder, "server.properties")))
        self._set_props_widgets(props)
        eula = os.path.join(folder, "eula.txt")
        accepted = False
        if os.path.isfile(eula):
            with open(eula, "r", encoding="utf-8", errors="replace") as f:
                accepted = bool(re.search(r"^\s*eula\s*=\s*true", f.read(), re.M | re.I))
        (self.cfg_eula.select if accepted else self.cfg_eula.deselect)()
        self.java_ctx_version = self.meta.get("version")
        self.update_java_warning()
        self.refresh_installed_plugins()
        self.refresh_icon_preview()

    # ---------------- server icon ----------------
    def icon_path(self):
        return os.path.join(self.folder, "server-icon.png")

    def refresh_icon_preview(self):
        path = self.icon_path()
        try:
            if os.path.isfile(path) and Image is not None:
                img = Image.open(path).convert("RGBA").resize((48, 48))
                self._icon_img = ctk.CTkImage(img, size=(48, 48))
                self.icon_preview.configure(image=self._icon_img, text="")
            else:
                self.icon_preview.configure(image=None, text="(set)" if os.path.isfile(path) else "(none)")
        except Exception:
            self.icon_preview.configure(text="(set)" if os.path.isfile(path) else "(none)")

    def choose_icon(self):
        if not self.folder or not os.path.isdir(self.folder):
            messagebox.showwarning("Folder", "Select a server folder first.")
            return
        src = filedialog.askopenfilename(title="Choose server icon",
                                         filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.gif"), ("All files", "*.*")])
        if not src:
            return
        try:
            if Image is not None:
                img = Image.open(src).convert("RGBA")
                side = min(img.size)
                left, top = (img.width - side) // 2, (img.height - side) // 2
                img = img.crop((left, top, left + side, top + side)).resize((64, 64), Image.LANCZOS)
                img.save(self.icon_path(), "PNG")
            else:
                import struct
                with open(src, "rb") as f:
                    data = f.read()
                if data[:8] != b"\x89PNG\r\n\x1a\n" or struct.unpack(">II", data[16:24]) != (64, 64):
                    raise RuntimeError("Without Pillow the image must be a 64x64 PNG.\n"
                                       "Run 'pip install pillow' to allow any image to be resized automatically.")
                shutil.copyfile(src, self.icon_path())
        except Exception as e:
            messagebox.showerror("Icon", str(e))
            return
        self.refresh_icon_preview()
        self.status_lbl.configure(text="Server icon set (restart the server and refresh the server list).")

    def remove_icon(self):
        try:
            if os.path.isfile(self.icon_path()):
                os.remove(self.icon_path())
        except OSError as e:
            messagebox.showerror("Icon", str(e))
        self.refresh_icon_preview()
    # ---------------- plugins (Modrinth) ----------------
    def _build_plugins(self, t):
        t.grid_columnconfigure(0, weight=1)
        t.grid_rowconfigure(2, weight=3)
        t.grid_rowconfigure(4, weight=2)
        self.pl_info = ctk.CTkLabel(t, text="", anchor="w", text_color="gray")
        self.pl_info.grid(row=0, column=0, sticky="ew", padx=6, pady=(4, 2))
        sf = ctk.CTkFrame(t, fg_color="transparent")
        sf.grid(row=1, column=0, sticky="ew", padx=6)
        sf.grid_columnconfigure(0, weight=1)
        self.pl_query = ctk.CTkEntry(sf, placeholder_text="Search plugins (e.g. EssentialsX, LuckPerms, WorldEdit) - empty = most downloaded")
        self.pl_query.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.pl_query.bind("<Return>", lambda e: self.plugin_search())
        ctk.CTkButton(sf, text="Search", width=90, command=self.plugin_search).grid(row=0, column=1)
        self.pl_results = ctk.CTkScrollableFrame(t, label_text="Search results (Modrinth)")
        self.pl_results.grid(row=2, column=0, sticky="nsew", padx=6, pady=4)
        self.pl_results.grid_columnconfigure(0, weight=1)
        hf = ctk.CTkFrame(t, fg_color="transparent")
        hf.grid(row=3, column=0, sticky="ew", padx=6)
        ctk.CTkLabel(hf, text="Installed", font=ctk.CTkFont(weight="bold")).pack(side="left")
        ctk.CTkButton(hf, text="Open folder", width=90, command=self.open_plugin_folder).pack(side="right")
        ctk.CTkButton(hf, text="Refresh", width=70, command=self.refresh_installed_plugins).pack(side="right", padx=6)
        self.pl_installed = ctk.CTkScrollableFrame(t)
        self.pl_installed.grid(row=4, column=0, sticky="nsew", padx=6, pady=(2, 4))
        self.pl_installed.grid_columnconfigure(0, weight=1)

    def loader_info(self):
        typ = self.meta.get("type")
        if typ == "Paper":
            return "plugins", ["paper", "spigot", "bukkit"]
        if typ == "Fabric":
            return "mods", ["fabric"]
        return None, None

    def _clear(self, frame):
        for w in frame.winfo_children():
            w.destroy()

    def plugin_search(self):
        kind, loaders = self.loader_info()
        if not loaders:
            messagebox.showinfo("Plugins", "Load a Paper (plugins) or Fabric (mods) server first.\nVanilla servers do not support plugins.")
            return
        q = self.pl_query.get().strip()
        ver = self.meta.get("version")
        facets = [[f"categories:{l}" for l in loaders]]
        if kind == "mods":
            facets.append(["project_type:mod"])
        if ver:
            facets.append([f"versions:{ver}"])
        params = {"query": q, "facets": json.dumps(facets), "limit": 30,
                  "index": "relevance" if q else "downloads"}
        self.set_status("Searching Modrinth...")

        def work():
            try:
                r = requests.get(f"{MODRINTH_API}/search", params=params, headers=HEADERS, timeout=20)
                r.raise_for_status()
                hits = r.json().get("hits", [])
            except Exception as e:
                self.set_status(f"Search failed: {e}")
                return
            self.ui(lambda: self._show_results(hits, kind))
        threading.Thread(target=work, daemon=True).start()

    def _show_results(self, hits, kind):
        self._clear(self.pl_results)
        self.set_status(f"{len(hits)} result(s).")
        if not hits:
            ctk.CTkLabel(self.pl_results, text="Nothing found for this Minecraft version.").grid(row=0, column=0, pady=10)
        for i, h in enumerate(hits):
            row = ctk.CTkFrame(self.pl_results)
            row.grid(row=i, column=0, sticky="ew", pady=3, padx=2)
            row.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(row, anchor="w", font=ctk.CTkFont(weight="bold"),
                         text=f"{h['title']}  by {h.get('author', '?')}  -  {h.get('downloads', 0):,} downloads"
                         ).grid(row=0, column=0, sticky="ew", padx=8, pady=(4, 0))
            ctk.CTkLabel(row, anchor="w", justify="left", wraplength=620, text_color="gray",
                         text=h.get("description", "")).grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 4))
            ctk.CTkButton(row, text="Install", width=80,
                          command=lambda hit=h: self.plugin_install(hit, kind)).grid(row=0, column=1, rowspan=2, padx=8)

    def plugin_install(self, hit, kind):
        if not self.folder or not os.path.isdir(self.folder):
            messagebox.showwarning("Folder", "Select a server folder first.")
            return
        _, loaders = self.loader_info()
        ver = self.meta.get("version")
        folder = self.folder

        def work():
            params = {"loaders": json.dumps(loaders)}
            if ver:
                params["game_versions"] = json.dumps([ver])
            r = requests.get(f"{MODRINTH_API}/project/{hit['project_id']}/version", params=params, headers=HEADERS, timeout=20)
            r.raise_for_status()
            versions = r.json()
            if not versions:
                raise RuntimeError(f"{hit['title']} has no build for Minecraft {ver}.")
            v = versions[0]
            files = v["files"]
            f = next((x for x in files if x.get("primary")), files[0])
            dest_dir = os.path.join(folder, kind)
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, os.path.basename(f["filename"]))
            self.download(f["url"], dest + ".part", f"Downloading {hit['title']}")
            os.replace(dest + ".part", dest)
            msg = f"Installed {f['filename']} into /{kind}.\nRestart the server to load it."
            dep_ids = [d["project_id"] for d in v.get("dependencies", [])
                       if d.get("dependency_type") == "required" and d.get("project_id")]
            if dep_ids:
                try:
                    rr = requests.get(f"{MODRINTH_API}/projects", params={"ids": json.dumps(dep_ids)}, headers=HEADERS, timeout=20)
                    names = ", ".join(p["title"] for p in rr.json())
                except Exception:
                    names = "(unknown)"
                msg += f"\n\nThis plugin also REQUIRES: {names}\nSearch and install those too."
            self.set_progress(1.0, f"Installed {hit['title']}.")
            self.ui(self.refresh_installed_plugins)
            self.ui(lambda: messagebox.showinfo("Installed", msg))
        self.run_task(work, f"Installing {hit['title']}...")

    def refresh_installed_plugins(self):
        kind, _ = self.loader_info()
        self._clear(self.pl_installed)
        if not kind:
            self.pl_info.configure(text="Plugins need a Paper server (or Fabric for mods). Load one in the folder box above.")
            return
        self.pl_info.configure(text=f"{self.meta.get('type')} {self.meta.get('version')} - installing into /{kind}")
        d = os.path.join(self.folder, kind)
        jars = sorted(f for f in os.listdir(d) if f.lower().endswith(".jar")) if os.path.isdir(d) else []
        if not jars:
            ctk.CTkLabel(self.pl_installed, text="None installed yet.").grid(row=0, column=0, pady=6)
        for i, name in enumerate(jars):
            ctk.CTkLabel(self.pl_installed, text=name, anchor="w").grid(row=i, column=0, sticky="ew", padx=8, pady=2)
            ctk.CTkButton(self.pl_installed, text="Remove", width=70, fg_color="#b03a3a", hover_color="#8a2d2d",
                          command=lambda n=name: self.remove_plugin(kind, n)).grid(row=i, column=1, padx=6)

    def remove_plugin(self, kind, name):
        if not messagebox.askyesno("Remove", f"Delete {name}?"):
            return
        try:
            os.remove(os.path.join(self.folder, kind, name))
        except OSError as e:
            messagebox.showerror("Error", f"Could not delete (stop the server first?):\n{e}")
        self.refresh_installed_plugins()

    def open_plugin_folder(self):
        kind, _ = self.loader_info()
        if not kind:
            return
        d = os.path.join(self.folder, kind)
        os.makedirs(d, exist_ok=True)
        os.startfile(d)

    def write_meta(self, folder, typ, info):
        with open(os.path.join(folder, META_FILE), "w", encoding="utf-8") as f:
            json.dump({"type": typ, "version": info["version"], "build": info["build"]}, f, indent=2)

    def write_eula(self, folder, accepted):
        with open(os.path.join(folder, "eula.txt"), "w", encoding="utf-8") as f:
            f.write("# Generated by Minecraft Server Manager (https://aka.ms/MinecraftEULA)\n")
            f.write(f"eula={'true' if accepted else 'false'}\n")

    # ---------------- create ----------------
    def create_server(self):
        folder, typ, ver = self.folder, self.create_type.get(), self.create_ver.get().strip()
        if not folder:
            messagebox.showwarning("Folder", "Select an installation folder first.")
            return
        if ver.startswith("(") or ver == "Loading...":
            messagebox.showwarning("Version", "Version list is not available yet.")
            return
        if os.path.isfile(os.path.join(folder, "server.jar")):
            messagebox.showwarning("Exists", "This folder already contains a server.jar. Use the Versions page.")
            return
        if self.proc is not None and self.proc.poll() is None:
            messagebox.showwarning("Running", "Stop the running server first.")
            return
        eula = bool(self.create_eula.get())

        def work():
            self.set_status(f"Resolving {typ} {ver}...")
            info = self.resolve(typ, ver)
            os.makedirs(folder, exist_ok=True)
            part = os.path.join(folder, "server.jar.part")
            self.download(info["url"], part, f"Downloading {typ} {ver}")
            os.replace(part, os.path.join(folder, "server.jar"))
            self.write_meta(folder, typ, info)
            if eula:
                self.write_eula(folder, True)
            self.set_progress(1.0, f"Server {typ} {ver} created in {folder}")
            self.ui(self.load_server)
            self.ui(lambda: self.show_page("Console"))
            self.ui(lambda: messagebox.showinfo("Done", f"{typ} {ver} server created.\nPress Start on the Console page."))
        self.run_task(work, f"Creating {typ} {ver} server...")

    # ---------------- update / switch ----------------
    def _check_change_allowed(self):
        if not self.folder or not os.path.isfile(self.jar_path()):
            messagebox.showwarning("No server", "Load a folder that contains a server.jar first.")
            return False
        if self.proc is not None and self.proc.poll() is None:
            messagebox.showwarning("Running", "Stop the server before changing its version.")
            return False
        if not self.catalog:
            messagebox.showwarning("Versions", "Version lists are not loaded yet.")
            return False
        return True

    def auto_update(self):
        if not self._check_change_allowed():
            return
        typ = self.upd_type.get()
        if not messagebox.askyesno("Auto-Update", f"Check for the latest {typ} version and update?\n"
                                   "Your worlds will be backed up first."):
            return
        self.run_task(lambda: self._change_version(typ, None), f"Checking latest {typ} version...")

    def switch_version(self):
        if not self._check_change_allowed():
            return
        typ, ver = self.upd_type.get(), self.upd_ver.get().strip()
        if ver.startswith("("):
            return
        old = self.meta.get("version", "unknown")
        if not messagebox.askyesno("Switch version", f"Switch {typ} server from {old} to {ver}?\n"
                                   "Your worlds will be backed up first."):
            return
        self.run_task(lambda: self._change_version(typ, ver), f"Switching to {typ} {ver}...")

    def _change_version(self, typ, ver):
        folder = self.folder
        latest = ver is None
        if latest:
            ver = self.latest_version(typ)
        self.set_status(f"Resolving {typ} {ver}...")
        info = self.resolve(typ, ver)
        old_ver = self.meta.get("version", "unknown")
        same_type = self.meta.get("type") == typ
        if same_type and old_ver == info["version"] and str(self.meta.get("build", "")) == str(info["build"]):
            self.set_progress(1.0, f"Already up to date ({typ} {old_ver}).")
            self.ui(lambda: messagebox.showinfo("Up to date", f"Your {typ} server is already on the newest version/build ({old_ver})."))
            return
        part = os.path.join(folder, "server.jar.part")
        self.download(info["url"], part, f"Downloading {typ} {info['version']}")
        backup = self.backup_worlds(folder, old_ver, info["version"])
        bdir = os.path.join(folder, "backups")
        os.makedirs(bdir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(self.jar_path(), os.path.join(bdir, f"server_{safe_name(old_ver)}_{stamp}.jar"))
        os.replace(part, self.jar_path())
        self.write_meta(folder, typ, info)
        msg = f"Server updated: {old_ver} -> {info['version']}."
        if backup:
            msg += f"\nWorld backup: {os.path.basename(backup)}"
        else:
            msg += "\n(No world folder found, so no backup was needed.)"
        self.set_progress(1.0, f"Updated to {typ} {info['version']}.")
        self.ui(self.load_server)
        self.ui(lambda: messagebox.showinfo("Done", msg))

    def backup_worlds(self, folder, old_ver, new_ver):
        level = read_properties(os.path.join(folder, "server.properties")).get("level-name", "world")
        dirs = [d for d in (level, level + "_nether", level + "_the_end") if os.path.isdir(os.path.join(folder, d))]
        if not dirs:
            return None
        files = []
        for d in dirs:
            for root, _, names in os.walk(os.path.join(folder, d)):
                for n in names:
                    if n == "session.lock":
                        continue
                    full = os.path.join(root, n)
                    files.append((full, os.path.relpath(full, folder)))
        bdir = os.path.join(folder, "backups")
        os.makedirs(bdir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(bdir, f"world_backup_{safe_name(old_ver)}_to_{safe_name(new_ver)}_{stamp}.zip")
        total = max(len(files), 1)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, (full, arc) in enumerate(files, 1):
                try:
                    zf.write(full, arc)
                except (PermissionError, OSError):
                    pass
                if i % 20 == 0 or i == total:
                    self.set_progress(i / total, f"Backing up world: {i}/{total} files")
        return path

    # ---------------- configuration ----------------
    def _set_props_widgets(self, p):
        self.p_gamemode.set(p.get("gamemode", "survival"))
        self.p_difficulty.set(p.get("difficulty", "easy"))
        self.p_maxplayers.delete(0, "end")
        self.p_maxplayers.insert(0, p.get("max-players", "20"))
        (self.p_online.select if p.get("online-mode", "true").lower() == "true" else self.p_online.deselect)()
        try:
            vd = int(p.get("view-distance", "10"))
        except ValueError:
            vd = 10
        self.p_view.set(max(2, min(32, vd)))
        self.p_view_lbl.configure(text=str(max(2, min(32, vd))))
        self.p_motd.delete(0, "end")
        self.p_motd.insert(0, p.get("motd", "A Minecraft Server"))

    def save_configuration(self):
        if not self.folder:
            messagebox.showwarning("Folder", "Select a server folder first.")
            return
        mp = self.p_maxplayers.get().strip()
        if not mp.isdigit() or int(mp) < 1:
            messagebox.showerror("Invalid", "Max Players must be a positive number.")
            return
        os.makedirs(self.folder, exist_ok=True)
        write_properties(os.path.join(self.folder, "server.properties"), {
            "gamemode": self.p_gamemode.get(),
            "difficulty": self.p_difficulty.get(),
            "max-players": mp,
            "online-mode": "true" if self.p_online.get() else "false",
            "view-distance": str(int(self.p_view.get())),
            "motd": self.p_motd.get(),
        })
        self.write_eula(self.folder, bool(self.cfg_eula.get()))
        self.status_lbl.configure(text="Configuration saved (restart the server to apply).")

    # ---------------- network ----------------
    def refresh_network(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            self.local_ip = s.getsockname()[0]
            s.close()
        except Exception:
            self.local_ip = "127.0.0.1"
        self.local_ip_lbl.configure(text=f"Local IP: {self.local_ip}")
        self.public_ip_lbl.configure(text="Public IP: ...")

        def work():
            try:
                ip = requests.get("https://api.ipify.org", headers=HEADERS, timeout=10).text.strip()
            except Exception:
                ip = ""
            def apply():
                self.public_ip = ip
                self.public_ip_lbl.configure(text=f"Public IP: {ip or 'unavailable'}")
            self.ui(apply)
        threading.Thread(target=work, daemon=True).start()

    # ---------------- server process ----------------
    def on_ram(self, v):
        gb = int(round(v))
        self.ram_lbl.configure(text=f"{gb} GB")
        self.cfg["ram"] = gb
        save_config(self.cfg)

    def log(self, line):
        box = self.console
        at_bottom = box.yview()[1] > 0.98
        box.configure(state="normal")
        box.insert("end", line if line.endswith("\n") else line + "\n")
        if int(box.index("end-1c").split(".")[0]) > 6000:
            box.delete("1.0", "1500.0")
        box.configure(state="disabled")
        if at_bottom:
            box.see("end")

    def start_server(self):
        if self.proc is not None and self.proc.poll() is None:
            return
        if not self.folder or not os.path.isfile(self.jar_path()):
            messagebox.showwarning("No server", "No server.jar found in the selected folder.")
            return
        eula_path = os.path.join(self.folder, "eula.txt")
        accepted = False
        if os.path.isfile(eula_path):
            with open(eula_path, "r", encoding="utf-8", errors="replace") as f:
                accepted = bool(re.search(r"^\s*eula\s*=\s*true", f.read(), re.M | re.I))
        if not accepted:
            if not messagebox.askyesno("EULA", "You must accept the Minecraft EULA (https://aka.ms/MinecraftEULA) to run the server.\nAccept it now?"):
                return
            self.write_eula(self.folder, True)
            self.cfg_eula.select()
        java = self.selected_java() or "java"
        gb = int(round(self.ram_slider.get()))
        cmd = [java, f"-Xms{gb}G", f"-Xmx{gb}G", "-jar", "server.jar", "nogui"]
        try:
            proc = subprocess.Popen(cmd, cwd=self.folder, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                    bufsize=1, creationflags=NO_WINDOW)
        except Exception as e:
            messagebox.showerror("Start failed", f"Could not launch Java:\n{e}")
            return
        self.proc = proc
        self.log(f"[Manager] Starting: {' '.join(cmd)}")
        threading.Thread(target=self._reader, args=(proc,), daemon=True).start()
        self.refresh_buttons()
        self.status_lbl.configure(text="Server starting...")

    def _reader(self, proc):
        try:
            for line in proc.stdout:
                self.ui(lambda l=line.rstrip("\r\n"): self.log(l))
        except Exception:
            pass
        code = proc.wait()
        self.ui(lambda: self._on_exit(proc, code))

    def _on_exit(self, proc, code):
        if self.proc is proc:
            self.proc = None
        self.log(f"[Manager] Server process exited with code {code}.")
        self.status_lbl.configure(text="Server stopped.")
        self.refresh_buttons()
        if self.restart_pending:
            self.restart_pending = False
            self.start_server()

    def send_raw(self, text):
        p = self.proc
        if p is None or p.poll() is not None or p.stdin is None:
            return False
        try:
            p.stdin.write(text + "\n")
            p.stdin.flush()
            return True
        except Exception:
            return False

    def send_command(self):
        cmd = self.cmd_entry.get().strip()
        if not cmd:
            return
        if self.send_raw(cmd):
            self.log(f"> {cmd}")
        else:
            self.log("[Manager] Server is not running.")
        self.cmd_entry.delete(0, "end")

    def pick_preset(self, name):
        text = self.bc_presets.get(name, "")
        if text:
            self.bc_entry.delete(0, "end")
            self.bc_entry.insert(0, text)

    def _running(self):
        return self.proc is not None and self.proc.poll() is None

    def broadcast_text(self, msg):
        payload = json.dumps({"text": f"[Server] {msg}", "color": "gold"})
        if self.send_raw(f"tellraw @a {payload}"):
            self.log(f"[Manager] Broadcast: {msg}")
            return True
        return False

    def broadcast(self):
        msg = self.bc_entry.get().strip()
        if not msg:
            return
        if not self._running():
            messagebox.showinfo("Broadcast", "The server is not running.")
            return
        self.broadcast_text(msg)
        self.bc_entry.delete(0, "end")

    def warn_restart(self):
        if not self._running():
            messagebox.showinfo("Restart", "The server is not running.")
            return
        if not messagebox.askyesno("Warn & Restart", "Warn players now and restart the server in 30 seconds?"):
            return
        steps = [(0, "Server restarting in 30 seconds!"), (20000, "Server restarting in 10 seconds!"),
                 (25000, "Server restarting in 5 seconds!")]
        for delay, text in steps:
            self.after(delay, lambda t=text: self._running() and self.broadcast_text(t))
        self.after(30000, lambda: self._running() and self.restart_server())
        self.status_lbl.configure(text="Restart countdown started (30s).")

    def stop_server(self):
        p = self.proc
        if p is None or p.poll() is not None:
            return
        self.send_raw("stop")
        self.status_lbl.configure(text="Stopping server...")

        def watchdog():
            try:
                p.wait(timeout=45)
            except subprocess.TimeoutExpired:
                p.kill()
        threading.Thread(target=watchdog, daemon=True).start()

    def restart_server(self):
        if self.proc is None or self.proc.poll() is not None:
            self.start_server()
            return
        self.restart_pending = True
        self.stop_server()

    def on_close(self):
        p = self.proc
        if p is not None and p.poll() is None:
            if not messagebox.askyesno("Quit", "The server is still running. Stop it and quit?"):
                return
            self.send_raw("stop")
            try:
                p.wait(timeout=20)
            except Exception:
                p.kill()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
