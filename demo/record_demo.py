"""Records a real demo of DragShift on Windows.

Runs on a GitHub Actions Windows machine: opens File Explorer, drags files with the real
mouse, presses Shift, uses the tools, and records the whole screen with ffmpeg.
Outputs go to demo_out/: demo_raw.mp4, steps.json (timestamps for captions), log.txt,
and a few screenshots for debugging."""
from __future__ import annotations

import ctypes
import json
import math
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

import pyautogui
from PIL import Image, ImageDraw, ImageFont, ImageGrab

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "demo_out"
OUT.mkdir(exist_ok=True)
DEMO = Path(r"C:\DragShiftDemo")
LOG = open(OUT / "log.txt", "w", encoding="utf-8")
STEPS: list[dict] = []
T0 = 0.0
user32 = ctypes.windll.user32

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.05

R_PETAL = 108  # matches radial.py: (R_IN + R_OUT) / 2


def log(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    LOG.write(f"[{time.time() - T0 if T0 else 0:7.2f}] {msg}\n")
    LOG.flush()


def step(caption: str):
    t = time.time() - T0
    STEPS.append({"t": round(t, 2), "caption": caption})
    log("STEP", caption)


def shot(name: str):
    try:
        ImageGrab.grab().save(OUT / f"{name}.png")
    except Exception as e:
        log("screenshot failed", e)


# ------------------------------------------------------------------ setup
def set_resolution(w: int, h: int):
    class DEVMODE(ctypes.Structure):
        _fields_ = [("dmDeviceName", ctypes.c_wchar * 32), ("dmSpecVersion", ctypes.c_ushort),
                    ("dmDriverVersion", ctypes.c_ushort), ("dmSize", ctypes.c_ushort),
                    ("dmDriverExtra", ctypes.c_ushort), ("dmFields", ctypes.c_ulong),
                    ("dmPositionX", ctypes.c_long), ("dmPositionY", ctypes.c_long),
                    ("dmDisplayOrientation", ctypes.c_ulong), ("dmDisplayFixedOutput", ctypes.c_ulong),
                    ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short),
                    ("dmYResolution", ctypes.c_short), ("dmTTOption", ctypes.c_short),
                    ("dmCollate", ctypes.c_short), ("dmFormName", ctypes.c_wchar * 32),
                    ("dmLogPixels", ctypes.c_ushort), ("dmBitsPerPel", ctypes.c_ulong),
                    ("dmPelsWidth", ctypes.c_ulong), ("dmPelsHeight", ctypes.c_ulong),
                    ("dmDisplayFlags", ctypes.c_ulong), ("dmDisplayFrequency", ctypes.c_ulong),
                    ("dmICMMethod", ctypes.c_ulong), ("dmICMIntent", ctypes.c_ulong),
                    ("dmMediaType", ctypes.c_ulong), ("dmDitherType", ctypes.c_ulong),
                    ("dmReserved1", ctypes.c_ulong), ("dmReserved2", ctypes.c_ulong),
                    ("dmPanningWidth", ctypes.c_ulong), ("dmPanningHeight", ctypes.c_ulong)]
    dm = DEVMODE()
    dm.dmSize = ctypes.sizeof(DEVMODE)
    if user32.EnumDisplaySettingsW(None, -1, ctypes.byref(dm)):
        dm.dmPelsWidth, dm.dmPelsHeight = w, h
        dm.dmFields = 0x80000 | 0x100000
        r = user32.ChangeDisplaySettingsW(ctypes.byref(dm), 0)
        log("ChangeDisplaySettings ->", r)


