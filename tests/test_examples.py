"""The real-world example deck in examples/: a design exported as pictures (cards, gradients
and icons are PNGs), which is exactly where the flat-picture rule matters."""
from pathlib import Path

import pytest

from pptx2template import analyze, convert
from pptx2template.classify import CHROME, PLACEHOLDER

DECK = Path(__file__).parent.parent / "examples" / "trj.pptx"
pytestmark = pytest.mark.skipif(not DECK.exists(), reason="example deck not present")


def test_flat_pictures_are_decoration_photos_are_placeholders():
    a = analyze(DECK)
    pics = [(s.index, sh) for s in a.deck.slides for sh in s.shapes if sh.is_picture]
    photos = [(i, sh.name) for i, sh in pics if sh.verdict == PLACEHOLDER]
    assert photos == [(2, "Image 1"), (4, "Image 0")]          # the two real photos
    assert all(sh.verdict == CHROME for i, sh in pics if (i, sh.name) not in photos)


def test_example_converts_cleanly():
    r = convert(DECK)
    assert r.problems == []
    assert len(r.layouts) == 5
