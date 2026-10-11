#!/usr/bin/env python3
"""
TenzXcraft - un éditeur vidéo multipistes pour Linux (façon Vegas / Premiere en version légère).

Dépendances : PySide6 (interface + aperçu) et ffmpeg/ffprobe (analyse + export).
Lancer :  python3 tenzxcraft.py [fichiers ou projet.vforge]
"""
import copy
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PySide6.QtCore import QMimeData, QPointF, QProcess, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QAction, QActionGroup, QColor, QFontMetrics, QIcon, QKeySequence,
                           QPainter, QPalette, QPen, QPixmap, QPolygonF)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout,
                               QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMainWindow, QMenu, QMessageBox, QProgressDialog, QPushButton,
                               QScrollBar, QSizePolicy, QSpinBox, QSplitter, QStackedWidget,
                               QStyle, QTabWidget, QToolBar, QToolButton, QVBoxLayout, QWidget)

APP_NAME = "TenzXcraft"
PROJECT_EXT = ".tzx"
LEGACY_PROJECT_EXT = ".vforge"
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif", ".tif", ".tiff"}
MEDIA_FILTER = ("Médias (*.mp4 *.mkv *.mov *.avi *.webm *.flv *.wmv *.m4v *.ts *.mp3 *.wav "
                "*.flac *.ogg *.m4a *.aac *.png *.jpg *.jpeg *.bmp *.webp *.gif);;Tous les fichiers (*)")

# (nom, type) - du haut (au premier plan) vers le bas
TRACKS = [("V2", "video"), ("V1", "video"), ("A1", "audio"), ("A2", "audio")]
DEFAULT_TRACK = {"video": 1, "image": 1, "audio": 2}
MIN_DUR = 0.1
IMAGE_DEFAULT_DUR = 5.0
IMAGE_MAX_DUR = 3600.0

HEADER_W = 64
RULER_H = 26
TRACK_H = 58


# --------------------------------------------------------------------------- modèle
@dataclass
class Media:
    path: str
    kind: str  # video | audio | image
    duration: float
    has_audio: bool
    width: int = 0
    height: int = 0

    @property
    def name(self):
        return os.path.basename(self.path)


@dataclass
class Clip:
    id: str
    path: str
    kind: str
    track: int
    start: float      # position sur la timeline (s)
    src_in: float     # point d'entrée dans la source (s)
    src_out: float    # point de sortie dans la source (s)
    src_dur: float    # durée max de la source
    has_audio: bool
    mute: bool = False
    volume: float = 1.0
    filters: list = field(default_factory=list)   # [{"id": ..., "params": {...}}]

    @property
    def duration(self):
        return self.src_out - self.src_in

    @property
    def end(self):
        return self.start + self.duration

    @property
    def name(self):
        return os.path.basename(self.path)


class Project:
    def __init__(self):
        self.media = []
        self.clips = []

    def duration(self):
        return max((c.end for c in self.clips), default=0.0)

    def get(self, clip_id):
        for c in self.clips:
            if c.id == clip_id:
                return c
        return None

    def to_dict(self):
        return {"version": 1, "media": [asdict(m) for m in self.media],
                "clips": [asdict(c) for c in self.clips]}

    def snapshot(self):
        return json.dumps(self.to_dict())

    def load_dict(self, d):
        self.media = [Media(**m) for m in d.get("media", [])]
        self.clips = [Clip(**c) for c in d.get("clips", [])]

    def load_snapshot(self, s):
        self.load_dict(json.loads(s))


def probe_media(path):
    """Analyse un fichier avec ffprobe et renvoie un Media."""
    ext = os.path.splitext(path)[1].lower()
    if not tool_path("ffprobe"):
        raise RuntimeError("ffprobe est introuvable. Installe ffmpeg (sudo apt install ffmpeg).")
    r = subprocess.run([tool_path("ffprobe"), "-v", "error", "-print_format", "json", "-show_format",
                        "-show_streams", path], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "Fichier illisible")
    info = json.loads(r.stdout)
    streams = info.get("streams", [])
    vstreams = [s for s in streams if s.get("codec_type") == "video"
                and not s.get("disposition", {}).get("attached_pic")]
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    w = int(vstreams[0].get("width", 0)) if vstreams else 0
    h = int(vstreams[0].get("height", 0)) if vstreams else 0
    if ext in IMAGE_EXT:
        return Media(path, "image", IMAGE_DEFAULT_DUR, False, w, h)
    try:
        dur = float(info.get("format", {}).get("duration", 0))
    except (TypeError, ValueError):
        dur = 0.0
    if dur <= 0:
        raise RuntimeError("Durée introuvable")
    kind = "video" if vstreams else "audio"
    return Media(path, kind, dur, has_audio, w, h)


