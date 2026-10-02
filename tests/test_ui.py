import http.client
import io
import json
import threading
import zipfile
from urllib.parse import quote

import pytest

from pptx2template.ui import server as ui


@pytest.fixture()
def srv():
    ui.STATE.__init__()
    httpd = ui.ThreadingHTTPServer(("127.0.0.1", 0), ui.Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def call(port, method, path, body=None, headers=None, raw=False):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    h = {"Host": "127.0.0.1:%d" % port}
    h.update(headers or {})
    if isinstance(body, (dict, list)):
        body = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    conn.request(method, path, body=body, headers=h)
    r = conn.getresponse()
    data = r.read()
    if raw:
        return r, data
    return r.status, json.loads(data)


def test_static_and_empty_state(srv):
    r, html = call(srv, "GET", "/", raw=True)
    assert r.status == 200 and b"pptx2template" in html
    r, js = call(srv, "GET", "/static/app.js", raw=True)
    assert r.status == 200
    assert call(srv, "GET", "/api/state")[1]["loaded"] is False


def test_rejects_foreign_pages(srv):
    r, _ = call(srv, "GET", "/api/state", headers={"Host": "evil.example"}, raw=True)
    assert r.status == 403
    r, _ = call(srv, "POST", "/api/reset", body={}, headers={"Origin": "https://evil.example"}, raw=True)
    assert r.status == 403
    r, _ = call(srv, "GET", "/static/../server.py", raw=True)
    assert r.status == 404


def test_full_flow(srv, sample):
    status, m = call(srv, "POST", "/api/open", body=sample.read_bytes(),
                     headers={"X-Filename": quote("Мой дек.pptx")})
    assert status == 200 and m["loaded"]
    assert [c["name"] for c in m["clusters"]][:2] == ["Cover", "Destination"]
    slide2 = m["slides"][1]
    photo = [s for s in slide2["shapes"] if s["name"] == "Photo"][0]
    assert photo["verdict"] == "placeholder" and photo["picture"]
    assert any(it["t"] == "img" and it["url"] for it in photo["draw"])
    assert any(it["geom"]["prst"] == "ellipse" for it in photo["draw"])

    # the picture itself is served
    url = [it["url"] for it in photo["draw"] if it["t"] == "img"][0]
    r, img = call(srv, "GET", url, raw=True)
    assert r.status == 200 and img[:4] == b"\x89PNG"

    # turn the photo into decoration and rename a layout
    status, m = call(srv, "POST", "/api/override", body={"shape": "Photo", "force": "chrome"})
    photo = [s for s in m["slides"][1]["shapes"] if s["name"] == "Photo"][0]
    assert photo["verdict"] == "chrome" and photo["override"] == {"force": "chrome", "type": None}
    status, m = call(srv, "POST", "/api/layout-name", body={"cluster": "cluster_2", "name": "Маршрут"})
    assert m["clusters"][1]["name"] == "Маршрут"
    r, yml = call(srv, "GET", "/api/overrides.yaml", raw=True)
    assert "Photo" in yml.decode() and "Маршрут" in yml.decode()

    status, res = call(srv, "POST", "/api/build", body={"keep_slides": True})
    assert res["ok"], res["problems"]
    r, data = call(srv, "GET", "/api/download", raw=True)
    assert r.status == 200 and "attachment" in r.getheader("Content-Disposition")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert "template.main+xml" in z.read("[Content_Types].xml").decode()
        assert "Маршрут" in z.read("ppt/slideLayouts/slideLayout2.xml").decode()

    status, m = call(srv, "POST", "/api/reset", body={})
    assert m["overrides"] == {}


def test_open_path_saves_sidecar(tmp_path, sample):
    deck = tmp_path / "deck.pptx"
    deck.write_bytes(sample.read_bytes())
    ui.STATE.__init__()
    ui.STATE.open_path(deck)
    ui.STATE.overrides.layouts["cluster_1"] = "Обложка"
    ui.STATE.save_overrides()
    assert "Обложка" in (tmp_path / "deck.overrides.yaml").read_text(encoding="utf-8")
    ui.STATE.overrides.layouts.clear()
    ui.STATE.save_overrides()
    assert not (tmp_path / "deck.overrides.yaml").exists()


def test_rejects_non_pptx(srv):
    status, m = call(srv, "POST", "/api/open", body=b"hello", headers={"X-Filename": "notes.txt"})
    assert status == 500 and "pptx" in m["error"]
