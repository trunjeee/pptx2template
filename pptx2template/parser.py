"""Unzip a deck and extract every slide into a flat list of top-level shapes."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from lxml import etree

from .ooxml import NS, RT_SLIDE, RT_SLIDE_LAYOUT, RT_SLIDE_MASTER, Package, local, qn

BBox = Tuple[int, int, int, int]  # x, y, cx, cy in EMU

TITLE_TYPES = {"title", "ctrTitle"}
SPECIAL_TYPES = {"dt", "ftr", "sldNum", "hdr"}
FILL_TAGS = {"noFill": "none", "solidFill": "solid", "gradFill": "grad", "blipFill": "blip",
             "pattFill": "patt", "grpFill": "grp"}
SHAPE_TAGS = {"sp", "pic", "grpSp", "graphicFrame", "cxnSp", "contentPart"}
_DEFAULT_NAME = re.compile(r"^\s*(slide|слайд|folie|diapositive|diapositiva|dia)?\s*\d*\s*$", re.I)


@dataclass
class Shape:
    el: etree._Element
    pos: int                      # z-order position among top-level shapes
    kind: str                     # sp, pic, grpSp, graphicFrame, cxnSp, contentPart, alt
    id: int
    name: str
    bbox: Optional[BBox]
    paragraphs: List[str]
    has_text: bool                # contains at least one letter/digit
    fill: str                     # none, solid, grad, blip, patt, grp, style, unspecified
    ph_type: Optional[str]        # original placeholder type (None also for untyped "obj")
    ph_idx: Optional[int]
    is_ph: bool
    max_font: Optional[int]       # hundredths of a point
    animated: bool
    frame_kind: str = ""          # for graphicFrame: table / chart / diagram / object
    av: bool = False              # picture that is really a video/audio frame
    # filled in by classify
    verdict: str = ""             # placeholder | media | chrome
    reason: str = ""
    role: Optional[str] = None    # placeholder type in the generated layout
    idx: Optional[int] = None     # placeholder idx in reading order
    forced: bool = False
    ov_type: Optional[str] = None  # placeholder type forced by an override

    @property
    def is_picture(self) -> bool:
        """A photo: a real picture or a shape filled with a picture."""
        return self.kind == "pic" or (self.kind == "sp" and self.fill == "blip")

    @property
    def text(self) -> str:
        return "\n".join(self.paragraphs)

    def label(self) -> str:
        return self.name or "#%d" % self.id


@dataclass
class Slide:
    index: int                    # 1-based position in the deck
    part: str
    root: etree._Element
    name: str
    custom_name: Optional[str]
    layout_part: Optional[str]
    master_part: Optional[str]
    shapes: List[Shape] = field(default_factory=list)


@dataclass
class Deck:
    pkg: Package
    pres_part: str
    slides: List[Slide]
    masters: List[str]
    slide_size: Tuple[int, int]


# --------------------------------------------------------------------------- helpers
def shape_kind(el) -> str:
    tag = local(el.tag)
    if tag == "AlternateContent":
        return "alt"
    return tag


def inner_shape(el):
    """For mc:AlternateContent return the first wrapped shape, else the element itself."""
    if local(el.tag) != "AlternateContent":
        return el
    for branch in el:
        for child in branch:
            if local(child.tag) in SHAPE_TAGS:
                return child
    return el


def c_nv_pr(el):
    el = inner_shape(el)
    for nv in el:
        if local(nv.tag).startswith("nv"):
            return nv.find(qn("p:cNvPr"))
    return None


def nv_pr(el):
    el = inner_shape(el)
    for nv in el:
        if local(nv.tag).startswith("nv"):
            return nv.find(qn("p:nvPr"))
    return None


def ph_of(el):
    nv = nv_pr(el)
    return None if nv is None else nv.find(qn("p:ph"))


def xfrm_of(el):
    el = inner_shape(el)
    tag = local(el.tag)
    if tag == "graphicFrame":
        return el.find(qn("p:xfrm"))
    pr = el.find(qn("p:grpSpPr")) if tag == "grpSp" else el.find(qn("p:spPr"))
    return None if pr is None else pr.find(qn("a:xfrm"))


def bbox_of_xfrm(xfrm) -> Optional[BBox]:
    if xfrm is None:
        return None
    off, ext = xfrm.find(qn("a:off")), xfrm.find(qn("a:ext"))
    if off is None or ext is None:
        return None
    return (int(off.get("x", 0)), int(off.get("y", 0)), int(ext.get("cx", 0)), int(ext.get("cy", 0)))


def paragraphs_of(el) -> List[str]:
    out = []
    for p in el.iter(qn("a:p")):
        out.append("".join(t.text or "" for t in p.iter(qn("a:t"))))
    return out


def has_real_text(paragraphs: List[str]) -> bool:
    # A lone "·" or "—" is a decorative glyph, not editable content.
    return any(ch.isalnum() for para in paragraphs for ch in para)


def fill_of(el) -> str:
    el = inner_shape(el)
    sp_pr = el.find(qn("p:spPr"))
    if sp_pr is not None:
        for child in sp_pr:
            kind = FILL_TAGS.get(local(child.tag))
            if kind:
                return kind
    style = el.find(qn("p:style"))
    if style is not None:
        ref = style.find(qn("a:fillRef"))
        if ref is not None and ref.get("idx", "0") != "0":
            return "style"
    return "unspecified"


def frame_kind(el) -> str:
    gd = inner_shape(el).find(".//" + qn("a:graphicData"))
    uri = (gd.get("uri") if gd is not None else "") or ""
    for key in ("table", "chart", "diagram"):
        if key in uri:
            return key
    return "object"


_AV_TAGS = {"videoFile", "audioFile", "quickTimeFile", "wavAudioFile", "audioCd", "media"}


def _is_av(el) -> bool:
    nv = nv_pr(el)
    return nv is not None and any(local(x.tag) in _AV_TAGS for x in nv.iter())


def is_custom_name(name: str) -> bool:
    return bool(name) and not _DEFAULT_NAME.match(name)


def sp_tree(root):
    return root.find(qn("p:cSld")).find(qn("p:spTree"))


def top_shapes(root) -> List[etree._Element]:
    return [el for el in sp_tree(root) if local(el.tag) in SHAPE_TAGS or local(el.tag) == "AlternateContent"]


# --------------------------------------------------------------------------- inheritance
def _norm_master_type(t: Optional[str]) -> str:
    if t in TITLE_TYPES:
        return "title"
    if t in SPECIAL_TYPES:
        return t
    return "body"


def find_ph(root, ph_type: Optional[str], ph_idx: Optional[int], master: bool = False):
    """Find the placeholder in a layout (or master) that a slide placeholder inherits from."""
    if root is None:
        return None
    candidates = [(el, ph_of(el)) for el in top_shapes(root)]
    candidates = [(el, ph) for el, ph in candidates if ph is not None]
    if master:
        want = _norm_master_type(ph_type)
        for el, ph in candidates:
            if _norm_master_type(ph.get("type")) == want:
                return el
        return None
    if ph_type in TITLE_TYPES:
        for el, ph in candidates:
            if ph.get("type") in TITLE_TYPES:
                return el
        return None
    if ph_idx is not None:
        for el, ph in candidates:
            if ph.get("idx") is not None and int(ph.get("idx")) == ph_idx:
                return el
    for el, ph in candidates:
        if ph.get("type") == ph_type:
            return el
    return None


class Inheritance:
    """Resolves what a slide placeholder inherits from its layout and master."""

    def __init__(self, pkg: Package):
        self.pkg = pkg

    def root(self, part: Optional[str]):
        return self.pkg.xml(part) if part and self.pkg.has(part) else None

    def chain(self, slide: Slide, ph_type, ph_idx):
        """(layout ph element or None, master ph element or None)"""
        layout_ph = find_ph(self.root(slide.layout_part), ph_type, ph_idx)
        master_ph = find_ph(self.root(slide.master_part), ph_type, ph_idx, master=True)
        return layout_ph, master_ph

    def xfrm(self, slide: Slide, ph_type, ph_idx):
        for el in self.chain(slide, ph_type, ph_idx):
            if el is not None and xfrm_of(el) is not None and bbox_of_xfrm(xfrm_of(el)):
                return xfrm_of(el)
        return None

    def font_size(self, slide: Slide, ph_type, ph_idx) -> Optional[int]:
        for el in self.chain(slide, ph_type, ph_idx):
            if el is None:
                continue
            d = el.find(".//" + qn("a:lstStyle") + "/" + qn("a:lvl1pPr") + "/" + qn("a:defRPr"))
            if d is not None and d.get("sz"):
                return int(d.get("sz"))
        master = self.root(slide.master_part)
        if master is not None:
            style = "titleStyle" if ph_type in TITLE_TYPES else "bodyStyle"
            d = master.find(".//" + qn("p:" + style) + "/" + qn("a:lvl1pPr") + "/" + qn("a:defRPr"))
            if d is not None and d.get("sz"):
                return int(d.get("sz"))
        return None


def default_text_size(pkg: Package, pres_part: str) -> int:
    d = pkg.xml(pres_part).find(qn("p:defaultTextStyle") + "/" + qn("a:lvl1pPr") + "/" + qn("a:defRPr"))
    if d is not None and d.get("sz"):
        return int(d.get("sz"))
    return 1800


# --------------------------------------------------------------------------- main entry
def animated_ids(root) -> Set[int]:
    timing = root.find(qn("p:timing"))
    if timing is None:
        return set()
    out = set()
    for el in timing.iter():
        spid = el.get("spid")
        if spid and spid.isdigit():
            out.add(int(spid))
    return out


def parse_deck(pkg: Package) -> Deck:
    pres_part = pkg.main_part()
    pres = pkg.xml(pres_part)
    prels = pkg.rels(pres_part)

    masters = []
    lst = pres.find(qn("p:sldMasterIdLst"))
    for el in (lst if lst is not None else []):
        info = prels.get(el.get(qn("r:id")))
        if info:
            masters.append(info[1])

    sz = pres.find(qn("p:sldSz"))
    slide_size = (int(sz.get("cx")), int(sz.get("cy"))) if sz is not None else (12192000, 6858000)

    inh = Inheritance(pkg)
    base_size = default_text_size(pkg, pres_part)
    slides = []
    lst = pres.find(qn("p:sldIdLst"))
    for n, el in enumerate(lst if lst is not None else [], start=1):
        info = prels.get(el.get(qn("r:id")))
        if not info or info[0] != RT_SLIDE:
            continue
        part = info[1]
        root = pkg.xml(part)
        layouts = pkg.rels(part).by_type(RT_SLIDE_LAYOUT)
        layout_part = layouts[0][1] if layouts else None
        master_part = None
        if layout_part:
            ms = pkg.rels(layout_part).by_type(RT_SLIDE_MASTER)
            master_part = ms[0][1] if ms else None
        name = root.find(qn("p:cSld")).get("name", "")
        slide = Slide(index=len(slides) + 1, part=part, root=root, name=name,
                      custom_name=name if is_custom_name(name) else None,
                      layout_part=layout_part, master_part=master_part)
        anim = animated_ids(root)
        for pos, sel in enumerate(top_shapes(root)):
            slide.shapes.append(_make_shape(sel, pos, slide, inh, base_size, anim))
        slides.append(slide)
    return Deck(pkg=pkg, pres_part=pres_part, slides=slides, masters=masters, slide_size=slide_size)


def _make_shape(el, pos, slide, inh: Inheritance, base_size: int, anim: Set[int]) -> Shape:
    kind = shape_kind(el)
    cnv = c_nv_pr(el)
    sid = int(cnv.get("id", 0)) if cnv is not None else 0
    name = cnv.get("name", "") if cnv is not None else ""
    ph = ph_of(el)
    ph_type = ph.get("type") if ph is not None else None
    ph_idx = int(ph.get("idx")) if ph is not None and ph.get("idx") is not None else None

    bbox = bbox_of_xfrm(xfrm_of(el))
    if bbox is None and ph is not None:
        bbox = bbox_of_xfrm(inh.xfrm(slide, ph_type, ph_idx))

    paras = paragraphs_of(inner_shape(el))
    sizes = []
    missing = False
    for r in inner_shape(el).iter(qn("a:r"), qn("a:fld")):
        t = r.find(qn("a:t"))
        if t is None or not (t.text or "").strip():
            continue
        rpr = r.find(qn("a:rPr"))
        if rpr is not None and rpr.get("sz"):
            sizes.append(int(rpr.get("sz")))
        else:
            missing = True
    if missing:
        inherited = inh.font_size(slide, ph_type, ph_idx) if ph is not None else base_size
        if inherited:
            sizes.append(inherited)

    return Shape(
        el=el, pos=pos, kind=kind, id=sid, name=name, bbox=bbox, paragraphs=paras,
        has_text=has_real_text(paras), fill=fill_of(el), ph_type=ph_type, ph_idx=ph_idx,
        is_ph=ph is not None, max_font=max(sizes) if sizes else None, animated=sid in anim,
        frame_kind=frame_kind(el) if kind == "graphicFrame" else "",
        av=kind == "pic" and _is_av(el),
    )