def make_files():
    DEMO.mkdir(exist_ok=True)
    for f in DEMO.iterdir():
        f.unlink()
    # a "photo": colourful scene
    W, H = 1600, 1066
    im = Image.new("RGB", (W, H))
    px = ImageDraw.Draw(im)
    for y in range(H):
        t = y / H
        px.line([(0, y), (W, y)], fill=(int(255 - 120 * t), int(170 - 90 * t), int(90 + 110 * t)))
    px.ellipse((W * 0.62, H * 0.18, W * 0.62 + 300, H * 0.18 + 300), fill=(255, 236, 170))
    for i, c in enumerate([(40, 60, 90), (30, 45, 70), (20, 30, 50)]):
        y0 = int(H * (0.62 + i * 0.1))
        pts = [(0, H)] + [(x, y0 - int(60 * math.sin(x / (180 + 40 * i) + i))) for x in range(0, W + 1, 20)] + [(W, H)]
        px.polygon(pts, fill=c)
    im.save(DEMO / "Sunset.jpg", quality=92)
    # a one-page "document" PDF
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 90), "Quarterly Notes", fontsize=26)
    y = 130
    for i in range(28):
        page.insert_text((72, y), f"{i + 1}. Lorem ipsum dolor sit amet, consectetur adipiscing elit.", fontsize=11)
        y += 20
    doc.save(DEMO / "Page 001.pdf")
    log("files:", [f.name for f in DEMO.iterdir()])


def set_wallpaper():
    W, H = 1920, 1080
    from dragshift.imaging import gradient
    wp = gradient((W, H), ["#ffb36b", "#ff5f6d", "#c2508f", "#7b8cff"], 160)
    p = DEMO.parent / "dragshift_wall.bmp"
    wp.save(p)
    ok = user32.SystemParametersInfoW(20, 0, str(p), 3)
    log("wallpaper", ok)


def start_ffmpeg() -> subprocess.Popen:
    import imageio_ffmpeg
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    out = OUT / "demo_raw.mp4"
    cmd = [exe, "-y", "-f", "gdigrab", "-framerate", "30", "-draw_mouse", "1", "-i", "desktop",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", str(out)]
    log("ffmpeg:", " ".join(cmd))
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=open(OUT / "ffmpeg.txt", "w"),
                            stderr=subprocess.STDOUT)


def stop_ffmpeg(p: subprocess.Popen):
    try:
        p.stdin.write(b"q")
        p.stdin.flush()
        p.wait(timeout=60)
    except Exception as e:
        log("ffmpeg stop error", e)
        p.kill()


# ------------------------------------------------------------------ UI helpers
def uia():
    from pywinauto import Desktop
    return Desktop(backend="uia")


