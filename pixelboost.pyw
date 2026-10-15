"""
PixelBoost
----------
Ablauf:
  1. Launcher-Fenster öffnet sich und fragt Firebase (Realtime Database, REST) nach der Version
  2. Ist eine neuere Version da, bricht der Launcher ab und zeigt
     "Bitte aktualisiere deinen Launcher". Ein Klick auf "Starten" lädt das Update,
     ersetzt die Datei und startet neu.
  3. Sonst öffnet sich das Hauptfenster (Grafik / Boost)
  4. Schließt man das Fenster, läuft PixelBoost im Infobereich (Tray) weiter.
     Rechtsklick auf das Symbol -> "Öffnen" oder "Beenden".
     Startet man PixelBoost erneut, wird die alte Instanz beendet und der Launcher
     prüft wieder auf Updates.

Grafik: Helligkeit, Kontrast, Gamma und Farben pro Bildschirm (oben rechts wählbar).
Boost:  Mit "+" Programme hinzufügen (Streams, YouTube-Videos, Games, Mehr). Solange der Haken
        gesetzt ist und das Programm läuft, bekommt es höhere Priorität, Windows wird auf
        "Höchstleistung" gestellt und bei Spielen wird die schnelle Grafikkarte bevorzugt.
        Beim Beenden von PixelBoost wird alles wieder zurückgesetzt.

Installation:  einfach starten – beim ersten Start werden fehlende Pakete
               (customtkinter, requests, pystray, Pillow, psutil) automatisch geladen.

Firebase Realtime Database:
{
  "pixelboost": {
    "version": "1.2.0",
    "download_url": "https://raw.githubusercontent.com/mirco5375-lab/pixelboost/main/pixelboost.pyw"
  }
}
Wichtig: APP_VERSION unten und "version" in Firebase müssen nach einem Update gleich sein.
"""

import os
import re
import sys
import json
import time
import uuid
import queue
import socket
import ctypes
import threading
import subprocess
import tempfile
import importlib
import importlib.util

# ---------------- Erststart: fehlende Pakete automatisch laden ----------------
REQUIRED = {
    "customtkinter": "customtkinter",
    "requests": "requests",
    "pystray": "pystray",
    "PIL": "Pillow",
    "psutil": "psutil",
}
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def missing_packages():
    return [pkg for mod, pkg in REQUIRED.items() if importlib.util.find_spec(mod) is None]


def first_run_setup(missing):
    """Kleines Setup-Fenster (nur Standard-tkinter), das alles beim ersten Start nachlädt."""
    import tkinter as tk
    from tkinter import ttk, messagebox

    root = tk.Tk()
    root.title("PixelBoost – Ersteinrichtung")
    root.configure(bg="#14161a")
    root.resizable(False, False)
    w, h = 420, 190
    root.geometry(f"{w}x{h}+{(root.winfo_screenwidth() - w) // 2}+{(root.winfo_screenheight() - h) // 2}")

    tk.Label(root, text="PixelBoost", font=("Segoe UI", 22, "bold"), fg="white", bg="#14161a").pack(pady=(24, 2))
    tk.Label(root, text="Erster Start – benötigte Komponenten werden geladen",
             fg="#9aa0a6", bg="#14161a").pack()
    status = tk.Label(root, text="Bereite vor …", fg="white", bg="#14161a")
    status.pack(pady=(18, 6))
    bar = ttk.Progressbar(root, length=320, maximum=len(missing))
    bar.pack()

    result = {"error": None}

    def work():
        for i, pkg in enumerate(missing):
            root.after(0, lambda p=pkg: status.configure(text=f"Lade {p} …"))
            proc = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--user", "--disable-pip-version-check", pkg],
                capture_output=True, text=True, creationflags=NO_WINDOW,
            )
            if proc.returncode != 0:
                result["error"] = f"{pkg} konnte nicht installiert werden:\n{proc.stderr[-400:]}"
                break
            root.after(0, lambda n=i + 1: bar.configure(value=n))
        root.after(0, root.destroy)

    threading.Thread(target=work, daemon=True).start()
    root.mainloop()

    if result["error"]:
        err = tk.Tk()
        err.withdraw()
        messagebox.showerror("PixelBoost", result["error"])
        sys.exit(1)
    importlib.invalidate_caches()


if not getattr(sys, "frozen", False):  # bei einer fertigen .exe sind die Pakete schon enthalten
    _missing = missing_packages()
    if _missing:
        first_run_setup(_missing)

import requests
import psutil
import customtkinter as ctk

