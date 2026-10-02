"use strict";

const $ = (id) => document.getElementById(id);
const SVGNS = "http://www.w3.org/2000/svg";
const XHTML = "http://www.w3.org/1999/xhtml";
const EMU_PER_MM = 36000;

let model = { loaded: false };
let current = 1;          // slide index (1-based)
let selected = null;      // shape pos on the current slide
let clipSeq = 0;

// ------------------------------------------------------------------ api
async function api(path, body, raw) {
  const opts = { method: body === undefined ? "GET" : "POST", headers: {} };
  if (raw) { opts.body = raw.data; opts.headers["X-Filename"] = encodeURIComponent(raw.name); }
  else if (body !== undefined) { opts.body = JSON.stringify(body); opts.headers["Content-Type"] = "application/json"; }
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({ error: res.statusText }));
  if (!res.ok || data.error) throw new Error(data.error || res.statusText);
  return data;
}

async function busy(text, fn) {
  $("busyText").textContent = text;
  const t = setTimeout(() => $("busy").classList.remove("hidden"), 150);
  try { return await fn(); }
  catch (e) { toast(e.message, true); }
  finally { clearTimeout(t); $("busy").classList.add("hidden"); }
}

function toast(msg, err) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast" + (err ? " err" : "");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.add("hidden"), err ? 6000 : 2500);
}

async function update(path, body) {
  const m = await busy("Пересчитываю…", () => api(path, body));
  if (m) setModel(m);
}

// ------------------------------------------------------------------ vocabulary
function cat(sh) {
  if (sh.verdict === "placeholder") return sh.picture || sh.role === "pic" ? "pic" : "ph";
  return sh.verdict === "chrome" ? "chrome" : "media";
}
const CAT_NAME = { ph: "Текст-плейсхолдер", pic: "Фото-плейсхолдер", chrome: "Декор лейаута", media: "Контент слайда" };
const ROLE_NAME = { title: "заголовок", ctrTitle: "заголовок", subTitle: "подзаголовок", body: "текст", obj: "контент",
  pic: "фото", dt: "дата", ftr: "нижний колонтитул", sldNum: "номер слайда" };
const KIND_NAME = { sp: "фигура", pic: "картинка", grpSp: "группа", graphicFrame: "таблица / диаграмма",
  cxnSp: "линия", alt: "встроенный объект", contentPart: "рукописный ввод" };
const SOURCE_NAME = { "slide name": "из имени слайда", "shape mix": "по составу фигур", override: "задано вручную" };

function tagText(sh) {
  const c = cat(sh);
  if (c === "chrome") return "декор";
  if (c === "media") return "контент";
  const r = ROLE_NAME[sh.role] || sh.role;
  return sh.idx ? `${r} ${sh.idx}` : r;
}

const REASONS = [
  [/^rule 1: has text/, "Есть текст → плейсхолдер (текст важнее заливки)"],
  [/^rule 1: group with editable text/, "Группа с текстом: группа не может быть плейсхолдером, остаётся на слайде"],
  [/^rule 2: video\/audio/, "Видео или аудио: остаётся на слайде"],
  [/^rule 2: small picture/, "Маленькая картинка (логотип/иконка): статичная графика"],
  [/^rule 2: picture/, "Картинка → фото-плейсхолдер той же формы"],
  [/^rule 3: picture fill/, "Фото внутри фигуры → фото-плейсхолдер, обрезанный по форме фигуры"],
  [/^rule 4/, "Заливка без текста → декор лейаута"],
  [/^rule 5/, "Ни заливки, ни текста (разделитель, значок) → декор"],
  [/^rule 6/, "Группа без текста (логотип) → декор целиком"],
  [/^empty placeholder/, "Пустой плейсхолдер из исходного лейаута"],
  [/frame \(cannot be a placeholder\)/, "Таблица/диаграмма не может быть плейсхолдером: остаётся на слайде"],
  [/^connector line/, "Линия → декор"],
  [/animated on this slide/, "Декор анимирован на слайде, поэтому остаётся на слайде"],
  [/kept on slide to preserve stacking/, "Декор лежит поверх фото: оставлен на слайде, чтобы не поменялся порядок слоёв"],
  [/^override/, "Задано вручную"],
  [/^embedded/, "Встроенный объект: остаётся на слайде"],
];
function reasonRu(r) {
  for (const [re, txt] of REASONS) if (re.test(r)) return txt;
  return r;
}

