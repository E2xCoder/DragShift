"""Video and audio conversion with ffmpeg (bundled through the imageio-ffmpeg package; a system
ffmpeg on PATH is the fallback). Everything runs locally and the original is never changed."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .util import UserError, sibling

GIF_SECONDS = 20          # a GIF of a whole film would be gigabytes: only the first seconds
GIF_FPS = 12
GIF_WIDTH = 480

_NO_WINDOW = 0x08000000            # Windows: no console flash from a windowed app
_BELOW_NORMAL = 0x00004000         # keep the PC responsive while encoding

_exe: str | None = None
_looked = False


def ffmpeg_exe() -> str | None:
    global _exe, _looked
    if not _looked:
        _looked = True
        try:
            import imageio_ffmpeg
            p = imageio_ffmpeg.get_ffmpeg_exe()
            if p and Path(p).is_file():
                _exe = p
        except Exception:      # noqa: BLE001
            pass
        if _exe is None:
            _exe = shutil.which("ffmpeg")
    return _exe


def available() -> bool:
    return ffmpeg_exe() is not None


def _flags() -> int:
    return (_NO_WINDOW | _BELOW_NORMAL) if sys.platform == "win32" else 0


def _tail(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else "unknown error"


def run_ffmpeg(args: list[str], capture: bool = False) -> subprocess.CompletedProcess:
    exe = ffmpeg_exe()
    if not exe:
        raise UserError("ffmpeg wasn't found, so video and audio can't be converted.")
    return subprocess.run([exe, "-hide_banner", "-nostdin", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", creationflags=_flags())


@dataclass
class Info:
    duration: float = 0.0
    has_video: bool = False
    has_audio: bool = False
    width: int = 0
    height: int = 0


def probe(src: str | Path) -> Info:
    """Duration and streams, read from `ffmpeg -i` (no ffprobe needed)."""
    r = run_ffmpeg(["-i", str(src)])
    text = r.stderr
    info = Info()
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if m:
        info.duration = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    for line in text.splitlines():
        if "Stream #" not in line:
            continue
        if " Video:" in line and "attached pic" not in line:
            info.has_video = True
            d = re.search(r",\s*(\d{2,5})x(\d{2,5})", line)
            if d and not info.width:
                info.width, info.height = int(d[1]), int(d[2])
        elif " Audio:" in line:
            info.has_audio = True
    if not (info.has_video or info.has_audio):
        raise UserError("This doesn't look like a video or audio file ffmpeg can read.")
    return info


def _encode(src: Path, out: Path, args: list[str], pre: list[str] | None = None) -> Path:
    """Run ffmpeg into a hidden temp file next to `out`, then rename it into place."""
    tmp = out.with_name(f".~{out.stem}.part{out.suffix}")
    try:
        r = run_ffmpeg(["-y", "-loglevel", "error", *(pre or []), "-i", str(src), *args, str(tmp)])
        if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            raise UserError(f"Couldn't convert {src.name}: {_tail(r.stderr)}")
        os.replace(tmp, out)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return out


# target → (output extension, arguments). Even dimensions: H.264 and yuv420p need them.
_EVEN = "scale=trunc(iw/2)*2:trunc(ih/2)*2"
VIDEO_PRESETS = {
    "MP4": (".mp4", ["-vf", _EVEN, "-c:v", "libx264", "-preset", "medium", "-crf", "23", "-pix_fmt", "yuv420p",
                     "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-map_metadata", "0"]),
    "WEBM": (".webm", ["-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0", "-row-mt", "1", "-cpu-used", "4",
                       "-c:a", "libopus", "-b:a", "128k"]),
    "GIF": (".gif", ["-t", str(GIF_SECONDS), "-an", "-loop", "0", "-vf",
                     f"fps={GIF_FPS},scale='min({GIF_WIDTH},iw)':-2:flags=lanczos,split[a][b];"
                     "[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=5"]),
}
AUDIO_PRESETS = {
    "MP3": (".mp3", ["-vn", "-c:a", "libmp3lame", "-q:a", "2", "-map_metadata", "0"]),
    "WAV": (".wav", ["-vn", "-c:a", "pcm_s16le"]),
    "FLAC": (".flac", ["-vn", "-c:a", "flac", "-map_metadata", "0"]),
    "M4A": (".m4a", ["-vn", "-c:a", "aac", "-b:a", "192k", "-map_metadata", "0"]),
    "OGG": (".ogg", ["-vn", "-c:a", "libvorbis", "-q:a", "5", "-map_metadata", "0"]),
}


def convert(src: str | Path, target: str) -> list[Path]:
    src, target = Path(src), target.upper()
    if target in AUDIO_PRESETS:
        ext, args = AUDIO_PRESETS[target]
        if not probe(src).has_audio:
            raise UserError(f"{src.name} has no sound to extract.")
    elif target in VIDEO_PRESETS:
        ext, args = VIDEO_PRESETS[target]
        if not probe(src).has_video:
            raise UserError(f"{src.name} has no picture, so it can't become {target}.")
    else:
        raise UserError(f"Can't convert {src.suffix} to {target}.")
    return [_encode(src, sibling(src, ext=ext), args)]


def compress_video(src: str | Path) -> Path:
    """Smaller file, same look to the eye: H.264 at CRF 28, at most 1080p, AAC 128k."""
    src = Path(src)
    if not probe(src).has_video:
        raise UserError(f"{src.name} has no picture to compress.")
    args = ["-vf", "scale='min(1920,iw)':-2", "-c:v", "libx264", "-preset", "medium", "-crf", "28",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", "-map_metadata", "0"]
    return _encode(src, sibling(src, "Compressed", ".mp4"), args)


def mute_video(src: str | Path) -> Path:
    """The same video without its sound. The picture is copied as it is, so it's instant and lossless."""
    src = Path(src)
    info = probe(src)
    if not info.has_video:
        raise UserError(f"{src.name} has no picture.")
    if not info.has_audio:
        raise UserError(f"{src.name} has no sound to remove.")
    return _encode(src, sibling(src, "Muted"), ["-c", "copy", "-an", "-map", "0:v", "-map_metadata", "0"])


def clock(seconds: float) -> str:
    s = int(round(seconds))
    h, m, s = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"