# ---------------- Konfiguration ----------------
APP_NAME = "PixelBoost"
APP_VERSION = "1.2.0"
FIREBASE_URL = "https://host-a4d95-default-rtdb.europe-west1.firebasedatabase.app"
FIREBASE_NODE = "pixelboost"
TIMEOUT = 8
INSTANCE_PORT = 49731  # lokaler Port, damit nur eine Instanz im Hintergrund läuft

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("green")

# ---------------- Gespeicherte Daten ----------------
DEFAULTS = {"brightness": 0, "contrast": 100, "gamma": 1.0, "red": 100, "green": 100, "blue": 100}
SETTINGS_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "PixelBoost")
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")


def load_state():
    st = {"monitors": {}, "selected": None, "boosts": []}
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for dev, s in (data.get("monitors") or {}).items():
            merged = dict(DEFAULTS)
            for key, default in DEFAULTS.items():
                if key in s:
                    merged[key] = type(default)(s[key])
            st["monitors"][dev] = merged
        st["selected"] = data.get("selected")
        st["boosts"] = [b for b in data.get("boosts", [])
                        if isinstance(b, dict) and b.get("id") and b.get("exes")]
    except Exception:
        pass
    return st


def save_state(st):
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f)
    except Exception:
        pass


# ---------------- Bildschirme: Helligkeit, Kontrast, Gamma, Farben ----------------
class DISPLAY_DEVICE(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("DeviceName", ctypes.c_wchar * 32),
        ("DeviceString", ctypes.c_wchar * 128),
        ("StateFlags", ctypes.c_ulong),
        ("DeviceID", ctypes.c_wchar * 128),
        ("DeviceKey", ctypes.c_wchar * 128),
    ]


def list_monitors():
    """Liste aus (Gerätename, Anzeigename) aller angeschlossenen Bildschirme."""
    result = []
    if os.name == "nt":
        try:
            user32 = ctypes.windll.user32
            i, n = 0, 0
            while True:
                dd = DISPLAY_DEVICE()
                dd.cb = ctypes.sizeof(dd)
                if not user32.EnumDisplayDevicesW(None, i, ctypes.byref(dd), 0):
                    break
                i += 1
                if not (dd.StateFlags & 0x1):  # nicht am Desktop angeschlossen
                    continue
                n += 1
                mon = DISPLAY_DEVICE()
                mon.cb = ctypes.sizeof(mon)
                name = mon.DeviceString if user32.EnumDisplayDevicesW(
                    dd.DeviceName, 0, ctypes.byref(mon), 0) else ""
                label = f"Bildschirm {n}"
                if name:
                    label += f" – {name}"
                if dd.StateFlags & 0x4:
                    label += " (Haupt)"
                result.append((dd.DeviceName, label))
        except Exception:
            result = []
    return result or [("", "Hauptbildschirm")]


def build_ramp(s):
    br = s["brightness"] / 100.0
    ct = s["contrast"] / 100.0
    gm = max(0.1, float(s["gamma"]))
    mult = (s["red"] / 100.0, s["green"] / 100.0, s["blue"] / 100.0)
    ramp = ((ctypes.c_ushort * 256) * 3)()
    for c in range(3):
        prev = 0
        for i in range(256):
            x = i / 255.0
            x = (x - 0.5) * ct + 0.5 + br
            x = min(1.0, max(0.0, x))
            x = x ** (1.0 / gm)
            x = min(1.0, x * mult[c])
            v = int(x * 65535)
            ident = i * 256
            v = max(ident - 0x7F00, min(ident + 0x7F00, v))  # Windows lehnt zu starke Abweichungen ab
            v = max(prev, min(65535, max(0, v)))
            ramp[c][i] = v
            prev = v
    return ramp


def apply_gamma(device, s):
    """Setzt die Farbkurve eines Bildschirms. Gibt (ok, Meldung) zurück."""
    if os.name != "nt":
        return False, "Nur unter Windows verfügbar."
    try:
        gdi32 = ctypes.windll.gdi32
        gdi32.CreateDCW.restype = ctypes.c_void_p
        gdi32.CreateDCW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p]
        gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
        gdi32.SetDeviceGammaRamp.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        gdi32.GetDeviceGammaRamp.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        hdc = gdi32.CreateDCW("DISPLAY", device or None, None, None)
        if not hdc:
            return False, "Dieser Bildschirm ist nicht erreichbar."
        try:
            ramp = build_ramp(s)
            if not gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(ramp)):
                return False, "Windows lehnt diese Einstellung ab – Wert etwas zurücknehmen."
            check = ((ctypes.c_ushort * 256) * 3)()
            if gdi32.GetDeviceGammaRamp(hdc, ctypes.byref(check)) and abs(check[1][200] - ramp[1][200]) > 512:
                return False, "Der Grafiktreiber ignoriert die Änderung."
            return True, ""
        finally:
            gdi32.DeleteDC(hdc)
    except Exception as e:
        return False, f"Fehler: {e}"


