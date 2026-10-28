"""Cut out the subject of a photo: remove the background, keep a transparent PNG.

Runs the 'silueta' model (a compressed U²-Net, Apache-2.0, from the rembg project) locally with
ONNX Runtime. Nothing leaves the computer. The model file ships in dragshift/assets/models."""
from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

import numpy as np
from PIL import Image

from .convert import flatten, open_image, save_image
from .util import UserError, sibling

MODEL_FILE = Path(__file__).with_name("assets") / "models" / "silueta.onnx"
MODEL_SHA256 = "75da6c8d2f8096ec743d071951be73b4a8bc7b3e51d9a6625d63644f90ffeedb"
SIZE = 320
NO_SUBJECT_BELOW = 0.5   # the model's strongest "this is the subject" score; blank images peak near 0.1
MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

_session = None
_lock = threading.Lock()


def _load():
    """The model session, created once (several jobs may ask at the same time)."""
    global _session
    with _lock:
        if _session is None:
            if not MODEL_FILE.is_file():
                raise UserError("The background-removal model is missing "
                                f"({MODEL_FILE.name}). Reinstall DragShift or put it in {MODEL_FILE.parent}.")
            if hashlib.sha256(MODEL_FILE.read_bytes()).hexdigest() != MODEL_SHA256:
                raise UserError("The background-removal model file is damaged. Reinstall DragShift.")
            import onnxruntime as ort
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = max(1, min(4, os.cpu_count() or 1))   # leave the PC usable
            opts.log_severity_level = 3
            _session = ort.InferenceSession(str(MODEL_FILE), opts, providers=["CPUExecutionProvider"])
        return _session


def subject_mask(im: Image.Image) -> Image.Image:
    """An 'L' image the size of `im`: 255 where the subject is, 0 for background, soft edges between."""
    sess = _load()
    rgb = np.asarray(flatten(im.convert("RGBA") if im.mode not in ("RGB", "L") else im.convert("RGB"))
                     .resize((SIZE, SIZE), Image.LANCZOS), dtype=np.float32)
    rgb = rgb / max(float(rgb.max()), 1e-6)
    x = ((rgb - MEAN) / STD).transpose(2, 0, 1)[None].astype(np.float32)
    pred = sess.run(None, {sess.get_inputs()[0].name: x})[0][0, 0]
    lo, hi = float(pred.min()), float(pred.max())
    if hi < NO_SUBJECT_BELOW:             # blank wall, flat colour, noise: don't stretch it into a fake cutout
        return Image.new("L", im.size, 0)
    pred = (pred - lo) / max(hi - lo, 1e-6)
    pred = np.clip((pred - 0.12) / 0.76, 0.0, 1.0)           # drop the faint haze around the subject
    mask = Image.fromarray((pred * 255).astype(np.uint8), "L")
    return mask.resize(im.size, Image.BICUBIC)


def remove_background(im: Image.Image) -> Image.Image:
    """RGBA copy of `im` with the background transparent (existing transparency is kept)."""
    mask = subject_mask(im)
    out = im.convert("RGBA")
    if im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info:
        a = np.asarray(out.getchannel("A"), dtype=np.uint16) * np.asarray(mask, dtype=np.uint16) // 255
        mask = Image.fromarray(a.astype(np.uint8), "L")
    out.putalpha(mask)
    return out


def cut_out(src: str | Path) -> list[Path]:
    result = remove_background(open_image(src))
    alpha = np.asarray(result.getchannel("A"))
    if alpha.max() == 0:
        raise UserError("Couldn't find a subject in this image.")
    out = sibling(src, "Cutout", ".png")
    save_image(result, out, "PNG")
    return [out]
