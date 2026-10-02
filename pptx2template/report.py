"""Human-readable dry-run / build report."""
from __future__ import annotations

from typing import List

from .classify import CHROME, MEDIA, PLACEHOLDER
from .cluster import Cluster
from .parser import Deck

EMU_PER_CM = 360000
_MARK = {PLACEHOLDER: "PH ", MEDIA: "MED", CHROME: "CHR"}


def _box(b) -> str:
    if not b:
        return "(no position)"
    return "%5.1f,%5.1f %5.1fx%-5.1f cm" % tuple(v / EMU_PER_CM for v in b)


def _short(text: str, n: int = 38) -> str:
    text = text.replace("\n", " / ").replace("\xa0", " ").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def format_report(deck: Deck, clusters: List[Cluster], warnings: List[str]) -> str:
    lines = []
    by_slide = {}
    for cl in clusters:
        for s in cl.slides:
            by_slide[s.index] = cl
    lines.append("SHAPES  (PH = placeholder, MED = stays on slide as content, CHR = chrome baked into layout)")
    for slide in deck.slides:
        cl = by_slide[slide.index]
        rep = " (representative)" if cl.rep is slide else ""
        name = " %r" % slide.custom_name if slide.custom_name else ""
        lines.append("")
        lines.append("Slide %d%s -> %s %r%s" % (slide.index, name, cl.id, cl.name, rep))
        for sh in slide.shapes:
            role = ""
            if sh.verdict == PLACEHOLDER:
                role = sh.role if sh.idx is None else "%s idx=%d" % (sh.role, sh.idx)
            lines.append("  %s %-22s %-12s %-15s %s  %s%s" % (
                _MARK[sh.verdict], _short(sh.label(), 22), sh.kind, role, _box(sh.bbox), sh.reason,
                ("  \"%s\"" % _short(sh.text)) if sh.text.strip() else ""))
        if not slide.shapes:
            lines.append("  (no shapes)")
    lines.append("")
    lines.append("LAYOUTS")
    for cl in clusters:
        phs = [s for s in cl.rep.shapes if s.verdict == PLACEHOLDER]
        chrome = [s for s in cl.rep.shapes if s.verdict == CHROME]
        media = [s for s in cl.rep.shapes if s.verdict == MEDIA]
        lines.append("  %-10s %-30s slides %-12s built from slide %d  (%d placeholders, %d chrome, %d media)  [name from %s]"
                     % (cl.id, repr(cl.name), ",".join(str(s.index) for s in cl.slides), cl.rep.index,
                        len(phs), len(chrome), len(media), cl.name_source))
    if not any(s.custom_name for s in deck.slides):
        lines.append("")
        lines.append("Tip: no slide has a custom name. Rename slides in PowerPoint (or set layouts.cluster_N.name"
                     " in the overrides file) to get meaningful layout names.")
    if warnings:
        lines.append("")
        lines.append("WARNINGS")
        for w in warnings:
            lines.append("  - " + w)
    return "\n".join(lines)