# ---------------- Boost-Motor ----------------
HIGH_PERF_GUID = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
GPU_KEY = r"Software\Microsoft\DirectX\UserGpuPreferences"

STREAM_EXES = ["obs64.exe", "obs32.exe", "streamlabs desktop.exe", "streamlabs obs.exe", "twitch studio.exe"]
BROWSER_EXES = ["chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe"]


class BoostEngine:
    """Läuft im Hintergrund und boostet alle Einträge mit gesetztem Haken, solange sie laufen."""

    def __init__(self, entries):
        self.lock = threading.Lock()
        self.run_lock = threading.Lock()
        self.entries = [dict(e) for e in entries]
        self.orig = {}          # pid -> (Startzeit, ursprüngliche Priorität)
        self.active = {}        # Eintrags-ID -> Anzahl geboosteter Prozesse
        self.plan_on = False
        self.plan_ok = True
        self.prev_plan = None
        self.gpu_keys = set()
        self.stop_flag = threading.Event()
        self.wake = threading.Event()
        self.enabled_os = os.name == "nt"
        threading.Thread(target=self.loop, daemon=True).start()

    def set_entries(self, entries):
        with self.lock:
            self.entries = [dict(e) for e in entries]
        self.wake.set()

    def snapshot(self):
        with self.lock:
            return dict(self.active), self.plan_on, self.plan_ok

    def loop(self):
        while not self.stop_flag.is_set():
            try:
                self.tick()
            except Exception:
                pass
            self.wake.wait(3)
            self.wake.clear()

    def tick(self):
        if not self.enabled_os:
            return
        with self.run_lock:
            with self.lock:
                entries = [e for e in self.entries if e.get("enabled", True)]
            high, above = psutil.HIGH_PRIORITY_CLASS, psutil.ABOVE_NORMAL_PRIORITY_CLASS

            exe_map = {}
            for e in entries:
                level = above if e["kind"] == "youtube" else high
                for exe in e["exes"]:
                    exe_map.setdefault(exe.lower(), []).append((e["id"], level))

            found = {}
            if exe_map:
                for p in psutil.process_iter(["pid", "name", "create_time"]):
                    hits = exe_map.get((p.info.get("name") or "").lower())
                    if not hits:
                        continue
                    level = high if any(l == high for _, l in hits) else above
                    found[p.info["pid"]] = (p, p.info.get("create_time") or 0, level, [i for i, _ in hits])

            active = {}
            for pid, (p, ctime, level, ids) in found.items():
                try:
                    if pid not in self.orig:
                        self.orig[pid] = (ctime, p.nice())
                    if p.nice() != level:
                        p.nice(level)
                    for i in ids:
                        active[i] = active.get(i, 0) + 1
                except Exception:
                    self.orig.pop(pid, None)  # kein Zugriff (z. B. Admin-Programm) -> überspringen

            for pid in list(self.orig):
                if pid in found:
                    continue
                ctime, orig_nice = self.orig.pop(pid)
                try:
                    p = psutil.Process(pid)
                    if abs(p.create_time() - ctime) < 1:
                        p.nice(orig_nice)
                except Exception:
                    pass

            if active and not self.plan_on:
                self.set_power(True)
            elif not active and self.plan_on:
                self.set_power(False)

            self.sync_gpu(entries)
            with self.lock:
                self.active = active

    def set_power(self, on):
        try:
            if on:
                out = subprocess.run(["powercfg", "/getactivescheme"], capture_output=True, text=True,
                                     creationflags=NO_WINDOW).stdout
                m = re.search(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}", out)
                cur = m.group(0).lower() if m else None
                self.plan_on = True
                if cur == HIGH_PERF_GUID:
                    self.prev_plan, self.plan_ok = None, True
                    return
                r = subprocess.run(["powercfg", "/setactive", HIGH_PERF_GUID], capture_output=True,
                                   text=True, creationflags=NO_WINDOW)
                if r.returncode == 0:
                    self.prev_plan, self.plan_ok = cur, True
                else:
                    self.prev_plan, self.plan_ok = None, False
            else:
                if self.prev_plan:
                    subprocess.run(["powercfg", "/setactive", self.prev_plan], capture_output=True,
                                   text=True, creationflags=NO_WINDOW)
                self.plan_on, self.prev_plan, self.plan_ok = False, None, True
        except Exception:
            self.plan_on = on

    def sync_gpu(self, entries):
        """Bei Spielen/Programmen mit bekanntem Pfad: schnelle Grafikkarte bevorzugen (Windows-Grafikeinstellung)."""
        try:
            import winreg
            desired = {e["path"] for e in entries if e.get("path") and os.path.exists(e["path"])}
            if not desired and not self.gpu_keys:
                return
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, GPU_KEY) as key:
                for path in desired - self.gpu_keys:
                    try:
                        winreg.QueryValueEx(key, path)  # gibt es schon -> nicht anfassen
                    except FileNotFoundError:
                        try:
                            winreg.SetValueEx(key, path, 0, winreg.REG_SZ, "GpuPreference=2;")
                            self.gpu_keys.add(path)
                        except OSError:
                            pass
                for path in list(self.gpu_keys - desired):
                    try:
                        winreg.DeleteValue(key, path)
                    except OSError:
                        pass
                    self.gpu_keys.discard(path)
        except Exception:
            pass

    def shutdown(self):
        """Alles zurücksetzen (Prioritäten, Energiesparplan, GPU-Einstellung)."""
        self.stop_flag.set()
        with self.lock:
            self.entries = []
        self.wake.set()
        try:
            self.tick()
        except Exception:
            pass


