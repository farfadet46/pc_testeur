#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PC Testeur - diagnostic graphique pour PC portable (Linux / Zorin OS / Ubuntu)

Onglets : Infos, Benchmark, Clavier, Souris/pavé tactile, Écran, Son/micro,
          Webcam, Ventilateurs & températures (avec stress CPU).

Installation des dépendances (une seule fois) :
    sudo apt install python3-tk python3-opencv

Lancement :
    python3 pc_testeur.py
    python3 pc_testeur.py --raccourci     # crée un lanceur dans le menu des applications
"""

import array
import datetime
import glob
import hashlib
import json
import math
import multiprocessing as mp
import os
import platform
import queue
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
from collections import deque

try:
    import tkinter as tk
    from tkinter import ttk, messagebox
except ImportError:
    sys.exit("Le module tkinter est absent.\nInstalle-le avec :  sudo apt install python3-tk")

HIST_FICHIER = os.path.expanduser("~/.pc_testeur_historique.json")
HWMON_RACINE = os.environ.get("PCTESTEUR_HWMON", "/sys/class/hwmon")
cv2 = None  # chargé à la demande (onglet Webcam)


def charger_cv2():
    global cv2
    if cv2 is None:
        try:
            import cv2 as _cv2
            cv2 = _cv2
        except ImportError:
            return None
    return cv2


# =============================================================== utilitaires système
def lire(chemin, defaut=None):
    try:
        with open(chemin) as f:
            return f.read().strip()
    except OSError:
        return defaut


def commande(cmd, timeout=5):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception:
        return ""


def cpu_times():
    v = list(map(int, (lire("/proc/stat", "cpu 0 0 0 0 0").splitlines()[0].split()[1:])))
    return sum(v), v[3] + (v[4] if len(v) > 4 else 0)


def frequence_mhz():
    freqs = []
    for f in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq"):
        v = lire(f)
        if v and v.isdigit():
            freqs.append(int(v) / 1000)
    return sum(freqs) / len(freqs) if freqs else None


def capteurs():
    """Retourne (ventilateurs, températures) : listes de (nom, valeur)."""
    fans, temps = [], []
    for hw in sorted(glob.glob(HWMON_RACINE + "/hwmon*")):
        nom = lire(hw + "/name", "?")
        for f in sorted(glob.glob(hw + "/fan*_input")):
            v = lire(f)
            if v and v.isdigit():
                lab = lire(f.replace("_input", "_label")) or os.path.basename(f).replace("_input", "")
                fans.append((f"{nom} · {lab}", int(v)))
        for t in sorted(glob.glob(hw + "/temp*_input")):
            v = lire(t)
            if v and v.lstrip("-").isdigit():
                lab = lire(t.replace("_input", "_label")) or os.path.basename(t).replace("_input", "")
                temps.append((f"{nom} · {lab}", int(v) / 1000))
    if not temps:
        for z in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
            v = lire(z + "/temp")
            if v and v.lstrip("-").isdigit():
                temps.append((f"zone · {lire(z + '/type', '?')}", int(v) / 1000))
    return fans, temps


HELPER = r"""
import glob, os, re, select, sys
racine, limite = sys.argv[1], float(sys.argv[2])
chemin_ok = re.compile("^" + re.escape(racine) + "/hwmon[0-9]+/pwm[0-9](_enable)?$")
orig = {}

def lire(p):
    try:
        with open(p) as f:
            return f.read().strip()
    except OSError:
        return None

def ecrire(p, v):
    try:
        with open(p, "w") as f:
            f.write(str(v))
        return True
    except OSError:
        return False

def temp_max():
    t = []
    for f in glob.glob("/sys/class/thermal/thermal_zone*/temp") + glob.glob(racine + "/hwmon*/temp*_input"):
        v = lire(f)
        if v and v.lstrip("-").isdigit():
            t.append(int(v) / 1000)
    return max(t) if t else 0

def restaurer():
    for p, v in orig.items():
        if v == "1":
            if not ecrire(p, 2):
                ecrire(p, v)
        else:
            ecrire(p, v)
    orig.clear()

def dire(m):
    print(m, flush=True)

def traiter(ligne):
    mots = ligne.split()
    if not mots:
        return
    if mots[0] == "QUIT":
        restaurer()
        sys.exit(0)
    if mots[0] == "AUTO":
        restaurer()
        dire("OK")
    elif mots[0] == "MANUEL" and len(mots) == 4:
        en, pwm, val = mots[1:]
        if not (chemin_ok.match(en) and chemin_ok.match(pwm) and en == pwm + "_enable"
                and val.isdigit() and 0 <= int(val) <= 255):
            dire("ERR commande refusee")
            return
        if en not in orig:
            orig[en] = lire(en) or "2"
        if not ecrire(en, 1) or not ecrire(pwm, val):
            dire("ERR le pilote a refuse l'ecriture")
        else:
            dire("OK")
    else:
        dire("ERR commande inconnue")

fd = sys.stdin.fileno()
tampon = b""
dire("PRET")
while True:
    prets = select.select([fd], [], [], 2)[0]
    if temp_max() >= limite:
        restaurer()
        dire("SURCHAUFFE")
        sys.exit(3)
    if prets:
        data = os.read(fd, 4096)
        if not data:
            restaurer()
            sys.exit(0)
        tampon += data
    while b"\n" in tampon:
        ligne, tampon = tampon.split(b"\n", 1)
        traiter(ligne.decode(errors="replace"))
