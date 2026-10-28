"""Zip tool."""
import zipfile

import pytest

from dragshift import archive, report
from dragshift.util import CONFIG, UserError


def make(folder, *names, body=None):
    folder.mkdir(exist_ok=True)
    out = []
    for n in names:
        p = folder / n
        p.write_bytes((body or n).encode() * 50)
        out.append(p)
    return out


def test_single_file_zip_is_named_after_the_file(tmp_path):
    (p,) = make(tmp_path, "photo.jpg")
    out = archive.zip_files([p])
    assert out == tmp_path / "photo.zip"
    with zipfile.ZipFile(out) as z:
        assert z.namelist() == ["photo.jpg"] and z.read("photo.jpg") == p.read_bytes()
        assert z.testzip() is None


def test_several_files_make_one_zip_named_after_the_folder(tmp_path):
    ps = make(tmp_path / "Trip", "a.png", "b.png", "c.pdf")
    out = archive.zip_files(ps)
    assert out.name == "Trip (3 files).zip" and out.parent == tmp_path / "Trip"
    with zipfile.ZipFile(out) as z:
        assert sorted(z.namelist()) == ["a.png", "b.png", "c.pdf"]


def test_same_name_from_two_folders_does_not_collide(tmp_path):
    a = make(tmp_path / "x", "img.png", body="one")[0]
    b = make(tmp_path / "y", "img.png", body="two")[0]
    with zipfile.ZipFile(archive.zip_files([a, b])) as z:
        assert sorted(z.namelist()) == ["img (1).png", "img.png"]
        assert z.read("img.png") == a.read_bytes() and z.read("img (1).png") == b.read_bytes()


def test_never_overwrites_an_existing_zip(tmp_path):
    (p,) = make(tmp_path, "a.png")
    (tmp_path / "a.zip").write_text("precious")
    out = archive.zip_files([p])
    assert out.name == "a (1).zip" and (tmp_path / "a.zip").read_text() == "precious"


def test_compressed_formats_are_stored_and_text_is_deflated(tmp_path):
    ps = make(tmp_path, "a.jpg", "b.pdf")
    with zipfile.ZipFile(archive.zip_files(ps)) as z:
        kinds = {i.filename: i.compress_type for i in z.infolist()}
    assert kinds == {"a.jpg": zipfile.ZIP_STORED, "b.pdf": zipfile.ZIP_DEFLATED}


def test_missing_files_are_skipped_and_all_missing_is_an_error(tmp_path):
    (p,) = make(tmp_path, "a.png")
    with zipfile.ZipFile(archive.zip_files([p, tmp_path / "gone.png"])) as z:
        assert z.namelist() == ["a.png"]
    with pytest.raises(UserError):
        archive.zip_files([tmp_path / "gone.png"])


def test_output_folder_setting_is_respected(tmp_path):
    (p,) = make(tmp_path, "a.png")
    CONFIG["output"] = "folder"
    try:
        out = archive.zip_files([p])
    finally:
        CONFIG["output"] = "next"
    assert out == tmp_path / "DragShift" / "a.zip"


def test_no_temp_file_is_left_behind_and_summary_reads_well(tmp_path):
    ps = make(tmp_path / "Trip", "a.png", "b.png")
    out = archive.zip_files(ps)
    assert [f.name for f in (tmp_path / "Trip").iterdir() if f.name.startswith(".")] == []
    r = report.summarize("tool:Zip", [str(p) for p in ps], [out])
    assert r.title == "Zipped 2 files" and out.name in r.detail
