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

Installation:  einfach starten – beim ersten Start werden fehlende Pakete
               (customtkinter, requests, pystray, Pillow) automatisch geladen.

Firebase Realtime Database:
{
  "pixelboost": {
    "version": "1.1.0",
    "download_url": "https://raw.githubusercontent.com/mirco5375-lab/pixelboost/main/pixelboost.pyw"
  }
}
Wichtig: APP_VERSION unten und "version" in Firebase müssen nach einem Update gleich sein.
"""

import os
import sys
import json
import time
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
import customtkinter as ctk

# ---------------- Konfiguration ----------------
APP_NAME = "PixelBoost"
APP_VERSION = "1.1.0"
FIREBASE_URL = "https://host-a4d95-default-rtdb.europe-west1.firebasedatabase.app"
FIREBASE_NODE = "pixelboost"
TIMEOUT = 8
INSTANCE_PORT = 49731  # lokaler Port, damit nur eine Instanz im Hintergrund läuft

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("green")

# ---------------- Einstellungen speichern ----------------
DEFAULTS = {"brightness": 0, "contrast": 100, "gamma": 1.0, "red": 100, "green": 100, "blue": 100}
SETTINGS_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "PixelBoost")
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")


def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for key, default in DEFAULTS.items():
            if key in data:
                s[key] = type(default)(data[key])
    except Exception:
        pass
    return s


def save_settings(s):
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f)
    except Exception:
        pass


# ---------------- Bildschirm: Helligkeit, Kontrast, Gamma, Farben ----------------
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


def apply_gamma(s):
    """Setzt die Gamma-Kurve des Hauptbildschirms. True = Windows hat es angenommen."""
    if os.name != "nt":
        return False
    try:
        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
        user32.GetDC.restype = ctypes.c_void_p
        user32.GetDC.argtypes = [ctypes.c_void_p]
        user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        gdi32.SetDeviceGammaRamp.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        hdc = user32.GetDC(None)
        ok = bool(gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(build_ramp(s))))
        user32.ReleaseDC(None, hdc)
        return ok
    except Exception:
        return False


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


# ---------------- App ----------------
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.q = queue.Queue()
        self.tray = None
        self.settings = load_settings()
        self.gamma_touched = False
        self.main_ready = False
        self._closing = False
        self._save_job = None
        self.update_info = None
        self.sliders = {}

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
        if self.gamma_touched:
            apply_gamma(DEFAULTS)
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        try:
            self.destroy()
        except Exception:
            pass

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
        if self.settings != DEFAULTS:
            self.apply_gfx()  # gespeicherte Werte wieder anwenden
        self.start_tray()
        self.main_ready = True

    def build_main(self):
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry(self.center(900, 600))
        self.minsize(720, 520)
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
        ctk.CTkLabel(p, text="Grafik", font=("Segoe UI", 24, "bold")).pack(anchor="w", padx=24, pady=(20, 2))
        ctk.CTkLabel(p, text="Gilt für den Hauptbildschirm.", text_color="gray").pack(anchor="w", padx=24, pady=(0, 12))

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
            val = ctk.CTkLabel(row, text=fmt(self.settings[key]), width=70, anchor="e")
            val.pack(side="right")
            slider = ctk.CTkSlider(
                row, from_=lo, to=hi, number_of_steps=steps,
                command=lambda v, k=key, l=val, f=fmt: self.on_slider(k, v, l, f),
            )
            slider.set(self.settings[key])
            slider.pack(side="left", fill="x", expand=True, padx=12)
            self.sliders[key] = (slider, val, fmt)

        self.gfx_hint = ctk.CTkLabel(p, text="", text_color="#e5a50a")
        self.gfx_hint.pack(anchor="w", padx=24, pady=(10, 0))
        ctk.CTkButton(p, text="Zurücksetzen", width=160, corner_radius=20,
                      command=self.reset_gfx).pack(anchor="w", padx=24, pady=(10, 0))

    def on_slider(self, key, value, label, fmt):
        value = round(value, 2) if key == "gamma" else int(round(value))
        self.settings[key] = value
        label.configure(text=fmt(value))
        self.apply_gfx()
        if self._save_job:
            self.after_cancel(self._save_job)
        self._save_job = self.after(500, lambda: save_settings(self.settings))

    def apply_gfx(self):
        ok = apply_gamma(self.settings)
        if self.settings != DEFAULTS:
            self.gamma_touched = True
        if hasattr(self, "gfx_hint"):
            self.gfx_hint.configure(
                text="" if ok else "Konnte nicht angewendet werden – Wert etwas zurücknehmen.")

    def reset_gfx(self):
        self.settings = dict(DEFAULTS)
        for key, (slider, label, fmt) in self.sliders.items():
            slider.set(DEFAULTS[key])
            label.configure(text=fmt(DEFAULTS[key]))
        self.apply_gfx()
        save_settings(self.settings)

    # ----- Seite: Boost -----
    def build_boost_page(self):
        p = self.page_boost
        ctk.CTkLabel(p, text="Boost", font=("Segoe UI", 24, "bold")).pack(anchor="w", padx=24, pady=(20, 2))
        ctk.CTkLabel(p, text="🚀", font=("Segoe UI Emoji", 56)).pack(pady=(110, 8))
        ctk.CTkLabel(p, text="Hier kann man noch nichts einstellen",
                     font=("Segoe UI", 20, "bold")).pack()
        ctk.CTkLabel(p, text="Kommt bald.", text_color="gray").pack(pady=6)

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
