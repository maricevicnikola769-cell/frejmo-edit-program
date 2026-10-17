#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FREJMO EDIT - Python verzija
============================

Pokretanje:   python frejmo_edit.py          (ili dupli klik na POKRENI.bat)

Šta radi ovaj program:
  * Pokreće lokalni server (samo na ovom računaru, 127.0.0.1) i otvara editor.
    Editor (tajmlajn, efekti, titlovi, pregled) izgleda i radi isto kao ranije.
  * EXPORT VIŠE NE RADI U BROWSERU. Browser samo nacrta svaki kadar i pošalje ga
    ovom programu; Python ih prosleđuje ffmpeg-u, koji kodira video (H.264 /
    H.265 / VP9 + AAC / Opus) i upisuje fajl pravo na disk.
    Nema više WebCodecs ograničenja: H.264 radi na svakoj rezoluciji (i 8K),
    nema ffmpeg.wasm konverzije, nema "0 KB" fajlova zbog memorije browsera.
  * I ostali izvozi (sličica, .srt, chapters, projekat .json) se snimaju preko
    Pythona - otvara se pravi Windows/macOS/Linux dijalog "Sačuvaj kao".

Potrebno:
  * Python 3.8+
  * ffmpeg - ako nije instaliran, program ponudi da sam instalira paket
    "imageio-ffmpeg" (sadrži gotov ffmpeg), ili ga instaliraj ručno.

Opcije:
  --port 8765        port lokalnog servera
  --out  FOLDER      podrazumevani folder za izvoz (Export Queue i kad nema dijaloga)
  --no-browser       ne otvaraj browser automatski
  --ffmpeg PUTANJA   tačna putanja do ffmpeg programa
  --hw {auto,off,nvenc,qsv,amf,videotoolbox}
                     hardverski enkoder (podrazumevano: off = softverski, najbolji kvalitet)
  --max-jobs N       najviše istovremenih exporta (podrazumevano 2)
  --idle-timeout S   posle koliko sekundi neaktivnosti se napušten export otkazuje (300)

Dodatni (opcioni) parametri koje editor može da pošalje u /api/export/start:
  frameFormat: "rgba" (podrazumevano) ili "rgb24"  - rgb24 smanjuje protok za 25%
  rateMode:    "capped-crf" (podrazumevano) ili "bitrate" - u "bitrate" režimu se
               poštuje izabrani bitrate, umesto kvalitet-prvo (CRF) režima
  hw:          "off" / "auto" / "nvenc" / "qsv" / "amf" / "videotoolbox"
