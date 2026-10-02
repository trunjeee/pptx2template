"""Merging DrawingML property elements (pPr/rPr/bodyPr/spPr) the way inheritance does."""
from __future__ import annotations

import copy
from typing import Dict, Iterable, List, Optional

from lxml import etree

from .ooxml import local, qn

PPR_ORDER = [["lnSpc"], ["spcBef"], ["spcAft"], ["buClrTx", "buClr"], ["buSzTx", "buSzPct", "buSzPts"],
             ["buFontTx", "buFont"], ["buNone", "buAutoNum", "buChar", "buBlip"], ["tabLst"], ["defRPr"],
             ["extLst"]]
RPR_ORDER = [["ln"], ["noFill", "solidFill", "gradFill", "blipFill", "pattFill", "grpFill"],
             ["effectLst", "effectDag"], ["highlight"], ["uLnTx", "uLn"], ["uFillTx", "uFill"], ["latin"],
             ["ea"], ["cs"], ["sym"], ["hlinkClick"], ["hlinkMouseOver"], ["rtl"], ["extLst"]]
BODYPR_ORDER = [["prstTxWarp"], ["noAutofit", "normAutofit", "spAutoFit"], ["scene3d"], ["sp3d"], ["flatTx"],
                ["extLst"]]
SPPR_ORDER = [["xfrm"], ["custGeom", "prstGeom"], ["noFill", "solidFill", "gradFill", "blipFill", "pattFill", "grpFill"],
              ["ln"], ["effectLst", "effectDag"], ["scene3d"], ["sp3d"], ["extLst"]]

RUN_ONLY_ATTRS = {"lang", "altLang", "dirty", "err", "noProof", "smtClean", "smtId", "bmk"}
RUN_ONLY_CHILDREN = {"hlinkClick", "hlinkMouseOver", "rtl", "extLst"}


def _slot(order, tag: str) -> int:
    name = local(tag)
    for i, group in enumerate(order):
        if name in group:
            return i
    return len(order) - 1  # unknown children go just before/with extLst


def merge_props(tag: str, order, layers: Iterable[Optional[etree._Element]]) -> etree._Element:
    """Merge property elements bottom-up: later layers override attributes and child groups."""
    out = etree.Element(qn(tag))
    slots: Dict[int, etree._Element] = {}
    for layer in layers:
        if layer is None:
            continue
        for k, v in layer.attrib.items():
            out.set(k, v)
        for child in layer:
            if not isinstance(child.tag, str):
                continue
            s = _slot(order, child.tag)
            if order is PPR_ORDER and local(child.tag) == "defRPr":
                slots[s] = merge_props("a:defRPr", RPR_ORDER, [slots.get(s), child])
            else:
                slots[s] = copy.deepcopy(child)
    for s in sorted(slots):
        out.append(slots[s])
    return out


def run_to_defrpr(rpr: Optional[etree._Element]) -> Optional[etree._Element]:
    if rpr is None:
        return None
    out = etree.Element(qn("a:defRPr"))
    for k, v in rpr.attrib.items():
        if k not in RUN_ONLY_ATTRS:
            out.set(k, v)
    for child in rpr:
        if isinstance(child.tag, str) and local(child.tag) not in RUN_ONLY_CHILDREN:
            out.append(copy.deepcopy(child))
    return out


def para_to_lvl(ppr: Optional[etree._Element], rpr: Optional[etree._Element]) -> etree._Element:
    """A paragraph's pPr + its first run's rPr, as an lvlNpPr layer."""
    out = etree.Element(qn("a:lvl1pPr"))
    if ppr is not None:
        for k, v in ppr.attrib.items():
            if k != "lvl":
                out.set(k, v)
        for child in ppr:
            if isinstance(child.tag, str):
                out.append(copy.deepcopy(child))
    d = run_to_defrpr(rpr)
    if d is not None:
        merged = merge_props("a:lvl1pPr", PPR_ORDER, [out, _wrap_defrpr(d)])
        return merged
    return out


def _wrap_defrpr(d):
    w = etree.Element(qn("a:lvl1pPr"))
    w.append(d)
    return w


def levels_of(lst: Optional[etree._Element]) -> Dict[int, etree._Element]:
    out = {}
    if lst is None:
        return out
    for child in lst:
        name = local(child.tag)
        if name.startswith("lvl") and name.endswith("pPr"):
            try:
                out[int(name[3:-3])] = child
            except ValueError:
                pass
    return out


def observed_levels(tx_body: Optional[etree._Element]) -> Dict[int, etree._Element]:
    """First paragraph of every indent level, with its first run's formatting."""
    out: Dict[int, etree._Element] = {}
    if tx_body is None:
        return out
    for p in tx_body.findall(qn("a:p")):
        ppr = p.find(qn("a:pPr"))
        lvl = int(ppr.get("lvl", "0")) + 1 if ppr is not None else 1
        if lvl in out:
            continue
        rpr = None
        for r in p:
            if local(r.tag) in ("r", "fld") and r.find(qn("a:rPr")) is not None:
                t = r.find(qn("a:t"))
                if t is not None and (t.text or "").strip():
                    rpr = r.find(qn("a:rPr"))
                    break
        if rpr is None:
            rpr = p.find(qn("a:endParaRPr"))
            has_text = any((t.text or "").strip() for t in p.iter(qn("a:t")))
            if has_text:
                rpr = None  # runs exist but carry no formatting: inherit
        out[lvl] = para_to_lvl(ppr, rpr)
    return out


def build_lst_style(layers: List[Dict[int, etree._Element]], levels: Iterable[int]) -> etree._Element:
    lst = etree.Element(qn("a:lstStyle"))
    for lvl in sorted(set(levels)):
        parts = [layer.get(lvl) for layer in layers]
        if all(p is None for p in parts):
            continue
        lst.append(merge_props("a:lvl%dpPr" % lvl, PPR_ORDER, parts))
    return lst


def reset_level(lvl: int) -> etree._Element:
    """What a plain text box looks like, so a converted text box does not pick up
    the master's bullets, indents, spacing or title font."""
    el = etree.Element(qn("a:lvl%dpPr" % lvl))
    el.set("marL", "0")
    el.set("indent", "0")
    el.set("algn", "l")
    ln = etree.SubElement(el, qn("a:lnSpc"))
    etree.SubElement(ln, qn("a:spcPct")).set("val", "100000")
    sb = etree.SubElement(el, qn("a:spcBef"))
    etree.SubElement(sb, qn("a:spcPts")).set("val", "0")
    etree.SubElement(el, qn("a:buNone"))
    d = etree.SubElement(el, qn("a:defRPr"))
    d.set("b", "0")
    d.set("i", "0")
    return el