// ------------------------------------------------------------------ svg drawing
function el(tag, attrs, parent) {
  const e = document.createElementNS(SVGNS, tag);
  for (const k in attrs) if (attrs[k] !== null && attrs[k] !== undefined) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}

function geomPath(g, x, y, w, h) {
  const prst = (g && g.prst) || "rect";
  const adj = (g && g.adj) || {};
  const m = Math.min(w, h);
  if (prst === "ellipse") return `M${x + w / 2},${y} a${w / 2},${h / 2} 0 1,0 0.01,0 Z`;
  let tl = 0, tr = 0, br = 0, bl = 0;
  if (prst === "roundRect") tl = tr = br = bl = m * (adj.adj ?? 16667) / 100000;
  else if (prst === "round2SameRect") { tl = tr = m * (adj.adj1 ?? 16667) / 100000; br = bl = m * (adj.adj2 ?? 0) / 100000; }
  else if (prst === "round1Rect") tr = m * (adj.adj ?? 16667) / 100000;
  else if (prst === "round2DiagRect") { tl = br = m * (adj.adj1 ?? 16667) / 100000; tr = bl = m * (adj.adj2 ?? 0) / 100000; }
  return `M${x + tl},${y} H${x + w - tr} Q${x + w},${y} ${x + w},${y + tr} V${y + h - br} Q${x + w},${y + h} ${x + w - br},${y + h}` +
    ` H${x + bl} Q${x},${y + h} ${x},${y + h - bl} V${y + tl} Q${x},${y} ${x + tl},${y} Z`;
}

function rotAttr(it) {
  if (!it.rot) return null;
  const [x, y, w, h] = it.box;
  return `rotate(${it.rot} ${x + w / 2} ${y + h / 2})`;
}

function drawItem(svg, it, defs) {
  const [x, y, w, h] = it.box;
  const g = el("g", { transform: rotAttr(it) }, svg);
  if (it.t === "shape") {
    el("path", { d: geomPath(it.geom, x, y, w, h), fill: it.fill || "none", "fill-opacity": it.opacity,
      stroke: it.stroke || "none", "stroke-width": it.stroke ? 1 : 0 }, g);
  } else if (it.t === "line") {
    el("line", { x1: x, y1: y, x2: x + w, y2: y + h, stroke: it.stroke || "#999", "stroke-width": 1 }, g);
  } else if (it.t === "frame") {
    el("rect", { x, y, width: w, height: h, fill: "#f3f4f6", stroke: "#c4c8cf", "stroke-width": 0.75 }, g);
  } else if (it.t === "img") {
    const id = "clip" + (++clipSeq);
    const cp = el("clipPath", { id }, defs);
    el("path", { d: geomPath(it.geom, x, y, w, h) }, cp);
    const holder = el("g", { "clip-path": `url(#${id})` }, g);
    if (it.url) {
      const [l, t, r, b] = it.crop;
      const vw = Math.max(0.01, 1 - l - r), vh = Math.max(0.01, 1 - t - b);
      const inner = el("svg", { x, y, width: w, height: h, viewBox: `${l} ${t} ${vw} ${vh}`, preserveAspectRatio: "none" }, holder);
      el("image", { href: it.url, x: 0, y: 0, width: 1, height: 1, preserveAspectRatio: "none" }, inner);
    } else {
      el("rect", { x, y, width: w, height: h, fill: "#d9dce2" }, holder);
    }
  } else if (it.t === "text") {
    const fo = el("foreignObject", { x, y, width: Math.max(w, 1), height: Math.max(h, 1) }, g);
    const div = document.createElementNS(XHTML, "div");
    div.className = "tx";
    const justify = { t: "flex-start", ctr: "center", b: "flex-end" }[it.anchor] || "flex-start";
    div.setAttribute("style", `width:100%;height:100%;display:flex;flex-direction:column;justify-content:${justify};` +
      `padding:3.6px 7.2px;box-sizing:border-box;overflow:visible;font-family:system-ui,Segoe UI,Arial,sans-serif;` +
      `line-height:1.15;color:#222;${it.table ? "font-size:9px;opacity:.75;" : ""}`);
    for (const p of it.paras) {
      const pe = document.createElementNS(XHTML, "div");
      pe.textContent = p.text || " ";
      const align = { ctr: "center", r: "right", just: "justify" }[p.algn] || "left";
      let st = `text-align:${align};white-space:${it.nowrap ? "pre" : "pre-wrap"};word-break:break-word;`;
      if (p.font) st += `font-family:"${p.font.replace(/"/g, "")}",system-ui,Arial,sans-serif;`;
      if (!it.table) st += `font-size:${p.size || 18}px;`;
      if (p.color) st += `color:${p.color};`;
      if (p.bold) st += "font-weight:700;";
      pe.setAttribute("style", st);
      div.appendChild(pe);
    }
    fo.appendChild(div);
  }
}

