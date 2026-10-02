"""Group slides into layouts by shape signature."""
from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from lxml import etree

from .classify import CHROME, MEDIA, PLACEHOLDER
from .ooxml import R_ATTR_PREFIX, Package, local, qn
from .parser import Deck, Shape, Slide, ph_of, top_shapes

DEFAULT_TOLERANCE = 50000


@dataclass
class SigItem:
    kind: str
    verdict: str
    bbox: tuple
    look: str
    pos: int


@dataclass
class Cluster:
    number: int
    slides: List[Slide]
    rep: Slide
    name: str = ""
    name_source: str = ""
    # slide index -> {slide shape pos -> rep shape pos}
    mapping: Dict[int, Dict[int, int]] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return "cluster_%d" % self.number


def canon_hash(pkg: Package, part: str, *elements) -> str:
    """Fingerprint of elements' appearance, ignoring position, ids, names and rIds."""
    h = hashlib.sha1()
    rels = pkg.rels(part)
    for src in elements:
        if src is None:
            h.update(b"-")
            continue
        el = copy.deepcopy(src)
        for x in list(el.iter(qn("a:xfrm"), qn("p:xfrm"), qn("a:extLst"), qn("p:extLst"))):
            x.getparent().remove(x)
        for node in el.iter():
            if local(node.tag) == "cNvPr":
                for attr in ("id", "name", "descr", "title"):
                    node.attrib.pop(attr, None)
            for attr, val in list(node.attrib.items()):
                if attr.startswith(R_ATTR_PREFIX) and val:
                    info = rels.get(val)
                    if info and info[2] != "External" and pkg.has(info[1]):
                        node.set(attr, hashlib.sha1(pkg.blob(info[1])).hexdigest()[:12])
                    elif info:
                        node.set(attr, info[1])
        h.update(etree.tostring(el, method="c14n"))
    return h.hexdigest()[:12]


def base_look(pkg: Package, slide: Slide) -> str:
    """What the slide inherits visually: master, layout decoration and background."""
    if not slide.layout_part:
        return "none"
    layout = pkg.xml(slide.layout_part)
    deco = [el for el in top_shapes(layout) if ph_of(el) is None]
    bg = layout.find(qn("p:cSld")).find(qn("p:bg"))
    key = "%s|%s|%s" % (slide.master_part, layout.get("showMasterSp", "1"),
                        canon_hash(pkg, slide.layout_part, bg, *deco))
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def signature(pkg: Package, slide: Slide) -> List[SigItem]:
    out = [SigItem("base", "", (0, 0, 0, 0), base_look(pkg, slide), -1)]
    for sh in slide.shapes:
        look = canon_hash(pkg, slide.part, sh.el) if sh.verdict == CHROME else ""
        out.append(SigItem(sh.kind, sh.verdict, sh.bbox or (0, 0, 0, 0), look, sh.pos))
    return out


def _diff(a: SigItem, b: SigItem) -> int:
    return max(abs(x - y) for x, y in zip(a.bbox, b.bbox))


def match(a: List[SigItem], b: List[SigItem], tol: Optional[int]) -> Optional[Dict[int, int]]:
    """Pair every item of `a` with a compatible item of `b`. tol=None means any position."""
    if len(a) != len(b):
        return None
    used = set()
    out = {}
    for ia in sorted(a, key=lambda s: (s.bbox[1], s.bbox[0])):
        best, best_d = None, None
        for ib in b:
            if ib.pos in used or ib.kind != ia.kind or ib.verdict != ia.verdict or ib.look != ia.look:
                continue
            d = _diff(ia, ib)
            if tol is not None and d > tol:
                continue
            if best is None or d < best_d:
                best, best_d = ib, d
        if best is None:
            return None
        used.add(best.pos)
        out[ia.pos] = best.pos
    return out


def cluster_slides(deck: Deck, tolerance: int = DEFAULT_TOLERANCE) -> List[Cluster]:
    sigs = [signature(deck.pkg, s) for s in deck.slides]
    n = len(deck.slides)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            if find(i) != find(j) and match(sigs[i], sigs[j], tolerance) is not None:
                parent[find(j)] = find(i)

    groups: Dict[int, List[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    clusters = []
    for number, members in enumerate(sorted(groups.values(), key=lambda m: m[0]), start=1):
        slides = [deck.slides[i] for i in members]
        rep_i = max(members, key=lambda i: (len(deck.slides[i].shapes), -i))
        cl = Cluster(number=number, slides=slides, rep=deck.slides[rep_i])
        for i in members:
            m = match(sigs[i], sigs[rep_i], tolerance)
            if m is None:  # joined transitively; pair by nearest compatible shape
                m = match(sigs[i], sigs[rep_i], None)
            m.pop(-1, None)
            cl.mapping[deck.slides[i].index] = m
        clusters.append(cl)
    return clusters


def _fallback_name(cl: Cluster) -> str:
    shapes = cl.rep.shapes
    parts = []
    if any(s.verdict == PLACEHOLDER and s.role in ("title", "ctrTitle") for s in shapes):
        parts.append("Title")
    if any(s.verdict == MEDIA and (s.kind == "pic" or s.fill == "blip") for s in shapes) or \
            any(s.verdict == PLACEHOLDER and s.role == "pic" for s in shapes):
        parts.append("Image")
    for kind, label in (("table", "Table"), ("chart", "Chart"), ("diagram", "Diagram")):
        if any(s.kind == "graphicFrame" and s.frame_kind == kind for s in shapes):
            parts.append(label)
    if any(s.verdict == PLACEHOLDER and s.role not in ("title", "ctrTitle", "pic", "dt", "ftr", "sldNum")
           for s in shapes):
        parts.append("Text")
    return "Layout-%s-%02d" % ("".join(parts) or "Blank", cl.number)


def name_clusters(clusters: List[Cluster], overrides_layouts: Optional[Dict[str, str]] = None) -> None:
    overrides_layouts = overrides_layouts or {}
    seen: Dict[str, int] = {}
    for cl in clusters:
        if cl.id in overrides_layouts:
            cl.name, cl.name_source = overrides_layouts[cl.id], "override"
        else:
            custom = [s.custom_name for s in cl.slides if s.custom_name]
            if custom:
                cl.name, cl.name_source = custom[0], "slide name"
            else:
                cl.name, cl.name_source = _fallback_name(cl), "shape mix"
        base = cl.name
        if base in seen:
            seen[base] += 1
            cl.name = "%s %d" % (base, seen[base])
        else:
            seen[base] = 1
