"""
PixelBoost Launcher
-------------------
Ablauf:
  1. Launcher-Fenster mit Ladebalken öffnet sich
  2. Aktuelle Version wird aus Firebase (Realtime Database, REST) gelesen
  3. Ist eine neuere Version da, wird sie geladen, die Datei ersetzt und neu gestartet
  4. Danach öffnet sich das Hauptfenster (aktuell: "Ist noch nicht fertig")

Installation:  einfach starten – beim ersten Start werden fehlende Pakete
               (customtkinter, requests) automatisch geladen. Firebase läuft über
               die REST-Schnittstelle, dafür ist kein extra Paket nötig.

Firebase Realtime Database -> Knoten "pixelboost" anlegen:
{
  "pixelboost": {
    "version": "1.0.1",
    "download_url": "https://raw.githubusercontent.com/DEIN_NAME/DEIN_REPO/main/pixelboost.pyw"
  }
}
Regeln (nur lesen erlauben):  { "rules": { "pixelboost": { ".read": true, ".write": false } } }
"""

import os
import sys
import threading
import subprocess
import tempfile
import importlib
import importlib.util

# ---------------- Erststart: fehlende Pakete automatisch laden ----------------
REQUIRED = {"customtkinter": "customtkinter", "requests": "requests"}
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
APP_VERSION = "1.0.0"
FIREBASE_URL = "https://host-a4d95-default-rtdb.europe-west1.firebasedatabase.app"
FIREBASE_NODE = "pixelboost"
TIMEOUT = 8

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("green")


# ---------------- Update-Logik ----------------
def parse_version(v: str):
    return tuple(int(p) for p in v.strip().split(".") if p.isdigit())


def fetch_remote_info():
    r = requests.get(f"{FIREBASE_URL}/{FIREBASE_NODE}.json", timeout=TIMEOUT)
    r.raise_for_status()
    return r.json() or {}


def download_update(url: str, progress_cb):
    target = os.path.abspath(sys.argv[0])
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".new", dir=os.path.dirname(target))
    os.close(tmp_fd)
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
        subprocess.Popen(["cmd", "/c", bat], creationflags=0x08000000)
    else:
        os.replace(tmp_path, target)
        subprocess.Popen([sys.executable, target])
    os._exit(0)


# ---------------- Hauptfenster ----------------
class MainWindow(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry("900x560")
        self.minsize(700, 440)

        sidebar = ctk.CTkFrame(self, width=200, corner_radius=0)
        sidebar.pack(side="left", fill="y")
        ctk.CTkLabel(sidebar, text=APP_NAME, font=("Segoe UI", 22, "bold")).pack(pady=(24, 4))
        ctk.CTkLabel(sidebar, text=f"Version {APP_VERSION}", text_color="gray").pack(pady=(0, 24))
        for name in ("Start", "Grafik", "Boost", "Einstellungen"):
            ctk.CTkButton(sidebar, text=name, corner_radius=20, fg_color="transparent",
                          border_width=1, anchor="w").pack(padx=16, pady=5, fill="x")

        content = ctk.CTkFrame(self, corner_radius=16)
        content.pack(side="left", fill="both", expand=True, padx=16, pady=16)
        ctk.CTkLabel(content, text="🚧", font=("Segoe UI Emoji", 56)).pack(pady=(110, 8))
        ctk.CTkLabel(content, text="Ist noch nicht fertig", font=("Segoe UI", 26, "bold")).pack()
        ctk.CTkLabel(content, text="Hier kommt bald das Boost-Menü hin.",
                     text_color="gray").pack(pady=6)


# ---------------- Launcher ----------------
class Launcher(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.overrideredirect(True)
        w, h = 420, 240
        x = (self.winfo_screenwidth() - w) // 2
        y = (self.winfo_screenheight() - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")

        frame = ctk.CTkFrame(self, corner_radius=18, border_width=1)
        frame.pack(fill="both", expand=True)
        ctk.CTkLabel(frame, text=APP_NAME, font=("Segoe UI", 32, "bold")).pack(pady=(40, 2))
        ctk.CTkLabel(frame, text=f"Launcher • v{APP_VERSION}", text_color="gray").pack()
        self.status = ctk.CTkLabel(frame, text="Starte …")
        self.status.pack(pady=(30, 8))
        self.bar = ctk.CTkProgressBar(frame, width=320)
        self.bar.set(0)
        self.bar.pack()

        threading.Thread(target=self.run, daemon=True).start()

    def ui(self, text=None, progress=None):
        if text is not None:
            self.after(0, lambda: self.status.configure(text=text))
        if progress is not None:
            self.after(0, lambda: self.bar.set(progress))

    def run(self):
        try:
            self.ui("Suche nach Updates …", 0.15)
            info = fetch_remote_info()
            remote = info.get("version", APP_VERSION)
            url = info.get("download_url")
            if url and parse_version(remote) > parse_version(APP_VERSION):
                self.ui(f"Lade Update {remote} …", 0.2)
                tmp, target = download_update(url, lambda p: self.ui(progress=0.2 + p * 0.75))
                self.ui("Installiere Update …", 0.98)
                apply_update_and_restart(tmp, target)
                return
            self.ui("Auf dem neuesten Stand", 1.0)
        except Exception:
            self.ui("Offline – starte ohne Update", 1.0)
        self.after(700, self.open_main)

    def open_main(self):
        self.destroy()
        MainWindow().mainloop()


if __name__ == "__main__":
    Launcher().mainloop()
