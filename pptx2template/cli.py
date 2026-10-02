"""Command line entry point."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import Overrides, __version__, analyze, convert
from .cluster import DEFAULT_TOLERANCE

EPILOG = """\
Best results: rename your slides in PowerPoint first (right-click a slide in the
Outline / Slide panel or use the Selection Pane) - custom slide names become
layout names. Run with --dry-run, check the report, then put corrections in
<input>.overrides.yaml next to the deck:

  shapes:
    "Rectangle 13": {force: chrome}          # chrome | placeholder | media
    "Text 7":       {force: placeholder, type: body}
  layouts:
    cluster_3: {name: "Destination"}
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="pptx2template",
        description="Convert an ordinary .pptx deck into a reusable multi-layout .potx template "
                    "(rule-based, offline, no AI).",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", type=Path, help="source .pptx")
    ap.add_argument("-o", "--output", type=Path, help="output .potx (default: <input>.potx)")
    ap.add_argument("--dry-run", action="store_true", help="print the inferred classification and layouts, write nothing")
    ap.add_argument("--overrides", type=Path, help="YAML/JSON overrides (default: <input>.overrides.yaml if present)")
    ap.add_argument("--tolerance", type=int, default=DEFAULT_TOLERANCE,
                    help="position tolerance in EMU when matching slides (default %(default)s = ~1.4 mm)")
    ap.add_argument("--no-slides", action="store_true", help="ship only the layouts, no sample slides")
    ap.add_argument("--no-verify", action="store_true", help="skip the python-pptx round-trip and text checks")
    ap.add_argument("--visual-check", action="store_true",
                    help="also render original and result with LibreOffice and diff them (needs soffice, pdftoppm, Pillow)")
    ap.add_argument("-q", "--quiet", action="store_true", help="do not print the report")
    ap.add_argument("--version", action="version", version="%(prog)s " + __version__)
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # legacy Windows consoles cannot print every glyph in a deck
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    if not args.input.exists():
        ap.error("no such file: %s" % args.input)
    ov_path = args.overrides or Overrides.find_sidecar(args.input)
    overrides = None
    if ov_path:
        try:
            overrides = Overrides.load(ov_path)
        except Exception as exc:
            print("error: cannot read overrides %s: %s" % (ov_path, exc), file=sys.stderr)
            return 2

    if args.dry_run:
        analysis = analyze(args.input, overrides, args.tolerance)
        if ov_path:
            print("Overrides: %s\n" % ov_path)
        print(analysis.report())
        return 0

    out = args.output or args.input.with_suffix(".potx")
    result = convert(args.input, overrides, args.tolerance, keep_slides=not args.no_slides,
                     verify=not args.no_verify)
    if not args.quiet:
        if ov_path:
            print("Overrides: %s\n" % ov_path)
        print(result.analysis.report())
        print()
    for w in result.warnings:
        print("warning: " + w, file=sys.stderr)
    if result.problems:
        print("VERIFICATION FAILED - nothing written:", file=sys.stderr)
        for p in result.problems:
            print("  - " + p, file=sys.stderr)
        return 1
    out.write_bytes(result.data)
    if args.visual_check:
        from .verify import visual_diff
        for w in visual_diff(args.input, result.data):
            print("visual: " + w, file=sys.stderr)
    print("Wrote %s: %d layouts%s" % (out, len(result.layouts),
                                      "" if args.no_slides else ", %d slides" % len(result.analysis.deck.slides)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
