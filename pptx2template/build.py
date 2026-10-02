"""Generate slide layouts, rewire the master and rewrite slides to use them."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from lxml import etree

from .classify import CHROME, MEDIA, PLACEHOLDER
from .cluster import Cluster
from .ooxml import (CT_SLIDE_LAYOUT, NS, R_ATTR_PREFIX, RT_SLIDE, RT_SLIDE_LAYOUT, RT_SLIDE_MASTER, Package,
                    local, qn)
from .parser import (TITLE_TYPES, Deck, Inheritance, Shape, Slide, inner_shape, nv_pr, ph_of, sp_tree,
                     top_shapes, xfrm_of)
from .styles import (BODYPR_ORDER, SPPR_ORDER, build_lst_style, levels_of, merge_props, observed_levels,
                     reset_level)

MIN_LAYOUT_ID = 2147483648


@dataclass
class LayoutPlan:
    part: str
    name: str
    cluster: Cluster
    placeholders: List[Tuple[str, Optional[int]]]   # (role, idx) in the layout
    root: etree._Element = None
    rels: "RelCopier" = None


@dataclass
class BuildResult:
    pkg: Package
    layouts: List[LayoutPlan]
    warnings: List[str] = field(default_factory=list)
    dropped_parts: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------- relationships
class RelCopier:
    """Collects relationships for a part that does not exist in the package yet."""

    def __init__(self, pkg: Package):
        self.pkg = pkg
        self.entries: List[Tuple[str, str, str, Optional[str]]] = []

    def add(self, rtype: str, target: str, mode: Optional[str] = None) -> str:
        for rid, t, tg, md in self.entries:
            if t == rtype and tg == target and md == mode:
                return rid
        rid = "rId%d" % (len(self.entries) + 1)
        self.entries.append((rid, rtype, target, mode))
        return rid

    def copy_refs(self, el, src_part: str) -> None:
        src = self.pkg.rels(src_part)
        for node in el.iter():
            for attr, val in list(node.attrib.items()):
                if not attr.startswith(R_ATTR_PREFIX) or not val:
                    continue
                info = src.get(val)
                if info is None:
                    del node.attrib[attr]
                elif info[0] == RT_SLIDE:  # slide jumps make no sense inside a layout
                    node.set(attr, "")
                else:
                    node.set(attr, self.add(info[0], info[1], info[2]))

    def write(self, part: str) -> None:
        rels = self.pkg.rels(part)
        for el in list(rels.root):
            rels.root.remove(el)
        for rid, rtype, target, mode in self.entries:
            new = rels.add(rtype, target, mode)
            if new != rid:  # keep our ids: rewrite the one just added
                rels.root[-1].set("Id", rid)


# --------------------------------------------------------------------------- element helpers
def _ensure_child(parent, tag: str, index: int = 0):
    el = parent.find(qn(tag))
    if el is None:
        el = etree.Element(qn(tag))
        parent.insert(index, el)
    return el


def _xfrm_from_bbox(bbox) -> etree._Element:
    x = etree.Element(qn("a:xfrm"))
    off = etree.SubElement(x, qn("a:off"))
    off.set("x", str(bbox[0]))
    off.set("y", str(bbox[1]))
    ext = etree.SubElement(x, qn("a:ext"))
    ext.set("cx", str(bbox[2]))
    ext.set("cy", str(bbox[3]))
    return x


def _sp_pr(el):
    tag = local(el.tag)
    if tag == "grpSp":
        return el.find(qn("p:grpSpPr"))
    if tag == "graphicFrame":
        return None
    sp_pr = el.find(qn("p:spPr"))
    if sp_pr is None:
        sp_pr = etree.Element(qn("p:spPr"))
        el.insert(1, sp_pr)
    return sp_pr


def _ensure_xfrm(el, bbox) -> None:
    if bbox is None:
        return
    tag = local(el.tag)
    if tag == "graphicFrame":
        x = el.find(qn("p:xfrm"))
        if x is None or x.find(qn("a:off")) is None:
            if x is not None:
                el.remove(x)
            x = _xfrm_from_bbox(bbox)
            x.tag = qn("p:xfrm")
            el.insert(1, x)
        return
    sp_pr = _sp_pr(el)
    if sp_pr is not None and sp_pr.find(qn("a:xfrm")) is None:
        sp_pr.insert(0, _xfrm_from_bbox(bbox))


def _replace(el, new):
    parent = el.getparent()
    parent.replace(el, new)


def _merge_sp_pr(el, inherited: List) -> None:
    """Fill in the spPr groups (geometry, fill, line...) a shape used to inherit."""
    own = _sp_pr(el)
    if own is None:
        return
    layers = [x.find(qn("p:spPr")) for x in reversed(inherited) if x is not None] + [own]
    merged = merge_props("p:spPr", SPPR_ORDER, layers)
    _replace(own, merged)


def _merge_body_pr(tx_body, layers: List) -> None:
    own = tx_body.find(qn("a:bodyPr"))
    merged = merge_props("a:bodyPr", BODYPR_ORDER, [l for l in layers if l is not None] + [own])
    if own is None:
        tx_body.insert(0, merged)
    else:
        _replace(own, merged)


def _body_pr_of(el):
    if el is None:
        return None
    tb = el.find(qn("p:txBody"))
    return None if tb is None else tb.find(qn("a:bodyPr"))


def _lst_of(el):
    if el is None:
        return None
    tb = el.find(qn("p:txBody"))
    return None if tb is None else tb.find(qn("a:lstStyle"))


def set_ph(el, role: str, idx: Optional[int], orig_ph=None) -> None:
    """Turn a p:sp into a placeholder of the given type/idx."""
    nv = nv_pr(el)
    for old in nv.findall(qn("p:ph")):
        nv.remove(old)
    ph = etree.Element(qn("p:ph"))
    if role != "obj":
        ph.set("type", role)
    if orig_ph is not None and orig_ph.get("orient"):
        ph.set("orient", orig_ph.get("orient"))
    if role in ("dt", "ftr", "sldNum"):
        ph.set("sz", "quarter")
    if idx is not None and role not in TITLE_TYPES:
        ph.set("idx", str(idx))
    nv.insert(0, ph)
    is_pic = local(el.tag) == "pic"
    nv_el = el.find(qn("p:nvPicPr" if is_pic else "p:nvSpPr"))
    c = nv_el.find(qn("p:cNvPicPr" if is_pic else "p:cNvSpPr")) if nv_el is not None else None
    if c is not None:
        c.attrib.pop("txBox", None)
        lock_tag = qn("a:picLocks" if is_pic else "a:spLocks")
        locks = c.find(lock_tag)
        if locks is None:
            locks = etree.Element(lock_tag)
            c.insert(0, locks)
        locks.set("noGrp", "1")


FILL_NAMES = {"noFill", "solidFill", "gradFill", "blipFill", "pattFill", "grpFill"}


def shape_to_picture(sp) -> etree._Element:
    """A shape filled with a photo, as an equivalent p:pic (same geometry crops the photo)."""
    pic = etree.Element(qn("p:pic"))
    nv = etree.SubElement(pic, qn("p:nvPicPr"))
    nv.append(copy.deepcopy(sp.find(qn("p:nvSpPr")).find(qn("p:cNvPr"))))
    etree.SubElement(nv, qn("p:cNvPicPr"))
    old_nv = sp.find(qn("p:nvSpPr")).find(qn("p:nvPr"))
    nv.append(copy.deepcopy(old_nv) if old_nv is not None else etree.Element(qn("p:nvPr")))
    sp_pr = copy.deepcopy(sp.find(qn("p:spPr")))
    blip_fill = sp_pr.find(qn("a:blipFill"))
    sp_pr.remove(blip_fill)
    blip_fill.tag = qn("p:blipFill")
    pic.append(blip_fill)
    pic.append(sp_pr)
    for tag in ("p:style", "p:extLst"):
        if sp.find(qn(tag)) is not None:
            pic.append(copy.deepcopy(sp.find(qn(tag))))
    return pic


def strip_ph(el, bbox, inherited: List) -> None:
    """Turn a placeholder into a free-standing shape, keeping what it looked like."""
    el = inner_shape(el)
    nv = nv_pr(el)
    if nv is None:
        return
    for ph in nv.findall(qn("p:ph")):
        nv.remove(ph)
    if local(el.tag) in ("sp", "pic", "cxnSp"):
        _merge_sp_pr(el, inherited)
    _ensure_xfrm(el, bbox)


def _font_ref_level(el) -> Optional[etree._Element]:
    """A shape's p:style/a:fontRef (theme font + text colour of autoshapes) as an lvlNpPr layer."""
    style = el.find(qn("p:style"))
    ref = style.find(qn("a:fontRef")) if style is not None else None
    if ref is None:
        return None
    lvl = etree.Element(qn("a:lvl1pPr"))
    d = etree.SubElement(lvl, qn("a:defRPr"))
    color = next((c for c in ref if isinstance(c.tag, str)), None)
    if color is not None:
        etree.SubElement(d, qn("a:solidFill")).append(copy.deepcopy(color))
    kind = {"major": "mj", "minor": "mn"}.get(ref.get("idx"))
    if kind:
        for script, suffix in (("latin", "lt"), ("ea", "ea"), ("cs", "cs")):
            etree.SubElement(d, qn("a:" + script)).set("typeface", "+%s-%s" % (kind, suffix))
    return lvl


