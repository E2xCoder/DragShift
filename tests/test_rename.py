"""Batch rename: patterns, safety, swaps, undo."""
import re
import time

import pytest

from dragshift import rename
from dragshift.util import UserError


def make(tmp_path, *names):
    out = []
    for n in names:
        p = tmp_path / n
        p.write_text(n)
        out.append(p)
    return out


def test_numbering_follows_natural_order_and_keeps_extension(tmp_path):
    ps = make(tmp_path, "img10.png", "img2.png", "img1.jpg")
    pairs = rename.plan(ps, "Trip {n}", start=1, digits=2)
    assert [(s.name, d.name) for s, d in pairs] == [
        ("img1.jpg", "Trip 01.jpg"), ("img2.png", "Trip 02.png"), ("img10.png", "Trip 03.png")]


def test_start_number_and_name_token(tmp_path):
    ps = make(tmp_path, "a.png", "b.png")
    pairs = rename.plan(ps, "{name}_{n}", start=7, digits=1)
    assert [d.name for _, d in pairs] == ["a_7.png", "b_8.png"]


def test_date_tokens(tmp_path):
    (p,) = make(tmp_path, "a.png")
    (_, d), = rename.plan([p], "{date}-{mdate}-{parent}")
    today = time.strftime("%Y-%m-%d")
    assert d.name == f"{today}-{today}-{tmp_path.name}.png"


@pytest.mark.parametrize("bad", ["", "   ", "{nope}", "a{", "{n", "x}y"])
def test_bad_patterns_are_rejected(tmp_path, bad):
    (p,) = make(tmp_path, "a.png")
    with pytest.raises(UserError):
        rename.plan([p], bad)


def test_illegal_characters_and_reserved_names_are_cleaned(tmp_path):
    (p,) = make(tmp_path, "a.png")
    assert rename.plan([p], 'a<b>:c?')[0][1].name == "a_b__c_.png"
    assert rename.plan([p], "CON")[0][1].name == "_CON.png"
    assert rename.plan([p], "dots...")[0][1].name == "dots.png"


def test_same_name_for_two_files_is_rejected(tmp_path):
    ps = make(tmp_path, "a.png", "b.png")
    with pytest.raises(UserError, match="Two files"):
        rename.plan(ps, "same")


def test_never_overwrites_an_existing_file(tmp_path):
    ps = make(tmp_path, "a.png", "taken.png")
    with pytest.raises(UserError, match="already exists"):
        rename.plan([ps[0]], "taken")
    assert (tmp_path / "taken.png").read_text() == "taken.png"


def test_apply_and_undo_round_trip(tmp_path):
    ps = make(tmp_path, "a.png", "b.png")
    pairs = rename.plan(ps, "x{n}")
    news = rename.apply(pairs)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["x1.png", "x2.png"]
    assert (tmp_path / "x1.png").read_text() == "a.png"
    assert rename.undo([s for s, _ in pairs], news) == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.png", "b.png"]
    assert (tmp_path / "a.png").read_text() == "a.png"


def test_swap_and_chain_work_via_temp_names(tmp_path):
    ps = make(tmp_path, "1.png", "2.png", "3.png")
    # shift every number up by one: 1->2, 2->3, 3->4 (2 and 3 are both sources AND targets)
    pairs = rename.plan(ps, "{n}", start=2)
    rename.apply(pairs)
    assert {p.name: p.read_text() for p in tmp_path.iterdir()} == {
        "2.png": "1.png", "3.png": "2.png", "4.png": "3.png"}
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".dragshift")]


def test_case_only_rename(tmp_path):
    (p,) = make(tmp_path, "photo.png")
    pairs = rename.plan([p], "PHOTO")
    rename.apply(pairs)
    assert [q.name for q in tmp_path.iterdir()] == ["PHOTO.png"]


def test_failed_rename_rolls_back(tmp_path, monkeypatch):
    ps = make(tmp_path, "a.png", "b.png")
    pairs = rename.plan(ps, "x{n}")
    real = rename.os.rename
    calls = {"n": 0}

    def flaky(a, b):
        calls["n"] += 1
        if calls["n"] == 4:           # second file's final step
            raise OSError("locked")
        return real(a, b)

    monkeypatch.setattr(rename.os, "rename", flaky)
    with pytest.raises(UserError, match="Nothing was changed"):
        rename.apply(pairs)
    monkeypatch.undo()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.png", "b.png"]


def test_undo_refuses_if_old_name_taken(tmp_path):
    ps = make(tmp_path, "a.png")
    pairs = rename.plan(ps, "new")
    news = rename.apply(pairs)
    (tmp_path / "a.png").write_text("someone else")
    with pytest.raises(UserError, match="exists again"):
        rename.undo([s for s, _ in pairs], news)
    assert (tmp_path / "a.png").read_text() == "someone else"
    assert (tmp_path / "new.png").exists()