def fmt_time(t):
    t = max(0.0, t)
    m = int(t // 60)
    s = t - m * 60
    return f"{m:02d}:{s:05.2f}"


# --------------------------------------------------------------------------- filtres
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "tenzxcraft" / "filters"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "tenzxcraft"
LUT_EXT = {".cube", ".3dl", ".dat", ".m3d", ".csp"}
PRESET_EXT = {".tzxfilter", ".json", ".txt", ".vf"}
FILTER_EXT = LUT_EXT | PRESET_EXT
FILTER_FILE_FILTER = "Filtres (*.tzxfilter *.cube *.3dl *.dat *.m3d *.csp *.tzxfilter *.json *.txt *.vf);;Tous les fichiers (*)"

def tool_path(name):
    """Résout ffmpeg/ffprobe selon l'OS : PATH puis voisin du script (Windows), chemins Homebrew (mac)."""
    exe = name + (".exe" if sys.platform.startswith("win") else "")
    candidates = [Path(__file__).resolve().parent / exe]
    if sys.platform == "darwin":
        candidates += [Path("/opt/homebrew/bin") / exe, Path("/usr/local/bin") / exe]
    found = shutil.which(exe)
    return found or next((str(x) for x in candidates if x.exists()), exe)


# params : (clé, libellé, min, max, défaut, pas) ; "vf" est un gabarit ffmpeg (str.format)
BUILTIN_FILTERS = {
    "brightness": {"name": "Luminosité / contraste",
                   "params": [("b", "Luminosité", -1.0, 1.0, 0.0, 0.05), ("c", "Contraste", 0.0, 3.0, 1.0, 0.05)],
                   "vf": "eq=brightness={b}:contrast={c}"},
    "saturation": {"name": "Saturation", "params": [("s", "Saturation", 0.0, 3.0, 1.5, 0.05)],
                   "vf": "eq=saturation={s}"},
    "gamma": {"name": "Gamma", "params": [("g", "Gamma", 0.1, 3.0, 1.2, 0.05)], "vf": "eq=gamma={g}"},
    "hue": {"name": "Teinte", "params": [("h", "Décalage (°)", -180.0, 180.0, 30.0, 5.0)], "vf": "hue=h={h}"},
    "bw": {"name": "Noir et blanc", "params": [], "vf": "hue=s=0"},
    "sepia": {"name": "Sépia", "params": [],
              "vf": "colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131"},
    "negative": {"name": "Négatif", "params": [], "vf": "negate"},
    "blur": {"name": "Flou", "params": [("s", "Intensité", 0.5, 40.0, 5.0, 0.5)], "vf": "gblur=sigma={s}"},
    "sharpen": {"name": "Netteté", "params": [("a", "Intensité", 0.0, 3.0, 1.0, 0.1)],
                "vf": "unsharp=5:5:{a}:5:5:0"},
    "vignette": {"name": "Vignettage", "params": [("a", "Angle (rad)", 0.1, 1.5, 0.6, 0.05)],
                 "vf": "vignette=angle={a}"},
    "grain": {"name": "Grain", "params": [("n", "Intensité", 1.0, 60.0, 15.0, 1.0)],
              "vf": "noise=alls={n}:allf=t"},
    "hflip": {"name": "Miroir horizontal", "params": [], "vf": "hflip"},
    "vflip": {"name": "Miroir vertical", "params": [], "vf": "vflip"},
    "fade_in": {"name": "Fondu en entrée (noir)", "params": [("d", "Durée (s)", 0.1, 30.0, 1.0, 0.1)],
                "vf": "fade=t=in:st=0:d={d}"},
    "fade_out": {"name": "Fondu en sortie (noir)", "params": [("d", "Durée (s)", 0.1, 30.0, 1.0, 0.1)],
                 "vf": "fade=t=out:st={st_out}:d={d}"},
}


def ff_quote(s):
    """Entoure une chaîne de quotes pour un graphe de filtres ffmpeg."""
    return "'" + str(s).replace("'", "'\\''") + "'"


def validate_vf(vf):
    """Teste une chaîne de filtres vidéo sur une image factice. Renvoie (ok, message)."""
    if not tool_path("ffmpeg"):
        return False, "ffmpeg est introuvable"
    r = subprocess.run([tool_path("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=64x64:d=1:r=10",
                        "-vf", vf.replace("{dur}", "1.0"), "-frames:v", "1", "-f", "null", "-"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        lines = [ln.strip() for ln in r.stderr.strip().splitlines() if ln.strip()
                 and not ln.strip().startswith(("Error opening", "Error initializing", "Error reinitializing"))]
        return False, lines[0] if lines else "filtre invalide"
    return True, ""


class FilterLibrary:
    """Filtres intégrés + filtres importés (LUT 3D et presets ffmpeg) stockés dans ~/.config/tenzxcraft."""

    def __init__(self):
        self.items = {}
        for fid, d in BUILTIN_FILTERS.items():
            self.items[fid] = {**d, "builtin": True}
        self.user_index = []
        self.load_user()

    # --- persistance
    def index_path(self):
        return CONFIG_DIR / "index.json"

    def load_user(self):
        try:
            self.user_index = json.loads(self.index_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.user_index = []
        for e in self.user_index:
            self.items[e["id"]] = {"name": e["name"], "params": [], "vf": e["vf"], "builtin": False,
                                   "kind": e.get("kind", "preset"), "file": e.get("file")}

    def save_user(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.index_path().write_text(json.dumps(self.user_index, indent=2, ensure_ascii=False), encoding="utf-8")

    # --- utilisation
    def defaults(self, fid):
        return {p[0]: p[4] for p in self.items.get(fid, {}).get("params", [])}

    def name(self, fid):
        d = self.items.get(fid)
        return d["name"] if d else f"{fid} (introuvable)"

    def build(self, entry, dur):
        """Chaîne ffmpeg d'un filtre appliqué à un clip de durée `dur` (chaîne vide si inconnu)."""
        d = self.items.get(entry.get("id"))
        if not d:
            return ""
        if d["builtin"]:
            ctx = self.defaults(entry["id"])
            ctx.update(entry.get("params", {}))
            ctx["dur"] = dur
            ctx["st_out"] = max(0.0, dur - float(ctx.get("d", 0)))
            return d["vf"].format(**ctx)
        return d["vf"].replace("{dur}", f"{dur:.3f}")

    # --- import / suppression
    def _add(self, name, vf, kind, file=None):
        ok, err = validate_vf(vf)
        if not ok:
            raise ValueError(f"« {name} » est invalide : {err}")
        e = {"id": "u_" + uuid.uuid4().hex[:8], "name": name, "vf": vf, "kind": kind, "file": file}
        self.user_index.append(e)
        self.items[e["id"]] = {"name": name, "params": [], "vf": vf, "builtin": False, "kind": kind, "file": file}
        return e["id"]

    def import_file(self, path):
        """Importe un fichier LUT (.cube/.3dl/...) ou preset (.json/.txt/.vf). Renvoie la liste des noms ajoutés."""
        p = Path(path)
        ext = p.suffix.lower()
        added = []
        if ext in LUT_EXT:
            dest_dir = CONFIG_DIR / "luts"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / p.name
            n = 1
            while dest.exists():
                dest = dest_dir / f"{p.stem}_{n}{p.suffix}"
                n += 1
            shutil.copy(p, dest)
            try:
                self._add(p.stem, "lut3d=file=" + ff_quote(dest), "lut", str(dest))
            except ValueError:
                dest.unlink(missing_ok=True)
                raise
            added.append(p.stem)
        elif ext in PRESET_EXT:
            text = p.read_text(encoding="utf-8", errors="replace")
            presets = []
            if ext in (".json", ".tzxfilter"):
                data = json.loads(text)
                for it in (data if isinstance(data, list) else [data]):
                    vf = (it.get("ffmpeg_filter") or it.get("vf")) if isinstance(it, dict) else None
                    if not vf:
                        raise ValueError("JSON attendu : {\"name\": \"…\", \"vf\": \"chaîne de filtres ffmpeg\"}")
                    presets.append((it.get("name") or p.stem, str(vf).strip()))
            else:
                lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
                if not lines:
                    raise ValueError("Fichier vide")
                presets.append((p.stem, ",".join(lines)))
            for name, vf in presets:
                self._add(name, vf, "preset")
                added.append(name)
        else:
            raise ValueError("Format non pris en charge (LUT : .cube .3dl .dat .m3d .csp ; preset : .json .txt .vf)")
        self.save_user()
        return added

    def remove(self, fid):
        d = self.items.get(fid)
        if not d or d["builtin"]:
            return False
        if d.get("file"):
            try:
                Path(d["file"]).unlink(missing_ok=True)
            except OSError:
                pass
        self.items.pop(fid, None)
        self.user_index = [e for e in self.user_index if e["id"] != fid]
        self.save_user()
        return True


# --------------------------------------------------------------------------- export ffmpeg
_AMIX_NORMALIZE = None


def amix_supports_normalize():
    global _AMIX_NORMALIZE
    if _AMIX_NORMALIZE is None:
        try:
            r = subprocess.run([tool_path("ffmpeg"), "-hide_banner", "-h", "filter=amix"],
                               capture_output=True, text=True)
            _AMIX_NORMALIZE = "normalize" in (r.stdout + r.stderr)
        except OSError:
            _AMIX_NORMALIZE = False
    return _AMIX_NORMALIZE


def build_ffmpeg_cmd(project, out, W, H, fps, crf=20, preset="medium", flib=None):
    total = project.duration()
    clips = list(project.clips)
    cmd = [tool_path("ffmpeg"), "-y", "-hide_banner", "-progress", "pipe:1", "-nostats",
           "-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r={fps}:d={total:.3f}",
           "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo:d={total:.3f}"]
    idx = {}
    for c in clips:
        idx[c.id] = 2 + len(idx)
        if c.kind == "image":
            cmd += ["-loop", "1", "-framerate", str(fps), "-t", f"{c.duration:.3f}", "-i", c.path]
        else:
            cmd += ["-i", c.path]

    parts = []
    prev = "[0:v]"
    vclips = sorted((c for c in clips if c.kind in ("video", "image")), key=lambda c: -c.track)
    for n, c in enumerate(vclips):
        i = idx[c.id]
        if c.kind == "image":
            chain = f"[{i}:v]setpts=PTS-STARTPTS"
        else:
            chain = (f"[{i}:v]trim=start={c.src_in:.3f}:end={c.src_out:.3f},"
                     f"setpts=PTS-STARTPTS")
        chain += (f",scale={W}:{H}:force_original_aspect_ratio=decrease,"
                  f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1")
        if flib:
            for f in c.filters:
                vf = flib.build(f, c.duration)
                if vf:
                    chain += "," + vf
        chain += f",setpts=PTS+{c.start:.3f}/TB[v{n}]"
        parts.append(chain)
        parts.append(f"{prev}[v{n}]overlay=eof_action=pass:shortest=0[o{n}]")
        prev = f"[o{n}]"
    parts.append(f"{prev}format=yuv420p[vout]")

    aclips = [c for c in clips if c.kind in ("video", "audio") and c.has_audio and not c.mute]
    parts.append("[1:a]aformat=sample_rates=48000:channel_layouts=stereo[abase]")
    labels = ["[abase]"]
    for n, c in enumerate(aclips):
        i = idx[c.id]
        ms = int(round(c.start * 1000))
        parts.append(f"[{i}:a]atrim=start={c.src_in:.3f}:end={c.src_out:.3f},asetpts=PTS-STARTPTS,"
                     f"aresample=48000,aformat=channel_layouts=stereo,volume={c.volume:.3f},"
                     f"adelay={ms}|{ms}[a{n}]")
        labels.append(f"[a{n}]")
    cnt = len(labels)
    if amix_supports_normalize():
        parts.append(f"{''.join(labels)}amix=inputs={cnt}:duration=longest:normalize=0[aout]")
    else:
        parts.append(f"{''.join(labels)}amix=inputs={cnt}:duration=longest,volume={cnt}[aout]")

    cmd += ["-filter_complex", ";".join(parts), "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-t", f"{total:.3f}", "-movflags", "+faststart", out]
    return cmd


# --------------------------------------------------------------------------- timeline
class Timeline(QWidget):
    seek_requested = Signal(float)
    edited = Signal(str)            # snapshot AVANT modification
    selection_changed = Signal()
    view_changed = Signal()
    drop_media = Signal(int, float, int)
    razor_cut = Signal(str, float)

    def __init__(self, project):
        super().__init__()
        self.project = project
        self.pps = 60.0
        self.scroll_x = 0.0
        self.playhead = 0.0
        self.selected = None
        self.snap = True
        self.drag = None
        self.tool = "select"      # select | razor | hand
        self.hover_x = None
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)
        self.setMinimumHeight(RULER_H + TRACK_H * len(TRACKS) + 6)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    # --- conversions
    def t2x(self, t):
        return HEADER_W + t * self.pps - self.scroll_x

    def x2t(self, x):
        return max(0.0, (x - HEADER_W + self.scroll_x) / self.pps)

    def track_y(self, i):
        return RULER_H + i * TRACK_H

    def track_at(self, y):
        i = int((y - RULER_H) // TRACK_H)
        return i if 0 <= i < len(TRACKS) else None

    def clip_rect(self, c):
        return QRectF(self.t2x(c.start), self.track_y(c.track) + 3, c.duration * self.pps, TRACK_H - 6)

    def clip_at(self, pos):
        for c in reversed(self.project.clips):
            if self.clip_rect(c).contains(pos):
                return c
        return None

    # --- zoom
    def zoom(self, factor, anchor_x=None):
        if anchor_x is None:
            anchor_x = self.t2x(self.playhead)
            if anchor_x < HEADER_W or anchor_x > self.width():
                anchor_x = HEADER_W + 100
        ta = self.x2t(anchor_x)
        self.pps = min(800.0, max(2.0, self.pps * factor))
        self.scroll_x = max(0.0, HEADER_W + ta * self.pps - anchor_x)
        self.view_changed.emit()
        self.update()

    def wheelEvent(self, ev):
        dy = ev.angleDelta().y() or ev.angleDelta().x()
        if ev.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom(1.15 if dy > 0 else 1 / 1.15, ev.position().x())
        else:
            self.scroll_x = max(0.0, self.scroll_x - dy * 0.8)
            self.view_changed.emit()
            self.update()

    def resizeEvent(self, ev):
        self.view_changed.emit()

    # --- aimantation
    def snap_points(self, exclude):
        pts = [0.0, self.playhead]
        for c in self.project.clips:
            if c is not exclude:
                pts += [c.start, c.end]
        return pts

    def snap_value(self, v, exclude):
        if not self.snap:
            return v
        thr = 8.0 / self.pps
        best, bd = v, thr
        for p in self.snap_points(exclude):
            d = abs(p - v)
            if d < bd:
                best, bd = p, d
        return best

    def overlaps(self, c):
        for o in self.project.clips:
            if o is c or o.track != c.track:
                continue
            if c.start < o.end - 1e-6 and o.start < c.end - 1e-6:
                return True
        return False

    @staticmethod
    def track_kind_for(c):
        return "audio" if c.kind == "audio" else "video"

    # --- dessin
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor("#1e1f22"))
        for i in range(len(TRACKS)):
            y = self.track_y(i)
            p.fillRect(HEADER_W, y, w - HEADER_W, TRACK_H, QColor("#26282c" if i % 2 == 0 else "#2b2d31"))
            p.setPen(QColor("#3a3c41"))
            p.drawLine(0, y + TRACK_H, w, y + TRACK_H)

        p.save()
        p.setClipRect(HEADER_W, RULER_H, w - HEADER_W, h - RULER_H)
        fm = QFontMetrics(p.font())
        colors = {"video": "#3b6ea8", "audio": "#3f8f5a", "image": "#b07a2c"}
        for c in self.project.clips:
            r = self.clip_rect(c)
            if r.right() < HEADER_W or r.left() > w:
                continue
            col = QColor(colors[c.kind])
            sel = c.id == self.selected
            p.setBrush(col)
            p.setPen(QPen(QColor("white") if sel else col.lighter(150), 2 if sel else 1))
            p.drawRoundedRect(r, 4, 4)
            label = c.name + ("  [muet]" if c.mute else "") + (f"  vol {int(c.volume*100)}%" if c.volume != 1 else "")
            p.setPen(QColor("white"))
            p.drawText(r.adjusted(6, 4, -4, -4), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                       fm.elidedText(label, Qt.TextElideMode.ElideRight, max(0, int(r.width()) - 10)))
            p.setPen(QColor(255, 255, 255, 170))
            p.drawText(r.adjusted(6, 4, -4, -6), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                       fm.elidedText(fmt_time(c.duration), Qt.TextElideMode.ElideRight, max(0, int(r.width()) - 10)))
        p.restore()

        # en-têtes de pistes
        p.fillRect(0, RULER_H, HEADER_W, h - RULER_H, QColor("#1a1b1e"))
        for i, (name, kind) in enumerate(TRACKS):
            y = self.track_y(i)
            p.setPen(QColor("#d0d0d0"))
            p.drawText(QRectF(0, y, HEADER_W, TRACK_H), Qt.AlignmentFlag.AlignCenter, name)
            p.setPen(QColor("#3a3c41"))
            p.drawLine(0, y + TRACK_H, HEADER_W, y + TRACK_H)

        # règle
        p.fillRect(0, 0, w, RULER_H, QColor("#18191c"))
        step = next((s for s in (0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800)
                     if s * self.pps >= 70), 3600)
        t = int(self.x2t(HEADER_W) / step) * step
        p.setPen(QColor("#9a9a9a"))
        while self.t2x(t) < w:
            x = self.t2x(t)
            if x >= HEADER_W:
                p.drawLine(int(x), RULER_H - 8, int(x), RULER_H)
                p.drawText(int(x) + 3, RULER_H - 10, fmt_time(t).split(".")[0] if step >= 1 else fmt_time(t))
            t += step

        # guide du rasoir
        if self.tool == "razor" and self.hover_x is not None and self.hover_x >= HEADER_W:
            p.setPen(QPen(QColor("#ffd24d"), 1, Qt.PenStyle.DashLine))
            p.drawLine(int(self.hover_x), RULER_H, int(self.hover_x), h)

        # tête de lecture
        x = self.t2x(self.playhead)
        if x >= HEADER_W:
            p.setPen(QPen(QColor("#ff4d4d"), 2))
            p.drawLine(int(x), 0, int(x), h)
            p.setBrush(QColor("#ff4d4d"))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPolygon(QPolygonF([QPointF(x - 6, 0), QPointF(x + 6, 0), QPointF(x, 10)]))
        p.end()

    # --- souris
    def mousePressEvent(self, ev):
        if ev.button() != Qt.MouseButton.LeftButton:
            return
        pos = ev.position()
        if pos.y() < RULER_H and pos.x() >= HEADER_W:
            self.drag = {"mode": "scrub"}
            self.seek_requested.emit(self.x2t(pos.x()))
            return
        if self.tool == "hand":
            self.drag = {"mode": "pan", "x0": pos.x(), "scroll0": self.scroll_x}
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        c = self.clip_at(pos)
        if c is None:
            self.selected = None
            self.selection_changed.emit()
            self.update()
            return
        self.selected = c.id
        self.selection_changed.emit()
        if self.tool == "razor":
            self.razor_cut.emit(c.id, self.snap_value(self.x2t(pos.x()), c))
            self.update()
            return
        r = self.clip_rect(c)
        mode = "move"
        if pos.x() - r.left() < 7:
            mode = "trim_l"
        elif r.right() - pos.x() < 7:
            mode = "trim_r"
        self.drag = {"mode": mode, "clip": c, "x0": pos.x(), "start": c.start, "in": c.src_in,
                     "out": c.src_out, "track": c.track, "snapshot": self.project.snapshot(),
                     "moved": False}
        self.update()

    def mouseMoveEvent(self, ev):
        pos = ev.position()
        d = self.drag
        if not d:
            c = self.clip_at(pos)
            if self.tool == "hand":
                self.setCursor(Qt.CursorShape.OpenHandCursor)
            elif self.tool == "razor":
                self.setCursor(Qt.CursorShape.CrossCursor if c else Qt.CursorShape.ArrowCursor)
                self.hover_x = pos.x() if c else None
                self.update()
            elif c:
                r = self.clip_rect(c)
                near = pos.x() - r.left() < 7 or r.right() - pos.x() < 7
                self.setCursor(Qt.CursorShape.SizeHorCursor if near else Qt.CursorShape.OpenHandCursor)
            else:
                self.setCursor(Qt.CursorShape.ArrowCursor)
            return
        if d["mode"] == "scrub":
            self.seek_requested.emit(self.x2t(pos.x()))
            return
        if d["mode"] == "pan":
            self.scroll_x = max(0.0, d["scroll0"] - (pos.x() - d["x0"]))
            self.view_changed.emit()
            self.update()
            return
        c = d["clip"]
        dt = (pos.x() - d["x0"]) / self.pps
        if abs(pos.x() - d["x0"]) > 2:
            d["moved"] = True
        if d["mode"] == "move":
            ns = max(0.0, d["start"] + dt)
            s1 = self.snap_value(ns, c)
            s2 = self.snap_value(ns + c.duration, c) - c.duration
            ns = s1 if abs(s1 - ns) <= abs(s2 - ns) else s2
            c.start = max(0.0, ns)
            ti = self.track_at(int(pos.y()))
            if ti is not None and TRACKS[ti][1] == self.track_kind_for(c):
                c.track = ti
        elif d["mode"] == "trim_l":
            ns = self.snap_value(max(0.0, d["start"] + dt), c)
            delta = ns - d["start"]
            delta = max(delta, -min(d["in"], d["start"]))
            delta = min(delta, (d["out"] - d["in"]) - MIN_DUR)
            c.start = d["start"] + delta
            c.src_in = d["in"] + delta
        elif d["mode"] == "trim_r":
            end = self.snap_value(d["start"] + (d["out"] - d["in"]) + dt, c)
            new_out = d["in"] + (end - d["start"])
            c.src_out = min(c.src_dur, max(d["in"] + MIN_DUR, new_out))
        self.update()
        self.view_changed.emit()

    def mouseReleaseEvent(self, ev):
        d, self.drag = self.drag, None
        if not d or d["mode"] == "scrub":
            return
        if d["mode"] == "pan":
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            return
        c = d["clip"]
        if self.overlaps(c):  # chevauchement interdit : on annule
            c.start, c.src_in, c.src_out, c.track = d["start"], d["in"], d["out"], d["track"]
        elif d["moved"]:
            self.edited.emit(d["snapshot"])
        self.update()
        self.view_changed.emit()

    def leaveEvent(self, ev):
        self.hover_x = None
        self.update()

    def show_context_menu(self, pos):
        menu = QMenu(self)
        c = self.clip_at(pos)
        if c:
            self.selected = c.id; self.selection_changed.emit()
            menu.addAction("Couper au curseur", lambda: self.window().split_at_playhead())
            menu.addAction("Dupliquer", lambda: self.window().duplicate_clip())
            menu.addAction("Supprimer", lambda: self.window().delete_selected())
            menu.addAction("Supprimer et combler", lambda: self.window().delete_selected(True))
            menu.addAction("Muet", lambda: self.window().toggle_mute())
            menu.addAction("Volume…", lambda: self.window().set_volume())
            menu.addSeparator(); menu.addAction("Filtres", lambda: self.window().filters_menu.exec())
            menu.addAction("Propriétés", lambda: self.window().show_clip_properties())
        else:
            menu.addAction("Coller", lambda: self.window().statusBar().showMessage("Aucun clip à coller."))
            menu.addAction("Ajouter une piste", lambda: self.window().statusBar().showMessage("Pistes vidéo/audio disponibles."))
            menu.addAction("Zoom ajusté", lambda: self.window().fit_zoom())
        menu.exec(self.mapToGlobal(pos))

    # --- glisser-déposer depuis la bibliothèque
    def dragEnterEvent(self, ev):
        if ev.mimeData().hasText() and ev.mimeData().text().startswith("media:"):
            ev.acceptProposedAction()

    def dragMoveEvent(self, ev):
        ev.acceptProposedAction()

    def dropEvent(self, ev):
        try:
            idx = int(ev.mimeData().text().split(":")[1])
        except (ValueError, IndexError):
            return
        pos = ev.position()
        ti = self.track_at(int(pos.y()))
        self.drop_media.emit(idx, self.x2t(pos.x()), -1 if ti is None else ti)
        ev.acceptProposedAction()


class TimelinePanel(QWidget):
    def __init__(self, project):
        super().__init__()
        self.timeline = Timeline(project)
        self.sb = QScrollBar(Qt.Orientation.Horizontal)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.timeline, 1)
        lay.addWidget(self.sb)
        self.timeline.view_changed.connect(self.sync)
        self.sb.valueChanged.connect(self.on_scroll)

    def sync(self):
        tl = self.timeline
        vis = max(1, tl.width() - HEADER_W)
        total = (tl.project.duration() + 30) * tl.pps
        self.sb.blockSignals(True)
        self.sb.setRange(0, int(max(0, total - vis)))
        self.sb.setPageStep(vis)
        self.sb.setValue(int(tl.scroll_x))
        tl.scroll_x = float(self.sb.value())
        self.sb.blockSignals(False)
        tl.update()

    def on_scroll(self, v):
        self.timeline.scroll_x = float(v)
        self.timeline.update()

    def ensure_visible(self, t):
        tl = self.timeline
        x = tl.t2x(t)
        if x > tl.width() - 40 or x < HEADER_W:
            tl.scroll_x = max(0.0, t * tl.pps - 40)
            self.sync()


# --------------------------------------------------------------------------- widgets divers
class MediaList(QListWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)

    def show_context_menu(self, pos):
        it = self.itemAt(pos); menu = QMenu(self)
        if it:
            row = self.row(it)
            menu.addAction("Ajouter à la timeline", lambda: self.window().add_media_at(row, self.window().playhead))
            menu.addAction("Retirer", lambda: self.window().remove_media(row))
            menu.addAction("Propriétés", lambda: self.window().show_media_properties(row))
        menu.exec(self.mapToGlobal(pos))

    def mimeData(self, items):
        md = QMimeData()
        md.setText(f"media:{self.row(items[0])}")
        return md


class ImageView(QLabel):
    def __init__(self):
        super().__init__()
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("background:black;")
        self.pix = None

    def set_image(self, path):
        self.pix = QPixmap(path)
        self.rescale()

    def rescale(self):
        if self.pix and not self.pix.isNull():
            self.setPixmap(self.pix.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                           Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, ev):
        self.rescale()


class ExportDialog(QDialog):
    def __init__(self, parent, default_path):
        super().__init__(parent)
        self.setWindowTitle("Exporter la vidéo")
        form = QFormLayout(self)
        self.path = QLineEdit(default_path)
        browse = QPushButton("Parcourir…")
        browse.clicked.connect(self.browse)
        row = QHBoxLayout()
        row.addWidget(self.path, 1)
        row.addWidget(browse)
        form.addRow("Fichier :", row)
        self.res = QComboBox()
        for label, wh in [("1920 x 1080 (Full HD)", (1920, 1080)), ("1280 x 720 (HD)", (1280, 720)),
                          ("3840 x 2160 (4K)", (3840, 2160)), ("854 x 480 (SD)", (854, 480)),
                          ("1080 x 1920 (vertical)", (1080, 1920))]:
            self.res.addItem(label, wh)
        form.addRow("Résolution :", self.res)
        self.fps = QComboBox()
        for f in (24, 25, 30, 60):
            self.fps.addItem(f"{f} i/s", f)
        self.fps.setCurrentIndex(2)
        form.addRow("Images/seconde :", self.fps)
        self.crf = QSpinBox()
        self.crf.setRange(0, 51)
        self.crf.setValue(20)
        self.crf.setToolTip("0 = sans perte, 18-23 = bonne qualité, 51 = très compressé")
        form.addRow("Qualité (CRF) :", self.crf)
        self.preset = QComboBox()
        self.preset.addItems(["ultrafast", "veryfast", "fast", "medium", "slow"])
        self.preset.setCurrentText("medium")
        form.addRow("Vitesse d'encodage :", self.preset)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)
        self.setMinimumWidth(460)

    def browse(self):
        p, _ = QFileDialog.getSaveFileName(self, "Exporter vers", self.path.text(), "MP4 (*.mp4);;MKV (*.mkv);;WebM (*.webm)")
        if p:
            self.path.setText(p)


# --------------------------------------------------------------------------- fenêtre principale
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.project = Project()
        self.undo_stack, self.redo_stack = [], []
        self.file_path = None
        self.playhead = 0.0
        self.playing = False
        self.last_tick = 0.0
        self.vcur = None
        self.acur = None
        self.vpending = False
        self.apending = False
        self.proc = None
        self.setAcceptDrops(True)
        self.flib = FilterLibrary()
        self.filter_preview_on = False
        self.pending_param_snap = None
        self._cf_sig = None
        self.param_timer = QTimer(self)
        self.param_timer.setSingleShot(True)
        self.param_timer.setInterval(500)
        self.param_timer.timeout.connect(self.flush_param)
        self.fprev_timer = QTimer(self)
        self.fprev_timer.setSingleShot(True)
        self.fprev_timer.setInterval(200)
        self.fprev_timer.timeout.connect(self.render_filter_preview)

        # --- aperçu
        self.video_widget = QVideoWidget()
        self.black = QLabel()
        self.black.setStyleSheet("background:black;")
        self.image_view = ImageView()
        self.stack = QStackedWidget()
        self.stack.addWidget(self.black)
        self.stack.addWidget(self.video_widget)
        self.stack.addWidget(self.image_view)
        self.vplayer = QMediaPlayer()
        self.vout = QAudioOutput()
        self.vplayer.setAudioOutput(self.vout)
        self.vplayer.setVideoOutput(self.video_widget)
        self.aplayer = QMediaPlayer()
        self.aout = QAudioOutput()
        self.aplayer.setAudioOutput(self.aout)
        self.vplayer.mediaStatusChanged.connect(self.on_vstatus)
        self.aplayer.mediaStatusChanged.connect(self.on_astatus)

        self.time_label = QLabel("00:00.00 / 00:00.00")
        self.time_label.setStyleSheet("font-family:monospace; font-size:15px; padding:2px 8px;")
        self.btn_play = QPushButton()
        self.btn_play.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.btn_play.clicked.connect(self.toggle_play)
        btn_start = QPushButton()
        btn_start.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaSkipBackward))
        btn_start.clicked.connect(lambda: self.set_playhead(0))
        btn_end = QPushButton()
        btn_end.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaSkipForward))
        btn_end.clicked.connect(lambda: self.set_playhead(self.project.duration()))
        transport = QHBoxLayout()
        transport.addStretch(1)
        for b in (btn_start, self.btn_play, btn_end):
            transport.addWidget(b)
        transport.addWidget(self.time_label)
        transport.addStretch(1)
        preview = QWidget()
        pl = QVBoxLayout(preview)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.addWidget(self.stack, 1)
        pl.addLayout(transport)

        # --- bibliothèque
        self.media_list = MediaList()
        self.media_list.setDragEnabled(True)
        self.media_list.setIconSize(QPixmap(48, 28).size())
        self.media_list.itemDoubleClicked.connect(lambda it: self.add_media_at(self.media_list.row(it), self.playhead))
        btn_import = QPushButton("Importer des médias…")
        btn_import.clicked.connect(self.import_dialog)
        lib = QWidget()
        ll = QVBoxLayout(lib)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(QLabel("Bibliothèque (double-clic ou glisser vers la timeline)"))
        ll.addWidget(self.media_list, 1)
        ll.addWidget(btn_import)

        self.left_tabs = QTabWidget()
        self.left_tabs.addTab(lib, "Médias")
        self.left_tabs.addTab(self.build_filters_tab(), "Filtres")
        top = QSplitter(Qt.Orientation.Horizontal)
        top.addWidget(self.left_tabs)
        top.addWidget(preview)
        top.setSizes([330, 870])

        # --- timeline
        self.panel = TimelinePanel(self.project)
        self.timeline = self.panel.timeline
        self.timeline.seek_requested.connect(lambda t: self.set_playhead(t))
        self.timeline.edited.connect(self.on_edited)
        self.timeline.selection_changed.connect(self.on_selection)
        self.timeline.drop_media.connect(self.on_drop_media)
        self.timeline.razor_cut.connect(self.razor)

        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(top)
        split.addWidget(self.panel)
        split.setSizes([480, 300])
        self.setCentralWidget(split)

        self.timer = QTimer(self)
        self.timer.setInterval(33)
        self.timer.timeout.connect(self.tick)

        self.build_actions()
        self.setWindowTitle(APP_NAME)
        self.statusBar().showMessage("Prêt. Importe des médias pour commencer." if tool_path("ffmpeg")
                                     else "⚠ ffmpeg est introuvable : installe-le (sudo apt install ffmpeg).")
        self.update_title()

    # ------------------------------------------------------------------ actions / menus
    def act(self, text, slot, shortcut=None, icon=None):
        a = QAction(text, self)
        if icon:
            a.setIcon(self.style().standardIcon(icon))
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        a.triggered.connect(lambda checked=False: slot())
        self.addAction(a)
        return a

    def build_actions(self):
        S = QStyle.StandardPixmap
        a_new = self.act("Nouveau projet", self.new_project, "Ctrl+N")
        a_open = self.act("Ouvrir un projet…", self.open_project, "Ctrl+O", S.SP_DialogOpenButton)
        a_save = self.act("Enregistrer", self.save_project, "Ctrl+S", S.SP_DialogSaveButton)
        a_saveas = self.act("Enregistrer sous…", lambda: self.save_project(True), "Ctrl+Shift+S")
        a_imp = self.act("Importer des médias…", self.import_dialog, "Ctrl+I", S.SP_FileDialogNewFolder)
        a_exp = self.act("Exporter la vidéo…", self.export_video, "Ctrl+E", S.SP_ArrowDown)
        a_quit = self.act("Quitter", self.close, "Ctrl+Q")
        a_undo = self.act("Annuler", self.undo, "Ctrl+Z", S.SP_ArrowBack)
        a_redo = self.act("Rétablir", self.redo, "Ctrl+Shift+Z")
        self.addAction(self.act("Rétablir (Ctrl+Y)", self.redo, "Ctrl+Y"))
        a_split = self.act("Couper au curseur", self.split_at_playhead, "S", S.SP_FileDialogContentsView)
        a_del = self.act("Supprimer le clip", self.delete_selected, "Delete", S.SP_TrashIcon)
        a_rip = self.act("Supprimer et combler le vide", lambda: self.delete_selected(True), "Shift+Delete")
        a_mute = self.act("Couper/rétablir le son du clip", self.toggle_mute, "M")
        a_vol = self.act("Volume du clip…", self.set_volume)
        a_play = self.act("Lecture / Pause", self.toggle_play, "Space")
        a_zin = self.act("Zoom +", lambda: self.timeline.zoom(1.3), "=", S.SP_ArrowUp)
        self.addAction(self.act("Zoom + (pavé)", lambda: self.timeline.zoom(1.3), "+"))
        a_zout = self.act("Zoom -", lambda: self.timeline.zoom(1 / 1.3), "-", S.SP_ArrowDown)
        a_home = self.act("Début", lambda: self.set_playhead(0), "Home")
        a_endk = self.act("Fin", lambda: self.set_playhead(self.project.duration()), "End")
        a_left = self.act("Image précédente", lambda: self.set_playhead(self.playhead - 0.04), "Left")
        a_right = self.act("Image suivante", lambda: self.set_playhead(self.playhead + 0.04), "Right")
        self.act("Seconde -", lambda: self.set_playhead(self.playhead - 1), "Shift+Left")
        self.act("Seconde +", lambda: self.set_playhead(self.playhead + 1), "Shift+Right")
        a_snap = QAction("Aimantation", self, checkable=True)
        a_snap.setChecked(True)
        a_snap.toggled.connect(lambda v: setattr(self.timeline, "snap", v))

        # --- outils (modes de la souris sur la timeline)
        self.tool_group = QActionGroup(self)
        self.tool_group.setExclusive(True)
        tool_actions = []
        for key, text, sc, tip in (("select", "Sélection", "V", "Sélectionner / déplacer / ajuster les clips (V)"),
                                   ("razor", "Rasoir", "C", "Cliquer sur un clip pour le couper (C)"),
                                   ("hand", "Main", "H", "Glisser pour faire défiler la timeline (H)")):
            a = QAction(text, self, checkable=True)
            a.setShortcut(QKeySequence(sc))
            a.setToolTip(tip)
            a.setStatusTip(tip)
            a.setData(key)
            a.triggered.connect(lambda checked=False, k=key: self.set_tool(k))
            self.tool_group.addAction(a)
            self.addAction(a)
            tool_actions.append(a)
        tool_actions[0].setChecked(True)

        a_dup = self.act("Dupliquer le clip", self.duplicate_clip, "Ctrl+D")
        a_detach = self.act("Détacher l'audio du clip", self.detach_audio, "Ctrl+Shift+D")
        a_gaps = self.act("Combler les vides de la piste", self.close_gaps)
        a_fit = self.act("Zoom ajusté au projet", self.fit_zoom, "Ctrl+0")
        a_capture = self.act("Capturer l'image au curseur (PNG)…", self.capture_frame, "Ctrl+Shift+C")

        # --- filtres
        a_fimport = self.act("Importer un filtre (LUT / preset)…", self.import_filter_dialog, "Ctrl+Shift+I",
                             S.SP_DialogOpenButton)
        a_fclear = self.act("Retirer tous les filtres du clip", self.clear_filters)
        self.filters_menu = QMenu("Appliquer un filtre", self)
        self.filters_menu.aboutToShow.connect(self.rebuild_filters_menu)

        mb = self.menuBar()
        m = mb.addMenu("&Fichier")
        for a in (a_new, a_open, a_save, a_saveas, None, a_imp, a_exp, None, a_quit):
            m.addSeparator() if a is None else m.addAction(a)
        m = mb.addMenu("&Édition")
        for a in (a_undo, a_redo, None, a_split, a_del, a_rip, None, a_mute, a_vol):
            m.addSeparator() if a is None else m.addAction(a)
        m = mb.addMenu("&Outils")
        for a in tool_actions:
            m.addAction(a)
        m.addSeparator()
        for a in (a_dup, a_detach, a_gaps, a_capture):
            m.addAction(a)
        m = mb.addMenu("Fi&ltres")
        m.addMenu(self.filters_menu)
        for a in (a_fimport, a_fclear):
            m.addAction(a)
        m = mb.addMenu("&Affichage")
        for a in (a_zin, a_zout, a_fit, a_snap):
            m.addAction(a)
        m = mb.addMenu("&Lecture")
        for a in (a_play, a_home, a_endk, a_left, a_right):
            m.addAction(a)
        m = mb.addMenu("&Aide")
        m.addAction("Raccourcis", self.show_help)

        tb = QToolBar("Outils")
        tb.setMovable(False)
        self.addToolBar(tb)
        for a in (a_imp, a_save, a_exp, None, a_undo, None, *tool_actions, None, a_split, a_dup, a_del, a_mute, a_vol,
                  None, a_zout, a_zin, a_fit, a_snap):
            tb.addSeparator() if a is None else tb.addAction(a)
        tb.addSeparator()
        fb = QToolButton()
        fb.setText("Filtres ▾")
        fb.setToolTip("Appliquer un filtre au clip sélectionné")
        fb.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        fb.setMenu(self.filters_menu)
        tb.addWidget(fb)
        tb.addAction(a_fimport)

    def show_help(self):
        QMessageBox.information(self, "Raccourcis", (
            "Espace : lecture/pause\nS : couper au curseur\nSuppr : supprimer (Maj+Suppr : combler le vide)\n"
            "M : couper le son du clip\nCtrl+Z / Ctrl+Y : annuler / rétablir\n"
            "Ctrl+I : importer — Ctrl+E : exporter — Ctrl+S : enregistrer\n"
            "+ / - ou Ctrl+molette : zoom\nMolette : défilement horizontal\n"
            "Clic sur la règle : déplacer le curseur\n"
            "Bords d'un clip : ajuster (trim) — Centre : déplacer (aimanté)\n"
            "Flèches : image par image — Maj+flèches : 1 seconde\n\n"
            "Outils : V sélection, C rasoir (clic = coupe), H main (glisser = défiler)\n"
            "Ctrl+D : dupliquer — Ctrl+Maj+D : détacher l'audio — Ctrl+0 : zoom ajusté\n"
            "Filtres : onglet « Filtres » à gauche (double-clic = appliquer au clip sélectionné)\n"
            "Ctrl+Maj+I : importer un filtre (LUT .cube/.3dl ou preset .json/.txt)"))

    # ------------------------------------------------------------------ projet / undo
    def update_title(self):
        name = os.path.basename(self.file_path) if self.file_path else "Sans titre"
        self.setWindowTitle(f"{name} — {APP_NAME}")

    def push_undo(self, snap):
        self.undo_stack.append(snap)
        self.undo_stack = self.undo_stack[-100:]
        self.redo_stack.clear()

    def on_edited(self, snap):
        self.push_undo(snap)
        self.refresh()

    def edit(self, fn):
        self.flush_param()
        snap = self.project.snapshot()
        fn()
        self.push_undo(snap)
        self.refresh()

    def restore(self, snap):
        self.project.load_snapshot(snap)
        if self.timeline.selected and not self.project.get(self.timeline.selected):
            self.timeline.selected = None
        self.refresh_media_list()
        self.refresh()

    def undo(self):
        self.flush_param()
        if self.undo_stack:
            self.redo_stack.append(self.project.snapshot())
            self.restore(self.undo_stack.pop())

    def redo(self):
        self.flush_param()
        if self.redo_stack:
            self.undo_stack.append(self.project.snapshot())
            self.restore(self.redo_stack.pop())

    def refresh(self):
        self.timeline.update()
        self.timeline.view_changed.emit()
        self.update_time_label()
        self.update_status()
        self.refresh_clip_filters()
        if self.filter_preview_on:
            self.fprev_timer.start()
        else:
            self.update_preview(force=True)

    def refresh_media_list(self):
        self.media_list.clear()
        for m in self.project.media:
            it = QListWidgetItem(f"{m.name}\n{fmt_time(m.duration)}" if m.kind != "image" else m.name)
            if m.kind == "image":
                it.setIcon(QIcon(m.path))
            else:
                it.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaVolume if m.kind == "audio"
                                                     else QStyle.StandardPixmap.SP_MediaPlay))
            it.setToolTip(m.path)
            self.media_list.addItem(it)

    def new_project(self):
        self.stop()
        self.project.media.clear()
        self.project.clips.clear()
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.file_path = None
        self.timeline.selected = None
        self.refresh_media_list()
        self.set_playhead(0)
        self.refresh()
        self.update_title()

    def open_project(self):
        p, _ = QFileDialog.getOpenFileName(self, "Ouvrir un projet", "", f"Projet {APP_NAME} (*{PROJECT_EXT} *{LEGACY_PROJECT_EXT})")
        if p:
            self.load_project_file(p)

    def load_project_file(self, p):
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            self.stop()
            self.project.load_dict(d)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Erreur", f"Impossible d'ouvrir le projet :\n{e}")
            return
        missing = {m.path for m in self.project.media if not os.path.exists(m.path)}
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.file_path = p
        self.refresh_media_list()
        self.set_playhead(0)
        self.refresh()
        self.update_title()
        if missing:
            QMessageBox.warning(self, "Fichiers manquants", "Médias introuvables :\n" + "\n".join(sorted(missing)))

    def save_project(self, as_new=False):
        p = self.file_path
        if as_new or not p:
            p, _ = QFileDialog.getSaveFileName(self, "Enregistrer le projet", "projet" + PROJECT_EXT,
                                               f"Projet {APP_NAME} (*{PROJECT_EXT} *{LEGACY_PROJECT_EXT})")
            if not p:
                return
            if not p.endswith(PROJECT_EXT):
                p += PROJECT_EXT
        with open(p, "w", encoding="utf-8") as f:
            json.dump(self.project.to_dict(), f, indent=2, ensure_ascii=False)
        self.file_path = p
        self.update_title()
        self.statusBar().showMessage(f"Projet enregistré : {p}", 4000)

    # ------------------------------------------------------------------ import / ajout de clips
    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        files = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        projects = [f for f in files if f.endswith((PROJECT_EXT, LEGACY_PROJECT_EXT))]
        if projects:
            self.load_project_file(projects[0])
        else:
            self.import_any(files)

    def import_any(self, files):
        """Route chaque fichier : filtre (LUT/preset) ou média."""
        filters = [f for f in files if os.path.splitext(f)[1].lower() in FILTER_EXT]
        media = [f for f in files if f not in filters]
        if filters:
            self.import_filter_files(filters)
        if media:
            self.import_files(media)

    def remove_media(self, row):
        if 0 <= row < len(self.project.media):
            path = self.project.media[row].path
            self.project.media.pop(row)
            self.project.clips[:] = [c for c in self.project.clips if c.path != path]
            self.refresh_media_list(); self.refresh()

    def show_media_properties(self, row):
        if 0 <= row < len(self.project.media):
            m = self.project.media[row]
            QMessageBox.information(self, "Propriétés du média", f"{m.name}\n{m.kind} — {fmt_time(m.duration)}\n{m.path}")

    def show_clip_properties(self):
        c = self.selected_clip()
        if c: QMessageBox.information(self, "Propriétés du clip", f"{c.name}\nPiste: {TRACKS[c.track][0]}\nDébut: {fmt_time(c.start)}\nDurée: {fmt_time(c.duration)}\nFiltres: {len(c.filters)}")

    def import_dialog(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Importer des médias", "", MEDIA_FILTER)
        if files:
            self.import_files(files)

    def import_files(self, files):
        snap = self.project.snapshot()
        added = 0
        errors = []
        known = {m.path for m in self.project.media}
        for f in files:
            f = os.path.abspath(f)
            if f in known:
                continue
            try:
                self.project.media.append(probe_media(f))
                known.add(f)
                added += 1
            except Exception as e:  # noqa: BLE001
                errors.append(f"{os.path.basename(f)} : {e}")
        if added:
            self.push_undo(snap)
            self.refresh_media_list()
            self.statusBar().showMessage(f"{added} média(s) importé(s). Double-clique pour les ajouter à la timeline.", 6000)
        if errors:
            QMessageBox.warning(self, "Import", "Impossible d'importer :\n" + "\n".join(errors))

    def on_drop_media(self, idx, t, track):
        self.add_media_at(idx, t, None if track < 0 else track)

    def add_media_at(self, idx, t, track=None):
        if not 0 <= idx < len(self.project.media):
            return
        m = self.project.media[idx]
        want = "audio" if m.kind == "audio" else "video"
        if track is None or TRACKS[track][1] != want:
            track = DEFAULT_TRACK[m.kind]
        clip = Clip(uuid.uuid4().hex[:8], m.path, m.kind, track, max(0.0, t), 0.0, m.duration,
                    IMAGE_MAX_DUR if m.kind == "image" else m.duration, m.has_audio)

        def do():
            changed = True
            while changed:
                changed = False
                for o in self.project.clips:
                    if o.track == clip.track and clip.start < o.end - 1e-6 and o.start < clip.end - 1e-6:
                        clip.start = o.end
                        changed = True
            self.project.clips.append(clip)
            self.timeline.selected = clip.id
        self.edit(do)

    # ------------------------------------------------------------------ édition
    def selected_clip(self):
        return self.project.get(self.timeline.selected) if self.timeline.selected else None

    def split_at_playhead(self):
        t = self.playhead
        sel = self.selected_clip()
        targets = [sel] if sel else [c for c in self.project.clips]
        targets = [c for c in targets if c.start + 0.05 < t < c.end - 0.05]
        if not targets:
            self.statusBar().showMessage("Place le curseur à l'intérieur d'un clip pour le couper.", 3000)
            return

        def do():
            for c in targets:
                self.do_split(c, t)
        self.edit(do)

    def do_split(self, c, t):
        off = t - c.start
        right = copy.copy(c)
        right.id = uuid.uuid4().hex[:8]
        right.filters = copy.deepcopy(c.filters)
        right.start = t
        right.src_in = c.src_in + off
        c.src_out = c.src_in + off
        self.project.clips.append(right)

    def razor(self, clip_id, t):
        c = self.project.get(clip_id)
        if c and c.start + 0.05 < t < c.end - 0.05:
            self.edit(lambda: self.do_split(c, t))

    def delete_selected(self, ripple=False):
        c = self.selected_clip()
        if not c:
            return

        def do():
            self.project.clips.remove(c)
            if ripple:
                for o in self.project.clips:
                    if o.track == c.track and o.start >= c.end - 1e-6:
                        o.start -= c.duration
            self.timeline.selected = None
        self.edit(do)

    def toggle_mute(self):
        c = self.selected_clip()
        if c and c.kind != "image":
            self.edit(lambda: setattr(c, "mute", not c.mute))

    def set_volume(self):
        c = self.selected_clip()
        if not c or c.kind == "image":
            return
        v, ok = QInputDialog.getInt(self, "Volume", "Volume du clip (%) :", int(c.volume * 100), 0, 300)
        if ok:
            self.edit(lambda: setattr(c, "volume", v / 100.0))

    def update_status(self):
        c = self.selected_clip()
        if c:
            self.statusBar().showMessage(
                f"{c.name} — piste {TRACKS[c.track][0]} — début {fmt_time(c.start)} — durée {fmt_time(c.duration)}"
                f" — source {fmt_time(c.src_in)} → {fmt_time(c.src_out)}")

    # ------------------------------------------------------------------ lecture / aperçu
    def update_time_label(self):
        self.time_label.setText(f"{fmt_time(self.playhead)} / {fmt_time(self.project.duration())}")

    def set_playhead(self, t):
        self.playhead = min(max(0.0, t), max(self.project.duration(), 0.0))
        self.timeline.playhead = self.playhead
        self.timeline.update()
        self.update_time_label()
        if self.filter_preview_on and self.playing:
            self.chk_preview.setChecked(False)   # la lecture reprend l'aperçu normal
        if self.filter_preview_on:
            self.fprev_timer.start()
        else:
            self.update_preview()
        if self.playing:
            self.panel.ensure_visible(self.playhead)

    def toggle_play(self):
        if self.playing:
            self.stop()
        else:
            if self.project.duration() <= 0:
                return
            if self.filter_preview_on:
                self.chk_preview.setChecked(False)
            if self.playhead >= self.project.duration() - 0.01:
                self.playhead = 0.0
            self.playing = True
            self.last_tick = time.monotonic()
            self.btn_play.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPause))
            self.timer.start()
            self.update_preview(force=True)

    def stop(self):
        self.playing = False
        self.timer.stop()
        self.btn_play.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.vplayer.pause()
        self.aplayer.pause()

    def tick(self):
        now = time.monotonic()
        t = self.playhead + (now - self.last_tick)
        self.last_tick = now
        end = self.project.duration()
        if t >= end:
            self.set_playhead(end)
            self.stop()
            return
        self.set_playhead(t)

    def clip_at_time(self, t, kind):
        best = None
        for c in self.project.clips:
            ok = (c.kind == "audio") if kind == "audio" else (c.kind in ("video", "image"))
            if ok and c.start <= t < c.end:
                if best is None or c.track < best.track:
                    best = c
        return best

    def on_vstatus(self, status):
        if self.vpending and status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia):
            self.vpending = False
            self.update_preview(force=True)

    def on_astatus(self, status):
        if self.apending and status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia):
            self.apending = False
            self.update_preview(force=True)

    def sync_player(self, player, out, clip, cur_attr, pend_attr, force):
        PS = QMediaPlayer.PlaybackState
        if clip is None:
            if getattr(self, cur_attr) is not None:
                player.stop()
                player.setSource(QUrl())
                setattr(self, cur_attr, None)
            return
        if getattr(self, cur_attr) != clip.id:
            player.setSource(QUrl.fromLocalFile(clip.path))
            setattr(self, cur_attr, clip.id)
            setattr(self, pend_attr, True)
            force = True
        pos = int((self.playhead - clip.start + clip.src_in) * 1000)
        out.setMuted(clip.mute)
        out.setVolume(min(1.0, clip.volume))
        if force or not self.playing or abs(player.position() - pos) > 300:
            player.setPosition(pos)
        if self.playing:
            if player.playbackState() != PS.PlayingState:
                player.play()
        else:
            player.pause()

    def update_preview(self, force=False):
        if self.filter_preview_on:
            return
        vc = self.clip_at_time(self.playhead, "video")
        if vc is None:
            self.sync_player(self.vplayer, self.vout, None, "vcur", "vpending", force)
            self.stack.setCurrentIndex(0)
        elif vc.kind == "image":
            if self.vcur != vc.id:
                self.vplayer.stop()
                self.vplayer.setSource(QUrl())
                self.vcur = vc.id
                self.image_view.set_image(vc.path)
            self.stack.setCurrentIndex(2)
        else:
            if self.vcur is not None and self.project.get(self.vcur) is not None \
                    and self.project.get(self.vcur).kind == "image":
                self.vcur = None
            self.sync_player(self.vplayer, self.vout, vc, "vcur", "vpending", force)
            self.stack.setCurrentIndex(1)
        ac = self.clip_at_time(self.playhead, "audio")
        self.sync_player(self.aplayer, self.aout, ac, "acur", "apending", force)

    # ------------------------------------------------------------------ outils
    def set_tool(self, key):
        self.timeline.tool = key
        self.timeline.hover_x = None
        self.timeline.setCursor({"select": Qt.CursorShape.ArrowCursor, "razor": Qt.CursorShape.CrossCursor,
                                 "hand": Qt.CursorShape.OpenHandCursor}[key])
        self.timeline.update()
        self.statusBar().showMessage({"select": "Outil Sélection : déplacer / ajuster les clips.",
                                      "razor": "Outil Rasoir : clique sur un clip pour le couper.",
                                      "hand": "Outil Main : glisse pour faire défiler la timeline."}[key], 4000)

    def duplicate_clip(self):
        c = self.selected_clip()
        if not c:
            self.statusBar().showMessage("Sélectionne un clip à dupliquer.", 3000)
            return
        new = copy.copy(c)
        new.id = uuid.uuid4().hex[:8]
        new.filters = copy.deepcopy(c.filters)
        new.start = c.end

        def do():
            changed = True
            while changed:
                changed = False
                for o in self.project.clips:
                    if o.track == new.track and new.start < o.end - 1e-6 and o.start < new.end - 1e-6:
                        new.start = o.end
                        changed = True
            self.project.clips.append(new)
            self.timeline.selected = new.id
        self.edit(do)

    def detach_audio(self):
        c = self.selected_clip()
        if not c or c.kind != "video" or not c.has_audio:
            self.statusBar().showMessage("Sélectionne un clip vidéo avec du son pour détacher l'audio.", 4000)
            return
        if not tool_path("ffmpeg"):
            QMessageBox.critical(self, "Détacher l'audio", "ffmpeg est introuvable.")
            return
        out_dir = CACHE_DIR / "audio"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{Path(c.path).stem}_{c.id}_{int(time.time())}.wav"
        r = subprocess.run([tool_path("ffmpeg"), "-v", "error", "-y", "-ss", f"{c.src_in:.3f}", "-t", f"{c.duration:.3f}",
                            "-i", c.path, "-vn", "-ac", "2", "-ar", "48000", str(out)],
                           capture_output=True, text=True)
        if r.returncode != 0 or not out.exists():
            QMessageBox.critical(self, "Détacher l'audio", r.stderr.strip()[-400:] or "Échec de l'extraction")
            return
        try:
            media = probe_media(str(out))
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Détacher l'audio", str(e))
            return
        aclip = Clip(uuid.uuid4().hex[:8], media.path, "audio", 2, c.start, 0.0, min(media.duration, c.duration),
                     media.duration, True)

        def do():
            self.project.media.append(media)
            for tr in (2, 3):
                aclip.track = tr
                if not any(o.track == tr and aclip.start < o.end - 1e-6 and o.start < aclip.end - 1e-6
                           for o in self.project.clips):
                    break
            c.mute = True
            self.project.clips.append(aclip)
            self.timeline.selected = aclip.id
        self.edit(do)
        self.refresh_media_list()
        self.statusBar().showMessage("Audio détaché sur la piste audio ; le clip vidéo d'origine est muet.", 5000)

    def close_gaps(self):
        sel = self.selected_clip()
        tracks = [sel.track] if sel else sorted({c.track for c in self.project.clips})

        def do():
            for tr in tracks:
                pos = None
                for c in sorted((c for c in self.project.clips if c.track == tr), key=lambda c: c.start):
                    if pos is not None and c.start > pos:
                        c.start = pos
                    pos = c.end
        self.edit(do)

    def fit_zoom(self):
        tl = self.timeline
        d = max(self.project.duration(), 1.0)
        tl.pps = min(800.0, max(2.0, (tl.width() - HEADER_W - 40) / d))
        tl.scroll_x = 0.0
        tl.view_changed.emit()
        tl.update()

    def capture_frame(self):
        c = self.clip_at_time(self.playhead, "video")
        if c is None:
            self.statusBar().showMessage("Aucun clip vidéo/image sous le curseur.", 3000)
            return
        p, _ = QFileDialog.getSaveFileName(self, "Capturer l'image", os.path.join(os.path.expanduser("~"), "capture.png"),
                                           "PNG (*.png);;JPEG (*.jpg)")
        if not p:
            return
        err = self.render_frame(c, max(0.0, self.playhead - c.start), p, None)
        if err:
            QMessageBox.critical(self, "Capture", err)
        else:
            self.statusBar().showMessage(f"Image enregistrée : {p}", 5000)

    # ------------------------------------------------------------------ filtres (interface)
    def build_filters_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QLabel("Bibliothèque — double-clic : appliquer au clip sélectionné"))
        self.filter_list = QListWidget()
        self.filter_list.itemDoubleClicked.connect(lambda it: self.apply_filter(it.data(Qt.ItemDataRole.UserRole)))
        lay.addWidget(self.filter_list, 3)
        row = QHBoxLayout()
        b_apply = QPushButton("Appliquer")
        b_apply.clicked.connect(lambda: self.apply_filter(self.current_library_filter()))
        b_import = QPushButton("Importer un filtre…")
        b_import.setToolTip("LUT 3D (.cube .3dl .dat .m3d .csp) ou preset ffmpeg (.json .txt .vf)")
        b_import.clicked.connect(self.import_filter_dialog)
        b_del = QPushButton("Supprimer")
        b_del.setToolTip("Supprime le filtre importé sélectionné de la bibliothèque")
        b_del.clicked.connect(self.delete_imported_filter)
        for b in (b_apply, b_import, b_del):
            row.addWidget(b)
        lay.addLayout(row)

        lay.addWidget(QLabel("Filtres du clip sélectionné"))
        self.clip_filters = QListWidget()
        self.clip_filters.currentRowChanged.connect(self.show_filter_params)
        lay.addWidget(self.clip_filters, 2)
        self.params_box = QWidget()
        self.params_layout = QFormLayout(self.params_box)
        lay.addWidget(self.params_box)
        row2 = QHBoxLayout()
        for text, fn in (("Retirer", self.remove_clip_filter), ("▲", lambda: self.move_clip_filter(-1)),
                         ("▼", lambda: self.move_clip_filter(1))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row2.addWidget(b)
        lay.addLayout(row2)
        self.chk_preview = QCheckBox("Aperçu image avec filtres")
        self.chk_preview.setToolTip("Affiche l'image du curseur avec les filtres du clip (calculée par ffmpeg). "
                                    "La lecture normale n'applique pas les filtres ; l'export, si.")
        self.chk_preview.toggled.connect(self.toggle_filter_preview)
        lay.addWidget(self.chk_preview)
        self.refresh_filter_list()
        return w

    def refresh_filter_list(self):
        self.filter_list.clear()
        for fid, d in self.flib.items.items():
            it = QListWidgetItem(d["name"] + ("" if d["builtin"] else "  (importé)"))
            it.setData(Qt.ItemDataRole.UserRole, fid)
            self.filter_list.addItem(it)

    def current_library_filter(self):
        it = self.filter_list.currentItem()
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    def rebuild_filters_menu(self):
        self.filters_menu.clear()
        imported = False
        for fid, d in self.flib.items.items():
            if not d["builtin"] and not imported:
                self.filters_menu.addSeparator()
                imported = True
            a = self.filters_menu.addAction(d["name"])
            a.triggered.connect(lambda checked=False, f=fid: self.apply_filter(f))

    def import_filter_dialog(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Importer un filtre", "", FILTER_FILE_FILTER)
        if files:
            self.import_filter_files(files)

    def import_filter_files(self, files):
        done, errors = [], []
        for f in files:
            try:
                done += self.flib.import_file(f)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{os.path.basename(f)} : {e}")
        if done:
            self.refresh_filter_list()
            self.left_tabs.setCurrentIndex(1)
            self.statusBar().showMessage("Filtre(s) importé(s) : " + ", ".join(done)
                                         + " — double-clic pour l'appliquer à un clip.", 8000)
        if errors:
            QMessageBox.warning(self, "Import de filtre", "Impossible d'importer :\n" + "\n".join(errors))

    def delete_imported_filter(self):
        fid = self.current_library_filter()
        if not fid:
            return
        if self.flib.items[fid]["builtin"]:
            self.statusBar().showMessage("Les filtres intégrés ne peuvent pas être supprimés.", 3000)
            return
        self.flib.remove(fid)
        self.refresh_filter_list()
        self.refresh_clip_filters(force=True)

    def apply_filter(self, fid):
        c = self.selected_clip()
        if not fid or fid not in self.flib.items:
            self.statusBar().showMessage("Choisis un filtre dans la liste.", 3000)
            return
        if not c or c.kind == "audio":
            self.statusBar().showMessage("Sélectionne d'abord un clip vidéo ou image sur la timeline.", 4000)
            return
        self.edit(lambda: c.filters.append({"id": fid, "params": self.flib.defaults(fid)}))
        self.clip_filters.setCurrentRow(len(c.filters) - 1)

    def clear_filters(self):
        c = self.selected_clip()
        if c and c.filters:
            self.edit(lambda: c.filters.clear())

    def remove_clip_filter(self):
        c, row = self.selected_clip(), self.clip_filters.currentRow()
        if c and 0 <= row < len(c.filters):
            self.edit(lambda: c.filters.pop(row))

    def move_clip_filter(self, delta):
        c, row = self.selected_clip(), self.clip_filters.currentRow()
        new = row + delta
        if c and 0 <= row < len(c.filters) and 0 <= new < len(c.filters):
            self.edit(lambda: c.filters.insert(new, c.filters.pop(row)))
            self.clip_filters.setCurrentRow(new)

    def cf_sig(self, c):
        return None if c is None else (c.id, json.dumps(c.filters, sort_keys=True))

    def refresh_clip_filters(self, force=False):
        c = self.selected_clip()
        sig = self.cf_sig(c)
        if sig == self._cf_sig and not force:
            return
        self._cf_sig = sig
        row = self.clip_filters.currentRow()
        self.clip_filters.blockSignals(True)
        self.clip_filters.clear()
        if c:
            for f in c.filters:
                self.clip_filters.addItem(self.flib.name(f["id"]))
        self.clip_filters.blockSignals(False)
        if c and c.filters:
            self.clip_filters.setCurrentRow(min(max(row, 0), len(c.filters) - 1))
        self.show_filter_params(self.clip_filters.currentRow())

    def show_filter_params(self, row):
        while self.params_layout.rowCount():
            self.params_layout.removeRow(0)
        c = self.selected_clip()
        if not c or not (0 <= row < len(c.filters)):
            return
        entry = c.filters[row]
        spec = self.flib.items.get(entry["id"], {}).get("params", [])
        if not spec:
            self.params_layout.addRow(QLabel("Aucun réglage pour ce filtre."))
            return
        for key, label, lo, hi, default, step in spec:
            sp = QDoubleSpinBox()
            sp.setRange(lo, hi)
            sp.setSingleStep(step)
            sp.setDecimals(2)
            sp.setValue(float(entry.get("params", {}).get(key, default)))
            sp.valueChanged.connect(lambda v, k=key, r=row: self.on_param(r, k, v))
            self.params_layout.addRow(label, sp)

    def on_param(self, row, key, value):
        c = self.selected_clip()
        if not c or not (0 <= row < len(c.filters)):
            return
        if self.pending_param_snap is None:
            self.pending_param_snap = self.project.snapshot()   # état avant la 1re modification
        c.filters[row].setdefault("params", {})[key] = round(float(value), 3)
        self._cf_sig = self.cf_sig(c)
        self.param_timer.start()

    def flush_param(self):
        """Valide (undo) une série de modifications de réglages et rafraîchit l'aperçu."""
        if self.param_timer.isActive():
            self.param_timer.stop()
        if self.pending_param_snap is not None:
            self.push_undo(self.pending_param_snap)
            self.pending_param_snap = None
            if self.filter_preview_on:
                self.fprev_timer.start()

    # ------------------------------------------------------------------ aperçu image avec filtres
    def toggle_filter_preview(self, on):
        self.filter_preview_on = bool(on)
        if on:
            self.render_filter_preview()
        else:
            self.fprev_timer.stop()
            self.update_preview(force=True)

    def preview_target_clip(self):
        c = self.selected_clip()
        if c and c.kind in ("video", "image"):
            return c
        return self.clip_at_time(self.playhead, "video")

    def render_filter_preview(self):
        if not self.filter_preview_on:
            return
        c = self.preview_target_clip()
        if c is None:
            self.stack.setCurrentIndex(0)
            return
        r = self.playhead - c.start
        r = min(max(r, 0.0), max(0.0, c.duration - 0.05))
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        out = str(CACHE_DIR / "preview_frame.png")
        err = self.render_frame(c, r, out, 960)
        if err:
            self.statusBar().showMessage("Aperçu des filtres impossible : " + err, 6000)
            return
        self.vplayer.pause()
        self.aplayer.pause()
        self.image_view.set_image(out)
        self.vcur = None
        self.stack.setCurrentIndex(2)

    def render_frame(self, clip, r, out, width):
        """Rend une image du clip (avec ses filtres) à r secondes de son début. Renvoie un message d'erreur ou None."""
        if not tool_path("ffmpeg"):
            return "ffmpeg est introuvable"
        chain = []
        if width:
            chain.append(f"scale={width}:-2")
        for f in clip.filters:
            vf = self.flib.build(f, clip.duration)
            if vf:
                chain.append(vf)
        cmd = [tool_path("ffmpeg"), "-v", "error", "-y"]
        if clip.kind == "image":
            cmd += ["-i", clip.path]
        else:
            cmd += ["-ss", f"{clip.src_in:.3f}", "-i", clip.path]
            chain.append(f"select='gte(t,{r:.3f})'")
        cmd += ["-vf", ",".join(chain) if chain else "null", "-frames:v", "1", "-update", "1", out]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            return "délai dépassé"
        if res.returncode != 0 or not os.path.exists(out):
            lines = [ln for ln in res.stderr.strip().splitlines() if ln.strip()]
            return lines[-1] if lines else "échec de ffmpeg"
        return None

    def on_selection(self):
        self.update_status()
        self.refresh_clip_filters()
        if self.filter_preview_on:
            self.fprev_timer.start()

    # ------------------------------------------------------------------ export
    def export_video(self):
        if not self.project.clips:
            QMessageBox.information(self, "Export", "La timeline est vide.")
            return
        if not tool_path("ffmpeg"):
            QMessageBox.critical(self, "Export", "ffmpeg est introuvable.\nInstalle-le : sudo apt install ffmpeg")
            return
        self.stop()
        default = os.path.join(os.path.expanduser("~"), "export.mp4")
        dlg = ExportDialog(self, default)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        out = dlg.path.text().strip()
        if not out:
            return
        W, H = dlg.res.currentData()
        fps = dlg.fps.currentData()
        total = self.project.duration()
        cmd = build_ffmpeg_cmd(self.project, out, W, H, fps, dlg.crf.value(), dlg.preset.currentText(),
                               self.flib)

        self.progress = QProgressDialog("Encodage en cours…", "Annuler", 0, 100, self)
        self.progress.setWindowTitle("Export")
        self.progress.setWindowModality(Qt.WindowModality.WindowModal)
        self.progress.setMinimumDuration(0)
        self.progress.setValue(0)
        self.proc = QProcess(self)
        self.proc.setProgram(cmd[0])
        self.proc.setArguments(cmd[1:])
        self.err_lines = []
        self.out_buf = ""

        def on_out():
            self.out_buf += bytes(self.proc.readAllStandardOutput()).decode(errors="ignore")
            *lines, self.out_buf = self.out_buf.split("\n")
            for ln in lines:
                if ln.startswith("out_time_us=") or ln.startswith("out_time_ms="):
                    try:
                        sec = int(ln.split("=")[1]) / 1e6
                        self.progress.setValue(min(99, int(sec / total * 100)))
                    except ValueError:
                        pass

        def on_err():
            txt = bytes(self.proc.readAllStandardError()).decode(errors="ignore")
            self.err_lines += txt.splitlines()
            self.err_lines = self.err_lines[-25:]

        def on_done(code, status):
            cancelled = self.progress.wasCanceled()
            self.progress.close()
            if cancelled:
                self.statusBar().showMessage("Export annulé.", 4000)
                if os.path.exists(out):
                    os.remove(out)
            elif code == 0:
                self.statusBar().showMessage(f"Export terminé : {out}", 8000)
                QMessageBox.information(self, "Export", f"Export terminé :\n{out}")
            else:
                QMessageBox.critical(self, "Erreur d'export", "ffmpeg a échoué :\n" + "\n".join(self.err_lines[-12:]))

        self.proc.readyReadStandardOutput.connect(on_out)
        self.proc.readyReadStandardError.connect(on_err)
        self.proc.finished.connect(on_done)
        self.progress.canceled.connect(lambda: self.proc.kill())
        self.proc.start()

    def closeEvent(self, ev):
        self.stop()
        if self.proc and self.proc.state() != QProcess.ProcessState.NotRunning:
            self.proc.kill()
        super().closeEvent(ev)


def apply_dark(app):
    app.setStyle("Fusion")
    pal = QPalette()
    c = QColor
    pal.setColor(QPalette.ColorRole.Window, c("#2b2d31"))
    pal.setColor(QPalette.ColorRole.WindowText, c("#e6e6e6"))
    pal.setColor(QPalette.ColorRole.Base, c("#1e1f22"))
    pal.setColor(QPalette.ColorRole.AlternateBase, c("#2b2d31"))
    pal.setColor(QPalette.ColorRole.Text, c("#e6e6e6"))
    pal.setColor(QPalette.ColorRole.Button, c("#35373c"))
    pal.setColor(QPalette.ColorRole.ButtonText, c("#e6e6e6"))
    pal.setColor(QPalette.ColorRole.ToolTipBase, c("#1e1f22"))
    pal.setColor(QPalette.ColorRole.ToolTipText, c("#e6e6e6"))
    pal.setColor(QPalette.ColorRole.Highlight, c("#3b6ea8"))
    pal.setColor(QPalette.ColorRole.HighlightedText, c("white"))
    app.setPalette(pal)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    apply_dark(app)
    w = MainWindow()
    w.resize(1280, 800)
    w.show()
    args = [a for a in sys.argv[1:] if os.path.exists(a)]
    projects = [a for a in args if a.endswith((PROJECT_EXT, LEGACY_PROJECT_EXT))]
    if projects:
        w.load_project_file(projects[0])
    elif args:
        w.import_any(args)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