"""

import argparse
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

APP_NAME = "FREJMO EDIT"
HERE = Path(__file__).resolve().parent
HTML_FILE = HERE / "frejmo_edit.html"
if not HTML_FILE.exists() and (HERE / "cadria_edit.html").exists():
    HTML_FILE = HERE / "cadria_edit.html"   # staro ime editora i dalje radi
TOKEN = secrets.token_urlsafe(24)       # štiti API od drugih sajtova u browseru
CHUNK = 4 * 1024 * 1024                 # čitanje tela zahteva u delovima od 4 MB
TMP_PREFIX = "frejmo_export_"
OLD_TMP_PREFIXES = ("cadria_export_",)   # ostaci iz stare verzije

FFMPEG = None                           # putanja do ffmpeg-a (postavlja se u main)
ENCODERS = set()                        # spisak enkodera koje ovaj ffmpeg ima
OUT_DIR = None                          # podrazumevani folder za izvoz
DEFAULT_HW = "off"                      # --hw
MAX_JOBS = 2                            # --max-jobs
IDLE_TIMEOUT = 300                      # --idle-timeout (sekunde)

# ekstenzije koje /api/save sme da upiše (editor izvozi samo ovo)
SAVE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".srt", ".vtt", ".json",
                   ".txt", ".csv", ".mp4", ".webm"}

log = logging.getLogger("frejmo")


def setup_logging():
    """Konzola + rotirajući log fajl pored programa (frejmo_edit.log)."""
    from logging.handlers import RotatingFileHandler
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.addHandler(sh)
    try:
        fh = RotatingFileHandler(HERE / "frejmo_edit.log", maxBytes=512 * 1024,
                                 backupCount=2, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)s  %(message)s"))
        log.addHandler(fh)
    except OSError:
        pass                              # folder samo za čitanje - preskoči log fajl


# --------------------------------------------------------------------------
#  ffmpeg
# --------------------------------------------------------------------------
def find_ffmpeg(explicit=None):
    """Traži ffmpeg: --ffmpeg, pa PATH, pa folder programa, pa imageio-ffmpeg."""
    if explicit:
        return explicit if Path(explicit).exists() else None
    found = shutil.which("ffmpeg")
    if found:
        return found
    for name in ("ffmpeg.exe", "ffmpeg"):
        local = HERE / name
        if local.exists():
            return str(local)
        local = HERE / "ffmpeg" / "bin" / name
        if local.exists():
            return str(local)
    try:
        import imageio_ffmpeg  # type: ignore
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def ensure_ffmpeg(explicit=None):
    path = find_ffmpeg(explicit)
    if path:
        return path
    print("\n[!] ffmpeg nije pronađen na ovom računaru.")
    print("    Mogu sam da instaliram Python paket 'imageio-ffmpeg' (sadrži gotov ffmpeg, ~30 MB).")
    try:
        answer = input("    Instaliraj sada? [D/n]: ").strip().lower()
    except EOFError:
        answer = "n"
    if answer in ("", "d", "da", "y", "yes"):
        subprocess.call([sys.executable, "-m", "pip", "install", "--user", "imageio-ffmpeg"])
        try:
            import importlib
            import site
            importlib.invalidate_caches()
            user_site = site.getusersitepackages()
            if user_site not in sys.path:
                sys.path.append(user_site)
            import imageio_ffmpeg  # type: ignore
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception as e:
            print("    Instalacija nije uspela:", e)
    print("\n    Instaliraj ffmpeg ručno (https://ffmpeg.org/download.html), ili stavi")
    print("    ffmpeg.exe u isti folder kao ovaj program, pa pokreni ponovo.")
    return None


def list_encoders(ffmpeg):
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True,
                             text=True, timeout=20).stdout
    except Exception:
        return set()
    enc = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS":
            enc.add(parts[1])
    return enc


# hardverski enkoderi po "porodici"; prvi koji radi se koristi
HW_FAMILIES = {
    "nvenc": {"h264": "h264_nvenc", "h265": "hevc_nvenc"},
    "qsv": {"h264": "h264_qsv", "h265": "hevc_qsv"},
    "amf": {"h264": "h264_amf", "h265": "hevc_amf"},
    "videotoolbox": {"h264": "h264_videotoolbox", "h265": "hevc_videotoolbox"},
}
_hw_ok_cache = {}


def hw_encoder_works(name):
    """Enkoder može biti naveden u ffmpeg-u, a da nema odgovarajući hardver.
    Zato ga jednom probamo na 1 sekundi crnog videa."""
    if name in _hw_ok_cache:
        return _hw_ok_cache[name]
    ok = False
    if name in ENCODERS:
        try:
            r = subprocess.run(
                [FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                 "color=c=black:s=256x256:r=10:d=1", "-pix_fmt", "yuv420p", "-c:v", name,
                 "-f", "null", "-"], capture_output=True, timeout=30)
            ok = r.returncode == 0
        except Exception:
            ok = False
    _hw_ok_cache[name] = ok
    return ok


def pick_hw_encoder(want, family):
    """want: off/auto/nvenc/qsv/amf/videotoolbox; family: h264/h265. Vraća ime ili None."""
    if want in (None, "", "off"):
        return None
    families = list(HW_FAMILIES) if want == "auto" else [want]
    for fam in families:
        name = HW_FAMILIES.get(fam, {}).get(family)
        if name and hw_encoder_works(name):
            return name
    return None


def pick_video_encoder(container, codec_mode, hw=None):
    """Vraća ime enkodera za izabrani kontejner/režim."""
    if container == "webm":
        order = ["libvpx-vp9", "libvpx"]
    else:
        family = "h265" if codec_mode == "maxres" else "h264"
        hw_name = pick_hw_encoder(hw if hw is not None else DEFAULT_HW, family)
        if hw_name:
            return hw_name
        order = ["libx265", "libx264"] if family == "h265" else ["libx264"]
    order += ["libx264", "mpeg4"]
    for e in order:
        if e in ENCODERS:
            return e
    raise RuntimeError("ffmpeg nema nijedan odgovarajući video enkoder (libx264/libx265/libvpx-vp9).")


def encoder_warning(enc, container):
    """Poruka za korisnika kad se desi tiha degradacija kvaliteta."""
    if enc == "mpeg4":
        return ("Ovaj ffmpeg nema libx264 - koristi se stariji MPEG-4 enkoder, "
                "slika će biti primetno lošija. Instaliraj pun ffmpeg za H.264.")
    return ""


def pick_audio_encoder(container):
    order = ["libopus", "opus"] if container == "webm" else ["aac", "libfdk_aac"]
    for e in order:
        if e in ENCODERS:
            return e
    return None


def fps_fraction(fps):
    """29.97 -> 30000/1001, 25 -> 25/1 ..."""
    fps = float(fps)
    for n in (24, 30, 48, 60, 120):                 # NTSC: 23.976, 29.97, 59.94...
        if abs(fps - n * 1000 / 1001) < 0.01:
            return Fraction(n * 1000, 1001)
    f = Fraction(fps).limit_denominator(1001)
    if f <= 0:
        f = Fraction(30)
    return f


PIX_FMT_BYTES = {"rgba": 4, "rgb24": 3}


def build_ffmpeg_cmd(job):
    W, H = job["width"], job["height"]
    fr = fps_fraction(job["fps"])
    container = job["container"]
    venc = job.get("encoder") or pick_video_encoder(container, job.get("codec", "compat"), job.get("hw"))
    br = max(500_000, int(job.get("bitrate") or 6_000_000))
    bitrate_mode = job.get("rateMode") == "bitrate"
    gop = max(1, round(float(fr) * 2))      # ključni kadar na ~2 s (lako premotavanje)
    pixels = W * H
    in_pix = job.get("frameFormat", "rgba")

    cmd = [FFMPEG, "-hide_banner", "-y", "-loglevel", "error", "-nostats",
           # video: sirovi kadrovi iz editora preko stdin-a
           "-thread_queue_size", "64",
           "-f", "rawvideo", "-pix_fmt", in_pix, "-s", f"{W}x{H}",
           "-framerate", f"{fr.numerator}/{fr.denominator}", "-i", "pipe:0"]
    if job.get("audio_path"):
        cmd += ["-thread_queue_size", "512",
                "-f", "f32le", "-ar", str(job["sample_rate"]), "-ac", "2",
                "-i", job["audio_path"]]

    # yuv420p traži parne dimenzije; BT.709 da boje budu iste u svakom plejeru
    vf = ("crop=trunc(iw/2)*2:trunc(ih/2)*2,"
          "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p")
    cmd += ["-map", "0:v:0"]
    if job.get("audio_path"):
        cmd += ["-map", "1:a:0"]
    cmd += ["-vf", vf, "-c:v", venc]

    if venc == "libx264":
        preset = "medium" if pixels <= 2560 * 1440 else ("fast" if pixels <= 3840 * 2160 else "faster")
        cmd += ["-preset", preset, "-profile:v", "high", "-g", str(gop)]
        if bitrate_mode:
            cmd += ["-b:v", str(br), "-maxrate", str(int(br * 1.5)), "-bufsize", str(int(br * 3))]
        else:
            cmd += ["-crf", "18", "-maxrate", str(int(br * 1.5)), "-bufsize", str(int(br * 3))]
    elif venc == "libx265":
        preset = "medium" if pixels <= 1920 * 1080 else "fast"
        cmd += ["-preset", preset, "-tag:v", "hvc1",
                "-x265-params", f"keyint={gop}:log-level=error"]
        if bitrate_mode:
            cmd += ["-b:v", str(br), "-maxrate", str(int(br * 1.2)), "-bufsize", str(int(br * 2.4))]
        else:
            cmd += ["-crf", "20", "-maxrate", str(int(br * 1.2)), "-bufsize", str(int(br * 2.4))]
    elif venc in ("libvpx-vp9", "libvpx"):
        cmd += ["-deadline", "good", "-cpu-used", "3", "-row-mt", "1", "-g", str(gop)]
        if bitrate_mode:
            cmd += ["-b:v", str(br)]
        else:
            cmd += ["-b:v", str(br), "-crf", "24"]
    elif venc in ("h264_nvenc", "hevc_nvenc"):
        cmd += ["-preset", "p5", "-rc", "vbr", "-cq", "20", "-b:v", str(br),
                "-maxrate", str(int(br * 1.5)), "-g", str(gop)]
        if venc == "hevc_nvenc":
            cmd += ["-tag:v", "hvc1"]
    elif venc in ("h264_qsv", "hevc_qsv"):
        cmd += ["-global_quality", "20", "-b:v", str(br), "-g", str(gop)]
        if venc == "hevc_qsv":
            cmd += ["-tag:v", "hvc1"]
    elif venc in ("h264_amf", "hevc_amf"):
        cmd += ["-quality", "quality", "-rc", "vbr_peak", "-b:v", str(br),
                "-maxrate", str(int(br * 1.5)), "-g", str(gop)]
        if venc == "hevc_amf":
            cmd += ["-tag:v", "hvc1"]
    elif venc in ("h264_videotoolbox", "hevc_videotoolbox"):
        cmd += ["-b:v", str(br), "-g", str(gop)]
        if venc == "hevc_videotoolbox":
            cmd += ["-tag:v", "hvc1"]
    else:
        cmd += ["-b:v", str(br), "-g", str(gop)]

    cmd += ["-colorspace", "bt709", "-color_primaries", "bt709",
            "-color_trc", "bt709", "-color_range", "tv"]

    if job.get("audio_path"):
        aenc = pick_audio_encoder(container)
        if aenc:
            cmd += ["-c:a", aenc, "-b:a", "192k", "-ar", "48000"]
        else:
            cmd += ["-an"]
        cmd += ["-shortest"]            # zvuk ne sme da produži video preko poslednjeg kadra

    if container == "mp4":
        cmd += ["-movflags", "+faststart", "-f", "mp4"]
    else:
        cmd += ["-f", "webm"]
    cmd += [job["part_path"]]
    return cmd


# --------------------------------------------------------------------------
#  Dijalog "Sačuvaj kao" (pravi, sistemski - ne iz browsera)
# --------------------------------------------------------------------------
_DIALOG_SCRIPT = r"""
import sys, json
try:
    import tkinter as tk
    from tkinter import filedialog