function slideSvg(slide, opts) {
  const W = model.width, H = model.height;
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, width: opts.width || W, height: opts.width ? opts.width * H / W : H });
  const defs = el("defs", {}, svg);
  const bg = slide.bg || {};
  el("rect", { x: 0, y: 0, width: W, height: H, fill: bg.color || "#fff" }, svg);
  if (bg.image) el("image", { href: bg.image, x: 0, y: 0, width: W, height: H, preserveAspectRatio: "none" }, svg);
  for (const it of slide.inherited || []) drawItem(svg, it, defs);
  for (const sh of slide.shapes) for (const it of sh.draw) drawItem(svg, it, defs);
  if (opts.overlay) drawOverlays(svg, slide);
  return svg;
}

function drawOverlays(svg, slide) {
  const layer = el("g", {}, svg);
  // later shapes sit on top; draw small shapes last so they stay clickable
  const order = slide.shapes.filter((s) => s.bbox)
    .sort((a, b) => b.bbox[2] * b.bbox[3] - a.bbox[2] * a.bbox[3]);
  for (const sh of order) {
    const [x, y, w, h] = sh.bbox;
    const c = cat(sh);
    const g = el("g", { class: `ov v-${c}${sh.pos === selected ? " sel" : ""}${sh.override ? " forced" : ""}` }, layer);
    el("rect", { class: "box", x, y, width: Math.max(w, 2), height: Math.max(h, 2), rx: 2 }, g);
    const label = tagText(sh) + (sh.override ? " ✎" : "");
    const fs = model.width / 58;   // readable on screen whatever the slide size
    const tw = label.length * fs * 0.58 + fs * 0.8, th = fs * 1.35;
    const ty = y - th >= 0 ? y - th : y;
    const tag = el("g", { class: "tag" }, g);
    el("rect", { x, y: ty, width: tw, height: th, rx: fs * 0.25 }, tag);
    const t = el("text", { x: x + fs * 0.4, y: ty + fs * 1.0, "font-size": fs,
      "font-family": "Segoe UI, system-ui, -apple-system, Roboto, Arial, sans-serif" }, tag);
    t.textContent = label;
    g.addEventListener("click", (e) => { e.stopPropagation(); select(sh.pos); });
    const title = el("title", {}, g);
    title.textContent = `${sh.label} — ${CAT_NAME[c]}`;
  }
  svg.addEventListener("click", () => select(null));
}

// ------------------------------------------------------------------ rendering
function slideByIndex(i) { return model.slides.find((s) => s.index === i); }
function clusterOf(slide) { return model.clusters.find((c) => c.id === slide.cluster); }

function setModel(m) {
  model = m;
  if (!m.loaded) {
    $("app").classList.add("hidden");
    $("empty").classList.remove("hidden");
    $("fileName").textContent = "";
    for (const id of ["buildBtn", "resetBtn", "yamlBtn", "tolWrap", "keepWrap"]) $(id).classList.add("hidden");
    return;
  }
  $("empty").classList.add("hidden");
  $("app").classList.remove("hidden");
  for (const id of ["buildBtn", "resetBtn", "yamlBtn", "tolWrap", "keepWrap"]) $(id).classList.remove("hidden");
  $("fileName").textContent = m.filename + (m.sidecar ? "  ·  правки сохраняются рядом с файлом" : "");
  $("fileName").title = m.sidecar ? `Правки: ${m.sidecar}` : "";
  $("tolerance").value = (m.tolerance / EMU_PER_MM).toFixed(1);
  if (!slideByIndex(current)) current = 1;
  renderLayouts();
  renderStage();
  renderInspector();
}