# ---------------- Update-Logik ----------------
def parse_version(v: str):
    return tuple(int(p) for p in str(v).strip().split(".") if p.isdigit())


def fetch_remote_info():
    r = requests.get(f"{FIREBASE_URL}/{FIREBASE_NODE}.json", timeout=TIMEOUT)
    r.raise_for_status()
    return r.json() or {}


def download_update(url: str, progress_cb):
    target = os.path.abspath(sys.argv[0])
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".new", dir=os.path.dirname(target))
    os.close(tmp_fd)
    try:
        with requests.get(url, stream=True, timeout=TIMEOUT) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            done = 0
            with open(tmp_path, "wb") as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        progress_cb(done / total)
        if os.path.getsize(tmp_path) < 500:
            raise ValueError("Heruntergeladene Datei ist zu klein")
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    return tmp_path, target


def apply_update_and_restart(tmp_path: str, target: str):
    if getattr(sys, "frozen", False):
        # .exe: kann sich nicht selbst ersetzen -> kleine Batch-Datei erledigt das nach dem Beenden
        bat = os.path.join(tempfile.gettempdir(), "pixelboost_update.bat")
        with open(bat, "w") as f:
            f.write(
                "@echo off\r\n"
                "timeout /t 2 /nobreak >nul\r\n"
                f'move /y "{tmp_path}" "{target}"\r\n'
                f'start "" "{target}"\r\n'
                "del \"%~f0\"\r\n"
            )
        subprocess.Popen(["cmd", "/c", bat], creationflags=NO_WINDOW)
    else:
        os.replace(tmp_path, target)
        subprocess.Popen([sys.executable, target])
    os._exit(0)


# ---------------- Nur eine Instanz im Hintergrund ----------------
def kick_old_instance():
    """Beendet eine schon laufende (versteckte) Instanz, damit der Launcher neu prüfen kann."""
    try:
        with socket.create_connection(("127.0.0.1", INSTANCE_PORT), timeout=1) as s:
            s.sendall(b"quit")
    except OSError:
        return
    for _ in range(30):  # warten, bis die alte Instanz wirklich weg ist
        try:
            socket.create_connection(("127.0.0.1", INSTANCE_PORT), timeout=0.5).close()
            time.sleep(0.1)
        except OSError:
            break


def start_instance_server(q):
    def run():
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        for _ in range(20):
            try:
                srv.bind(("127.0.0.1", INSTANCE_PORT))
                break
            except OSError:
                time.sleep(0.25)
        else:
            return
        srv.listen(2)
        while True:
            try:
                conn, _ = srv.accept()
                with conn:
                    conn.settimeout(1)
                    if conn.recv(16) == b"quit":
                        q.put("quit")
            except OSError:
                time.sleep(0.1)

    threading.Thread(target=run, daemon=True).start()


