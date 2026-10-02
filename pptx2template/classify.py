"""The heuristic rules table: chrome vs. placeholder vs. content media."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .parser import SPECIAL_TYPES, TITLE_TYPES, Deck, Shape, Slide

PLACEHOLDER, MEDIA, CHROME = "placeholder", "media", "chrome"
VERDICTS = (PLACEHOLDER, MEDIA, CHROME)
PH_TYPES = {"title", "ctrTitle", "subTitle", "body", "obj", "pic", "dt", "ftr", "sldNum",
            "chart", "tbl", "dgm", "media", "clipArt"}

ROW_BAND = 228600  # 0.25in: shapes whose tops are this close count as one row


# --------------------------------------------------------------------------- overrides
@dataclass
class ShapeOverride:
    force: Optional[str] = None
    type: Optional[str] = None


@dataclass
class Overrides:
    shapes: Dict[str, ShapeOverride] = field(default_factory=dict)
    layouts: Dict[str, str] = field(default_factory=dict)   # cluster id -> layout name
    source: Optional[str] = None

    @classmethod
    def load(cls, path) -> "Overrides":
        path = Path(path)
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            data = json.loads(text)
        else:
            import yaml
            data = yaml.safe_load(text)
        data = data or {}
        ov = cls(source=str(path))
        for name, spec in (data.get("shapes") or {}).items():
            spec = spec or {}
            force = spec.get("force")
            if force is not None and force not in VERDICTS:
                raise ValueError("%s: shapes.%r.force must be one of %s" % (path, name, ", ".join(VERDICTS)))
            ptype = spec.get("type")
            if ptype is not None and ptype not in PH_TYPES:
                raise ValueError("%s: shapes.%r.type must be one of %s" % (path, name, ", ".join(sorted(PH_TYPES))))
            ov.shapes[str(name)] = ShapeOverride(force=force, type=ptype)
        for cid, spec in (data.get("layouts") or {}).items():
            if isinstance(spec, dict) and spec.get("name"):
                ov.layouts[str(cid)] = str(spec["name"])
            elif isinstance(spec, str):
                ov.layouts[str(cid)] = spec
        return ov

    def to_dict(self) -> dict:
        shapes = {}
        for name, ov in self.shapes.items():
            spec = {k: v for k, v in (("force", ov.force), ("type", ov.type)) if v}
            if spec:
                shapes[name] = spec
        out = {}
        if shapes:
            out["shapes"] = shapes
        if self.layouts:
            out["layouts"] = {cid: {"name": name} for cid, name in self.layouts.items()}
        return out

    def to_yaml(self) -> str:
        import yaml
        return yaml.safe_dump(self.to_dict(), allow_unicode=True, sort_keys=False)

    def save(self, path) -> None:
        Path(path).write_text(self.to_yaml(), encoding="utf-8")

    @staticmethod
    def find_sidecar(input_path) -> Optional[Path]:
        p = Path(input_path)
        for suffix in (".overrides.yaml", ".overrides.yml", ".overrides.json"):
            cand = p.with_name(p.stem + suffix)
            if cand.exists():
                return cand
        return None


# --------------------------------------------------------------------------- rules
SMALL_PICTURE = 0.02  # pictures under 2% of the slide area are logos/icons, not photos
FLAT_PICTURE = 40     # image_detail() below this: a flat colour / gradient / simple graphic, not a photo


def rule(sh: Shape, slide_area: int = 0) -> Tuple[str, str]:
    """Apply the rules table to one shape. First match wins."""
    if sh.kind == "graphicFrame":
        return MEDIA, "%s frame (cannot be a placeholder)" % (sh.frame_kind or "graphic")
    if sh.kind in ("alt", "contentPart"):
        return MEDIA, "embedded/extended content"
    if sh.kind == "cxnSp":
        return CHROME, "connector line"
    # Rule 1: text always wins over fill.
    if sh.has_text:
        if sh.kind == "sp":
            return PLACEHOLDER, "rule 1: has text"
        return MEDIA, "rule 1: group with editable text, kept on slide (groups cannot be placeholders)"
    # Rules 2-3: photos become picture placeholders, shaped like the original.
    if sh.is_picture:
        if sh.av:
            return MEDIA, "rule 2: video/audio, kept on slide"
        if sh.detail is not None and sh.detail < FLAT_PICTURE:
            return CHROME, "rule 4: picture of a flat colour or gradient, static graphic"
        if slide_area and sh.bbox and sh.bbox[2] * sh.bbox[3] < SMALL_PICTURE * slide_area:
            return CHROME, "rule 2: small picture (logo/icon), static graphic"
        if sh.kind == "pic":
            return PLACEHOLDER, "rule 2: picture -> picture placeholder"
        return PLACEHOLDER, "rule 3: picture fill -> picture placeholder cropped to the shape"
    if sh.kind == "sp" and sh.is_ph:
        return PLACEHOLDER, "empty placeholder (%s)" % (sh.ph_type or "content")
    # Rule 4: flat / gradient fill, no text.
    if sh.fill in ("solid", "grad", "patt", "style"):
        return CHROME, "rule 4: %s fill, no text" % {"style": "theme"}.get(sh.fill, sh.fill)
    if sh.kind == "sp":
        return CHROME, "rule 5: no fill, no text"
    # Rule 6: group without editable text (logo lockups).
    if sh.kind == "grpSp":
        return CHROME, "rule 6: group without text"
    return CHROME, "fallback"


def _overlap(a, b) -> int:
    if not a or not b:
        return 0
    w = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    h = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0


def _guards(slide: Slide) -> None:
    """Keep chrome on the slide when moving it into the layout would change what you see."""
    medias = [s for s in slide.shapes if s.verdict == MEDIA or (s.verdict == PLACEHOLDER and s.is_picture)]
    for sh in slide.shapes:
        if sh.verdict != CHROME or sh.forced:
            continue
        if sh.animated:
            sh.verdict, sh.reason = MEDIA, "chrome, but animated on this slide: kept on slide"
            continue
        if not sh.bbox:
            continue
        area = max(1, sh.bbox[2] * sh.bbox[3])
        for m in medias:
            if m.pos < sh.pos and m.bbox:
                smaller = max(1, min(area, m.bbox[2] * m.bbox[3]))
                if _overlap(sh.bbox, m.bbox) > 0.25 * smaller:
                    sh.verdict = MEDIA
                    sh.reason = "chrome drawn over %r: kept on slide to preserve stacking" % m.label()
                    break


def _reading_key(sh: Shape):
    x, y = (sh.bbox[0], sh.bbox[1]) if sh.bbox else (0, 0)
    return (y // ROW_BAND, x, y)


def assign_roles(slide: Slide) -> None:
    phs = [s for s in slide.shapes if s.verdict == PLACEHOLDER]
    for s in phs:
        s.role, s.idx = None, None

    forced_title = [s for s in phs if s.forced and s.ov_type in TITLE_TYPES]
    title: Optional[Shape] = forced_title[0] if forced_title else None
    if title is None:
        existing = [s for s in phs if s.ph_type in TITLE_TYPES and s.ov_type is None]
        if existing:
            title = existing[0]
    if title is None:
        candidates = [s for s in phs if s.has_text and not s.is_picture and s.ov_type is None
                      and s.ph_type not in SPECIAL_TYPES and s.ph_type not in ("pic", "chart", "tbl", "dgm", "media", "clipArt")]
        if candidates:
            best = max(s.max_font or 0 for s in candidates)
            top = [s for s in candidates if (s.max_font or 0) == best]
            title = sorted(top, key=_reading_key)[0]
    if title is not None:
        title.role = title.ov_type or ("ctrTitle" if title.ph_type == "ctrTitle" else "title")

    idx = 1
    for s in sorted((s for s in phs if s is not title), key=_reading_key):
        ot = s.ov_type
        if ot in TITLE_TYPES:
            ot = "body"
        if s.is_picture:
            s.role = "pic"  # a photo can only become a picture placeholder
        elif ot:
            s.role = ot
        elif s.ph_type in TITLE_TYPES:
            s.role = "body"
        elif s.ph_type is None and s.is_ph:
            s.role = "obj"
        else:
            s.role = s.ph_type or "body"
        s.idx = idx
        idx += 1


def classify_deck(deck: Deck, overrides: Optional[Overrides] = None) -> List[str]:
    """Classify every shape in place. Returns warnings."""
    overrides = overrides or Overrides()
    warnings = []
    used = set()
    slide_area = deck.slide_size[0] * deck.slide_size[1]
    for slide in deck.slides:
        for sh in slide.shapes:
            sh.verdict, sh.reason = rule(sh, slide_area)
            sh.forced = False
            sh.ov_type = None
            ov = overrides.shapes.get(sh.name)
            if ov is None:
                continue
            used.add(sh.name)
            if ov.force:
                if ov.force == PLACEHOLDER and sh.kind not in ("sp", "pic"):
                    warnings.append("slide %d: %r is a %s and cannot become a placeholder; override ignored"
                                    % (slide.index, sh.name, sh.kind))
                elif ov.force == CHROME and sh.kind in ("graphicFrame", "alt", "contentPart"):
                    warnings.append("slide %d: %r is a %s and cannot become chrome; override ignored"
                                    % (slide.index, sh.name, sh.kind))
                else:
                    sh.verdict, sh.reason, sh.forced = ov.force, "override: force %s" % ov.force, True
            if ov.type:
                sh.ov_type = ov.type
                if sh.verdict == PLACEHOLDER:
                    sh.forced = True
        _guards(slide)
        assign_roles(slide)
    for name in overrides.shapes:
        if name not in used:
            warnings.append("override for shape %r matched nothing" % name)
    return warnings