function renderLayouts() {
  const list = $("layoutList");
  list.textContent = "";
  $("layoutCount").textContent = model.clusters.length;
  const cur = slideByIndex(current);
  for (const c of model.clusters) {
    const card = document.createElement("div");
    card.className = "cluster" + (cur && cur.cluster === c.id ? " active" : "");
    const input = document.createElement("input");
    input.className = "name";
    input.value = c.name;
    input.title = "Название лейаута — можно изменить";
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") input.blur(); if (e.key === "Escape") { input.value = c.name; input.blur(); } });
    input.addEventListener("change", () => update("/api/layout-name", { cluster: c.id, name: input.value }));
    card.appendChild(input);
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = `${c.id} · ${c.slides.length} ${plural(c.slides.length, "слайд", "слайда", "слайдов")} · ${SOURCE_NAME[c.source] || c.source}`;
    card.appendChild(meta);
    const thumbs = document.createElement("div");
    thumbs.className = "thumbs";
    for (const i of c.slides) {
      const s = slideByIndex(i);
      const t = document.createElement("div");
      t.className = "thumb" + (i === current ? " active" : "");
      t.appendChild(slideSvg(s, { width: 240 }));
      t.insertAdjacentHTML("beforeend", `<span class="num">${i}</span>` + (i === c.rep ? `<span class="rep" title="Лейаут строится из этого слайда">основа</span>` : ""));
      t.addEventListener("click", () => goto(i));
      thumbs.appendChild(t);
    }
    card.appendChild(thumbs);
    list.appendChild(card);
  }
  const w = $("warnings");
  w.textContent = "";
  for (const msg of model.warnings || []) {
    const d = document.createElement("div");
    d.className = "warn";
    d.textContent = "⚠ " + msg;
    w.appendChild(d);
  }
}

function renderStage() {
  const s = slideByIndex(current);
  const c = clusterOf(s);
  $("slideTitle").textContent = `Слайд ${s.index} из ${model.slides.length}` + (s.name ? ` · «${s.name}»` : "") + `  →  ${c.name}`;
  const canvas = $("canvas");
  canvas.textContent = "";
  canvas.appendChild(slideSvg(s, { overlay: true }));
  canvas.classList.toggle("hideov", !$("overlayToggle").checked);
}