def find_window(title_re: str, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        try:
            w = uia().window(title_re=title_re)
            if w.exists(timeout=0.5):
                return w
        except Exception:
            pass
        time.sleep(0.3)
    raise RuntimeError(f"window not found: {title_re}")


def explorer_item_center(win, name: str):
    stem = name.rsplit(".", 1)[0]
    item = win.child_window(title_re=f"^{stem}(\\..*)?$", control_type="ListItem", found_index=0)
    item.wait("exists", timeout=10)
    r = item.rectangle()
    # aim at the icon, a bit above the label
    return (r.left + r.right) // 2, r.top + int((r.bottom - r.top) * 0.4)


def petal_xy(center, n, i, r=R_PETAL):
    span = 360 / n
    a = math.radians(90 - i * span)
    return int(center[0] + r * math.cos(a)), int(center[1] - r * math.sin(a))


def glide(x, y, dur=0.6):
    pyautogui.moveTo(x, y, duration=dur, tween=pyautogui.easeInOutQuad)


def drag_with_shift(src, nudge=(160, 40)):
    """Press on a file, start dragging, press Shift: the menu opens at the cursor."""
    glide(*src, 0.7)
    time.sleep(0.3)
    pyautogui.mouseDown()
    time.sleep(0.15)
    # start the drag with a few small moves (Explorer needs to see movement)
    for k in range(1, 9):
        pyautogui.moveTo(src[0] + k * 4, src[1] + k * 2)
        time.sleep(0.03)
    glide(src[0] + nudge[0], src[1] + nudge[1], 0.7)
    time.sleep(0.25)
    pyautogui.keyDown("shift")
    time.sleep(0.35)
    center = pyautogui.position()
    # tiny wiggle so the OLE drag enters the menu window
    pyautogui.moveTo(center[0] + 3, center[1] + 3)
    time.sleep(0.1)
    pyautogui.moveTo(center[0], center[1])
    time.sleep(0.4)
    return center


def release_shift():
    pyautogui.keyUp("shift")


def tap_shift():
    pyautogui.keyDown("shift")
    time.sleep(0.12)
    pyautogui.keyUp("shift")


def drop():
    pyautogui.mouseUp()
    time.sleep(0.2)
    pyautogui.keyUp("shift")


def click_button(win, title: str, pause=0.6):
    b = win.child_window(title=title, found_index=0)
    try:
        b.wait("exists", timeout=6)
    except Exception:
        log("controls:", [(c.element_info.control_type, c.window_text()) for c in win.descendants()][:80])
        raise
    r = b.rectangle()
    glide((r.left + r.right) // 2, (r.top + r.bottom) // 2, 0.5)
    time.sleep(0.15)
    pyautogui.click()
    time.sleep(pause)


def drag_slider(win, index: int, frac: float):
    sliders = win.descendants(control_type="Slider")
    s = sliders[index]
    r = s.rectangle()
    y = (r.top + r.bottom) // 2
    # handle is at the current value; click-drag from current to target
    try:
        cur = float(s.iface_range_value.CurrentValue)
        lo = float(s.iface_range_value.Minimum)
        hi = float(s.iface_range_value.Maximum)
        f0 = (cur - lo) / (hi - lo) if hi > lo else 0
    except Exception:
        f0 = 0.5
    x0 = r.left + 7 + int((r.right - r.left - 14) * f0)
    x1 = r.left + 7 + int((r.right - r.left - 14) * frac)
    glide(x0, y, 0.4)
    pyautogui.mouseDown()
    glide(x1, y, 0.9)
    pyautogui.mouseUp()
    time.sleep(0.4)


# ------------------------------------------------------------------ the demo
def main() -> int:
    global T0
    sys.path.insert(0, str(ROOT))
    set_resolution(1920, 1080)
    time.sleep(1)
    sw, sh = pyautogui.size()
    log("screen", sw, sh)
    make_files()
    set_wallpaper()
    subprocess.run(["taskkill", "/F", "/IM", "ServerManager.exe"], capture_output=True)
    # show file extensions so the video shows .pdf -> .jpg
    subprocess.run(["reg", "add", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
                    "/v", "HideFileExt", "/t", "REG_DWORD", "/d", "0", "/f"], capture_output=True)
    user32.SendMessageTimeoutW(0xFFFF, 0x1A, 0, "Environment", 2, 1000, None)
    # clear the desktop (minimise the runner console etc.)
    pyautogui.hotkey("win", "d")
    time.sleep(1.5)

    # open Explorer on the demo folder, large icons, left side of the screen
    subprocess.Popen(["explorer", str(DEMO)])
    ex = find_window(".*DragShiftDemo.*", 20)
    ex.set_focus()
    h = ex.handle
    user32.ShowWindow(h, 1)
    user32.MoveWindow(h, int(sw * 0.04), int(sh * 0.08), int(sw * 0.42), int(sh * 0.7), True)
    time.sleep(0.8)
    pyautogui.hotkey("ctrl", "shift", "2")  # large icons
    time.sleep(1.0)

    # start DragShift from source (pythonw: no console window)
    pyw = Path(sys.executable).with_name("pythonw.exe")
    app = subprocess.Popen([str(pyw if pyw.exists() else sys.executable), "-m", "dragshift"], cwd=str(ROOT))
    time.sleep(4)

    rec = start_ffmpeg()
    time.sleep(2)
    T0 = time.time()
    try:
        step("DragShift runs in the tray")
        glide(sw * 0.5, sh * 0.5, 0.8)
        time.sleep(2.5)

        # 1) PDF -> JPG
        step("Drag a file… then press Shift")
        src = explorer_item_center(ex, "Page 001.pdf")
        log("pdf at", src)
        c = drag_with_shift(src, nudge=(int(sw * 0.36), 60))
        shot("01_menu_pdf")
        step("Drop it on a format")
        n = 4  # JPG, PNG, DOCX, TXT
        for i in (1, 2, 3, 0):  # sweep to show hover
            glide(*petal_xy(c, n, i), 0.45)
            time.sleep(0.35)
        shot("02_hover_jpg")
        drop()
        time.sleep(2.5)
        shot("03_after_pdf")
        step("Converted copy saved next to the original")
        time.sleep(2.5)

        # 2) JPG -> Crop
        step("Tap Shift again for Tools")
        src = explorer_item_center(ex, "Sunset.jpg")
        c = drag_with_shift(src, nudge=(int(sw * 0.36), 30))
        time.sleep(0.6)
        release_shift()
        time.sleep(0.5)
        tap_shift()
        time.sleep(0.9)
        shot("04_tools_ring")
        n = 7  # Compress, Metadata, Redact, Crop, Edit, Annotate, Add BG
        for i in (0, 1, 2, 3):
            glide(*petal_xy(c, n, i), 0.4)
            time.sleep(0.3)
        drop()
        step("Crop: pick a ratio, Apply")
        crop = find_window("Crop Image.*", 10)
        time.sleep(1.0)
        shot("05_crop")
        click_button(crop, "16:9", 1.0)
        click_button(crop, "1:1", 1.0)
        click_button(crop, "16:9", 1.0)
        click_button(crop, "Apply", 1.5)
        time.sleep(1.5)

        # 3) JPG -> Add BG
        step("Add BG: gradient, padding, corners, shadow")
        src = explorer_item_center(ex, "Sunset.jpg")
        c = drag_with_shift(src, nudge=(int(sw * 0.36), 30))
        time.sleep(0.5)
        release_shift()
        time.sleep(0.4)
        tap_shift()
        time.sleep(0.8)
        glide(*petal_xy(c, 7, 5), 0.4)
        glide(*petal_xy(c, 7, 6), 0.4)
        time.sleep(0.4)
        drop()
        bg = find_window("Add BG.*", 10)
        time.sleep(1.2)
        shot("06_addbg")
        try:
            drag_slider(bg, 0, 0.55)   # padding
            drag_slider(bg, 1, 0.45)   # corners
            drag_slider(bg, 3, 0.15)   # angle
        except Exception as e:
            log("slider error", e)
        def is_swatch(c):
            r = c.rectangle()
            return 18 <= r.width() <= 34 and 18 <= r.height() <= 34 and not c.window_text().strip()
        sw_buttons = [c for c in bg.descendants() if is_swatch(c)]
        sw_buttons.sort(key=lambda c: (c.rectangle().top, c.rectangle().left))
        log("swatches found", len(sw_buttons))
        for k in (3, 1):  # Aurora, then Prism (rainbow)
            if k < len(sw_buttons):
                r = sw_buttons[k].rectangle()
                glide((r.left + r.right) // 2, (r.top + r.bottom) // 2, 0.5)
                pyautogui.click()
                time.sleep(1.0)
        click_button(bg, "1:1", 1.2)
        shot("07_addbg_set")
        click_button(bg, "Save with Background", 2.0)

        # 4) finale: show the results in the folder
        step("All results land in the same folder")
        ex.set_focus()
        pyautogui.hotkey("ctrl", "shift", "2")  # large icons: everything fits
        time.sleep(1.5)
        glide(sw * 0.25, sh * 0.45, 1.0)
        time.sleep(3.5)
        shot("08_final")
        log("folder now:", sorted(f.name for f in DEMO.iterdir()))
        res = OUT / "results"
        res.mkdir(exist_ok=True)
        import shutil
        for f in DEMO.iterdir():
            shutil.copy(f, res / f.name)
    except Exception:
        log("DEMO ERROR\n" + traceback.format_exc())
        shot("zz_error")
        time.sleep(2)
    finally:
        pyautogui.mouseUp()
        pyautogui.keyUp("shift")
        stop_ffmpeg(rec)
        app.kill()
        (OUT / "steps.json").write_text(json.dumps(STEPS, indent=1))
        log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