except Exception:
    print(json.dumps({"error": "notk"})); sys.exit(0)
a = json.loads(sys.argv[1])
root = tk.Tk(); root.withdraw()
try:
    root.attributes('-topmost', True)
except Exception:
    pass
root.update()
p = filedialog.asksaveasfilename(parent=root, title=a['title'], initialdir=a['dir'],
        initialfile=a['name'], defaultextension=a['ext'],
        filetypes=[(a['desc'], '*' + a['ext']), ('Svi fajlovi', '*.*')])
root.destroy()
print(json.dumps({"path": p or ""}))
"""

_dialog_lock = threading.Lock()
_last_dir = None


def ask_save_path(suggested_name, ext, desc):
    """Otvara sistemski dijalog. Vraća putanju, "" (otkazano) ili None (nema dijaloga)."""
    global _last_dir
    start_dir = _last_dir or OUT_DIR
    args = json.dumps({"title": f"{APP_NAME} - Sačuvaj kao", "dir": str(start_dir),
                       "name": suggested_name, "ext": ext, "desc": desc})
    with _dialog_lock:
        try:
            r = subprocess.run([sys.executable, "-c", _DIALOG_SCRIPT, args],
                               capture_output=True, text=True, timeout=3600)
            res = json.loads((r.stdout or "").strip().splitlines()[-1])
        except Exception:
            return None
    if res.get("error"):
        return None
    p = res.get("path", "")
    if p:
        if not p.lower().endswith(ext.lower()):
            p += ext
        _last_dir = str(Path(p).parent)
    return p


def safe_name(name, fallback="export"):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "-", (name or "").strip())
    name = name.strip(" .")                           # Windows ne trpi tačku/razmak na kraju
    if name in ("", ".", ".."):
        name = fallback
    # rezervisana Windows imena (CON, NUL, COM1...) - dodaj donju crtu
    if re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?", name):
        name = "_" + name
    return name[:150]


def unique_path(folder, filename):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / filename
    stem, suf = p.stem, p.suffix
    i = 2
    while p.exists():
        p = folder / f"{stem} ({i}){suf}"
        i += 1
    return p


def resolve_target(filename, ask, desc):
    """Bira gde se fajl snima: dijalog (ako je traženo i moguće) ili OUT_DIR."""
    filename = safe_name(filename)
    ext = Path(filename).suffix or ""
    if ask:
        p = ask_save_path(filename, ext, desc)
        if p == "":
            return None                       # korisnik je otkazao
        if p:
            return Path(p)
    return unique_path(OUT_DIR, filename)


def reveal_in_folder(path):
    path = Path(path)
    try:
        if sys.platform.startswith("win"):
            subprocess.Popen(["explorer", "/select,", str(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path.parent)])
        return True
    except Exception:
        return False


def cleanup_stale_temp(max_age_hours=12):
    """Briše ostatke prethodnih (srušenih) exporta iz privremenog foldera."""
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    try:
        for d in [x for pre in (TMP_PREFIX,) + OLD_TMP_PREFIXES
                  for x in Path(tempfile.gettempdir()).glob(pre + "*")]:
            try:
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
                    removed += 1
            except OSError:
                pass
    except OSError:
        pass
    if removed:
        log.info("Obrisano %d starih privremenih foldera.", removed)


# --------------------------------------------------------------------------
#  Export poslovi
# --------------------------------------------------------------------------
def copy_body(rfile, length, write):
    left = length
    while left > 0:
        chunk = rfile.read(min(CHUNK, left))
        if not chunk:
            raise RuntimeError("Veza je prekinuta usred slanja podataka.")
        write(chunk)
        left -= len(chunk)


class ExportJob:
    def __init__(self, params, target):
        self.id = uuid.uuid4().hex
        self.lock = threading.Lock()
        self.p = dict(params)
        self.target = Path(target)
        self.container = "webm" if self.p.get("container") == "webm" else "mp4"
        fmt = self.p.get("frameFormat", "rgba")
        self.frame_format = fmt if fmt in PIX_FMT_BYTES else "rgba"
        self.encoder = pick_video_encoder(self.container, self.p.get("codec", "compat"), self.p.get("hw"))
        self.tmpdir = Path(tempfile.mkdtemp(prefix=TMP_PREFIX))
        self.audio_path = self.tmpdir / "audio.f32" if self.p.get("audio") else None
        self.audio_fh = open(self.audio_path, "wb") if self.audio_path else None
        self.part_path = self.target.with_name(self.target.name + ".part")
        self.log_path = self.tmpdir / "ffmpeg.log"
        self.proc = None
        self.log_fh = None
        self.frames = 0
        self.frame_bytes = int(self.p["width"]) * int(self.p["height"]) * PIX_FMT_BYTES[self.frame_format]
        self.started = time.time()
        self.last_activity = time.time()
        self.finishing = False
        self.cmd = None

    def touch(self):
        self.last_activity = time.time()

    def add_audio(self, rfile, length):
        self.touch()
        if not self.audio_fh:
            raise RuntimeError("Ovaj export nema zvuk.")
        if self.proc:
            raise RuntimeError("Zvuk mora da stigne pre prvog kadra.")
        copy_body(rfile, length, self.audio_fh.write)

    def _start_ffmpeg(self):
        if self.audio_fh:
            self.audio_fh.close()
            self.audio_fh = None
            if self.audio_path.stat().st_size == 0:
                self.audio_path = None
        job = {
            "width": int(self.p["width"]), "height": int(self.p["height"]),
            "fps": float(self.p.get("fps") or 30), "container": self.container,
            "codec": self.p.get("codec", "compat"), "bitrate": self.p.get("bitrate"),
            "rateMode": self.p.get("rateMode", "capped-crf"),
            "frameFormat": self.frame_format, "encoder": self.encoder,
            "audio_path": str(self.audio_path) if self.audio_path else None,
            "sample_rate": int(self.p.get("sampleRate") or 48000),
            "part_path": str(self.part_path),
        }
        self.cmd = build_ffmpeg_cmd(job)
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.log_fh = open(self.log_path, "wb")
        flags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0
        self.proc = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=self.log_fh, creationflags=flags)

    def add_frame(self, rfile, length):
        self.touch()
        if length != self.frame_bytes:
            raise RuntimeError(f"Pogrešna veličina kadra ({length} umesto {self.frame_bytes} bajtova).")
        if not self.proc:
            self._start_ffmpeg()
        if self.proc.poll() is not None:
            raise RuntimeError("ffmpeg se prekinuo: " + self.log_tail())
        try:
            copy_body(rfile, length, self.proc.stdin.write)
        except (BrokenPipeError, OSError):
            raise RuntimeError("ffmpeg se prekinuo: " + self.log_tail())
        self.frames += 1

    def finish(self):
        self.touch()
        if not self.proc:
            raise RuntimeError("Nije primljen nijedan kadar.")
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        code = self.proc.wait()
        self.log_fh.close()                  # ffmpeg je gotov - log je potpun
        if code != 0 or not self.part_path.exists() or self.part_path.stat().st_size == 0:
            msg = f"ffmpeg je završio sa greškom ({code}): " + self.log_tail()
            self.cleanup(remove_part=True)
            raise RuntimeError(msg)
        if self.target.exists():
            self.target.unlink()
        os.replace(self.part_path, self.target)
        self.cleanup(remove_part=False)
        return {"path": str(self.target), "size": self.target.stat().st_size,
                "frames": self.frames, "seconds": round(time.time() - self.started, 1),
                "encoder": self.encoder}

    def abort(self):
        try:
            if self.audio_fh:
                self.audio_fh.close()
            if self.proc and self.proc.poll() is None:
                try:
                    self.proc.stdin.close()
                except Exception:
                    pass
                self.proc.kill()
                self.proc.wait(timeout=10)
        except Exception:
            pass
        self.cleanup(remove_part=True)

    def cleanup(self, remove_part):
        try:
            if self.log_fh and not self.log_fh.closed:
                self.log_fh.close()
        except Exception:
            pass
        if remove_part:
            try:
                self.part_path.unlink()
            except Exception:
                pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def log_tail(self):
        """Čita kraj ffmpeg loga; pre čitanja sačeka kratko da proces ispiše poslednje poruke."""
        try:
            if self.proc and self.proc.poll() is None:
                try:
                    self.proc.wait(timeout=2)
                except Exception:
                    pass
            if self.log_fh and not self.log_fh.closed:
                self.log_fh.flush()
            txt = self.log_path.read_text(errors="replace").strip()
            return txt[-800:] if txt else "(nema poruke)"
        except Exception:
            return "(nema poruke)"

    def status(self):
        el = max(time.time() - self.started, 0.001)
        return {"frames": self.frames, "elapsed": round(el, 1),
                "fps": round(self.frames / el, 2), "encoder": self.encoder}


JOBS = {}
JOBS_LOCK = threading.Lock()


def reaper_loop(stop_event):
    """Otkazuje napuštene exporte (browser se zatvorio/srušio usred izvoza)."""
    while not stop_event.wait(15):
        now = time.time()
        with JOBS_LOCK:
            stale = [j for j in JOBS.values()
                     if not j.finishing and now - j.last_activity > IDLE_TIMEOUT]
        for j in stale:
            if not j.lock.acquire(blocking=False):
                continue                      # zahtev je upravo u toku
            try:
                with JOBS_LOCK:
                    if JOBS.pop(j.id, None) is None:
                        continue
                log.warning("Export %s napušten (nema aktivnosti %ds) - otkazan.", j.id[:8], IDLE_TIMEOUT)
                j.abort()
            finally:
                j.lock.release()


# --------------------------------------------------------------------------
#  HTTP server
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FrejmoEdit/1.1"

    def log_message(self, fmt, *args):   # tiho - bez reda u konzoli za svaki kadar
        pass

    # ---- pomoćne ----
    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _host_ok(self):
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):                      # IPv6: [::1]:8765
            host = host.split("]")[0] + "]"
        else:
            host = host.split(":")[0]
        return host in ("127.0.0.1", "localhost", "[::1]")

    def _auth_ok(self):
        sent = self.headers.get("X-Frejmo-Token") or self.headers.get("X-Cadria-Token") or ""
        return self._host_ok() and secrets.compare_digest(sent, TOKEN)

    def _length(self):
        try:
            return max(0, int(self.headers.get("Content-Length", "0")))
        except ValueError:
            return 0

    def _json_body(self):
        n = self._length()
        if n > 10 * 1024 * 1024:
            raise RuntimeError("Zahtev je prevelik.")
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def _drain(self):
        n = self._length()
        try:
            copy_body(self.rfile, n, lambda b: None)
        except Exception:
            self.close_connection = True

    # ---- GET ----
    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden"})
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                html = HTML_FILE.read_text(encoding="utf-8")
            except FileNotFoundError:
                return self._send(500, f"Nema fajla {HTML_FILE.name} pored {Path(__file__).name}.",
                                  "text/plain; charset=utf-8")
            inject = ('<script>window.CADRIA_PY=window.FREJMO_PY={token:%s,version:1};</script>\n'
                      % json.dumps(TOKEN))
            if "<head>" in html:
                html = html.replace("<head>", "<head>\n" + inject, 1)
            else:                                    # <head lang=".."> ili bez head-a
                m = re.search(r"<head[^>]*>", html, re.I)
                html = (html[:m.end()] + "\n" + inject + html[m.end():]) if m else inject + html
            return self._send(200, html, "text/html; charset=utf-8")
        if path == "/favicon.ico":
            return self._send(204, b"")
        return self._send(404, {"error": "not found"})

    do_HEAD = do_GET

    # ---- POST ----
    def do_POST(self):
        if not self._auth_ok():
            self._drain()
            return self._send(403, {"error": "Pristup odbijen."})
        url = urlparse(self.path)
        q = parse_qs(url.query)               # parse_qs već dekodira - bez drugog unquote
        parts = [p for p in url.path.split("/") if p]
        try:
            if parts == ["api", "info"]:
                self._drain()
                hw = [n for fam in HW_FAMILIES.values() for n in fam.values() if hw_encoder_works(n)]
                return self._send(200, {
                    "ok": True, "ffmpeg": FFMPEG, "outDir": str(OUT_DIR),
                    "encoders": sorted(e for e in ENCODERS if e in (
                        "libx264", "libx265", "libvpx-vp9", "aac", "libopus", "libsvtav1")),
                    "hwEncoders": sorted(hw), "frameFormats": sorted(PIX_FMT_BYTES),
                    "maxJobs": MAX_JOBS,
                })

            if parts == ["api", "export", "start"]:
                params = self._json_body()
                W, H = int(params.get("width", 0)), int(params.get("height", 0))
                if not (16 <= W <= 16384 and 16 <= H <= 16384):
                    return self._send(400, {"error": f"Neispravna rezolucija {W}×{H}."})
                with JOBS_LOCK:
                    busy = len(JOBS)
                if busy >= MAX_JOBS:
                    return self._send(429, {"error": f"Već je pokrenuto {busy} exporta "
                                                     f"(najviše {MAX_JOBS}). Sačekaj da se završi."})
                container = "webm" if params.get("container") == "webm" else "mp4"
                filename = safe_name(params.get("name"), "export") + "." + container
                target = resolve_target(filename, bool(params.get("askPath")),
                                        "MP4 video" if container == "mp4" else "WebM video")
                if target is None:
                    return self._send(200, {"cancelled": True})
                job = ExportJob(params, target)
                with JOBS_LOCK:
                    JOBS[job.id] = job
                warn = encoder_warning(job.encoder, container)
                log.info("export %s: %dx%d @ %s fps, %s → %s", job.id[:8], W, H,
                         params.get("fps"), job.encoder, target)
                if warn:
                    log.warning(warn)
                return self._send(200, {"id": job.id, "path": str(target),
                                        "encoder": job.encoder, "frameFormat": job.frame_format,
                                        "warning": warn})

            if len(parts) == 4 and parts[:2] == ["api", "export"]:
                job_id, action = parts[2], parts[3]
                with JOBS_LOCK:
                    job = JOBS.get(job_id)
                if not job:
                    self._drain()
                    return self._send(404, {"error": "Export posao ne postoji (možda je otkazan)."})
                if action == "audio":
                    with job.lock:
                        job.add_audio(self.rfile, self._length())
                    return self._send(200, {"ok": True})
                if action == "frame":
                    with job.lock:
                        job.add_frame(self.rfile, self._length())
                        n = job.frames
                    return self._send(200, {"ok": True, "frames": n})
                if action == "status":
                    self._drain()
                    return self._send(200, dict(job.status(), ok=True))
                if action == "finish":
                    self._drain()
                    job.finishing = True                 # reaper ga ne dira dok ffmpeg dovršava
                    with JOBS_LOCK:
                        JOBS.pop(job_id, None)           # odmah oslobodi mesto u redu
                    with job.lock:
                        res = job.finish()
                    mb = res["size"] / 1048576
                    log.info("export %s gotov: %s (%.1f MB, %d kadrova, %s s)",
                             job_id[:8], res["path"], mb, res["frames"], res["seconds"])
                    return self._send(200, dict(res, ok=True))
                if action == "abort":
                    self._drain()
                    with JOBS_LOCK:
                        JOBS.pop(job_id, None)
                    job.abort()
                    log.info("export %s otkazan", job_id[:8])
                    return self._send(200, {"ok": True})
                self._drain()
                return self._send(404, {"error": "Nepoznata akcija."})

            if parts == ["api", "save"]:
                # obični izvozi: sličica, .srt, chapters, projekat (.json)
                name = q.get("name", ["fajl"])[0]
                ask = q.get("ask", ["1"])[0] == "1"
                desc = q.get("desc", ["Fajl"])[0]
                n = self._length()
                ext = Path(safe_name(name)).suffix.lower()
                if ext not in SAVE_EXTENSIONS:
                    self._drain()
                    return self._send(400, {"error": f"Tip fajla '{ext or '(bez ekstenzije)'}' nije dozvoljen."})
                if n > 2 * 1024 * 1024 * 1024:
                    self._drain()
                    return self._send(400, {"error": "Fajl je prevelik."})
                target = resolve_target(name, ask, desc)
                if target is None:
                    self._drain()
                    return self._send(200, {"cancelled": True})
                if target.suffix.lower() not in SAVE_EXTENSIONS:   # korisnik promenio ekstenziju u dijalogu
                    self._drain()
                    return self._send(400, {"error": "Izabrana ekstenzija nije dozvoljena."})
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(target.name + ".part")
                try:
                    with open(tmp, "wb") as fh:
                        copy_body(self.rfile, n, fh.write)
                    os.replace(tmp, target)
                except Exception:
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
                    raise
                log.info("sačuvano %s", target)
                return self._send(200, {"ok": True, "path": str(target)})

            if parts == ["api", "reveal"]:
                body = self._json_body()
                p = body.get("path", "")
                ok = bool(p) and Path(p).exists() and reveal_in_folder(p)
                return self._send(200, {"ok": ok})

            self._drain()
            return self._send(404, {"error": "not found"})
        except Exception as e:
            log.error("greška: %s", e)
            self.close_connection = True
            return self._send(500, {"error": str(e)})


def default_out_dir():
    home = Path.home()
    for cand in (home / "Videos", home / "Movies", home / "Downloads"):
        if cand.is_dir():
            return cand / APP_NAME
    return home / APP_NAME


def main():
    global FFMPEG, ENCODERS, OUT_DIR, DEFAULT_HW, MAX_JOBS, IDLE_TIMEOUT
    ap = argparse.ArgumentParser(description=f"{APP_NAME} - video editor sa Python exportom")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--out", help="podrazumevani folder za izvoz")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--ffmpeg", help="putanja do ffmpeg-a")
    ap.add_argument("--hw", default="off", choices=["off", "auto", "nvenc", "qsv", "amf", "videotoolbox"],
                    help="hardverski enkoder (podrazumevano: off)")
    ap.add_argument("--max-jobs", type=int, default=2)
    ap.add_argument("--idle-timeout", type=int, default=300)
    args = ap.parse_args()

    setup_logging()
    DEFAULT_HW = args.hw
    MAX_JOBS = max(1, args.max_jobs)
    IDLE_TIMEOUT = max(30, args.idle_timeout)

    if not HTML_FILE.exists():
        print(f"[!] Nedostaje {HTML_FILE.name} - mora biti u istom folderu kao {Path(__file__).name}.")
        sys.exit(1)

    FFMPEG = ensure_ffmpeg(args.ffmpeg)
    if not FFMPEG:
        sys.exit(1)
    ENCODERS = list_encoders(FFMPEG)
    OUT_DIR = Path(args.out).expanduser() if args.out else default_out_dir()
    cleanup_stale_temp()

    server = None
    for port in range(args.port, args.port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if not server:
        print("[!] Nijedan slobodan port nije pronađen.")
        sys.exit(1)
    server.daemon_threads = True
    url = f"http://127.0.0.1:{server.server_address[1]}/"

    stop_event = threading.Event()
    threading.Thread(target=reaper_loop, args=(stop_event,), daemon=True).start()

    print("=" * 60)
    print(f"  {APP_NAME} radi na: {url}")
    print(f"  ffmpeg: {FFMPEG}")
    print(f"  enkoderi: " + ", ".join(e for e in ("libx264", "libx265", "libvpx-vp9", "aac", "libopus") if e in ENCODERS))
    if args.hw != "off":
        print(f"  hardverski enkoder: {args.hw} (ako ne radi, koristi se softverski)")
    print(f"  folder za izvoz: {OUT_DIR}")
    print("  Zatvori ovaj prozor (ili Ctrl+C) da ugasiš program.")
    print("=" * 60)
    if "libx264" not in ENCODERS:
        log.warning("Ovaj ffmpeg nema libx264 - H.264 export će biti lošijeg kvaliteta.")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        with JOBS_LOCK:
            for j in list(JOBS.values()):
                j.abort()
        server.server_close()


if __name__ == "__main__":
    main()
