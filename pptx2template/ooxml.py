"""Low-level OOXML package access: parts, relationships, content types."""
from __future__ import annotations

import posixpath
import zipfile
from typing import Dict, Iterator, List, Optional, Tuple

from lxml import etree

NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "p14": "http://schemas.microsoft.com/office/powerpoint/2010/main",
}

_RT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
RT_OFFICE_DOC = _RT + "officeDocument"
RT_SLIDE = _RT + "slide"
RT_SLIDE_LAYOUT = _RT + "slideLayout"
RT_SLIDE_MASTER = _RT + "slideMaster"

_CT = "application/vnd.openxmlformats-officedocument.presentationml."
CT_SLIDE_LAYOUT = _CT + "slideLayout+xml"
CT_PRES_MAIN = _CT + "presentation.main+xml"
CT_TEMPLATE_MAIN = _CT + "template.main+xml"

R_ATTR_PREFIX = "{%s}" % NS["r"]


def qn(tag: str) -> str:
    prefix, local = tag.split(":")
    return "{%s}%s" % (NS[prefix], local)


def local(tag) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def serialize(root: etree._Element) -> bytes:
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


_PARSER = etree.XMLParser(remove_blank_text=False, resolve_entities=False, huge_tree=True)


def parse(data: bytes) -> etree._Element:
    return etree.fromstring(data, _PARSER)


def rels_name(part: str) -> str:
    d, f = posixpath.split(part)
    return posixpath.join(d, "_rels", f + ".rels")


def resolve(source_part: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    base = posixpath.dirname(source_part)
    return posixpath.normpath(posixpath.join(base, target))


def relativize(source_part: str, target_abs: str) -> str:
    base = posixpath.dirname(source_part) or "."
    return posixpath.relpath(target_abs, base)


class Rels:
    """Relationship set of one part, backed by an XML root kept in the package cache."""

    def __init__(self, source_part: str, root: etree._Element):
        self.source = source_part
        self.root = root

    def __iter__(self) -> Iterator[Tuple[str, str, str, Optional[str]]]:
        for el in self.root.findall(qn("rel:Relationship")):
            yield el.get("Id"), el.get("Type"), el.get("Target"), el.get("TargetMode")

    def _el(self, rid: str):
        for el in self.root.findall(qn("rel:Relationship")):
            if el.get("Id") == rid:
                return el
        return None

    def get(self, rid: str) -> Optional[Tuple[str, str, Optional[str]]]:
        """(type, absolute-target-or-external-url, mode)."""
        el = self._el(rid)
        if el is None:
            return None
        mode = el.get("TargetMode")
        target = el.get("Target")
        if mode != "External":
            target = resolve(self.source, target)
        return el.get("Type"), target, mode

    def by_type(self, rtype: str) -> List[Tuple[str, str]]:
        out = []
        for rid, t, target, mode in self:
            if t == rtype and mode != "External":
                out.append((rid, resolve(self.source, target)))
        return out

    def next_id(self) -> str:
        used = {el.get("Id") for el in self.root.findall(qn("rel:Relationship"))}
        n = 1
        while "rId%d" % n in used:
            n += 1
        return "rId%d" % n

    def add(self, rtype: str, target: str, mode: Optional[str] = None) -> str:
        rid = self.next_id()
        el = etree.SubElement(self.root, qn("rel:Relationship"))
        el.set("Id", rid)
        el.set("Type", rtype)
        el.set("Target", target if mode == "External" else relativize(self.source, target))
        if mode:
            el.set("TargetMode", mode)
        return rid

    def remove(self, rid: str) -> None:
        el = self._el(rid)
        if el is not None:
            self.root.remove(el)

    def set_target(self, rid: str, target_abs: str) -> None:
        el = self._el(rid)
        el.set("Target", relativize(self.source, target_abs))


class Package:
    """An OPC package held fully in memory. Part names have no leading slash."""

    CT_PART = "[Content_Types].xml"

    def __init__(self, parts: Dict[str, bytes]):
        self.parts = dict(parts)
        self._xml: Dict[str, etree._Element] = {}

    @classmethod
    def open(cls, path) -> "Package":
        with zipfile.ZipFile(path) as z:
            return cls({i.filename: z.read(i.filename) for i in z.infolist() if not i.is_dir()})

    # -- parts -------------------------------------------------------------
    def has(self, name: str) -> bool:
        return name in self.parts or name in self._xml

    def names(self) -> List[str]:
        return sorted(set(self.parts) | set(self._xml))

    def xml(self, name: str) -> etree._Element:
        if name not in self._xml:
            self._xml[name] = parse(self.parts[name])
        return self._xml[name]

    def set_xml(self, name: str, root: etree._Element) -> None:
        self._xml[name] = root
        self.parts[name] = b""  # placeholder; serialized on flush

    def blob(self, name: str) -> bytes:
        if name in self._xml:
            return serialize(self._xml[name])
        return self.parts[name]

    def delete(self, name: str) -> None:
        self.parts.pop(name, None)
        self._xml.pop(name, None)

    def flush(self) -> Dict[str, bytes]:
        out = {}
        for name in self.names():
            out[name] = self.blob(name)
        return out

    # -- relationships -----------------------------------------------------
    def rels(self, part: str) -> Rels:
        name = rels_name(part) if part else "_rels/.rels"
        if not self.has(name):
            root = etree.Element(qn("rel:Relationships"), nsmap={None: NS["rel"]})
            self.set_xml(name, root)
        return Rels(part, self.xml(name))

    def main_part(self) -> str:
        return self.rels("").by_type(RT_OFFICE_DOC)[0][1]

    # -- content types -----------------------------------------------------
    def _ct(self) -> etree._Element:
        return self.xml(self.CT_PART)

    def content_type(self, part: str) -> Optional[str]:
        for el in self._ct().findall(qn("ct:Override")):
            if el.get("PartName").lstrip("/") == part:
                return el.get("ContentType")
        ext = part.rsplit(".", 1)[-1].lower()
        for el in self._ct().findall(qn("ct:Default")):
            if el.get("Extension").lower() == ext:
                return el.get("ContentType")
        return None

    def set_override(self, part: str, ctype: str) -> None:
        self.remove_override(part)
        el = etree.SubElement(self._ct(), qn("ct:Override"))
        el.set("PartName", "/" + part)
        el.set("ContentType", ctype)

    def remove_override(self, part: str) -> None:
        for el in self._ct().findall(qn("ct:Override")):
            if el.get("PartName").lstrip("/") == part:
                self._ct().remove(el)

    def prune_overrides(self) -> None:
        present = set(self.names())
        for el in self._ct().findall(qn("ct:Override")):
            if el.get("PartName").lstrip("/") not in present:
                self._ct().remove(el)

    # -- garbage collection ------------------------------------------------
    def collect_garbage(self) -> List[str]:
        """Drop every part not reachable through relationships from the package root."""
        reachable = {self.CT_PART, "_rels/.rels"}
        stack = [""]
        seen = set()
        while stack:
            part = stack.pop()
            if part in seen:
                continue
            seen.add(part)
            rname = rels_name(part) if part else "_rels/.rels"
            if not self.has(rname):
                continue
            reachable.add(rname)
            for rid, t, target, mode in self.rels(part):
                if mode == "External":
                    continue
                tgt = resolve(part, target)
                if self.has(tgt):
                    reachable.add(tgt)
                    stack.append(tgt)
        dropped = [n for n in self.names() if n not in reachable]
        for n in dropped:
            self.delete(n)
        self.prune_overrides()
        return dropped
