"""pptx2template: turn an ordinary .pptx deck into a multi-layout .potx template, no AI involved."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Union

from .build import BuildResult, LayoutPlan, build
from .classify import Overrides, classify_deck
from .cluster import DEFAULT_TOLERANCE, Cluster, cluster_slides, name_clusters
from .ooxml import Package
from .package import finalize
from .parser import Deck, parse_deck
from .report import format_report

__version__ = "0.1.0"

__all__ = ["analyze", "convert", "Analysis", "ConvertResult", "Overrides", "__version__"]


@dataclass
class Analysis:
    deck: Deck
    clusters: List[Cluster]
    warnings: List[str]

    def report(self) -> str:
        return format_report(self.deck, self.clusters, self.warnings)


@dataclass
class ConvertResult:
    analysis: Analysis
    data: bytes
    layouts: List[LayoutPlan]
    problems: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def analyze(source: Union[str, Path], overrides: Optional[Overrides] = None,
            tolerance: int = DEFAULT_TOLERANCE) -> Analysis:
    deck = parse_deck(Package.open(source))
    if not deck.masters:
        raise ValueError("%s has no slide master" % source)
    warnings = classify_deck(deck, overrides)
    clusters = cluster_slides(deck, tolerance)
    name_clusters(clusters, (overrides or Overrides()).layouts)
    if overrides:
        known = {c.id for c in clusters}
        for cid in overrides.layouts:
            if cid not in known:
                warnings.append("override for layout %r matched no cluster" % cid)
    return Analysis(deck, clusters, warnings)


def convert(source: Union[str, Path], overrides: Optional[Overrides] = None, tolerance: int = DEFAULT_TOLERANCE,
            keep_slides: bool = True, verify: bool = True) -> ConvertResult:
    from . import verify as checks

    analysis = analyze(source, overrides, tolerance)
    texts = checks.deck_texts(analysis.deck)
    result: BuildResult = build(analysis.deck, analysis.clusters, keep_slides=keep_slides)
    data = finalize(result.pkg, template=True)
    problems: List[str] = []
    if verify:
        expect = {}
        if keep_slides:
            for plan in result.layouts:
                for s in plan.cluster.slides:
                    expect[s.index] = plan.name
        problems += checks.check_structure(data, result.layouts, expect)
        if keep_slides:
            problems += checks.check_text(texts, data)
    return ConvertResult(analysis, data, result.layouts, problems, result.warnings)