"""


def ventilos_controlables():
    """Ventilateurs réglables : le pilote expose pwmN et pwmN_enable."""
    res = []
    for hw in sorted(glob.glob(HWMON_RACINE + "/hwmon*")):
        nom = lire(hw + "/name", "?")
        for p in sorted(glob.glob(hw + "/pwm[0-9]")):
            if os.path.exists(p + "_enable"):
                res.append({"nom": f"{nom} · {os.path.basename(p)}", "pwm": p, "enable": p + "_enable"})
    return res


def collecter_infos(ecran=""):
    L = []

    def dmi(n):
        return lire(f"/sys/class/dmi/id/{n}")

    modele = f"{dmi('sys_vendor') or ''} {dmi('product_name') or ''}".strip()
    if modele:
        L.append(("Modèle", modele))
    if dmi("bios_version"):
        L.append(("BIOS", f"{dmi('bios_version')} ({dmi('bios_date') or '?'})"))
    os_nom = platform.platform()
    for ligne in (lire("/etc/os-release", "") or "").splitlines():
        if ligne.startswith("PRETTY_NAME="):
            os_nom = ligne.split("=", 1)[1].strip('"')
    L.append(("Système", os_nom))
    L.append(("Noyau", platform.release()))
    session = os.environ.get("XDG_SESSION_TYPE")
    if session:
        L.append(("Session graphique", session))
    cpu = "?"
    for ligne in (lire("/proc/cpuinfo", "") or "").splitlines():
        if ligne.startswith("model name"):
            cpu = ligne.split(":", 1)[1].strip()
            break
    L.append(("Processeur", cpu))
    L.append(("Cœurs logiques", str(os.cpu_count())))
    mem = {}
    for ligne in (lire("/proc/meminfo", "") or "").splitlines():
        p = ligne.split()
        if len(p) >= 2 and p[1].isdigit():
            mem[p[0].rstrip(":")] = int(p[1])
    if "MemTotal" in mem:
        L.append(("Mémoire RAM", f"{mem['MemTotal'] / 1048576:.1f} Go "
                                 f"({mem.get('MemAvailable', 0) / 1048576:.1f} Go disponibles)"))
    for ligne in commande(["lspci"]).splitlines():
        if any(k in ligne for k in ("VGA", "3D controller", "Display controller")):
            L.append(("Carte graphique", ligne.split(": ", 1)[-1]))
    if ecran:
        L.append(("Écran", ecran))
    for ligne in commande(["lsblk", "-d", "-n", "-o", "NAME,SIZE,ROTA,MODEL"]).splitlines():
        p = ligne.split(None, 3)
        if len(p) >= 3 and not p[0].startswith(("loop", "zram", "sr")):
            type_ = "SSD" if p[2] == "0" else "HDD"
            L.append((f"Disque {p[0]}", f"{p[1]} · {type_} · {p[3] if len(p) > 3 else ''}".strip(" ·")))
    for bat in sorted(glob.glob("/sys/class/power_supply/BAT*")):
        txt = f"{lire(bat + '/capacity', '?')} % ({lire(bat + '/status', '?')})"
        pleine = lire(bat + "/energy_full") or lire(bat + "/charge_full")
        conc = lire(bat + "/energy_full_design") or lire(bat + "/charge_full_design")
        if pleine and conc and pleine.isdigit() and conc.isdigit() and int(conc) > 0:
            txt += f" · santé {int(pleine) / int(conc) * 100:.0f} %"
        cyc = lire(bat + "/cycle_count")
        if cyc and cyc.isdigit() and int(cyc) > 0:
            txt += f" · {cyc} cycles"
        L.append((f"Batterie {os.path.basename(bat)}", txt))
    for ac in glob.glob("/sys/class/power_supply/A*/online"):
        L.append(("Secteur", "branché" if lire(ac) == "1" else "débranché"))
        break
    return L


def sur_batterie():
    for bat in glob.glob("/sys/class/power_supply/BAT*"):
        return lire(bat + "/status") == "Discharging"
    return False


# ==================================================================== benchmark
def charge_cpu(n):
    s = 0
    for i in range(n):
        s += (i * i) % 7
    return s


def _chrono(f, *a):
    t = time.perf_counter()
    f(*a)
    return time.perf_counter() - t


def test_cpu_mono(n, passes):
    return n / min(_chrono(charge_cpu, n) for _ in range(passes)) / 1e6


def test_cpu_multi(n, passes):
    nb = os.cpu_count() or 1
    meilleur = None
    with mp.get_context("spawn").Pool(nb) as pool:
        pool.map(charge_cpu, [1000] * nb)
        for _ in range(passes):
            t = time.perf_counter()
            pool.map(charge_cpu, [n] * nb)
            d = time.perf_counter() - t
            meilleur = d if meilleur is None else min(meilleur, d)
    return n * nb / meilleur / 1e6


def test_sha256(mo):
    bloc = os.urandom(1 << 20)
    h = hashlib.sha256()
    t = time.perf_counter()
    for _ in range(mo):
        h.update(bloc)
    return mo / (time.perf_counter() - t)


def test_ram(taille_mo, passes):
    taille = taille_mo * 1024 * 1024
    src, dst = memoryview(bytearray(taille)), memoryview(bytearray(taille))
    meilleur = None
    for _ in range(passes):
        t = time.perf_counter()
        dst[:] = src
        d = time.perf_counter() - t
        meilleur = d if meilleur is None else min(meilleur, d)
    return taille / meilleur / 1e9


def test_disque(dossier, taille_mo):
    res = {}
    bloc = os.urandom(4 * 1024 * 1024)
    nb = max(1, taille_mo // 4)
    fd, chemin = tempfile.mkstemp(prefix="pctest_", dir=dossier)
    try:
        t = time.perf_counter()
        for _ in range(nb):
            os.write(fd, bloc)
        os.fsync(fd)
        res["disk_w"] = nb * 4 / (time.perf_counter() - t)
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        except (AttributeError, OSError):
            pass
        os.lseek(fd, 0, os.SEEK_SET)
        t = time.perf_counter()
        lu = 0
        while True:
            data = os.read(fd, 4 * 1024 * 1024)
            if not data:
                break
            lu += len(data)
        res["disk_r"] = lu / 1048576 / (time.perf_counter() - t)
        petit = os.urandom(4096)
        os.lseek(fd, 0, os.SEEK_SET)
        t = time.perf_counter()
        for _ in range(300):
            os.write(fd, petit)
            os.fsync(fd)
        res["disk_4k"] = 300 / (time.perf_counter() - t)
    finally:
        os.close(fd)
        os.remove(chemin)
    return res


NOMS_BENCH = {
    "cpu_mono": ("CPU mono-cœur", "M it/s"),
    "cpu_multi": ("CPU multi-cœur", "M it/s"),
    "sha256": ("SHA-256", "Mo/s"),
    "ram": ("Copie mémoire", "Go/s"),
    "disk_w": ("Disque · écriture séquentielle", "Mo/s"),
    "disk_r": ("Disque · lecture séquentielle", "Mo/s"),
    "disk_4k": ("Disque · écritures 4K synchrones", "IOPS"),
}


class OngletBench(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=10)
        barre = ttk.Frame(self)
        barre.pack(fill="x")
        self.btn = ttk.Button(barre, text="▶  Lancer le benchmark", command=self.lancer)
        self.btn.pack(side="left")
        self.rapide = tk.BooleanVar(value=False)
        self.avec_disque = tk.BooleanVar(value=True)
        self.taille = tk.StringVar(value="512")
        ttk.Checkbutton(barre, text="Test rapide", variable=self.rapide).pack(side="left", padx=(15, 5))
        ttk.Checkbutton(barre, text="Inclure le disque", variable=self.avec_disque).pack(side="left", padx=5)
        ttk.Label(barre, text="Taille (Mo) :").pack(side="left", padx=(10, 2))
        ttk.Combobox(barre, textvariable=self.taille, values=("128", "256", "512", "1024"),
                     width=6, state="readonly").pack(side="left")
        self.avert = ttk.Label(self, text="", foreground="#b36b00")
        self.avert.pack(fill="x", pady=(8, 0))
        self.prog = ttk.Progressbar(self, maximum=7)
        self.prog.pack(fill="x", pady=8)
        self.etat = ttk.Label(self, text="Prêt. Branche le secteur et ferme les autres applications.")
        self.etat.pack(fill="x")
        self.arbre = ttk.Treeview(self, columns=("res", "unite", "ecart"), height=10)
        self.arbre.heading("#0", text="Test")
        self.arbre.heading("res", text="Résultat")
        self.arbre.heading("unite", text="Unité")
        self.arbre.heading("ecart", text="Écart vs test précédent")
        self.arbre.column("#0", width=300)
        self.arbre.column("res", width=110, anchor="e")
        self.arbre.column("unite", width=80)
        self.arbre.column("ecart", width=170, anchor="e")
        self.arbre.tag_configure("plus", foreground="#1a8a3a")
        self.arbre.tag_configure("moins", foreground="#c0392b")
        self.arbre.pack(fill="both", expand=True, pady=(8, 0))
        ttk.Label(self, text="Scores CPU en Python pur : ils servent à comparer cette machine à elle-même "
                             "dans le temps (avant/après une mise à jour, un nettoyage…).",
                  foreground="#666").pack(fill="x", pady=(8, 0))
        self.q = queue.Queue()
        self.en_cours = False

    def lancer(self):
        if self.en_cours:
            return
        self.en_cours = True
        self.btn.state(["disabled"])
        self.arbre.delete(*self.arbre.get_children())
        self.prog["value"] = 0
        self.avert.config(text="⚠ Sur batterie : branche le secteur pour des résultats fiables."
                          if sur_batterie() else "")
        opts = (self.rapide.get(), self.avec_disque.get(), int(self.taille.get()))
        threading.Thread(target=self._travail, args=opts, daemon=True).start()
        self.after(100, self._poll)

    def _travail(self, rapide, avec_disque, taille):
        try:
            n = 2_000_000 if rapide else 5_000_000
            passes = 2 if rapide else 3
            res = {}
            self.q.put(("etat", "CPU mono-cœur…"))
            res["cpu_mono"] = test_cpu_mono(n, passes)
            self.q.put(("etat", "CPU multi-cœur…"))
            res["cpu_multi"] = test_cpu_multi(n, passes)
            self.q.put(("etat", "SHA-256…"))
            res["sha256"] = test_sha256(128 if rapide else 512)
            self.q.put(("etat", "Mémoire…"))
            res["ram"] = test_ram(128 if rapide else 256, passes)
            if avec_disque:
                self.q.put(("etat", "Disque (peut durer un moment)…"))
                res.update(test_disque(os.path.expanduser("~"), 128 if rapide else taille))
            self.q.put(("fin", res))
        except Exception as e:  # noqa
            self.q.put(("err", str(e)))

    def _poll(self):
        try:
            while True:
                type_, val = self.q.get_nowait()
                if type_ == "etat":
                    self.etat.config(text=val)
                    self.prog["value"] += 1
                elif type_ == "fin":
                    self._afficher(val)
                    return
                elif type_ == "err":
                    self.etat.config(text="Erreur : " + val)
                    self._fini()
                    return
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _fini(self):
        self.en_cours = False
        self.btn.state(["!disabled"])

    def _afficher(self, res):
        precedent = {}
        hist = []
        try:
            with open(HIST_FICHIER) as f:
                hist = json.load(f)
            precedent = hist[-1]["resultats"] if hist else {}
        except (OSError, ValueError, KeyError):
            pass
        for cle, val in res.items():
            nom, unite = NOMS_BENCH[cle]
            ecart, tag = "", ""
            if precedent.get(cle):
                diff = (val - precedent[cle]) / precedent[cle] * 100
                ecart = f"{diff:+.1f} %"
                tag = "plus" if diff >= 0 else "moins"
            self.arbre.insert("", "end", text=nom, values=(f"{val:,.1f}".replace(",", " "), unite, ecart),
                              tags=(tag,))
        hist.append({"date": datetime.datetime.now().strftime("%d/%m/%Y %H:%M"),
                     "resultats": {k: round(v, 3) for k, v in res.items()}})
        try:
            with open(HIST_FICHIER, "w") as f:
                json.dump(hist[-50:], f, indent=2)
        except OSError:
            pass
        self.prog["value"] = 7
        self.etat.config(text="Terminé · résultats enregistrés dans " + HIST_FICHIER)
        self._fini()


# ======================================================================== infos
class OngletInfos(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=10)
        barre = ttk.Frame(self)
        barre.pack(fill="x")
        ttk.Button(barre, text="↻ Actualiser", command=self.actualiser).pack(side="left")
        ttk.Button(barre, text="Copier", command=self.copier).pack(side="left", padx=6)
        ttk.Button(barre, text="Exporter en .txt", command=self.exporter).pack(side="left")
        self.arbre = ttk.Treeview(self, columns=("val",), show="tree")
        self.arbre.column("#0", width=200, stretch=False)
        self.arbre.column("val", width=600)
        self.arbre.pack(fill="both", expand=True, pady=(10, 0))
        self.infos = []
        self.after(200, self.actualiser)

    def actualiser(self):
        ecran = f"{self.winfo_screenwidth()}×{self.winfo_screenheight()} px"
        self.infos = collecter_infos(ecran)
        self.arbre.delete(*self.arbre.get_children())
        for k, v in self.infos:
            self.arbre.insert("", "end", text=k, values=(v,))

    def _texte(self):
        return "\n".join(f"{k} : {v}" for k, v in self.infos)

    def copier(self):
        self.clipboard_clear()
        self.clipboard_append(self._texte())

    def exporter(self):
        chemin = os.path.expanduser(f"~/infos_pc_{datetime.date.today()}.txt")
        with open(chemin, "w") as f:
            f.write(self._texte() + "\n")
        messagebox.showinfo("Export", f"Fichier enregistré :\n{chemin}")


# ====================================================================== clavier
QW = {49: "`", 20: "-", 21: "=", 34: "[", 35: "]", 47: ";", 48: "'", 51: "\\", 94: "<",
      59: ",", 60: ".", 61: "/"}
QW.update(zip(range(10, 20), "1234567890"))
QW.update(zip(range(24, 34), "QWERTYUIOP"))
QW.update(zip(range(38, 47), "ASDFGHJKL"))
QW.update(zip(range(52, 59), "ZXCVBNM"))
AZ = dict(QW)
AZ.update({49: "²", 20: ")", 21: "=", 34: "^", 35: "$", 47: "M", 48: "ù", 51: "*",
           59: ";", 60: ":", 61: "!", 58: ","})
AZ.update(zip(range(10, 20), ["&", "é", '"', "'", "(", "-", "è", "_", "ç", "à"]))
AZ.update(zip(range(24, 34), "AZERTYUIOP"))
AZ.update(zip(range(38, 47), "QSDFGHJKL"))
AZ.update(zip(range(52, 58), "WXCVBN"))

LIGNES_CLAVIER = [
    [(9, 1, "Échap")] + [(c, 1, f"F{i + 1}") for i, c in enumerate((67, 68, 69, 70, 71, 72, 73, 74, 75, 76, 95, 96))]
    + [(119, 1, "Suppr")],
    [(49, 1, None)] + [(c, 1, None) for c in range(10, 20)] + [(20, 1, None), (21, 1, None), (22, 2, "⌫")],
    [(23, 1.5, "Tab")] + [(c, 1, None) for c in range(24, 34)] + [(34, 1, None), (35, 1, None)],
    [(66, 1.75, "Maj ⇪")] + [(c, 1, None) for c in range(38, 48)] + [(48, 1, None), (51, 1, None),
                                                                      (36, 1.5, "Entrée")],
    [(50, 1.25, "Maj ⇧"), (94, 1, None)] + [(c, 1, None) for c in range(52, 62)] + [(62, 2.75, "Maj ⇧")],
    [(37, 1.25, "Ctrl"), (133, 1.25, "Super"), (64, 1.25, "Alt"), (65, 5.5, "Espace"), (108, 1.25, "Alt Gr"),
     (105, 1.25, "Ctrl"), (113, 1, "←"), (111, 1, "↑"), (116, 1, "↓"), (114, 1, "→")],
    [(107, 1, "Impr"), (118, 1, "Inser"), (110, 1, "Début"), (115, 1, "Fin"), (112, 1, "Pg ↑"), (117, 1, "Pg ↓"),
     (135, 1, "Menu"), (77, 1, "Num"), (121, 1, "🔇"), (122, 1, "🔉"), (123, 1, "🔊"), (232, 1, "☀−"),
     (233, 1, "☀+")],
]


class OngletClavier(ttk.Frame):
    U = 50

    def __init__(self, master):
        super().__init__(master, padding=10)
        barre = ttk.Frame(self)
        barre.pack(fill="x")
        ttk.Label(barre, text="Disposition :").pack(side="left")
        self.dispo = tk.StringVar(value="AZERTY")
        for d in ("AZERTY", "QWERTY"):
            ttk.Radiobutton(barre, text=d, value=d, variable=self.dispo, command=self.maj_labels).pack(
                side="left", padx=5)
        ttk.Button(barre, text="Réinitialiser", command=self.reset).pack(side="right")
        largeur = int(max(sum(w for _, w, _ in l) for l in LIGNES_CLAVIER) * self.U) + 20
        hauteur = len(LIGNES_CLAVIER) * self.U + 20
        self.c = tk.Canvas(self, width=largeur, height=hauteur, bg="#f5f5f5", highlightthickness=2,
                           highlightbackground="#bbb", highlightcolor="#3584e4", takefocus=True)
        self.c.pack(pady=10)
        self.rects, self.textes, self.fixes = {}, {}, {}
        for r, ligne in enumerate(LIGNES_CLAVIER):
            x = 10
            for code, w, fixe in ligne:
                x0, y0 = x, 10 + r * self.U
                self.rects[code] = self.c.create_rectangle(x0, y0, x0 + w * self.U - 4, y0 + self.U - 4,
                                                           fill="#e8e8e8", outline="#999", width=1)
                self.textes[code] = self.c.create_text(x0 + (w * self.U - 4) / 2, y0 + (self.U - 4) / 2,
                                                       text="", font=("Sans", 11, "bold"))
                if fixe:
                    self.fixes[code] = fixe
                x += w * self.U
        self.testees, self.autres = set(), set()
        self.info = ttk.Label(self, text="Clique dans la zone ci-dessus puis appuie sur toutes les touches.",
                              font=("Sans", 11))
        self.info.pack(fill="x")
        self.compte = ttk.Label(self, text="")
        self.compte.pack(fill="x", pady=4)
        ttk.Label(self, foreground="#666", wraplength=820, text=(
            "Les touches vertes ont bien été détectées. Fn seule n'envoie aucun signal, et certaines touches "
            "(Super, volume, luminosité) peuvent être interceptées par le bureau avant d'arriver ici."
        )).pack(fill="x")
        self.c.bind("<KeyPress>", self.presse)
        self.c.bind("<KeyRelease>", self.relache)
        self.c.bind("<Button-1>", lambda e: self.c.focus_set())
        self.maj_labels()
        self.maj_compte()

    def au_focus(self):
        self.c.focus_set()

    def maj_labels(self):
        table = AZ if self.dispo.get() == "AZERTY" else QW
        for code, t in self.textes.items():
            txt = self.fixes.get(code) or table.get(code, "?")
            taille = 11 if len(txt) <= 2 else 9
            self.c.itemconfig(t, text=txt, font=("Sans", taille, "bold"))

    def maj_compte(self):
        txt = f"Touches testées : {len(self.testees)} / {len(self.rects)}"
        if self.autres:
            txt += f"   ·   autres touches détectées : {len(self.autres)}"
        self.compte.config(text=txt)

    def presse(self, e):
        self.info.config(text=f"Dernière touche : keysym = {e.keysym}   ·   code = {e.keycode}   ·   "
                              f"caractère = {e.char!r}")
        if e.keycode in self.rects:
            self.c.itemconfig(self.rects[e.keycode], fill="#ffa500")
            self.testees.add(e.keycode)
        else:
            self.autres.add(e.keycode)
        self.maj_compte()
        return "break"

    def relache(self, e):
        if e.keycode in self.rects:
            self.c.itemconfig(self.rects[e.keycode], fill="#7bd88f")
        return "break"

    def reset(self):
        self.testees.clear()
        self.autres.clear()
        for r in self.rects.values():
            self.c.itemconfig(r, fill="#e8e8e8")
        self.maj_compte()
        self.c.focus_set()


# ======================================================================== souris
class OngletSouris(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=10)
        ttk.Label(self, text="Clique (gauche / milieu / droit), double-clique, fais défiler (molette ou deux doigts) "
                             "et bouge le curseur dans la zone du bas.").pack(anchor="w")
        self.c = tk.Canvas(self, width=520, height=170, bg="#f5f5f5", highlightthickness=1,
                           highlightbackground="#bbb")
        self.c.pack(pady=8)
        self.zones = {
            1: self.c.create_rectangle(20, 20, 200, 150, fill="#e8e8e8", outline="#888", width=2),
            2: self.c.create_rectangle(215, 20, 305, 150, fill="#e8e8e8", outline="#888", width=2),
            3: self.c.create_rectangle(320, 20, 500, 150, fill="#e8e8e8", outline="#888", width=2),
        }
        for x, t in ((110, "Gauche"), (260, "Milieu"), (410, "Droit")):
            self.c.create_text(x, 85, text=t, font=("Sans", 12, "bold"))
        self.nb = {"g": 0, "m": 0, "d": 0, "dbl": 0, "haut": 0, "bas": 0, "gauche": 0, "droite": 0}
        self.resume = ttk.Label(self, font=("Sans", 11))
        self.resume.pack(anchor="w")
        for b in (1, 2, 3):
            self.c.bind(f"<ButtonPress-{b}>", lambda e, b=b: self.clic(b, True))
            self.c.bind(f"<ButtonRelease-{b}>", lambda e, b=b: self.clic(b, False))
        self.c.bind("<Double-Button-1>", lambda e: self.compter("dbl"))
        self.c.bind("<Button-4>", lambda e: self.compter("haut"))
        self.c.bind("<Button-5>", lambda e: self.compter("bas"))
        self.c.bind("<Shift-Button-4>", lambda e: self.compter("gauche"))
        self.c.bind("<Shift-Button-5>", lambda e: self.compter("droite"))
        self.c.bind("<MouseWheel>", lambda e: self.compter("haut" if e.delta > 0 else "bas"))
        ttk.Label(self, text="Zone de mouvement (pavé tactile / souris) :").pack(anchor="w", pady=(10, 2))
        self.z = tk.Canvas(self, height=170, bg="white", highlightthickness=1, highlightbackground="#bbb",
                           cursor="crosshair")
        self.z.pack(fill="both", expand=True)
        self.pos = ttk.Label(self, text="Position : –")
        self.pos.pack(anchor="w")
        ttk.Button(self, text="Effacer la zone", command=lambda: self.z.delete("all")).pack(anchor="e")
        self.dernier = None
        self.z.bind("<Motion>", self.bouge)
        self.z.bind("<B1-Motion>", self.bouge)
        self.maj()

    def clic(self, b, enfonce):
        self.c.itemconfig(self.zones[b], fill="#ffa500" if enfonce else "#7bd88f")
        if enfonce:
            self.compter({1: "g", 2: "m", 3: "d"}[b])

    def compter(self, cle):
        self.nb[cle] += 1
        self.maj()

    def maj(self):
        n = self.nb
        self.resume.config(text=f"Clics — gauche : {n['g']} · milieu : {n['m']} · droit : {n['d']} · "
                                f"doubles : {n['dbl']}   |   Défilement — ↑ {n['haut']} · ↓ {n['bas']} · "
                                f"← {n['gauche']} · → {n['droite']}")

    def bouge(self, e):
        if self.dernier:
            self.z.create_line(*self.dernier, e.x, e.y, fill="#3584e4", width=2)
        self.dernier = (e.x, e.y)
        self.pos.config(text=f"Position : {e.x}, {e.y}")


# ======================================================================== écran
class PleinEcran(tk.Toplevel):
    COULEURS = [("Noir", "#000000"), ("Blanc", "#ffffff"), ("Rouge", "#ff0000"), ("Vert", "#00ff00"),
                ("Bleu", "#0000ff"), ("Gris moyen", "#808080"), ("Dégradé de gris", None),
                ("Barres de couleur", None)]

    def __init__(self, master):
        super().__init__(master)
        self.attributes("-fullscreen", True)
        self.configure(bg="black")
        self.i = 0
        self.c = tk.Canvas(self, highlightthickness=0, bd=0)
        self.c.pack(fill="both", expand=True)
        self.c.bind("<Button-1>", lambda e: self.change(1))
        self.c.bind("<Button-3>", lambda e: self.change(-1))
        self.bind("<Key>", self.touche)
        self.c.bind("<Configure>", lambda e: self.dessiner())
        self.after(150, self.focus_force)

    def touche(self, e):
        if e.keysym == "Escape":
            self.destroy()
        elif e.keysym in ("space", "Right", "Return", "Down"):
            self.change(1)
        elif e.keysym in ("Left", "BackSpace", "Up"):
            self.change(-1)

    def change(self, d):
        self.i = (self.i + d) % len(self.COULEURS)
        self.dessiner()

    def dessiner(self):
        c = self.c
        c.delete("all")
        w, h = max(c.winfo_width(), 1), max(c.winfo_height(), 1)
        nom, col = self.COULEURS[self.i]
        if col:
            c.configure(bg=col)
        elif nom.startswith("Dégradé"):
            c.configure(bg="black")
            for k in range(256):
                c.create_rectangle(w * k / 256, 0, w * (k + 1) / 256 + 1, h,
                                   fill=f"#{k:02x}{k:02x}{k:02x}", outline="")
        else:
            c.configure(bg="black")
            barres = ["#ffffff", "#ffff00", "#00ffff", "#00ff00", "#ff00ff", "#ff0000", "#0000ff", "#000000"]
            for k, b in enumerate(barres):
                c.create_rectangle(w * k / 8, 0, w * (k + 1) / 8 + 1, h, fill=b, outline="")
        c.create_text(15, h - 15, anchor="sw", fill="#888", tag="aide", font=("Sans", 11),
                      text=f"{nom} ({self.i + 1}/{len(self.COULEURS)}) — clic / Espace : suivant · "
                           f"clic droit : précédent · Échap : quitter")
        self.after(3000, lambda: c.delete("aide"))


class OngletEcran(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=15)
        ttk.Label(self, font=("Sans", 12), wraplength=800, text=(
            "Test des pixels morts et de l'uniformité : l'écran passe en plein écran avec des couleurs unies "
            "(noir, blanc, rouge, vert, bleu, gris, dégradé, barres). Cherche les points qui restent allumés "
            "ou éteints, les taches ou les zones plus lumineuses.")).pack(anchor="w")
        ttk.Button(self, text="▶  Lancer le test d'écran (plein écran)", command=self.lancer).pack(pady=15,
                                                                                                   anchor="w")
        self.res = ttk.Label(self, font=("Sans", 11))
        self.res.pack(anchor="w")
        self.after(300, lambda: self.res.config(
            text=f"Résolution détectée : {self.winfo_screenwidth()}×{self.winfo_screenheight()} px"))

    def lancer(self):
        PleinEcran(self.winfo_toplevel())


# ========================================================================== son
def generer_wav(chemin, gauche, droite, freq=440.0, duree=1.0, balayage=None, rate=44100):
    n = int(rate * duree)
    fondu = int(rate * 0.01)
    phase = 0.0
    out = bytearray()
    for i in range(n):
        f = freq if balayage is None else balayage[0] + (balayage[1] - balayage[0]) * i / n
        phase += 2 * math.pi * f / rate
        v = math.sin(phase) * 0.4
        v *= min(1.0, i / fondu, (n - i) / fondu)
        out += struct.pack("<hh", int(v * 32767 * gauche), int(v * 32767 * droite))
    with wave.open(chemin, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(out))


def commande_lecture(chemin):
    for outil, args in (("paplay", []), ("pw-play", []), ("aplay", ["-q"])):
        if shutil.which(outil):
            return [outil] + args + [chemin]
    return None


class OngletSon(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=15)
        self.dossier = tempfile.mkdtemp(prefix="pctest_son_")
        ttk.Label(self, text="Haut-parleurs", font=("Sans", 12, "bold")).pack(anchor="w")
        ligne = ttk.Frame(self)
        ligne.pack(anchor="w", pady=8)
        for txt, g, d, bal in (("◀ Gauche", 1, 0, None), ("Droite ▶", 0, 1, None), ("◀ ▶ Les deux", 1, 1, None),
                               ("Balayage 100 Hz → 8 kHz", 1, 1, (100, 8000))):
            ttk.Button(ligne, text=txt, command=lambda g=g, d=d, b=bal: self.jouer(g, d, b)).pack(side="left", padx=4)
        ttk.Label(self, text="Règle le volume du système avant de commencer.", foreground="#666").pack(anchor="w")
        ttk.Separator(self).pack(fill="x", pady=15)
        ttk.Label(self, text="Microphone", font=("Sans", 12, "bold")).pack(anchor="w")
        self.btn_micro = ttk.Button(self, text="🎤  Enregistrer 3 s puis réécouter", command=self.micro)
        self.btn_micro.pack(anchor="w", pady=8)
        self.etat = ttk.Label(self, text="", font=("Sans", 11))
        self.etat.pack(anchor="w")
        self.q = queue.Queue()
        self.after(150, self._poll)

    def _poll(self):
        try:
            while True:
                self.etat.config(text=self.q.get_nowait())
        except queue.Empty:
            pass
        self.after(150, self._poll)

    def jouer(self, g, d, bal):
        def travail():
            chemin = os.path.join(self.dossier, "test.wav")
            generer_wav(chemin, g, d, duree=3.0 if bal else 1.0, balayage=bal)
            cmd = commande_lecture(chemin)
            if not cmd:
                self.q.put("Aucun lecteur audio trouvé (paplay, pw-play ou aplay).")
                return
            self.q.put("Lecture…")
            subprocess.run(cmd, capture_output=True)
            self.q.put("Terminé.")
        threading.Thread(target=travail, daemon=True).start()

    def micro(self):
        self.btn_micro.state(["disabled"])

        def travail():
            chemin = os.path.join(self.dossier, "micro.wav")
            try:
                if os.path.exists(chemin):
                    os.remove(chemin)
                if shutil.which("arecord"):
                    self.q.put("Enregistrement… parle maintenant (3 s)")
                    subprocess.run(["arecord", "-q", "-d", "3", "-f", "cd", "-t", "wav", chemin],
                                   capture_output=True, timeout=10)
                elif shutil.which("parecord"):
                    self.q.put("Enregistrement… parle maintenant (3 s)")
                    p = subprocess.Popen(["parecord", "--file-format=wav", chemin], stderr=subprocess.DEVNULL)
                    time.sleep(3)
                    p.send_signal(signal.SIGINT)
                    p.wait(timeout=5)
                else:
                    self.q.put("Aucun outil d'enregistrement (arecord / parecord). sudo apt install alsa-utils")
                    return
                if not os.path.exists(chemin) or os.path.getsize(chemin) < 100:
                    self.q.put("Aucun enregistrement obtenu : micro désactivé ou non détecté ?")
                    return
                with wave.open(chemin) as w:
                    donnees = array.array("h", w.readframes(w.getnframes()))
                pic = max((abs(x) for x in donnees), default=0) / 32768 * 100
                verdict = "OK" if pic > 2 else "très faible — micro muet ?"
                self.q.put(f"Niveau maximal capté : {pic:.0f} % ({verdict}). Lecture…")
                cmd = commande_lecture(chemin)
                if cmd:
                    subprocess.run(cmd, capture_output=True)
                self.q.put(f"Niveau maximal capté : {pic:.0f} % ({verdict}).")
            except Exception as e:  # noqa
                self.q.put(f"Erreur : {e}")
            finally:
                self.after(0, lambda: self.btn_micro.state(["!disabled"]))
        threading.Thread(target=travail, daemon=True).start()

    def nettoyer(self):
        shutil.rmtree(self.dossier, ignore_errors=True)


# ======================================================================= webcam
def lister_cameras():
    res = []
    for dev in glob.glob("/dev/video*"):
        n = dev[len("/dev/video"):]
        if n.isdigit():
            res.append((int(n), f"{dev} — {lire(f'/sys/class/video4linux/video{n}/name', 'caméra')}"))
    return sorted(res)


class OngletWebcam(ttk.Frame):
    RESOLUTIONS = ("Auto", "640x480", "1280x720", "1920x1080")

    def __init__(self, master):
        super().__init__(master, padding=10)
        self.cap = None
        self.derniere = None
        self.img = None
        self.cams = lister_cameras()
        barre = ttk.Frame(self)
        barre.pack(fill="x")
        self.choix = tk.StringVar(value=self.cams[0][1] if self.cams else "Aucune caméra détectée")
        self.combo = ttk.Combobox(barre, textvariable=self.choix, values=[c[1] for c in self.cams],
                                  width=38, state="readonly")
        self.combo.pack(side="left")
        self.res = tk.StringVar(value="Auto")
        ttk.Combobox(barre, textvariable=self.res, values=self.RESOLUTIONS, width=10,
                     state="readonly").pack(side="left", padx=6)
        self.btn = ttk.Button(barre, text="▶ Démarrer", command=self.basculer)
        self.btn.pack(side="left")
        ttk.Button(barre, text="📷 Photo", command=self.photo).pack(side="left", padx=6)
        self.miroir = tk.BooleanVar(value=True)
        ttk.Checkbutton(barre, text="Miroir", variable=self.miroir).pack(side="left")
        self.vue = tk.Label(self, bg="black", fg="#aaa", text="Caméra arrêtée", width=80, height=22)
        self.vue.pack(fill="both", expand=True, pady=8)
        self.info = ttk.Label(self, text="")
        self.info.pack(fill="x")
        if not self.cams:
            self.info.config(text="Aucun /dev/video* trouvé : webcam absente, désactivée (touche Fn ?) "
                                  "ou driver manquant.")
        elif charger_cv2() is None:
            self.info.config(text="OpenCV manquant :  sudo apt install python3-opencv")

    def basculer(self):
        if self.cap is None:
            self.demarrer()
        else:
            self.arreter()

    def demarrer(self):
        if not self.cams:
            return
        if charger_cv2() is None:
            if shutil.which("cheese"):
                subprocess.Popen(["cheese"])
            messagebox.showwarning("OpenCV manquant", "Installe OpenCV :\n\nsudo apt install python3-opencv\n\n"
                                                       "(Cheese a été lancé à la place s'il est installé.)")
            return
        idx = self.cams[self.combo.current() if self.combo.current() >= 0 else 0][0]
        cap = cv2.VideoCapture(idx, cv2.CAP_V4L2)
        if not cap.isOpened():
            messagebox.showerror("Webcam", f"Impossible d'ouvrir /dev/video{idx}.\n\nEssaie un autre périphérique "
                                           "de la liste (les caméras en exposent souvent 2) ou vérifie que ton "
                                           "utilisateur est dans le groupe « video ».")
            return
        if self.res.get() != "Auto":
            w, h = map(int, self.res.get().split("x"))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        self.cap = cap
        self.echecs, self.n, self.t0, self.fps = 0, 0, time.time(), 0.0
        self.btn.config(text="■ Arrêter")
        self._boucle()

    def arreter(self):
        if self.cap is not None:
            self.cap.release()
        self.cap = None
        self.btn.config(text="▶ Démarrer")
        self.vue.config(image="", text="Caméra arrêtée")
        self.img = None

    def _boucle(self):
        if self.cap is None:
            return
        ok, frame = self.cap.read()
        if ok:
            self.echecs = 0
            self.derniere = frame
            h, w = frame.shape[:2]
            aff = cv2.flip(frame, 1) if self.miroir.get() else frame
            if w > 640:
                aff = cv2.resize(aff, (640, int(h * 640 / w)))
            ok2, buf = cv2.imencode(".ppm", aff)
            if ok2:
                self.img = tk.PhotoImage(data=buf.tobytes())
                self.vue.config(image=self.img, text="")
            self.n += 1
            t = time.time()
            if t - self.t0 >= 1:
                self.fps, self.n, self.t0 = self.n / (t - self.t0), 0, t
                self.info.config(text=f"Résolution : {w}×{h} · {self.fps:.0f} images/s · "
                                      f"luminosité moyenne : {frame.mean():.0f}/255")
        else:
            self.echecs += 1
            if self.echecs > 60:
                self.arreter()
                self.info.config(text="La caméra ne renvoie plus d'images.")
                return
        self.after(5, self._boucle)

    def photo(self):
        if self.derniere is None:
            messagebox.showinfo("Photo", "Démarre d'abord la caméra.")
            return
        dossier = os.path.expanduser("~/Images")
        os.makedirs(dossier, exist_ok=True)
        chemin = os.path.join(dossier, "webcam_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".jpg")
        cv2.imwrite(chemin, self.derniere)
        self.info.config(text=f"Photo enregistrée : {chemin}")

    def on_hide(self):
        self.arreter()

    def nettoyer(self):
        self.arreter()


# ========================================================= ventilateurs / thermique
class OngletThermique(ttk.Frame):
    HIST = 180

    def __init__(self, master):
        super().__init__(master, padding=10)
        barre = ttk.Frame(self)
        barre.pack(fill="x")
        self.duree = tk.StringVar(value="60")
        self.btn = ttk.Button(barre, text="🔥 Stress CPU", command=self.stress)
        self.btn.pack(side="left")
        ttk.Label(barre, text="durée (s) :").pack(side="left", padx=(8, 2))
        ttk.Combobox(barre, textvariable=self.duree, values=("30", "60", "120", "300"), width=5,
                     state="readonly").pack(side="left")
        ttk.Button(barre, text="■ Arrêter", command=self.arreter_stress).pack(side="left", padx=8)
        self.statut = ttk.Label(barre, text="")
        self.statut.pack(side="left", padx=8)
        corps = ttk.Frame(self)
        corps.pack(fill="both", expand=True, pady=8)
        self.arbre = ttk.Treeview(corps, columns=("val",), show="tree", height=9)
        self.arbre.column("#0", width=300, stretch=False)
        self.arbre.column("val", width=120, anchor="e")
        self.arbre.pack(side="left", fill="y")
        self.graph = tk.Canvas(corps, bg="white", highlightthickness=1, highlightbackground="#bbb")
        self.graph.pack(side="left", fill="both", expand=True, padx=(10, 0))
        self.ctl = ttk.LabelFrame(self, text="Contrôle manuel du ventilateur", padding=6)
        self.ctl.pack(fill="x", pady=(0, 6))
        self.ventilos = ventilos_controlables()
        self.manuel = tk.BooleanVar(value=False)
        self.helper, self.qh, self._job = None, queue.Queue(), None
        if not self.ventilos:
            ttk.Label(self.ctl, foreground="#666", wraplength=850, text=(
                "Ton système n'expose aucun réglage de vitesse (pas de fichier pwm dans /sys/class/hwmon), "
                "donc le ventilateur ne peut pas être piloté d'ici. Pour vérifier :  "
                "ls /sys/class/hwmon/hwmon*/pwm*   — si rien n'apparaît, la vitesse est gérée uniquement par "
                "le firmware de la carte mère.")).pack(anchor="w")
        else:
            ligne = ttk.Frame(self.ctl)
            ligne.pack(fill="x")
            self.cb_vent = ttk.Combobox(ligne, values=[v["nom"] for v in self.ventilos], state="readonly", width=26)
            self.cb_vent.current(0)
            self.cb_vent.pack(side="left")
            ttk.Checkbutton(ligne, text="Activer le contrôle manuel", variable=self.manuel,
                            command=self.basculer_manuel).pack(side="left", padx=10)
            self.lbl_pct = ttk.Label(ligne, text="50 %", width=6)
            self.scale = ttk.Scale(ligne, from_=10, to=100, orient="horizontal", length=300, command=self.glisse)
            self.scale.set(50)
            self.scale.state(["disabled"])
            self.scale.pack(side="left", padx=6)
            self.lbl_pct.pack(side="left")
            ttk.Label(self.ctl, foreground="#b36b00", wraplength=850, text=(
                "Réduire la vitesse peut faire chauffer la machine. Retour automatique au mode d'origine à 80 °C, "
                "en décochant la case, ou à la fermeture de l'application. Mot de passe administrateur demandé.")
                      ).pack(anchor="w", pady=(4, 0))
        self.aide = ttk.Label(self, foreground="#b36b00", wraplength=850)
        self.aide.pack(fill="x")
        self.log = tk.Text(self, height=4, state="disabled", bg="#f5f5f5")
        self.log.pack(fill="x", pady=(6, 0))
        self.h_temp, self.h_rpm, self.h_load = (deque(maxlen=self.HIST) for _ in range(3))
        self.rpm_max_vu = 3000
        self.courant = (None, None, 0)
        self.procs, self.fin = [], 0
        self.cpu_prec = cpu_times()
        self.after(300, self.tick)

    def ecrire(self, txt):
        self.log.config(state="normal")
        self.log.insert("end", txt + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def tick(self):
        try:
            self._tick_corps()
        except Exception as e:  # noqa
            msg = f"Erreur d'actualisation : {e}"
            if msg != getattr(self, "_derniere_err", None):
                self._derniere_err = msg
                self.ecrire(msg)
        self.after(1000, self.tick)

    def _tick_corps(self):
        fans, temps = capteurs()
        total, idle = cpu_times()
        dt, di = total - self.cpu_prec[0], idle - self.cpu_prec[1]
        charge = (1 - di / dt) * 100 if dt > 0 else 0
        self.cpu_prec = (total, idle)
        freq = frequence_mhz()

        self.arbre.delete(*self.arbre.get_children())
        if fans:
            for n, v in fans:
                self.arbre.insert("", "end", text="🌀 " + n, values=(f"{v} tr/min",))
        else:
            self.arbre.insert("", "end", text="🌀 Ventilateur", values=("non détecté",))
        for n, v in temps[:12]:
            self.arbre.insert("", "end", text="🌡 " + n, values=(f"{v:.0f} °C",))
        self.arbre.insert("", "end", text="⚙ Charge CPU", values=(f"{charge:.0f} %",))
        if freq:
            self.arbre.insert("", "end", text="⚙ Fréquence CPU moyenne", values=(f"{freq:.0f} MHz",))

        tmax = max((v for _, v in temps), default=None)
        rpm = max((v for _, v in fans), default=None)
        self.surveiller_helper(tmax)
        self.h_temp.append(tmax)
        self.h_rpm.append(rpm)
        self.h_load.append(charge)
        self.courant = (tmax, rpm, charge)
        if rpm:
            self.rpm_max_vu = max(self.rpm_max_vu, rpm * 1.25)
        self.aide.config(text="" if fans else (
            "Aucun ventilateur n'est exposé par le système (fréquent sur portable). Essaie :  "
            "sudo apt install lm-sensors && sudo sensors-detect  — puis relance. Dell : modprobe dell-smm-hwmon · "
            "ThinkPad : thinkpad_acpi fan_control=1. À défaut, lance le stress et écoute le ventilateur."))
        self.dessiner()

        if self.procs:
            self.stat["tmax"] = max(self.stat["tmax"], tmax or 0)
            self.stat["rpm"] = max(self.stat["rpm"], rpm or 0)
            if freq:
                self.stat["freqs"].append(freq)
            reste = int(self.fin - time.time())
            self.statut.config(text=f"Stress en cours… {max(reste, 0)} s")
            if reste <= 0 or all(p.poll() is not None for p in self.procs):
                self.arreter_stress(verdict=True)

    def dessiner(self):
        g = self.graph
        g.delete("all")
        w, h = max(g.winfo_width(), 50), max(g.winfo_height(), 120)
        m, haut = 8, 30
        tmax, rpm, charge = self.courant
        g.create_text(m, 6, anchor="nw", fill="#d94a3d", font=("Sans", 9, "bold"),
                      text="— température : " + (f"{tmax:.0f} °C" if tmax is not None else "n/d"))
        g.create_text(w * 0.36, 6, anchor="nw", fill="#2e9e4f", font=("Sans", 9, "bold"),
                      text=f"— charge CPU : {charge:.0f} %")
        g.create_text(w * 0.64, 6, anchor="nw", fill="#3584e4", font=("Sans", 9, "bold"),
                      text="— ventilateur : " + (f"{rpm} tr/min" if rpm is not None else "n/d"))
        g.create_line(0, haut - 4, w, haut - 4, fill="#ddd")
        for k in range(5):
            y = haut + (h - haut - m) * k / 4
            g.create_line(m, y, w - m, y, fill="#eee")

        def courbe(data, vmin, vmax, couleur):
            pts = []
            for i, v in enumerate(data):
                if v is None:
                    continue
                x = m + (w - 2 * m) * i / (self.HIST - 1)
                y = h - m - (h - haut - m) * (min(max(v, vmin), vmax) - vmin) / (vmax - vmin)
                pts += [x, y]
            if len(pts) >= 4:
                g.create_line(*pts, fill=couleur, width=2)
            elif len(pts) == 2:
                g.create_oval(pts[0] - 2, pts[1] - 2, pts[0] + 2, pts[1] + 2, fill=couleur, outline=couleur)

        courbe(self.h_load, 0, 100, "#2e9e4f")
        courbe(self.h_temp, 20, 100, "#d94a3d")
        if any(v is not None for v in self.h_rpm):
            courbe(self.h_rpm, 0, self.rpm_max_vu, "#3584e4")
        g.create_text(w - m, haut, anchor="ne", fill="#aaa", font=("Sans", 8),
                      text=f"ventilateur : 0 – {self.rpm_max_vu:.0f} tr/min")

    def stress(self):
        if self.procs:
            return
        d = int(self.duree.get())
        code = f"import time\nt=time.time()+{d}\nwhile time.time()<t: pass"
        self.procs = [subprocess.Popen([sys.executable, "-c", code]) for _ in range(os.cpu_count() or 1)]
        self.fin = time.time() + d
        fans, temps = capteurs()
        f0 = frequence_mhz()
        self.stat = {"t0": max((v for _, v in temps), default=None), "tmax": 0,
                     "r0": max((v for _, v in fans), default=0), "rpm": 0, "freqs": [f0] if f0 else []}
        self.btn.state(["disabled"])
        self.ecrire(f"▶ Stress de {d} s sur {len(self.procs)} cœurs lancé.")

    def arreter_stress(self, verdict=False):
        for p in self.procs:
            if p.poll() is None:
                p.terminate()
        had = bool(self.procs)
        self.procs = []
        self.btn.state(["!disabled"])
        self.statut.config(text="")
        if had and verdict:
            s = self.stat
            msg = "■ Terminé"
            if s["t0"] is not None:
                msg += f" — température : {s['t0']:.0f} → {s['tmax']:.0f} °C"
            if s["r0"] or s["rpm"]:
                msg += f" · ventilateur : {s['r0']} → {s['rpm']} tr/min"
                if s["rpm"] <= s["r0"] * 1.1 and s["t0"] is not None and s["tmax"] - s["t0"] > 10:
                    msg += " (le ventilateur n'a pas accéléré malgré la chauffe !)"
            if len(s["freqs"]) > 5:
                debut, fin = max(s["freqs"][:5]), min(s["freqs"][-5:])
                msg += f" · fréquence : {debut:.0f} → {fin:.0f} MHz"
                if fin < debut * 0.85:
                    msg += " (bridage thermique probable)"
            self.ecrire(msg)
        elif had:
            self.ecrire("■ Stress arrêté.")

    # ---- contrôle manuel du ventilateur
    def envoyer(self, txt):
        try:
            self.helper.stdin.write(txt + "\n")
            self.helper.stdin.flush()
            return True
        except (OSError, ValueError, AttributeError):
            return False

    def _lecteur(self, proc):
        for ligne in proc.stdout:
            self.qh.put(ligne.strip())

    def demarrer_helper(self):
        if self.helper is not None and self.helper.poll() is None:
            return True
        v = self.ventilos[self.cb_vent.current()]
        cmd = [sys.executable, "-c", HELPER, HWMON_RACINE, "85"]
        if not os.access(v["enable"], os.W_OK):
            if not shutil.which("pkexec"):
                messagebox.showerror("Ventilateur", "pkexec est introuvable : impossible d'obtenir les droits "
                                                    "administrateur.\nSolution : sudo apt install policykit-1")
                return False
            cmd = ["pkexec"] + cmd
        try:
            self.helper = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL, text=True, bufsize=1)
        except OSError as e:
            messagebox.showerror("Ventilateur", str(e))
            return False
        threading.Thread(target=self._lecteur, args=(self.helper,), daemon=True).start()
        return True

    def basculer_manuel(self):
        if self.manuel.get():
            if not self.demarrer_helper():
                self.manuel.set(False)
                return
            v = self.ventilos[self.cb_vent.current()]
            actuel = lire(v["pwm"])
            self.cb_vent.state(["disabled"])
            self.scale.state(["!disabled"])
            if actuel and actuel.isdigit():
                self.scale.set(min(100, max(10, round(int(actuel) * 100 / 255))))
            self.appliquer()
            self.ecrire("🎚 Contrôle manuel activé.")
        else:
            self.envoyer("AUTO")
            self.cb_vent.state(["!disabled"])
            self.scale.state(["disabled"])
            self.ecrire("🎚 Ventilateur remis en mode d'origine.")

    def glisse(self, v):
        self.lbl_pct.config(text=f"{int(float(v))} %")
        if self.manuel.get():
            if self._job:
                self.after_cancel(self._job)
            self._job = self.after(400, self.appliquer)

    def appliquer(self):
        self._job = None
        if not self.manuel.get():
            return
        v = self.ventilos[self.cb_vent.current()]
        pct = int(float(self.scale.get()))
        self.envoyer(f"MANUEL {v['enable']} {v['pwm']} {round(pct * 255 / 100)}")

    def desactiver_manuel(self, message):
        self.manuel.set(False)
        if self.ventilos:
            self.cb_vent.state(["!disabled"])
            self.scale.state(["disabled"])
        self.ecrire(message)

    def surveiller_helper(self, tmax):
        if not self.ventilos:
            return
        try:
            while True:
                ligne = self.qh.get_nowait()
                if ligne == "SURCHAUFFE":
                    self.desactiver_manuel("⚠ Sécurité : température très élevée, ventilateur remis en mode d'origine.")
                elif ligne.startswith("ERR"):
                    self.envoyer("AUTO")
                    self.desactiver_manuel("⚠ Réglage refusé par le pilote (" + ligne[4:] + ") — mode d'origine rétabli.")
        except queue.Empty:
            pass
        if self.helper is not None and self.helper.poll() is not None:
            if self.manuel.get():
                self.desactiver_manuel("Contrôle manuel indisponible (authentification annulée ou refusée).")
            self.helper = None
        if self.manuel.get() and tmax is not None and tmax >= 80:
            self.envoyer("AUTO")
            self.desactiver_manuel("⚠ Température ≥ 80 °C : ventilateur remis automatiquement en mode d'origine.")

    def nettoyer(self):
        self.arreter_stress()
        if self.helper is not None:
            self.envoyer("AUTO")
            self.envoyer("QUIT")
            try:
                self.helper.stdin.close()
                self.helper.wait(timeout=3)
            except Exception:  # noqa
                pass


# ============================================================== application
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PC Testeur — diagnostic de portable")
        self.geometry("1000x800")
        self.minsize(900, 620)
        try:
            ttk.Style().theme_use("clam")
        except tk.TclError:
            pass
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True)
        self.onglets = []
        for titre, cls in (("ℹ Infos", OngletInfos), ("⏱ Benchmark", OngletBench), ("⌨ Clavier", OngletClavier),
                           ("🖱 Souris", OngletSouris), ("🖥 Écran", OngletEcran), ("🔊 Son / micro", OngletSon),
                           ("📷 Webcam", OngletWebcam), ("🌀 Ventilateurs", OngletThermique)):
            f = cls(self.nb)
            self.nb.add(f, text=titre)
            self.onglets.append(f)
        self.courant = self.onglets[0]
        self.nb.bind("<<NotebookTabChanged>>", self.changement)
        self.protocol("WM_DELETE_WINDOW", self.quitter)

    def changement(self, _):
        nouveau = self.nametowidget(self.nb.select())
        if hasattr(self.courant, "on_hide") and nouveau is not self.courant:
            self.courant.on_hide()
        self.courant = nouveau
        if hasattr(nouveau, "au_focus"):
            self.after(50, nouveau.au_focus)

    def quitter(self):
        for f in self.onglets:
            if hasattr(f, "nettoyer"):
                f.nettoyer()
        self.destroy()


def creer_raccourci():
    chemin = os.path.abspath(__file__)
    dossier = os.path.expanduser("~/.local/share/applications")
    os.makedirs(dossier, exist_ok=True)
    fichier = os.path.join(dossier, "pc-testeur.desktop")
    with open(fichier, "w") as f:
        f.write("[Desktop Entry]\nType=Application\nName=PC Testeur\n"
                "Comment=Diagnostic et benchmark de PC portable\n"
                f"Exec=python3 \"{chemin}\"\nIcon=utilities-system-monitor\nTerminal=false\n"
                "Categories=System;Utility;\n")
    os.chmod(fichier, 0o755)
    print("Raccourci créé :", fichier, "(cherche « PC Testeur » dans le menu des applications)")


if __name__ == "__main__":
    if "--raccourci" in sys.argv:
        creer_raccourci()
    else:
        App().mainloop()
