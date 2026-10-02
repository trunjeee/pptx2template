"""Well-formedness check, content-type flip and rezip."""
from __future__ import annotations

import io
import zipfile
from typing import Dict, List
from xml.dom import minidom

from .ooxml import CT_PRES_MAIN, CT_TEMPLATE_MAIN, Package

_MAIN_TYPES = {
    "presentation.main+xml": "template.main+xml",
    "slideshow.main+xml": "template.main+xml",
    "presentation.macroEnabled.main+xml": "template.macroEnabled.main+xml",
    "slideshow.macroEnabled.main+xml": "template.macroEnabled.main+xml",
}


class PackageError(Exception):
    pass


def check_wellformed(parts: Dict[str, bytes]) -> List[str]:
    errors = []
    for name, data in parts.items():
        if name.endswith(".xml") or name.endswith(".rels"):
            try:
                minidom.parseString(data)
            except Exception as exc:  # expat raises several types
                errors.append("%s: %s" % (name, exc))
    return errors


def set_main_type(pkg: Package, template: bool) -> None:
    main = pkg.main_part()
    ct = pkg.content_type(main) or CT_PRES_MAIN
    prefix, _, suffix = ct.rpartition("presentationml.")
    if template:
        suffix = _MAIN_TYPES.get(suffix, suffix)
    else:
        suffix = {v: k for k, v in _MAIN_TYPES.items() if k.startswith("presentation")}.get(suffix, suffix)
    pkg.set_override(main, prefix + "presentationml." + suffix)


def to_bytes(parts: Dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        order = ["[Content_Types].xml", "_rels/.rels"]
        for name in order + sorted(n for n in parts if n not in order):
            if name in parts:
                z.writestr(name, parts[name])
    return buf.getvalue()


def finalize(pkg: Package, template: bool = True) -> bytes:
    """Serialize the package; refuses to produce a file with malformed XML."""
    set_main_type(pkg, template)
    parts = pkg.flush()
    errors = check_wellformed(parts)
    if errors:
        raise PackageError("malformed XML in generated package:\n  " + "\n  ".join(errors))
    return to_bytes(parts)


def as_presentation(data: bytes) -> bytes:
    """Same package with the main part typed as a plain presentation (python-pptx can open it)."""
    pkg = Package.open(io.BytesIO(data))
    return finalize(pkg, template=False)
