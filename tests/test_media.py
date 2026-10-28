"""Video / audio conversion with a real ffmpeg on small generated clips."""
import shutil
import sys

import pytest
from PIL import Image

from dragshift import app, convert, media, report
from dragshift.util import UserError, category

pytestmark = pytest.mark.skipif(not media.available(), reason="ffmpeg not available")


def make_clip(path, seconds=2, audio=True, size="320x240", rate=10, vcodec=("libx264", "-pix_fmt", "yuv420p"),
              extra=()):
    args = ["-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc2=duration={seconds}:size={size}:rate={rate}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    args += ["-c:v", *vcodec]
    if audio:
        args += ["-c:a", "aac" if str(path).endswith((".mp4", ".mov", ".m4a")) else "libvorbis"]
    r = media.run_ffmpeg([*args, *extra, str(path)])
    assert r.returncode == 0, r.stderr
    return path


def make_audio(path, seconds=2, title=None):
    args = ["-y", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    if title:
        args += ["-metadata", f"title={title}"]
    r = media.run_ffmpeg([*args, str(path)])
    assert r.returncode == 0, r.stderr
    return path


def text_of(path):
    return media.run_ffmpeg(["-i", str(path)]).stderr


def leftovers(folder):
    return [p.name for p in folder.iterdir() if p.name.startswith(".~")]


def test_file_types_and_targets():
    assert category("a.MOV") == "video" and category("a.mp3") == "audio" and category("a.gif") == "image"
    assert convert.targets_for("video", ".mov") == ["MP4", "WEBM", "GIF", "MP3", "WAV"]
    assert convert.targets_for("video", ".mp4") == ["WEBM", "GIF", "MP3", "WAV"]
    assert convert.targets_for("audio", ".mp3") == ["WAV", "FLAC", "M4A", "OGG"]


def test_probe_reads_duration_streams_and_size(tmp_path):
    info = media.probe(make_clip(tmp_path / "a.mp4"))
    assert abs(info.duration - 2) < 0.3 and info.has_video and info.has_audio and (info.width, info.height) == (320, 240)
    silent = media.probe(make_clip(tmp_path / "b.mp4", audio=False))
    assert silent.has_video and not silent.has_audio
    snd = media.probe(make_audio(tmp_path / "c.wav"))
    assert snd.has_audio and not snd.has_video


def test_garbage_is_a_clear_error(tmp_path):
    bad = tmp_path / "x.mp4"
    bad.write_text("this is not a video")
    with pytest.raises(UserError, match="doesn't look like"):
        media.probe(bad)
    with pytest.raises(UserError):
        convert.convert(bad, "video", "MP3")
    assert leftovers(tmp_path) == []


def test_mov_to_mp4_is_h264_aac_with_even_size_even_from_odd_sources(tmp_path):
    src = make_clip(tmp_path / "odd.mkv", size="321x241", vcodec=("ffv1",), audio=False)
    (out,) = convert.convert(src, "video", "MP4")
    assert out == tmp_path / "odd.mp4"
    t = text_of(out)
    assert "h264" in t and media.probe(out).width % 2 == 0 and media.probe(out).height % 2 == 0
    mov = make_clip(tmp_path / "clip.mov")
    (out2,) = convert.convert(mov, "video", "MP4")
    assert "aac" in text_of(out2) and abs(media.probe(out2).duration - 2) < 0.3
    assert mov.exists() and leftovers(tmp_path) == []


def test_webm_uses_vp9_and_opus(tmp_path):
    (out,) = convert.convert(make_clip(tmp_path / "a.mp4"), "video", "WEBM")
    t = text_of(out)
    assert out.suffix == ".webm" and "vp9" in t and "opus" in t


def test_gif_is_small_looping_and_capped_in_length(tmp_path):
    (g,) = convert.convert(make_clip(tmp_path / "a.mp4", seconds=3, size="640x360"), "video", "GIF")
    with Image.open(g) as im:
        assert im.format == "GIF" and im.width == 480 and im.info.get("loop") == 0 and im.n_frames >= 30
    long = make_clip(tmp_path / "long.mp4", seconds=26, size="160x120", rate=5, audio=False)
    (g2,) = convert.convert(long, "video", "GIF")
    with Image.open(g2) as im:
        assert im.n_frames <= media.GIF_SECONDS * media.GIF_FPS + 2       # first 20 s only
        assert im.width == 160                                           # never enlarged


def test_extract_audio_from_video(tmp_path):
    src = make_clip(tmp_path / "talk.mp4")
    for target, ext, codec in [("MP3", ".mp3", "mp3"), ("WAV", ".wav", "pcm_s16le")]:
        (out,) = convert.convert(src, "video", target)
        info = media.probe(out)
        assert out.suffix == ext and codec in text_of(out) and info.has_audio and not info.has_video
        assert abs(info.duration - 2) < 0.3


def test_video_without_sound_cannot_become_audio_and_audio_cannot_become_video(tmp_path):
    with pytest.raises(UserError, match="no sound"):
        convert.convert(make_clip(tmp_path / "mute.mp4", audio=False), "video", "MP3")
    with pytest.raises(UserError, match="no picture"):
        media.convert(make_audio(tmp_path / "s.wav"), "MP4")
    assert leftovers(tmp_path) == []


@pytest.mark.parametrize("target,codec", [("MP3", "mp3"), ("FLAC", "flac"), ("M4A", "aac"), ("OGG", "vorbis")])
def test_audio_conversions_keep_tags_and_length(tmp_path, target, codec):
    src = make_audio(tmp_path / "song.wav", title="My Song")
    if target != "MP3":
        src = convert.convert(src, "audio", "MP3")[0]               # a tagged mp3 to convert from
        src.rename(tmp_path / "src.mp3")
        src = tmp_path / "src.mp3"
    (out,) = convert.convert(src, "audio", target)
    t = text_of(out)
    assert codec in t and abs(media.probe(out).duration - 2) < 0.3


def test_tags_survive_conversion(tmp_path):
    src = tmp_path / "song.mp3"
    media.run_ffmpeg(["-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=1", "-metadata", "title=My Song",
                      "-metadata", "artist=Me", str(src)])
    (out,) = convert.convert(src, "audio", "M4A")
    assert "My Song" in text_of(out) and "artist" in text_of(out)


def test_compress_makes_a_smaller_mp4_capped_at_1080p(tmp_path):
    src = make_clip(tmp_path / "big.mp4", size="2560x1440", rate=10, extra=("-b:v", "8M", "-maxrate", "8M", "-bufsize", "8M"))
    out = media.compress_video(src)
    assert out == tmp_path / "big Compressed.mp4" and out.stat().st_size < src.stat().st_size * 0.7
    info = media.probe(out)
    assert info.width == 1920 and info.height == 1080 and info.has_audio
    small = make_clip(tmp_path / "small.mp4", size="320x240")
    assert media.probe(media.compress_video(small)).width == 320         # never upscaled


def test_mute_copies_the_picture_and_drops_the_sound(tmp_path):
    src = make_clip(tmp_path / "party.mov")
    out = media.mute_video(src)
    assert out == tmp_path / "party Muted.mov"
    info = media.probe(out)
    assert info.has_video and not info.has_audio and "h264" in text_of(out)
    assert media.probe(src).has_audio                                      # original untouched
    with pytest.raises(UserError, match="no sound to remove"):
        media.mute_video(out)


def test_never_overwrites_and_leaves_no_temp_files(tmp_path):
    src = make_clip(tmp_path / "clip.mov")
    (tmp_path / "clip.mp4").write_text("precious")
    (out,) = convert.convert(src, "video", "MP4")
    assert out.name == "clip (1).mp4" and (tmp_path / "clip.mp4").read_text() == "precious"
    assert leftovers(tmp_path) == []


def test_ffmpeg_falls_back_to_the_system_one(monkeypatch):
    monkeypatch.setattr(media, "_looked", False)
    monkeypatch.setattr(media, "_exe", None)
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", None)           # makes `import imageio_ffmpeg` fail
    monkeypatch.setattr(shutil, "which", lambda name: r"C:\fake\ffmpeg.exe")
    assert media.ffmpeg_exe() == r"C:\fake\ffmpeg.exe"


def test_menu_for_video_and_audio(tmp_path):
    v = make_clip(tmp_path / "a.mov")
    r = app.DragShiftApp.build_rings(None, [str(v)])
    assert list(r) == ["Convert", "Tools", "Files"]
    assert [i.label for i in r["Convert"]] == ["MP4", "WEBM", "GIF", "MP3", "WAV"]
    assert [i.label for i in r["Tools"]] == ["Compress", "Mute"]
    assert [i.label for i in r["Files"]] == ["Rename", "Zip"]
    assert {i.label: i.hint for i in r["Convert"]}["MP3"] == "Extract the sound"
    a = make_audio(tmp_path / "a.wav")
    ra = app.DragShiftApp.build_rings(None, [str(a)])
    assert list(ra) == ["Convert", "Files"] and {i.label: i.hint for i in ra["Convert"]}["MP3"] == "Small, universal"


def test_summaries(tmp_path):
    v = make_clip(tmp_path / "a.mov")
    (mp4,) = convert.convert(v, "video", "MP4")
    r = report.summarize("convert:MP4", [str(v)], [mp4])
    assert r.title == "MOV → MP4" and "0:02" in r.detail and mp4.name in r.detail
    m = media.mute_video(mp4)
    assert report.summarize("tool:Mute", [str(mp4)], [m]).title == "Sound removed"
    c = media.compress_video(mp4)
    assert report.summarize("tool:Compress", [str(mp4)], [c]).title == "Compressed"
