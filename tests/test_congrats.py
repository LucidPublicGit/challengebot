"""Congrats image picker — uniqueness + content/tier preference."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from app import congrats as cg
from app.db import init_db


def _touch(p: Path, size: int = 100) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)


def test_slugify():
    assert "steins" in cg.slugify("Steins;Gate")
    assert cg.slugify("One Piece") == "one-piece"


def test_pick_prefers_content_then_tier():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        root = td / "pics"
        _touch(root / "general.png")
        _touch(root / "easy" / "tier.png")
        _touch(root / "content" / "one-piece" / "luffy.png")
        old = os.environ.get("CONGRATS_DIR")
        os.environ["CONGRATS_DIR"] = str(root)
        try:
            init_db(str(td / "t.db"))
            p = cg.pick_congrats(tier="easy", content="One Piece", mark=True)
            assert p is not None and p.name == "luffy.png"
            p2 = cg.pick_congrats(tier="easy", content="", mark=True)
            assert p2 is not None and p2.name == "tier.png"
        finally:
            if old is None:
                os.environ.pop("CONGRATS_DIR", None)
            else:
                os.environ["CONGRATS_DIR"] = old


def test_prefers_unused():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        root = td / "pics"
        _touch(root / "a.png")
        _touch(root / "b.png")
        old = os.environ.get("CONGRATS_DIR")
        os.environ["CONGRATS_DIR"] = str(root)
        try:
            init_db(str(td / "t.db"))
            p1 = cg.pick_congrats(mark=True)
            p2 = cg.pick_congrats(mark=True)
            assert p1 is not None and p2 is not None
            assert p1.name != p2.name
        finally:
            if old is None:
                os.environ.pop("CONGRATS_DIR", None)
            else:
                os.environ["CONGRATS_DIR"] = old


def test_skips_huge_files():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        root = td / "pics"
        _touch(root / "big.gif", size=9_000_000)
        _touch(root / "ok.png", size=500)
        old = os.environ.get("CONGRATS_DIR")
        os.environ["CONGRATS_DIR"] = str(root)
        try:
            init_db(str(td / "t.db"))
            p = cg.pick_congrats(mark=True)
            assert p is not None and p.name == "ok.png"
        finally:
            if old is None:
                os.environ.pop("CONGRATS_DIR", None)
            else:
                os.environ["CONGRATS_DIR"] = old


def test_filename_content_match():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        root = td / "pics"
        _touch(root / "MHA-AllMight.png")
        _touch(root / "other.png")
        old = os.environ.get("CONGRATS_DIR")
        os.environ["CONGRATS_DIR"] = str(root)
        try:
            init_db(str(td / "t.db"))
            p = cg.pick_congrats(content="My Hero Academia", mark=True)
            assert p is not None and "MHA" in p.name
        finally:
            if old is None:
                os.environ.pop("CONGRATS_DIR", None)
            else:
                os.environ["CONGRATS_DIR"] = old


if __name__ == "__main__":
    test_slugify()
    test_pick_prefers_content_then_tier()
    test_prefers_unused()
    test_skips_huge_files()
    test_filename_content_match()
    print("ok")