function renderInspector() {
  const box = $("inspector");
  box.textContent = "";
  const s = slideByIndex(current);
  const sh = s.shapes.find((x) => x.pos === selected);
  if (!sh) return renderShapeList(box, s);

  const c = cat(sh);
  box.insertAdjacentHTML("beforeend", `<div class="shapehead"><span class="dot ${c}"></span><h3></h3></div>`);
  box.querySelector("h3").textContent = sh.label;
  const kv = document.createElement("dl");
  kv.className = "kv";
  const rows = [["Сейчас", CAT_NAME[c] + (c === "ph" || c === "pic" ? ` (${tagText(sh)})` : "")],
    ["Тип", KIND_NAME[sh.kind] || sh.kind], ["Почему", reasonRu(sh.reason)]];
  for (const [k, v] of rows) {
    const dt = document.createElement("dt"); dt.textContent = k;
    const dd = document.createElement("dd"); dd.textContent = v;
    kv.append(dt, dd);
  }
  box.appendChild(kv);
  if (sh.text && sh.text.trim()) {
    const q = document.createElement("div");
    q.className = "quote";
    q.textContent = sh.text;
    box.appendChild(q);
  }

  const h = document.createElement("h2");
  h.textContent = "Чем сделать";
  box.appendChild(h);
  const ov = sh.override || {};
  const options = choicesFor(sh);
  const wrap = document.createElement("div");
  wrap.className = "choices";
  for (const o of options) {
    const b = document.createElement("button");
    b.className = "choice" + ((ov.force || null) === o.force ? " on" : "");
    b.innerHTML = `<b></b><span></span>`;
    b.querySelector("b").textContent = o.title;
    b.querySelector("span").textContent = o.hint;
    b.addEventListener("click", () => update("/api/override", { shape: sh.name, force: o.force, type: o.force === "placeholder" ? ov.type || null : null }));
    wrap.appendChild(b);
  }
  box.appendChild(wrap);

  if (c === "ph" && sh.kind === "sp") {
    const row = document.createElement("label");
    row.className = "typesel";
    row.innerHTML = `Роль текста <select>
      <option value="">автоматически</option><option value="title">заголовок</option>
      <option value="body">текст</option><option value="subTitle">подзаголовок</option></select>`;
    const sel = row.querySelector("select");
    sel.value = ov.type || "";
    sel.addEventListener("change", () => update("/api/override", { shape: sh.name, force: ov.force || null, type: sel.value || null }));
    box.appendChild(row);
  }

  const note = document.createElement("div");
  note.className = "note";
  note.textContent = sh.same_name > 1
    ? `Правка применяется ко всем фигурам с именем «${sh.name}» во всей презентации (${sh.same_name} шт.). Чтобы править по отдельности, дайте фигурам разные имена в «Области выделения» PowerPoint.`
    : `Правка привязана к имени фигуры «${sh.name}».`;
  box.appendChild(note);
  const back = document.createElement("button");
  back.className = "btn ghost";
  back.style.marginTop = "10px";
  back.textContent = "← Все фигуры слайда";
  back.addEventListener("click", () => select(null));
  box.appendChild(back);
}

function choicesFor(sh) {
  const auto = { force: null, title: "Автоматически", hint: "Как решили правила" };
  if (sh.kind === "graphicFrame" || sh.kind === "alt" || sh.kind === "contentPart")
    return [auto, { force: "media", title: "Контент слайда", hint: "Таблицы и диаграммы всегда остаются на слайде" }];
  const chrome = { force: "chrome", title: "Декор лейаута", hint: "Статичная графика, одинаковая на всех слайдах лейаута" };
  if (sh.picture)
    return [auto,
      { force: "placeholder", title: "Фото-плейсхолдер", hint: "Можно вставить своё фото, оно обрежется по форме" },
      chrome,
      { force: "media", title: "Картинка на слайде", hint: "Остаётся обычной картинкой, в лейаут не попадает" }];
  if (sh.kind === "grpSp")
    return [auto, chrome, { force: "media", title: "Контент слайда", hint: "Группа остаётся на слайде" }];
  return [auto,
    { force: "placeholder", title: "Текст-плейсхолдер", hint: "Поле для ввода текста с сохранённым оформлением" },
    chrome,
    { force: "media", title: "Контент слайда", hint: "Остаётся на слайде как есть, в лейаут не попадает" }];
}

function renderShapeList(box, s) {
  box.insertAdjacentHTML("beforeend", `<h2>Фигуры слайда ${s.index}</h2>`);
  const ul = document.createElement("ul");
  ul.className = "shapelist";
  for (const sh of [...s.shapes].reverse()) {
    const li = document.createElement("li");
    li.innerHTML = `<span class="dot ${cat(sh)}"></span><span class="nm"></span><span class="rl"></span>`;
    li.querySelector(".nm").textContent = sh.label + (sh.override ? " ✎" : "");
    li.querySelector(".rl").textContent = tagText(sh);
    li.title = reasonRu(sh.reason);
    li.addEventListener("click", () => select(sh.pos));
    ul.appendChild(li);
  }
  box.appendChild(ul);
  box.insertAdjacentHTML("beforeend", `<div class="note">Кликните фигуру на слайде или в списке, чтобы поменять её роль. Пунктирная рамка и ✎ — правка вручную.</div>`);
}

function plural(n, one, few, many) {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
  return many;
}

// ------------------------------------------------------------------ navigation
function goto(i) {
  if (!slideByIndex(i)) return;
  current = i;
  selected = null;
  renderLayouts();
  renderStage();
  renderInspector();
}