# ---------------- Dialog: Boost hinzufügen ----------------
class AddBoostDialog(ctk.CTkToplevel):
    INFO = {
        "Streams": "Boostet Streaming-Programme (OBS, Streamlabs, Twitch Studio), sobald sie laufen.",
        "YouTube-Videos": "Boostet deinen Browser (Chrome, Edge, Firefox, Brave, Opera, Vivaldi), "
                          "sobald er läuft – dabei alle Tabs, nicht nur YouTube.",
        "Games": "Wähle die .exe deines Spiels. Sie wird geboostet, sobald das Spiel läuft.",
        "Mehr": "Wähle ein beliebiges Programm, das geboostet werden soll.",
    }

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Boost hinzufügen")
        self.geometry("480x340")
        self.resizable(False, False)
        self.transient(app)
        self.path = None
        self.kind = "Streams"

        ctk.CTkLabel(self, text="Was soll geboostet werden?",
                     font=("Segoe UI", 18, "bold")).pack(pady=(20, 10))
        self.seg = ctk.CTkSegmentedButton(self, values=list(self.INFO), command=self.on_kind)
        self.seg.pack(padx=20, fill="x")

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(side="bottom", pady=20)
        ctk.CTkButton(btns, text="Abbrechen", width=120, corner_radius=20, fg_color="transparent",
                      border_width=1, command=self.destroy).pack(side="left", padx=8)
        self.ok_btn = ctk.CTkButton(btns, text="OK", width=120, corner_radius=20, command=self.ok)
        self.ok_btn.pack(side="left", padx=8)

        self.info = ctk.CTkLabel(self, text="", wraplength=420, justify="left")
        self.info.pack(pady=(20, 10), padx=20, anchor="w")
        self.pick_btn = ctk.CTkButton(self, text="Programm wählen …", corner_radius=20, command=self.pick)
        self.picked = ctk.CTkLabel(self, text="", text_color="gray", wraplength=420)

        self.seg.set("Streams")
        self.on_kind("Streams")
        self.after(150, self.bring_front)

    def bring_front(self):
        try:
            self.lift()
            self.focus_force()
            self.grab_set()
        except Exception:
            pass

    def on_kind(self, value):
        self.kind = value
        self.path = None
        self.picked.configure(text="")
        self.info.configure(text=self.INFO[value])
        if value in ("Games", "Mehr"):
            self.pick_btn.pack(pady=(4, 6))
            self.picked.pack()
            self.ok_btn.configure(state="disabled")
        else:
            self.pick_btn.pack_forget()
            self.picked.pack_forget()
            self.ok_btn.configure(state="normal")

    def pick(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(parent=self, title="Programm wählen",
                                          filetypes=[("Programme", "*.exe")])
        if path:
            self.path = path
            self.picked.configure(text=os.path.basename(path))
            self.ok_btn.configure(state="normal")

    def ok(self):
        if self.kind == "Streams":
            entry = {"kind": "stream", "name": "Streams (OBS, Streamlabs, Twitch Studio)", "exes": STREAM_EXES}
        elif self.kind == "YouTube-Videos":
            entry = {"kind": "youtube", "name": "YouTube-Videos (Browser)", "exes": BROWSER_EXES}
        else:
            base = os.path.basename(self.path)
            stem = base[:-4] if base.lower().endswith(".exe") else base
            if self.kind == "Games":
                entry = {"kind": "game", "name": f"Game: {stem}", "exes": [base.lower()], "path": self.path}
            else:
                entry = {"kind": "custom", "name": f"Programm: {stem}", "exes": [base.lower()], "path": self.path}
        entry["id"] = uuid.uuid4().hex
        entry["enabled"] = True
        self.app.add_boost(entry)
        self.destroy()


# ---------------- App ----------------
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.q = queue.Queue()
        self.tray = None
        self.engine = None
        self.state = load_state()
        self.monitors = list_monitors()
        self.mon_by_label = {label: dev for dev, label in self.monitors}
        devs = [d for d, _ in self.monitors]
        self.cur_dev = self.state["selected"] if self.state["selected"] in devs else devs[0]
        self.gamma_touched = set()
        self.main_ready = False
        self._closing = False
        self._save_job = None
        self.update_info = None
        self.sliders = {}
        self.boost_rows = {}

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self.poll)
        self.show_launcher()

    # ----- Hilfen -----
    def center(self, w, h):
        x = (self.winfo_screenwidth() - w) // 2
        y = (self.winfo_screenheight() - h) // 2
        return f"{w}x{h}+{x}+{y}"

    def run_ui(self, fn):
        """Aus Threads heraus sicher etwas im Fenster ändern."""
        self.q.put(("call", fn))

    def poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                if isinstance(item, tuple):
                    item[1]()
                elif item == "show":
                    self.show_window()
                elif item == "quit":
                    self.quit_app()
                    return
        except queue.Empty:
            pass
        except Exception:
            pass
        if not self._closing:
            self.after(100, self.poll)

    def show_window(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def on_close(self):
        if self.main_ready and self.tray is not None:
            self.withdraw()  # läuft im Hintergrund weiter
        else:
            self.quit_app()

    def quit_app(self):
        if self._closing:
            return
        self._closing = True
        for dev in list(self.gamma_touched):
            apply_gamma(dev, DEFAULTS)
        if self.engine is not None:
            self.engine.shutdown()
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        try:
            self.destroy()
        except Exception:
            pass

    def cur_settings(self):
        return self.state["monitors"].setdefault(self.cur_dev, dict(DEFAULTS))

    # ----- Launcher -----
    def show_launcher(self):
        self.title(f"{APP_NAME} Launcher")
        self.geometry(self.center(420, 290))
        self.resizable(False, False)

        self.lf = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.lf.pack(fill="both", expand=True)
        ctk.CTkLabel(self.lf, text=APP_NAME, font=("Segoe UI", 32, "bold")).pack(pady=(34, 2))
        self.l_sub = ctk.CTkLabel(self.lf, text=f"Launcher • v{APP_VERSION}", text_color="gray")
        self.l_sub.pack()
        self.l_status = ctk.CTkLabel(self.lf, text="Starte …", font=("Segoe UI", 14))
        self.l_status.pack(pady=(24, 8))
        self.l_bar = ctk.CTkProgressBar(self.lf, width=320)
        self.l_bar.set(0)
        self.l_bar.pack()
        self.l_btn = ctk.CTkButton(self.lf, text="Starten", width=180, height=36,
                                   corner_radius=20, command=self.start_update)

        threading.Thread(target=self.check_update, daemon=True).start()

    def set_status(self, text, progress=None):
        self.l_status.configure(text=text)
        if progress is not None:
            self.l_bar.set(progress)

    def check_update(self):
        self.run_ui(lambda: self.set_status("Suche nach Updates …", 0.15))
        try:
            info = fetch_remote_info()
            remote = str(info.get("version", APP_VERSION))
            url = info.get("download_url")
            if url and parse_version(remote) > parse_version(APP_VERSION):
                self.update_info = (remote, url)
                self.run_ui(self.show_update_needed)
                return
            self.run_ui(lambda: self.set_status("Auf dem neuesten Stand", 1.0))
        except Exception:
            self.run_ui(lambda: self.set_status("Offline – starte ohne Update", 1.0))
        time.sleep(0.7)
        self.run_ui(self.open_main)

    def show_update_needed(self):
        remote, _ = self.update_info
        self.l_sub.configure(text=f"Version {APP_VERSION}  →  {remote}")
        self.l_status.configure(text="Bitte aktualisiere deinen Launcher")
        self.l_bar.set(0)
        self.l_btn.configure(state="normal", text="Starten")
        self.l_btn.pack(pady=(22, 0))

    def start_update(self):
        self.l_btn.configure(state="disabled", text="Lade …")
        self.l_status.configure(text="Update wird geladen …")
        threading.Thread(target=self.do_update, daemon=True).start()

    def do_update(self):
        _, url = self.update_info
        try:
            tmp, target = download_update(url, lambda p: self.run_ui(lambda: self.l_bar.set(p)))
            self.run_ui(lambda: self.set_status("Installiere Update …", 1.0))
            apply_update_and_restart(tmp, target)
        except Exception:
            self.run_ui(self.update_failed)

    def update_failed(self):
        self.l_bar.set(0)
        self.l_status.configure(text="Update fehlgeschlagen – bitte erneut versuchen")
        self.l_btn.configure(state="normal", text="Erneut versuchen")

    # ----- Hauptfenster -----
    def open_main(self):
        self.lf.destroy()
        self.build_main()
        present = {d for d, _ in self.monitors}
        for dev, s in self.state["monitors"].items():  # gespeicherte Grafik-Werte wieder anwenden
            if dev in present and s != DEFAULTS:
                apply_gamma(dev, s)
                self.gamma_touched.add(dev)
        self.engine = BoostEngine(self.state["boosts"])
        self.start_tray()
        self.main_ready = True
        self.after(1500, self.refresh_boost_status)

    def build_main(self):
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry(self.center(900, 620))
        self.minsize(760, 540)
        self.resizable(True, True)

        sidebar = ctk.CTkFrame(self, width=200, corner_radius=0)
        sidebar.pack(side="left", fill="y")
        ctk.CTkLabel(sidebar, text=APP_NAME, font=("Segoe UI", 22, "bold")).pack(pady=(24, 4))
        ctk.CTkLabel(sidebar, text=f"Version {APP_VERSION}", text_color="gray").pack(pady=(0, 24))
        for label, page in (("Grafik", "gfx"), ("Boost", "boost")):
            ctk.CTkButton(sidebar, text=label, corner_radius=20, fg_color="transparent",
                          border_width=1, anchor="w",
                          command=lambda p=page: self.show_page(p)).pack(padx=16, pady=5, fill="x")

        content = ctk.CTkFrame(self, corner_radius=16)
        content.pack(side="left", fill="both", expand=True, padx=16, pady=16)

        self.page_gfx = ctk.CTkFrame(content, fg_color="transparent")
        self.page_boost = ctk.CTkFrame(content, fg_color="transparent")
        self.build_gfx_page()
        self.build_boost_page()
        self.show_page("gfx")

    def show_page(self, name):
        self.page_gfx.pack_forget()
        self.page_boost.pack_forget()
        (self.page_gfx if name == "gfx" else self.page_boost).pack(fill="both", expand=True)

    # ----- Seite: Grafik -----
    def build_gfx_page(self):
        p = self.page_gfx
        head = ctk.CTkFrame(p, fg_color="transparent")
        head.pack(fill="x", padx=24, pady=(20, 4))
        ctk.CTkLabel(head, text="Grafik", font=("Segoe UI", 24, "bold")).pack(side="left")
        labels = [label for _, label in self.monitors]
        cur_label = next(label for dev, label in self.monitors if dev == self.cur_dev)
        self.mon_menu = ctk.CTkOptionMenu(head, values=labels, width=280, command=self.on_monitor)
        self.mon_menu.set(cur_label)
        self.mon_menu.pack(side="right")

        ctk.CTkLabel(p, text="Die Einstellungen gelten für den oben gewählten Bildschirm.",
                     text_color="gray").pack(anchor="w", padx=24, pady=(0, 12))

        s = self.cur_settings()
        rows = [
            ("Helligkeit", "brightness", -30, 30, 60, lambda v: f"{int(v):+d} %"),
            ("Kontrast", "contrast", 50, 150, 100, lambda v: f"{int(v)} %"),
            ("Gamma", "gamma", 0.5, 2.0, 30, lambda v: f"{float(v):.2f}"),
            ("Rot", "red", 50, 150, 100, lambda v: f"{int(v)} %"),
            ("Grün", "green", 50, 150, 100, lambda v: f"{int(v)} %"),
            ("Blau", "blue", 50, 150, 100, lambda v: f"{int(v)} %"),
        ]
        for label, key, lo, hi, steps, fmt in rows:
            row = ctk.CTkFrame(p, fg_color="transparent")
            row.pack(fill="x", padx=24, pady=7)
            ctk.CTkLabel(row, text=label, width=90, anchor="w").pack(side="left")
            val = ctk.CTkLabel(row, text=fmt(s[key]), width=70, anchor="e")
            val.pack(side="right")
            slider = ctk.CTkSlider(
                row, from_=lo, to=hi, number_of_steps=steps,
                command=lambda v, k=key, l=val, f=fmt: self.on_slider(k, v, l, f),
            )
            slider.set(s[key])
            slider.pack(side="left", fill="x", expand=True, padx=12)
            self.sliders[key] = (slider, val, fmt)

        self.gfx_hint = ctk.CTkLabel(p, text="", text_color="#e5a50a", wraplength=560, justify="left")
        self.gfx_hint.pack(anchor="w", padx=24, pady=(10, 0))
        ctk.CTkButton(p, text="Zurücksetzen", width=160, corner_radius=20,
                      command=self.reset_gfx).pack(anchor="w", padx=24, pady=(10, 0))

    def on_monitor(self, label):
        self.cur_dev = self.mon_by_label[label]
        self.state["selected"] = self.cur_dev
        s = self.cur_settings()
        for key, (slider, lbl, fmt) in self.sliders.items():
            slider.set(s[key])
            lbl.configure(text=fmt(s[key]))
        self.gfx_hint.configure(text="")
        save_state(self.state)

    def on_slider(self, key, value, label, fmt):
        value = round(value, 2) if key == "gamma" else int(round(value))
        self.cur_settings()[key] = value
        label.configure(text=fmt(value))
        self.apply_gfx()
        if self._save_job:
            self.after_cancel(self._save_job)
        self._save_job = self.after(500, lambda: save_state(self.state))

    def apply_gfx(self):
        s = self.cur_settings()
        ok, msg = apply_gamma(self.cur_dev, s)
        if s != DEFAULTS:
            self.gamma_touched.add(self.cur_dev)
        self.gfx_hint.configure(text="" if ok else msg)

    def reset_gfx(self):
        self.state["monitors"][self.cur_dev] = dict(DEFAULTS)
        for key, (slider, label, fmt) in self.sliders.items():
            slider.set(DEFAULTS[key])
            label.configure(text=fmt(DEFAULTS[key]))
        self.apply_gfx()
        save_state(self.state)

    # ----- Seite: Boost -----
    def build_boost_page(self):
        p = self.page_boost
        head = ctk.CTkFrame(p, fg_color="transparent")
        head.pack(fill="x", padx=24, pady=(20, 2))
        ctk.CTkLabel(head, text="Boost", font=("Segoe UI", 24, "bold")).pack(side="left")
        ctk.CTkButton(head, text="+", width=40, height=40, corner_radius=20,
                      font=("Segoe UI", 22, "bold"), command=self.open_add_boost).pack(side="right")
        ctk.CTkLabel(p, text="Haken gesetzt = wird geboostet, sobald das Programm läuft.",
                     text_color="gray").pack(anchor="w", padx=24, pady=(0, 8))

        self.boost_list = ctk.CTkScrollableFrame(p, fg_color="transparent")
        self.boost_list.pack(fill="both", expand=True, padx=14, pady=(0, 6))
        self.boost_info = ctk.CTkLabel(p, text="", text_color="gray", anchor="w")
        self.boost_info.pack(fill="x", padx=24, pady=(0, 12))
        self.render_boosts()

    def render_boosts(self):
        for w in self.boost_list.winfo_children():
            w.destroy()
        self.boost_rows = {}
        if not self.state["boosts"]:
            ctk.CTkLabel(self.boost_list, text="Noch nichts hinzugefügt – drück oben auf +",
                         text_color="gray").pack(pady=40)
            return
        for e in self.state["boosts"]:
            row = ctk.CTkFrame(self.boost_list, corner_radius=12)
            row.pack(fill="x", padx=6, pady=5)
            cb = ctk.CTkCheckBox(row, text=e["name"], command=lambda i=e["id"]: self.toggle_boost(i))
            if e.get("enabled", True):
                cb.select()
            cb.pack(side="left", padx=14, pady=12)
            ctk.CTkButton(row, text="✕", width=30, height=30, corner_radius=15, fg_color="transparent",
                          border_width=1, command=lambda i=e["id"]: self.remove_boost(i)).pack(side="right", padx=10)
            status = ctk.CTkLabel(row, text="", text_color="gray")
            status.pack(side="right", padx=8)
            self.boost_rows[e["id"]] = (cb, status)

    def open_add_boost(self):
        AddBoostDialog(self)

    def push_boosts(self):
        save_state(self.state)
        if self.engine is not None:
            self.engine.set_entries(self.state["boosts"])

    def add_boost(self, entry):
        for e in self.state["boosts"]:
            if e["kind"] == entry["kind"] and e["exes"] == entry["exes"]:
                return  # gibt es schon
        self.state["boosts"].append(entry)
        self.render_boosts()
        self.push_boosts()

    def toggle_boost(self, boost_id):
        cb = self.boost_rows[boost_id][0]
        for e in self.state["boosts"]:
            if e["id"] == boost_id:
                e["enabled"] = bool(cb.get())
        self.push_boosts()

    def remove_boost(self, boost_id):
        self.state["boosts"] = [e for e in self.state["boosts"] if e["id"] != boost_id]
        self.render_boosts()
        self.push_boosts()

    def refresh_boost_status(self):
        if self._closing:
            return
        try:
            if self.engine is not None:
                active, plan_on, plan_ok = self.engine.snapshot()
                for e in self.state["boosts"]:
                    row = self.boost_rows.get(e["id"])
                    if not row:
                        continue
                    n = active.get(e["id"], 0)
                    if not e.get("enabled", True):
                        row[1].configure(text="", text_color="gray")
                    elif n:
                        txt = "geboostet" if n == 1 else f"geboostet · {n} Prozesse"
                        row[1].configure(text=txt, text_color="#2ecc71")
                    else:
                        row[1].configure(text="wartet auf Start", text_color="gray")
                parts = []
                if active:
                    parts.append("Priorität erhöht")
                if plan_on:
                    parts.append("Energiesparplan: Höchstleistung" if plan_ok
                                 else "Energiesparplan nicht umstellbar")
                self.boost_info.configure(text=" • ".join(parts))
        except Exception:
            pass
        self.after(1500, self.refresh_boost_status)

    # ----- Tray (Hintergrund) -----
    def start_tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception:
            return  # ohne Tray wird das Programm beim Schließen komplett beendet
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((4, 4, 60, 60), radius=14, fill=(46, 204, 113, 255))
        dark = (20, 22, 26, 255)
        d.rectangle((22, 16, 30, 48), fill=dark)
        d.rectangle((22, 16, 42, 24), fill=dark)
        d.rectangle((22, 30, 42, 38), fill=dark)
        d.rectangle((36, 16, 44, 38), fill=dark)
        menu = pystray.Menu(
            pystray.MenuItem("Öffnen", lambda icon, item: self.q.put("show"), default=True),
            pystray.MenuItem("Beenden", lambda icon, item: self.q.put("quit")),
        )
        self.tray = pystray.Icon("PixelBoost", img, APP_NAME, menu)
        self.tray.run_detached()


if __name__ == "__main__":
    kick_old_instance()
    app = App()
    start_instance_server(app.q)
    app.mainloop()
    os._exit(0)
