"""Turn an analysis into JSON the browser can draw: a schematic of every slide in points."""
from __future__ import annotations

import colorsys
from typing import Dict, List, Optional, Tuple

from .. import Analysis
from ..classify import Overrides
from ..ooxml import Package, local, qn
from ..parser import Slide, inner_shape, sp_tree

EMU_PER_PT = 12700
BROWSER_IMAGES = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg", ".webp")
PRESET_COLORS = {"black": "000000", "white": "FFFFFF", "red": "FF0000", "green": "008000", "blue": "0000FF",
                 "yellow": "FFFF00", "gray": "808080", "grey": "808080"}


def pt(v: float) -> float:
    return round(v / EMU_PER_PT, 2)


# --------------------------------------------------------------------------- colours
class Palette:
    """Resolves DrawingML colours through the master's colour map and theme."""

    def __init__(self, pkg: Package, master_part: Optional[str]):
        self.scheme: Dict[str, str] = {}
        self.cmap: Dict[str, str] = {}
        self.fonts: Dict[str, str] = {}   # "+mj-lt" / "+mn-lt" -> typeface
        if not master_part or not pkg.has(master_part):
            return
        master = pkg.xml(master_part)
        cm = master.find(qn("p:clrMap"))
        if cm is not None:
            self.cmap = dict(cm.attrib)
        for rid, rtype, target, mode in pkg.rels(master_part):
            if rtype.endswith("/theme"):
                from ..ooxml import resolve
                theme = pkg.xml(resolve(master_part, target))
                for kind, key in (("majorFont", "+mj-lt"), ("minorFont", "+mn-lt")):
                    latin = theme.find(".//" + qn("a:" + kind) + "/" + qn("a:latin"))
                    if latin is not None and latin.get("typeface"):
                        self.fonts[key] = latin.get("typeface")
                cs = theme.find(".//" + qn("a:clrScheme"))
                for child in (cs if cs is not None else []):
                    for c in child:
                        val = c.get("lastClr") or c.get("val")
                        if val:
                            self.scheme[local(child.tag)] = val

    def color(self, el) -> Optional[Tuple[str, float]]:
        """(hex, opacity) of an a:srgbClr / a:schemeClr / ... element."""
        if el is None:
            return None
        tag = local(el.tag)
        if tag == "srgbClr":
            base = el.get("val")
        elif tag == "schemeClr":
            name = el.get("val")
            name = self.cmap.get(name, name)
            base = self.scheme.get(name)
        elif tag == "sysClr":
            base = el.get("lastClr") or ("000000" if el.get("val") == "windowText" else "FFFFFF")
        elif tag == "prstClr":
            base = PRESET_COLORS.get(el.get("val"), "808080")
        else:
            return None
        if not base or len(base) != 6:
            return None
        r, g, b = (int(base[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
        h, l, s = colorsys.rgb_to_hls(r, g, b)
        alpha = 1.0
        for mod in el:
            v = int(mod.get("val", "100000")) / 100000.0
            m = local(mod.tag)
            if m == "lumMod":
                l *= v
            elif m == "lumOff":
                l += v
            elif m == "tint":
                l = l + (1 - l) * (1 - v)
            elif m == "shade":
                l *= v
            elif m == "alpha":
                alpha = v
        r, g, b = colorsys.hls_to_rgb(h, max(0.0, min(1.0, l)), s)
        return "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255)), alpha

    def fill(self, fill_el) -> Optional[Tuple[str, float]]:
        if fill_el is None:
            return None
        tag = local(fill_el.tag)
        if tag == "solidFill":
            return self.color(next((c for c in fill_el if isinstance(c.tag, str)), None))
        if tag == "gradFill":
            gs = fill_el.find(".//" + qn("a:gs"))
            if gs is not None:
                return self.color(next((c for c in gs if isinstance(c.tag, str)), None))
        if tag == "pattFill":
            fg = fill_el.find(qn("a:fgClr"))
            if fg is not None:
                return self.color(next((c for c in fg if isinstance(c.tag, str)), None))
        return None


# --------------------------------------------------------------------------- geometry
class Xf:
    """Child-to-slide coordinate transform for shapes nested in groups."""

    def __init__(self, sx=1.0, sy=1.0, dx=0.0, dy=0.0):
        self.sx, self.sy, self.dx, self.dy = sx, sy, dx, dy

    def box(self, x, y, w, h):
        return (x * self.sx + self.dx, y * self.sy + self.dy, w * self.sx, h * self.sy)

    def child(self, xfrm) -> "Xf":
        off, ext = xfrm.find(qn("a:off")), xfrm.find(qn("a:ext"))
        ch_off, ch_ext = xfrm.find(qn("a:chOff")), xfrm.find(qn("a:chExt"))
        if None in (off, ext, ch_off, ch_ext):
            return self
        cw, chh = int(ch_ext.get("cx")) or 1, int(ch_ext.get("cy")) or 1
        sx = int(ext.get("cx")) / cw
        sy = int(ext.get("cy")) / chh
        gx, gy, _, _ = self.box(int(off.get("x")), int(off.get("y")), 0, 0)
        return Xf(self.sx * sx, self.sy * sy, gx - int(ch_off.get("x")) * sx * self.sx,
                  gy - int(ch_off.get("y")) * sy * self.sy)


def _xfrm(el):
    tag = local(el.tag)
    if tag == "graphicFrame":
        return el.find(qn("p:xfrm"))
    pr = el.find(qn("p:grpSpPr" if tag == "grpSp" else "p:spPr"))
    return None if pr is None else pr.find(qn("a:xfrm"))


def _geom(sp_pr) -> dict:
    if sp_pr is None:
        return {"prst": "rect"}
    g = sp_pr.find(qn("a:prstGeom"))
    if g is None:
        return {"prst": "custom" if sp_pr.find(qn("a:custGeom")) is not None else "rect"}
    adj = {}
    for gd in g.iter(qn("a:gd")):
        f = (gd.get("fmla") or "").split()
        if len(f) == 2 and f[0] == "val":
            adj[gd.get("name")] = int(f[1])
    return {"prst": g.get("prst"), "adj": adj}


# --------------------------------------------------------------------------- drawing
class Drawer:
    def __init__(self, pkg: Package, slide: Slide, palette: Palette):
        self.pkg = pkg
        self.slide = slide
        self.pal = palette
        self.rels = pkg.rels(slide.part)

    def media_url(self, rid: Optional[str], part: Optional[str] = None) -> Optional[str]:
        rels = self.pkg.rels(part) if part else self.rels
        info = rels.get(rid) if rid else None
        if not info or info[2] == "External" or not self.pkg.has(info[1]):
            return None
        if not info[1].lower().endswith(BROWSER_IMAGES):
            return None
        return "/api/media?part=" + info[1]

    def image(self, blip_fill, box, geom, part=None) -> Optional[dict]:
        blip = blip_fill.find(qn("a:blip")) if blip_fill is not None else None
        url = self.media_url(blip.get(qn("r:embed")) if blip is not None else None, part)
        crop = [0, 0, 0, 0]
        src = blip_fill.find(qn("a:srcRect")) if blip_fill is not None else None
        if src is not None:
            crop = [int(src.get(k, 0)) / 100000.0 for k in ("l", "t", "r", "b")]
        return {"t": "img", "box": [pt(v) for v in box], "url": url, "crop": crop, "geom": geom}

    def items(self, el, xf: Xf, fallback_bbox=None) -> List[dict]:
        el = inner_shape(el)
        tag = local(el.tag)
        x = _xfrm(el)
        if x is not None and x.find(qn("a:off")) is not None:
            off, ext = x.find(qn("a:off")), x.find(qn("a:ext"))
            box = xf.box(int(off.get("x")), int(off.get("y")), int(ext.get("cx")), int(ext.get("cy")))
            rot = int(x.get("rot", 0)) / 60000.0
        elif fallback_bbox:
            box, rot = xf.box(*fallback_bbox), 0
        else:
            return []
        out: List[dict] = []
        if tag == "grpSp":
            child_xf = xf.child(x) if x is not None else xf
            for child in el:
                if local(child.tag) in ("sp", "pic", "grpSp", "graphicFrame", "cxnSp", "AlternateContent"):
                    out += self.items(child, child_xf)
            return out
        if tag == "graphicFrame":
            out.append({"t": "frame", "box": [pt(v) for v in box], "rot": rot})
            out += self.text_items(el, box, None, table=True)
            return out
        sp_pr = el.find(qn("p:spPr"))
        geom = _geom(sp_pr)
        if tag == "pic":
            img = self.image(el.find(qn("p:blipFill")), box, geom)
            img["rot"] = rot
            out.append(img)
            return out
        fill_el = None
        if sp_pr is not None:
            fill_el = next((c for c in sp_pr if local(c.tag) in
                            ("noFill", "solidFill", "gradFill", "blipFill", "pattFill")), None)
        if fill_el is not None and local(fill_el.tag) == "blipFill":
            img = self.image(fill_el, box, geom)
            img["rot"] = rot
            out.append(img)
        else:
            color = None
            if fill_el is not None:
                color = self.pal.fill(fill_el)
            else:
                style = el.find(qn("p:style"))
                ref = style.find(qn("a:fillRef")) if style is not None else None
                if ref is not None and ref.get("idx", "0") != "0":
                    color = self.pal.color(next((c for c in ref if isinstance(c.tag, str)), None))
            line = None
            ln = sp_pr.find(qn("a:ln")) if sp_pr is not None else None
            if ln is not None and ln.find(qn("a:solidFill")) is not None:
                line = self.pal.fill(ln.find(qn("a:solidFill")))
            if color or line or tag == "cxnSp":
                out.append({"t": "line" if tag == "cxnSp" else "shape", "box": [pt(v) for v in box], "geom": geom,
                            "fill": color[0] if color else None, "opacity": color[1] if color else 1,
                            "stroke": line[0] if line else ("#999999" if tag == "cxnSp" else None), "rot": rot})
        out += self.text_items(el, box, el.find(qn("p:style")), rot=rot)
        return out

    def text_items(self, el, box, style, table=False, rot=0.0) -> List[dict]:
        paras = []
        for p in el.iter(qn("a:p")):
            text = "".join(t.text or "" for t in p.iter(qn("a:t")))
            size, color, bold, font = None, None, False, None
            for r in p.iter(qn("a:r"), qn("a:fld")):
                rpr = r.find(qn("a:rPr"))
                if rpr is not None:
                    size = int(rpr.get("sz")) / 100.0 if rpr.get("sz") else None
                    bold = rpr.get("b") == "1"
                    latin = rpr.find(qn("a:latin"))
                    if latin is not None and latin.get("typeface"):
                        font = self.pal.fonts.get(latin.get("typeface"), latin.get("typeface"))
                    sf = rpr.find(qn("a:solidFill"))
                    if sf is not None:
                        c = self.pal.fill(sf)
                        color = c[0] if c else None
                break
            ppr = p.find(qn("a:pPr"))
            paras.append({"text": text, "size": size, "color": color, "bold": bold,
                          "font": font or self.pal.fonts.get("+mn-lt"),
                          "algn": ppr.get("algn") if ppr is not None else None})
        if not any(p["text"].strip() for p in paras):
            return []
        if style is not None and not any(p["color"] for p in paras):
            ref = style.find(qn("a:fontRef"))
            c = self.pal.color(next((x for x in ref if isinstance(x.tag, str)), None)) if ref is not None else None
            if c:
                for p in paras:
                    p["color"] = c[0]
        body = el.find(".//" + qn("a:bodyPr"))
        anchor = body.get("anchor", "t") if body is not None else "t"
        nowrap = body is not None and body.get("wrap") == "none"
        return [{"t": "text", "box": [pt(v) for v in box], "paras": paras, "anchor": anchor, "nowrap": nowrap,
                 "table": table, "rot": rot}]

    def background(self) -> dict:
        """Slide background: the first p:bg along slide -> layout -> master."""
        for part in (self.slide.part, self.slide.layout_part, self.slide.master_part):
            if not part or not self.pkg.has(part):
                continue
            bg = self.pkg.xml(part).find(qn("p:cSld")).find(qn("p:bg"))
            if bg is None:
                continue
            pr = bg.find(qn("p:bgPr"))
            if pr is not None:
                fill = next((c for c in pr if local(c.tag) in ("solidFill", "gradFill", "blipFill", "pattFill")), None)
                if fill is not None and local(fill.tag) == "blipFill":
                    blip = fill.find(qn("a:blip"))
                    return {"image": self.media_url(blip.get(qn("r:embed")) if blip is not None else None, part)}
                c = self.pal.fill(fill)
                return {"color": c[0] if c else "#ffffff"}
            ref = bg.find(qn("p:bgRef"))
            if ref is not None:
                c = self.pal.color(next((x for x in ref if isinstance(x.tag, str)), None))
                return {"color": c[0] if c else "#ffffff"}
        return {"color": "#ffffff"}

    def inherited(self) -> List[dict]:
        """Decoration the slide shows from its layout/master (not placeholders), drawn faded."""
        out = []
        from ..parser import ph_of, top_shapes
        parts = []
        if self.slide.layout_part and self.pkg.has(self.slide.layout_part):
            layout = self.pkg.xml(self.slide.layout_part)
            if layout.get("showMasterSp", "1") != "0" and self.slide.master_part:
                parts.append(self.slide.master_part)
            parts.append(self.slide.layout_part)
        for part in parts:
            drawer = Drawer(self.pkg, self.slide, self.pal)
            drawer.rels = self.pkg.rels(part)
            for el in top_shapes(self.pkg.xml(part)):
                if ph_of(el) is None:
                    out += drawer.items(el, Xf())
        return out


# --------------------------------------------------------------------------- model
def build_model(analysis: Analysis, overrides: Overrides) -> dict:
    deck = analysis.deck
    pkg = deck.pkg
    by_slide = {}
    for cl in analysis.clusters:
        for s in cl.slides:
            by_slide[s.index] = cl.id
    palettes: Dict[str, Palette] = {}
    slides = []
    for slide in deck.slides:
        key = slide.master_part or ""
        if key not in palettes:
            palettes[key] = Palette(pkg, slide.master_part)
        drawer = Drawer(pkg, slide, palettes[key])
        shapes = []
        for sh in slide.shapes:
            ov = overrides.shapes.get(sh.name)
            shapes.append({
                "pos": sh.pos, "id": sh.id, "name": sh.name, "label": sh.label(), "kind": sh.kind,
                "verdict": sh.verdict, "role": sh.role, "idx": sh.idx, "reason": sh.reason, "forced": sh.forced,
                "picture": sh.is_picture, "text": sh.text,
                "bbox": [pt(v) for v in sh.bbox] if sh.bbox else None,
                "override": {"force": ov.force, "type": ov.type} if ov else None,
                "draw": drawer.items(sh.el, Xf(), sh.bbox),
            })
        slides.append({"index": slide.index, "name": slide.custom_name, "cluster": by_slide[slide.index],
                       "bg": drawer.background(), "inherited": drawer.inherited(), "shapes": shapes})
    name_count: Dict[str, int] = {}
    for slide in deck.slides:
        for sh in slide.shapes:
            name_count[sh.name] = name_count.get(sh.name, 0) + 1
    for s in slides:
        for sh in s["shapes"]:
            sh["same_name"] = name_count.get(sh["name"], 1)
    return {
        "loaded": True,
        "width": pt(deck.slide_size[0]), "height": pt(deck.slide_size[1]),
        "clusters": [{"id": c.id, "name": c.name, "source": c.name_source, "rep": c.rep.index,
                      "slides": [s.index for s in c.slides]} for c in analysis.clusters],
        "slides": slides,
        "warnings": analysis.warnings,
        "overrides": overrides.to_dict(),
    }