function select(pos) {
  selected = pos;
  renderStage();
  renderInspector();
}

document.addEventListener("keydown", (e) => {
  if (!model.loaded || e.target.closest("input, select, textarea")) return;
  if (e.key === "ArrowRight" || e.key === "PageDown") goto(current + 1);
  else if (e.key === "ArrowLeft" || e.key === "PageUp") goto(current - 1);
  else if (e.key === "Escape") select(null);
});

// ------------------------------------------------------------------ files
async function openFile(file) {
  if (!file) return;
  if (!/\.(pptx|pptm|ppsx|potx)$/i.test(file.name)) return toast("Нужен файл PowerPoint (.pptx)", true);
  const data = await file.arrayBuffer();
  const m = await busy("Анализирую презентацию…", () => api("/api/open", null, { name: file.name, data }));
  if (m) { current = 1; selected = null; setModel(m); }
}

$("openBtn").addEventListener("click", () => $("fileInput").click());
$("pickBtn").addEventListener("click", () => $("fileInput").click());
$("fileInput").addEventListener("change", (e) => { openFile(e.target.files[0]); e.target.value = ""; });

let dragDepth = 0;
window.addEventListener("dragenter", (e) => { e.preventDefault(); dragDepth++; $("dropOverlay").classList.remove("hidden"); });
window.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; $("dropOverlay").classList.add("hidden"); } });
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => {
  e.preventDefault();
  dragDepth = 0;
  $("dropOverlay").classList.add("hidden");
  openFile(e.dataTransfer.files[0]);
});

// ------------------------------------------------------------------ toolbar
$("overlayToggle").addEventListener("change", () => $("canvas").classList.toggle("hideov", !$("overlayToggle").checked));
$("tolerance").addEventListener("change", () => {
  const mm = parseFloat($("tolerance").value);
  if (!isNaN(mm) && mm >= 0) update("/api/tolerance", { value: Math.round(mm * EMU_PER_MM) });
});
$("resetBtn").addEventListener("click", () => {
  if (confirm("Убрать все ручные правки фигур и названий лейаутов?")) update("/api/reset", {});
});
$("buildBtn").addEventListener("click", async () => {
  const r = await busy("Собираю шаблон и проверяю…", () => api("/api/build", { keep_slides: $("keepSlides").checked }));
  if (!r) return;
  const body = $("dialogBody");
  body.textContent = "";
  if (r.ok) {
    body.insertAdjacentHTML("beforeend", `<h3>Шаблон готов ✓</h3><p></p><p><a class="btn primary" href="/api/download">Скачать ещё раз</a></p>`);
    body.querySelector("p").textContent = `${r.filename}: ${r.layouts} ${plural(r.layouts, "лейаут", "лейаута", "лейаутов")}. Все проверки пройдены. Файл скачивается.`;
    const a = document.createElement("a");
    a.href = "/api/download";
    a.download = r.filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
  } else {
    body.insertAdjacentHTML("beforeend", `<h3>Проверка не пройдена</h3><p>Файл не записан. Что не так:</p><ul></ul>`);
    const ul = body.querySelector("ul");
    for (const p of r.problems) { const li = document.createElement("li"); li.textContent = p; ul.appendChild(li); }
  }
  for (const w of r.warnings || []) body.insertAdjacentHTML("beforeend", `<p class="muted"></p>`), body.lastChild.textContent = "⚠ " + w;
  $("dialog").showModal();
});
$("quitBtn").addEventListener("click", async () => {
  if (!confirm("Закрыть pptx2template?")) return;
  await api("/api/quit", {}).catch(() => {});
  document.body.innerHTML = `<div class="empty"><div class="drop"><h1>Приложение закрыто</h1><p class="muted">Эту вкладку можно закрыть.</p></div></div>`;
});

// heartbeat: lets a windowless build exit once the tab is gone
setInterval(() => api("/api/ping", {}).catch(() => {}), 15000);
api("/api/ping", {}).catch(() => {});

busy("Загрузка…", () => api("/api/state")).then((m) => {
  if (!m) return;
  const want = /slide=(\d+)/.exec(location.hash);
  if (want) current = +want[1];
  setModel(m);
});
