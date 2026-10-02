"""Local web UI: a tiny stdlib HTTP server on 127.0.0.1 that the browser talks to.

Nothing leaves the machine. Works the same on Windows, macOS and Linux.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, quote, unquote, urlparse

from .. import __version__, analyze, convert
from ..classify import Overrides, ShapeOverride, VERDICTS, PH_TYPES
from ..cluster import DEFAULT_TOLERANCE
from .model import build_model

STATIC = Path(__file__).with_name("static")
IDLE_EXIT = 90  # seconds without a browser heartbeat before a windowless build exits


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.workdir = Path(tempfile.mkdtemp(prefix="pptx2template-"))
        self.deck: Optional[Path] = None       # file being analysed
        self.filename = ""
        self.sidecar: Optional[Path] = None    # where overrides are saved automatically
        self.overrides = Overrides()
        self.tolerance = DEFAULT_TOLERANCE
        self.analysis = None
        self.result: Optional[bytes] = None
        self.last_ping = time.time()

    # .................................................................. actions
    def open_path(self, path: Path, writable_sidecar: bool = True) -> None:
        self.deck = path
        self.filename = path.name
        found = Overrides.find_sidecar(path)
        self.overrides = Overrides.load(found) if found else Overrides()
        self.sidecar = (found or path.with_name(path.stem + ".overrides.yaml")) if writable_sidecar else None
        self.result = None
        self.refresh()

    def open_upload(self, filename: str, data: bytes) -> None:
        safe = "".join(c for c in Path(filename).name if c not in '<>:"/\\|?*') or "deck.pptx"
        if not safe.lower().endswith((".pptx", ".pptm", ".ppsx", ".potx")):
            raise ValueError("Нужен файл PowerPoint (.pptx)")
        target = self.workdir / safe
        target.write_bytes(data)
        self.open_path(target, writable_sidecar=False)

    def refresh(self) -> None:
        self.analysis = analyze(self.deck, self.overrides, self.tolerance)

    def save_overrides(self) -> None:
        if not self.sidecar:
            return
        if self.overrides.to_dict():
            self.overrides.save(self.sidecar)
        elif self.sidecar.exists():
            self.sidecar.unlink()

    def model(self) -> dict:
        if self.analysis is None:
            return {"loaded": False, "version": __version__}
        m = build_model(self.analysis, self.overrides)
        m.update({"version": __version__, "filename": self.filename, "tolerance": self.tolerance,
                  "sidecar": str(self.sidecar) if self.sidecar else None,
                  "path": str(self.deck) if self.sidecar else None})
        return m


STATE = State()


class Handler(BaseHTTPRequestHandler):
    server_version = "pptx2template/" + __version__

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    # .................................................................. plumbing
    def _send(self, code: int, body: bytes, ctype: str, extra: Optional[dict] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _same_origin(self) -> bool:
        """Block other web pages from driving the local server (CSRF / DNS rebinding)."""
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            return False
        origin = self.headers.get("Origin")
        return origin is None or urlparse(origin).hostname in ("127.0.0.1", "localhost")

    # .................................................................. routes
    def do_GET(self):
        if not self._same_origin():
            return self._send(403, b"forbidden", "text/plain")
        url = urlparse(self.path)
        try:
            if url.path in ("/", "/index.html"):
                return self._static("index.html")
            if url.path.startswith("/static/"):
                return self._static(url.path[len("/static/"):])
            if url.path == "/api/state":
                with STATE.lock:
                    return self._json(STATE.model())
            if url.path == "/api/media":
                return self._media(parse_qs(url.query).get("part", [""])[0])
            if url.path == "/api/download":
                if STATE.result is None:
                    return self._send(404, b"build first", "text/plain")
                name = Path(STATE.filename).stem + ".potx"
                return self._send(200, STATE.result, "application/vnd.openxmlformats-officedocument.presentationml.template",
                                  {"Content-Disposition": "attachment; filename*=UTF-8''" + quote(name)})
            if url.path == "/api/overrides.yaml":
                name = Path(STATE.filename).stem + ".overrides.yaml"
                return self._send(200, STATE.overrides.to_yaml().encode("utf-8"), "text/yaml; charset=utf-8",
                                  {"Content-Disposition": "attachment; filename*=UTF-8''" + quote(name)})
            self._send(404, b"not found", "text/plain")
        except Exception as exc:
            traceback.print_exc()
            self._json({"error": str(exc)}, 500)

    def do_POST(self):
        if not self._same_origin():
            return self._send(403, b"forbidden", "text/plain")
        path = urlparse(self.path).path
        try:
            if path == "/api/ping":
                STATE.last_ping = time.time()
                return self._json({"ok": True})
            if path == "/api/quit":
                self._json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if path == "/api/open":
                name = unquote(self.headers.get("X-Filename") or "deck.pptx")
                data = self._body()
                with STATE.lock:
                    STATE.open_upload(name, data)
                    return self._json(STATE.model())
            body = json.loads(self._body() or b"{}")
            with STATE.lock:
                if STATE.analysis is None:
                    return self._json({"error": "Сначала откройте презентацию"}, 400)
                if path == "/api/override":
                    self._override(body)
                elif path == "/api/layout-name":
                    cid, name = body.get("cluster"), (body.get("name") or "").strip()
                    if name:
                        STATE.overrides.layouts[cid] = name
                    else:
                        STATE.overrides.layouts.pop(cid, None)
                elif path == "/api/tolerance":
                    STATE.tolerance = max(0, int(body.get("value", DEFAULT_TOLERANCE)))
                elif path == "/api/reset":
                    STATE.overrides = Overrides()
                elif path == "/api/build":
                    return self._build(bool(body.get("keep_slides", True)))
                else:
                    return self._send(404, b"not found", "text/plain")
                STATE.save_overrides()
                STATE.refresh()
                STATE.result = None
                return self._json(STATE.model())
        except Exception as exc:
            traceback.print_exc()
            self._json({"error": str(exc)}, 500)

    def _override(self, body: dict) -> None:
        name = body.get("shape")
        force = body.get("force") or None
        ptype = body.get("type") or None
        if force is not None and force not in VERDICTS:
            raise ValueError("bad force %r" % force)
        if ptype is not None and ptype not in PH_TYPES:
            raise ValueError("bad type %r" % ptype)
        if force is None and ptype is None:
            STATE.overrides.shapes.pop(name, None)
        else:
            STATE.overrides.shapes[name] = ShapeOverride(force=force, type=ptype)

    def _build(self, keep_slides: bool) -> None:
        result = convert(STATE.deck, STATE.overrides, STATE.tolerance, keep_slides=keep_slides)
        ok = not result.problems
        STATE.result = result.data if ok else None
        self._json({"ok": ok, "problems": result.problems, "warnings": result.warnings,
                    "layouts": len(result.layouts), "filename": Path(STATE.filename).stem + ".potx"})

    def _static(self, rel: str) -> None:
        target = (STATIC / rel).resolve()
        if STATIC.resolve() not in target.parents or not target.is_file():
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)

    def _media(self, part: str) -> None:
        with STATE.lock:
            pkg = STATE.analysis.deck.pkg if STATE.analysis else None
            if pkg is None or not part.startswith("ppt/media/") or not pkg.has(part):
                return self._send(404, b"not found", "text/plain")
            data = pkg.blob(part)
        ctype = mimetypes.guess_type(part)[0] or "application/octet-stream"
        self._send(200, data, ctype, {"Cache-Control": "max-age=3600"})


def _watchdog(server: ThreadingHTTPServer) -> None:
    """Without a console window the only way to stop is the browser: exit once it is gone."""
    while True:
        time.sleep(10)
        if time.time() - STATE.last_ping > IDLE_EXIT:
            server.shutdown()
            return


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pptx2template-ui", description="Open the pptx2template web interface.")
    ap.add_argument("input", nargs="?", type=Path, help="deck to open right away (optional)")
    ap.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: any free port)")
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    ap.add_argument("--exit-when-closed", action="store_true",
                    help="stop the server when the browser tab has been closed for a while")
    args = ap.parse_args(argv)

    mimetypes.add_type("text/javascript", ".js")
    mimetypes.add_type("text/css", ".css")
    mimetypes.add_type("image/svg+xml", ".svg")

    if args.input:
        try:
            STATE.open_path(args.input.resolve())
        except Exception as exc:
            print("Не удалось открыть %s: %s" % (args.input, exc), file=sys.stderr)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = "http://127.0.0.1:%d/" % server.server_address[1]
    print("pptx2template %s: интерфейс открыт на %s  (Ctrl+C, чтобы выйти)" % (__version__, url))
    if args.exit_when_closed or getattr(sys, "frozen", False) and sys.stdout is None:
        threading.Thread(target=_watchdog, args=(server,), daemon=True).start()
    if not args.no_browser:
        threading.Timer(0.3, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        shutil.rmtree(STATE.workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
