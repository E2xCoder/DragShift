"""Make a photo bigger, or clean it up at the same size, with Real-ESRGAN 'general v3'.

Local and offline: a small (1.2 M parameter) network run through ONNX Runtime on the CPU.
  4×  the model's native scale        2×  the 4× result halved (sharper than a plain 2× resize)
  1×  'Enhance': the 4× result shrunk back, which removes noise, blur and JPEG blocks at the same size.
The model file ships in dragshift/assets/models (BSD-3-Clause, Xintao Wang et al.)."""
from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image, ImageFilter

from .convert import open_image, save_like_source
from .util import UserError

MODEL_FILE = Path(__file__).with_name("assets") / "models" / "realesr-general-x4v3.onnx"
MODEL_SHA256 = "7b10e80021ec3e0ff35017ae5022791879d93a799b2e201e1010a37510202266"
NATIVE = 4
TILE = 192            # input pixels per tile (kept small: memory use and responsiveness)
PAD = 16              # extra context on every side so tile borders don't show
MAX_OUTPUT_PIXELS = 80_000_000
SCALES = {1: "Enhanced", 2: "2x", 4: "4x"}

_session = None
_lock = threading.Lock()


def _load():
    global _session
    with _lock:
        if _session is None:
            if not MODEL_FILE.is_file():
                raise UserError(f"The upscaling model is missing ({MODEL_FILE.name}). Reinstall DragShift.")
            if hashlib.sha256(MODEL_FILE.read_bytes()).hexdigest() != MODEL_SHA256:
                raise UserError("The upscaling model file is damaged. Reinstall DragShift.")
            import onnxruntime as ort
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = max(1, min(6, os.cpu_count() or 1))
            opts.log_severity_level = 3
            _session = ort.InferenceSession(str(MODEL_FILE), opts, providers=["CPUExecutionProvider"])
        return _session


def output_size(size: tuple[int, int], scale: int) -> tuple[int, int]:
    return size[0] * scale, size[1] * scale


def check_size(size: tuple[int, int], scale: int) -> None:
    w, h = output_size(size, scale)
    if w * h > MAX_OUTPUT_PIXELS:
        raise UserError(f"The result would be {w}×{h} px ({w * h / 1e6:.0f} megapixels), over the "
                        f"{MAX_OUTPUT_PIXELS // 1_000_000} MP limit. Choose a smaller scale or a smaller image.")


def _run(sess, tile: np.ndarray) -> np.ndarray:
    """One float tile (h, w, 3) in 0..1 → (4h, 4w, 3) in 0..1."""
    x = np.ascontiguousarray(tile.transpose(2, 0, 1)[None], dtype=np.float32)
    y = sess.run(None, {sess.get_inputs()[0].name: x})[0][0]
    return np.clip(y.transpose(1, 2, 0), 0.0, 1.0)


def _resize(arr: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """Float RGB array → resized float array (Lanczos, per channel, no 8-bit rounding)."""
    chans = [np.asarray(Image.fromarray(arr[..., c], "F").resize(size, Image.LANCZOS)) for c in range(3)]
    return np.clip(np.stack(chans, -1), 0.0, 1.0)


def upscale_rgb(rgb: Image.Image, scale: int, progress: Callable[[float], None] | None = None) -> Image.Image:
    """Tile by tile: each tile is run with PAD pixels of real neighbours around it, then only its
    own centre is kept, so the pieces join without seams."""
    if scale not in SCALES:
        raise ValueError(f"scale must be one of {sorted(SCALES)}")
    check_size(rgb.size, scale)
    sess = _load()
    src = np.asarray(rgb.convert("RGB"), dtype=np.float32) / 255.0
    h, w = src.shape[:2]
    padded = np.pad(src, ((PAD, PAD), (PAD, PAD), (0, 0)), mode="reflect" if min(h, w) > PAD else "edge")
    out = np.zeros((h * scale, w * scale, 3), dtype=np.uint8)
    ratio = scale / NATIVE
    tiles = [(y, x) for y in range(0, h, TILE) for x in range(0, w, TILE)]
    for i, (y, x) in enumerate(tiles):
        th, tw = min(TILE, h - y), min(TILE, w - x)
        block = padded[y:y + th + 2 * PAD, x:x + tw + 2 * PAD]
        big = _run(sess, block)                                           # (4(th+2P), 4(tw+2P))
        if ratio != 1:
            big = _resize(big, (round(big.shape[1] * ratio), round(big.shape[0] * ratio)))
        p = round(PAD * scale)
        core = big[p:p + th * scale, p:p + tw * scale]
        out[y * scale:y * scale + th * scale, x * scale:x * scale + tw * scale] = (core * 255 + 0.5).astype(np.uint8)
        if progress:
            progress((i + 1) / len(tiles))
    return Image.fromarray(out, "RGB")


def _bleed_colour(rgb: np.ndarray, alpha: np.ndarray, radius: int = 6) -> np.ndarray:
    """Fill fully transparent pixels with the colour of the nearest visible ones, so the model
    doesn't see a hard black/white fringe around the subject."""
    a = alpha.astype(np.float32) / 255.0
    pre = Image.fromarray((rgb.astype(np.float32) * a[..., None]).astype(np.uint8), "RGB").filter(ImageFilter.GaussianBlur(radius))
    cover = Image.fromarray(alpha, "L").filter(ImageFilter.GaussianBlur(radius))
    bled = np.asarray(pre, dtype=np.float32) / np.maximum(np.asarray(cover, dtype=np.float32)[..., None] / 255.0, 1e-3)
    return np.where((alpha < 255)[..., None], np.clip(np.where(a[..., None] > 0, rgb, bled), 0, 255), rgb).astype(np.uint8)


def upscale_image(im: Image.Image, scale: int = 4, progress: Callable[[float], None] | None = None) -> Image.Image:
    """`im` made `scale` times bigger ('1' = enhanced at the same size). Transparency is kept."""
    if im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info:
        rgba = im.convert("RGBA")
        arr = np.asarray(rgba)
        alpha = Image.fromarray(arr[..., 3], "L")
        rgb = _bleed_colour(arr[..., :3], arr[..., 3]) if (arr[..., 3] < 255).any() else arr[..., :3]
        up = upscale_rgb(Image.fromarray(rgb, "RGB"), scale, progress)
        up = up.convert("RGBA")
        up.putalpha(alpha.resize(up.size, Image.LANCZOS) if scale != 1 else alpha)
        return up
    return upscale_rgb(im, scale, progress)


def upscale_file(src: str | Path, scale: int = 4) -> list[Path]:
    im = open_image(src)
    check_size(im.size, scale)
    return [save_like_source(upscale_image(im, scale), src, SCALES[scale])]