def _class(t: Optional[str]) -> str:
    return "title" if t in TITLE_TYPES else "body"


def _ns_union(*roots) -> Tuple[Dict, List[str]]:
    nsmap = {"a": NS["a"], "r": NS["r"], "p": NS["p"]}
    ignorable: List[str] = []
    for root in roots:
        if root is None:
            continue
        for k, v in root.nsmap.items():
            if k and k not in nsmap and v not in nsmap.values():
                nsmap[k] = v
        for tok in (root.get(qn("mc:Ignorable")) or "").split():
            if tok not in ignorable:
                ignorable.append(tok)
    if ignorable and "mc" not in nsmap:
        nsmap["mc"] = NS["mc"]
    ignorable = [t for t in ignorable if t in nsmap]
    return nsmap, ignorable


# --------------------------------------------------------------------------- builder
class Builder:
    def __init__(self, deck: Deck, clusters: List[Cluster], keep_slides: bool = True):
        self.deck = deck
        self.pkg = deck.pkg
        self.clusters = clusters
        self.keep_slides = keep_slides
        self.inh = Inheritance(self.pkg)
        self.target_master = deck.masters[0]
        self.warnings: List[str] = []
        pres = self.pkg.xml(deck.pres_part)
        dts = pres.find(qn("p:defaultTextStyle"))
        self.default_levels = levels_of(dts)

    # .................................................................. placeholders
    def picture_placeholder(self, sh: Shape, slide: Slide) -> etree._Element:
        """An empty picture placeholder with the photo's geometry, so inserted photos get the same crop."""
        src = inner_shape(sh.el)
        el = etree.Element(qn("p:sp"))
        nv = etree.SubElement(el, qn("p:nvSpPr"))
        c_nv = copy.deepcopy(src[0].find(qn("p:cNvPr")))
        nv.append(c_nv)
        etree.SubElement(nv, qn("p:cNvSpPr"))
        etree.SubElement(nv, qn("p:nvPr"))
        set_ph(el, "pic", sh.idx, ph_of(src))

        sp_pr = copy.deepcopy(src.find(qn("p:spPr"))) if src.find(qn("p:spPr")) is not None else etree.Element(qn("p:spPr"))
        el.append(sp_pr)
        if sh.is_ph:
            layout_ph, master_ph = self.inh.chain(slide, sh.ph_type, sh.ph_idx)
            foreign = slide.master_part != self.target_master
            _merge_sp_pr(el, [x for x in ([layout_ph, master_ph] if foreign else [layout_ph]) if x is not None])
            sp_pr = el.find(qn("p:spPr"))
        for child in list(sp_pr):
            if local(child.tag) in FILL_NAMES:
                sp_pr.remove(child)
        _ensure_xfrm(el, sh.bbox)
        if sp_pr.find(qn("a:prstGeom")) is None and sp_pr.find(qn("a:custGeom")) is None:
            geom = etree.Element(qn("a:prstGeom"))
            geom.set("prst", "rect")
            etree.SubElement(geom, qn("a:avLst"))
            sp_pr.insert(1, geom)
        if src.find(qn("p:style")) is not None:
            el.append(copy.deepcopy(src.find(qn("p:style"))))

        tx = etree.SubElement(el, qn("p:txBody"))
        etree.SubElement(tx, qn("a:bodyPr")).set("anchor", "ctr")
        lst = etree.SubElement(tx, qn("a:lstStyle"))
        lvl = etree.SubElement(lst, qn("a:lvl1pPr"))
        lvl.set("marL", "0")
        lvl.set("indent", "0")
        lvl.set("algn", "ctr")
        etree.SubElement(lvl, qn("a:buNone"))
        etree.SubElement(lvl, qn("a:defRPr"))
        etree.SubElement(tx, qn("a:p"))
        return el

    def layout_placeholder(self, sh: Shape, slide: Slide) -> etree._Element:
        if sh.is_picture:
            return self.picture_placeholder(sh, slide)
        el = copy.deepcopy(inner_shape(sh.el))
        orig_ph = ph_of(el)
        layout_ph = master_ph = None
        full_chain = False
        if sh.is_ph:
            layout_ph, master_ph = self.inh.chain(slide, sh.ph_type, sh.ph_idx)
            foreign = slide.master_part != self.target_master
            full_chain = foreign or _class(sh.role) != _class(sh.ph_type)
        set_ph(el, sh.role, sh.idx, orig_ph)

        inherited = [layout_ph, master_ph] if full_chain else [layout_ph]
        if sh.is_ph:
            _merge_sp_pr(el, [x for x in inherited if x is not None])
        _ensure_xfrm(el, sh.bbox)

        tx = el.find(qn("p:txBody"))
        if tx is None:
            tx = etree.SubElement(el, qn("p:txBody"))
            etree.SubElement(tx, qn("a:bodyPr"))
            etree.SubElement(tx, qn("a:lstStyle"))
            src_tx = layout_ph.find(qn("p:txBody")) if layout_ph is not None else None
            paras = src_tx.findall(qn("a:p")) if src_tx is not None else []
            for p in paras:
                tx.append(copy.deepcopy(p))
            if not paras:
                etree.SubElement(tx, qn("a:p"))
            ext = el.find(qn("p:extLst"))
            if ext is not None:  # txBody must precede extLst
                el.remove(ext)
                el.append(ext)

        # bodyPr: anchoring, insets, autofit
        if sh.is_ph:
            _merge_body_pr(tx, [_body_pr_of(x) for x in reversed(inherited)])
        else:
            base = etree.Element(qn("a:bodyPr"))
            base.set("anchor", "t")
            etree.SubElement(base, qn("a:noAutofit"))
            _merge_body_pr(tx, [base])

        # lstStyle: the part PowerPoint actually uses for text typed into an empty placeholder
        observed = observed_levels(tx)
        own = levels_of(tx.find(qn("a:lstStyle")))
        if not sh.is_ph:
            font_ref = _font_ref_level(el)
            layers = [{l: reset_level(l) for l in range(1, 10)}, self.default_levels,
                      {l: font_ref for l in range(1, 10)} if font_ref is not None else {}, own, observed]
            levels = range(1, 10)
        elif full_chain:
            mroot = self.inh.root(slide.master_part)
            style = "p:titleStyle" if _class(sh.ph_type) == "title" else "p:bodyStyle"
            mstyle = mroot.find(".//" + qn(style)) if mroot is not None else None
            layers = [levels_of(mstyle), levels_of(_lst_of(master_ph)), levels_of(_lst_of(layout_ph)), own, observed]
            levels = range(1, 10)
        else:
            layers = [levels_of(_lst_of(layout_ph)), own, observed]
            levels = set(l for layer in layers for l in layer) | {1}
        lst = build_lst_style(layers, levels)
        if lst.find(qn("a:lvl1pPr")) is None:
            lst.insert(0, etree.Element(qn("a:lvl1pPr")))
        lvl1 = lst.find(qn("a:lvl1pPr"))
        if lvl1.find(qn("a:defRPr")) is None:
            d = etree.Element(qn("a:defRPr"))
            ext = lvl1.find(qn("a:extLst"))
            lvl1.insert(lvl1.index(ext) if ext is not None else len(lvl1), d)
        old = tx.find(qn("a:lstStyle"))
        if old is not None:
            _replace(old, lst)
        else:
            tx.insert(1, lst)
        return el

    # .................................................................. layouts
    def build_layout(self, cl: Cluster, part: str) -> LayoutPlan:
        rep = cl.rep
        pkg = self.pkg
        old_layout = self.inh.root(rep.layout_part)
        old_master = self.inh.root(rep.master_part)
        foreign = rep.master_part is not None and rep.master_part != self.target_master
        hides_master = old_layout is not None and old_layout.get("showMasterSp") == "0"

        nsmap, ignorable = _ns_union(rep.root, old_layout)
        root = etree.Element(qn("p:sldLayout"), nsmap=nsmap)
        if ignorable:
            root.set(qn("mc:Ignorable"), " ".join(ignorable))
        if foreign or hides_master:
            root.set("showMasterSp", "0")
        root.set("preserve", "1")
        root.set("userDrawn", "1")

        rels = RelCopier(pkg)
        rels.add(RT_SLIDE_MASTER, self.target_master)

        c_sld = etree.SubElement(root, qn("p:cSld"))
        c_sld.set("name", cl.name)
        for src_part, src_root in ((rep.part, rep.root), (rep.layout_part, old_layout),
                                   (rep.master_part if foreign else None, old_master if foreign else None)):
            if src_root is None or src_part is None:
                continue
            bg = src_root.find(qn("p:cSld")).find(qn("p:bg"))
            if bg is not None:
                bg = copy.deepcopy(bg)
                rels.copy_refs(bg, src_part)
                c_sld.append(bg)
                break

        tree = etree.SubElement(c_sld, qn("p:spTree"))
        nv = etree.SubElement(tree, qn("p:nvGrpSpPr"))
        c = etree.SubElement(nv, qn("p:cNvPr"))
        c.set("id", "1")
        c.set("name", "")
        etree.SubElement(nv, qn("p:cNvGrpSpPr"))
        etree.SubElement(nv, qn("p:nvPr"))
        gpr = etree.SubElement(tree, qn("p:grpSpPr"))
        x = etree.SubElement(gpr, qn("a:xfrm"))
        for tag, attrs in (("a:off", ("x", "y")), ("a:ext", ("cx", "cy")), ("a:chOff", ("x", "y")),
                           ("a:chExt", ("cx", "cy"))):
            e = etree.SubElement(x, qn(tag))
            for a in attrs:
                e.set(a, "0")

        sources: List[List[etree._Element]] = []

        def take(elements, src_part):
            group = []
            for el in elements:
                el = copy.deepcopy(el)
                rels.copy_refs(el, src_part)
                tree.append(el)
                group.append(el)
            sources.append(group)

        # decoration the slides used to inherit
        if foreign and not hides_master and old_master is not None:
            take([e for e in top_shapes(old_master) if ph_of(e) is None], rep.master_part)
        if old_layout is not None:
            take([e for e in top_shapes(old_layout) if ph_of(e) is None], rep.layout_part)

        placeholders = []
        own = []
        for sh in rep.shapes:
            if sh.verdict == CHROME:
                el = copy.deepcopy(sh.el)
                if sh.is_ph:
                    layout_ph, master_ph = self.inh.chain(rep, sh.ph_type, sh.ph_idx)
                    strip_ph(el, sh.bbox, [layout_ph, master_ph])
            elif sh.verdict == PLACEHOLDER:
                el = self.layout_placeholder(sh, rep)
                placeholders.append((sh.role, sh.idx))
            else:
                continue
            own.append(el)
        group = []
        for el in own:
            rels.copy_refs(el, rep.part)
            tree.append(el)
            group.append(el)
        sources.append(group)
        _renumber(sources)

        ovr = etree.SubElement(root, qn("p:clrMapOvr"))
        cmap = old_master.find(qn("p:clrMap")) if (foreign and old_master is not None) else None
        if cmap is not None:
            o = etree.SubElement(ovr, qn("a:overrideClrMapping"))
            for k, v in cmap.attrib.items():
                o.set(k, v)
        elif old_layout is not None and old_layout.find(qn("p:clrMapOvr")) is not None:
            root.replace(ovr, copy.deepcopy(old_layout.find(qn("p:clrMapOvr"))))
        else:
            etree.SubElement(ovr, qn("a:masterClrMapping"))

        return LayoutPlan(part=part, name=cl.name, cluster=cl, placeholders=placeholders, root=root, rels=rels)

    # .................................................................. slides
    def rewrite_slide(self, slide: Slide, cl: Cluster, layout_part: str) -> None:
        mapping = cl.mapping[slide.index]
        rep = cl.rep
        for sh in slide.shapes:
            rep_sh = rep.shapes[mapping[sh.pos]]
            if sh.verdict == CHROME:
                sh.el.getparent().remove(sh.el)
            elif sh.verdict == PLACEHOLDER and sh.is_picture:
                el = inner_shape(sh.el)
                if local(el.tag) == "sp":
                    pic = shape_to_picture(el)
                    _replace(el, pic)
                    el = pic
                set_ph(el, rep_sh.role, rep_sh.idx, ph_of(el))
            elif sh.verdict == PLACEHOLDER:
                set_ph(inner_shape(sh.el), rep_sh.role, rep_sh.idx, ph_of(sh.el))
            elif sh.is_ph:
                layout_ph, master_ph = self.inh.chain(slide, sh.ph_type, sh.ph_idx)
                strip_ph(sh.el, sh.bbox, [layout_ph, master_ph])
        rels = self.pkg.rels(slide.part)
        layouts = rels.by_type(RT_SLIDE_LAYOUT)
        if layouts:
            rels.set_target(layouts[0][0], layout_part)
        else:
            rels.add(RT_SLIDE_LAYOUT, layout_part)

    # .................................................................. package
    def run(self) -> BuildResult:
        pkg = self.pkg
        plans = []
        for n, cl in enumerate(self.clusters, start=1):
            plans.append(self.build_layout(cl, "ppt/slideLayouts/slideLayout%d.xml" % n))
        for plan in plans:
            for slide in plan.cluster.slides:
                self.rewrite_slide(slide, plan.cluster, plan.part)

        # drop every old layout and every master but the first
        pres = pkg.xml(self.deck.pres_part)
        prels = pkg.rels(self.deck.pres_part)
        old_layouts = set()
        for m in self.deck.masters:
            for rid, part in pkg.rels(m).by_type(RT_SLIDE_LAYOUT):
                old_layouts.add(part)
        for part in old_layouts:
            pkg.delete(part)
            pkg.delete(part.rsplit("/", 1)[0] + "/_rels/" + part.rsplit("/", 1)[1] + ".rels")
        lst = pres.find(qn("p:sldMasterIdLst"))
        for el in list(lst):
            info = prels.get(el.get(qn("r:id")))
            if info and info[1] != self.target_master:
                prels.remove(el.get(qn("r:id")))
                lst.remove(el)

        master = pkg.xml(self.target_master)
        mrels = pkg.rels(self.target_master)
        for rid, _ in mrels.by_type(RT_SLIDE_LAYOUT):
            mrels.remove(rid)
        id_lst = master.find(qn("p:sldLayoutIdLst"))
        if id_lst is None:
            id_lst = etree.Element(qn("p:sldLayoutIdLst"))
            master.insert(list(master).index(master.find(qn("p:clrMap"))) + 1, id_lst)
        for el in list(id_lst):
            id_lst.remove(el)
        used = [int(el.get("id")) for el in lst]
        next_id = max(used + [MIN_LAYOUT_ID - 1]) + 1
        for plan in plans:
            pkg.set_xml(plan.part, plan.root)
            plan.rels.write(plan.part)
            pkg.set_override(plan.part, CT_SLIDE_LAYOUT)
            el = etree.SubElement(id_lst, qn("p:sldLayoutId"))
            el.set("id", str(next_id))
            el.set(qn("r:id"), mrels.add(RT_SLIDE_LAYOUT, plan.part))
            next_id += 1

        if not self.keep_slides:
            self._drop_slides(pres, prels)

        dropped = pkg.collect_garbage()
        return BuildResult(pkg=pkg, layouts=plans, warnings=self.warnings, dropped_parts=dropped)

    def _drop_slides(self, pres, prels) -> None:
        lst = pres.find(qn("p:sldIdLst"))
        if lst is not None:
            for el in list(lst):
                prels.remove(el.get(qn("r:id")))
                lst.remove(el)
        for tag in ("p:custShowLst",):
            el = pres.find(qn(tag))
            if el is not None:
                pres.remove(el)
        ext_lst = pres.find(qn("p:extLst"))
        if ext_lst is not None:
            for ext in list(ext_lst):
                if ext.find(qn("p14:sectionLst")) is not None:
                    ext_lst.remove(ext)


def _renumber(sources: List[List[etree._Element]]) -> None:
    """Shape ids must be unique within a part; keep connector endpoints consistent per source."""
    next_id = 2
    for group in sources:
        idmap = {}
        for el in group:
            for c in el.iter(qn("p:cNvPr")):
                old = c.get("id")
                c.set("id", str(next_id))
                if old is not None:
                    idmap.setdefault(old, str(next_id))
                next_id += 1
        for el in group:
            for cx in list(el.iter(qn("a:stCxn"), qn("a:endCxn"))):
                new = idmap.get(cx.get("id"))
                if new is None:
                    cx.getparent().remove(cx)
                else:
                    cx.set("id", new)


def build(deck: Deck, clusters: List[Cluster], keep_slides: bool = True) -> BuildResult:
    return Builder(deck, clusters, keep_slides=keep_slides).run()
