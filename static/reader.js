/* ComicPack — 阅读器（原生 JS，无框架）
 *
 * 三种阅读模式：
 *   single  单页翻页
 *   double  双页并排（两页之间留缝，可设置封面单独）
 *   scroll  竖屏连续滚动（条漫/长图）
 */

const $ = (sel) => document.querySelector(sel);

const state = {
  sid: null,
  path: "",
  name: "",
  pages: [],
  index: 0,
  mode: "single",      // single | double | scroll
  coverSolo: true,     // 双页模式下第 1 页单独显示
  fit: "page",         // page | width | actual
  zoom: 1,
  libraryPath: null,
  scrollBuiltFor: null,
  chapters: [],
  chapterIndex: -1,
  sidebarOpen: true,
  wheelAt: 0,
  lastScroll: 0,
};

const ZOOM_STEP = 1.25;
const ZOOM_MIN = 0.1;
const ZOOM_MAX = 8;
const PRELOAD_KEEP = 6;
const SPREAD_GAP = 6;

/* ---------------- 工具 ---------------- */
function humanSize(n) {
  n = Number(n) || 0;
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n.toFixed(0) : n.toFixed(1)) + " " + units[i];
}

function toast(msg, ms = 3200) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove("show"), ms);
}

async function post(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (!res.ok) throw new Error("HTTP " + res.status);
  return res.json();
}

function posKey(path) { return "comicpack.read.pos." + path; }

function pageUrl(i) { return `/api/reader/page/${state.sid}/${i}`; }

function parentOf(p) {
  const norm = String(p || "").replace(/[\\/]+$/, "");
  const i = Math.max(norm.lastIndexOf("\\"), norm.lastIndexOf("/"));
  if (i < 0) return "";
  const parent = norm.slice(0, i);
  return parent.length <= 2 ? (parent ? parent + "\\" : "") : parent;
}

/* ---------------- 打开 ---------------- */
async function openDialog(mode) {
  try {
    const r = await post("/api/reader/browse", { mode, initial: state.path || state.libraryPath || "" });
    if (!r.ok) throw new Error(r.error || "无法打开选择框");
    if (r.path) await openPath(r.path);
  } catch (e) {
    toast(e.message);
  }
}

async function openPath(path, preferImages) {
  setBusy(true, true);
  try {
    const r = await post("/api/reader/open", { path, prefer_images: !!preferImages });
    if (!r.ok) throw new Error(r.error || "打开失败");
    if (r.kind === "library") {
      showLibrary(r);
      return;
    }
    await openDoc(r);
  } catch (e) {
    toast(e.message);
  } finally {
    setBusy(false, true);
  }
}

async function openDoc(r) {
  if (state.sid) post("/api/reader/close/" + state.sid, {}).catch(() => {});

  state.sid = r.id;
  state.path = r.path;
  state.name = r.name;
  state.pages = r.pages;
  state.index = 0;
  state.libraryPath = null;
  state.scrollBuiltFor = null;
  state.chapters = r.chapters || [];
  state.chapterIndex = typeof r.chapter_index === "number" ? r.chapter_index : -1;

  $("#doc-name").textContent = r.name;
  $("#doc-name").title = r.path;
  $("#doc-count").textContent = `共 ${r.page_count} 页`;
  $("#doc-count").classList.remove("hidden");
  $("#page-total").textContent = r.page_count;
  $("#slider").max = String(r.page_count);
  $("#scroll").innerHTML = "";

  document.title = r.name + " · ComicPack";
  localStorage.setItem("comicpack.read.last", r.path);

  // 从地址栏读 page / mode（打开时就取，之后会重写 URL）
  const here = new URLSearchParams(location.search);
  const asked = parseInt(here.get("page") || "", 10);
  const keepMode = here.get("mode");
  const nextUrl = new URLSearchParams({ path: r.path });
  if (keepMode) nextUrl.set("mode", keepMode);
  history.replaceState(null, "", "/reader?" + nextUrl.toString());

  $("#empty").classList.add("hidden");
  $("#library").classList.add("hidden");
  renderView();
  renderChapters();
  updateDocBadge();

  const saved = parseInt(localStorage.getItem(posKey(r.path)) || "0", 10) || 0;
  const start = Number.isFinite(asked) && asked > 0
    ? Math.min(asked - 1, r.page_count - 1)
    : Math.min(Math.max(saved, 0), r.page_count - 1);
  await goTo(start, true);
}

/* ---------------- 书架 ---------------- */
function showLibrary(r) {
  if (state.sid) {
    post("/api/reader/close/" + state.sid, {}).catch(() => {});
    state.sid = null;
  }
  state.pages = [];
  state.index = 0;
  state.scrollBuiltFor = null;
  state.libraryPath = r.path;
  state.chapters = [];
  state.chapterIndex = -1;
  $("#chapters").classList.add("hidden");

  $("#empty").classList.add("hidden");
  $("#canvas").classList.add("hidden");
  $("#scroll").classList.add("hidden");
  $("#library").classList.remove("hidden");

  $("#doc-name").textContent = "书架 · " + r.name;
  $("#doc-name").title = r.path;
  $("#doc-count").classList.add("hidden");
  $("#page-total").textContent = "0";
  $("#page-input").value = "";
  let libMeta = `${r.archive_count} 个漫画包 · ${r.folder_count} 个文件夹`;
  if (r.image_count) libMeta += ` · ${r.image_count} 张图片`;
  $("#page-meta").textContent = libMeta;
  history.replaceState(null, "", "/reader?path=" + encodeURIComponent(r.path));

  $("#lib-path").textContent = r.path;
  $("#btn-lib-up").disabled = !parentOf(r.path) || parentOf(r.path) === r.path;

  const list = $("#lib-list");
  list.innerHTML = "";
  if (!r.items.length) {
    const li = document.createElement("li");
    li.className = "lib-empty";
    li.textContent = "这个文件夹里没有漫画包";
    list.appendChild(li);
    return;
  }
  r.items.forEach((item) => {
    const li = document.createElement("li");
    const kind = document.createElement("span");
    kind.className = "lib-kind";
    kind.textContent = item.kind === "archive" ? "📦" : (item.kind === "images" ? "🖼️" : "📁");

    const name = document.createElement("span");
    name.className = "lib-name";
    name.textContent = item.name;

    const meta = document.createElement("span");
    meta.className = "lib-meta";
    if (item.kind === "archive") meta.textContent = item.bytes_text || humanSize(item.size);
    else if (item.kind === "images") meta.textContent = `${item.count} 张图片`;
    else meta.textContent = "文件夹";

    li.append(kind, name, meta);
    li.title = item.path;
    li.addEventListener("click", () => openPath(item.path, item.kind === "images"));
    list.appendChild(li);
  });
}

/* ---------------- 章节目录 ---------------- */
function updateDocBadge() {
  let text = `共 ${state.pages.length} 页`;
  if (state.chapters.length > 1 && state.chapterIndex >= 0) {
    text += ` · 第 ${state.chapterIndex + 1}/${state.chapters.length} 话`;
  }
  $("#doc-count").textContent = text;
}

function renderChapters() {
  const list = $("#chapter-list");
  const total = state.chapters.length;
  list.innerHTML = "";
  $("#chapters-count").textContent = String(total);

  if (!total) {
    $("#chapters").classList.add("hidden");
    $("#btn-chapters").disabled = true;
    return;
  }

  state.chapters.forEach((ch, i) => {
    const li = document.createElement("li");
    if (i === state.chapterIndex) li.className = "active";
    li.title = ch.path;

    const idx = document.createElement("span");
    idx.className = "chapter-idx";
    idx.textContent = String(i + 1);

    const name = document.createElement("span");
    name.className = "chapter-name";
    name.textContent = ch.name;

    li.append(idx, name);
    li.addEventListener("click", () => {
      if (i !== state.chapterIndex) openPath(ch.path);
    });
    list.appendChild(li);
  });

  const active = list.querySelector("li.active");
  if (active) active.scrollIntoView({ block: "nearest" });

  $("#btn-prev-chapter").disabled = state.chapterIndex <= 0;
  $("#btn-next-chapter").disabled = state.chapterIndex < 0 || state.chapterIndex >= total - 1;
  updateSidebarVisibility();
}

function updateSidebarVisibility() {
  const canShow = state.chapters.length > 1;
  const show = canShow && state.sidebarOpen;
  $("#chapters").classList.toggle("hidden", !show);
  $("#btn-chapters").disabled = !canShow;
  $("#btn-chapters").classList.toggle("active", show);
}

function setSidebar(open) {
  state.sidebarOpen = open;
  localStorage.setItem("comicpack.sidebar", open ? "1" : "0");
  updateSidebarVisibility();
}

function nextChapter() {
  if (state.chapterIndex >= 0 && state.chapterIndex < state.chapters.length - 1) {
    return openPath(state.chapters[state.chapterIndex + 1].path);
  }
  toast("已经是最后一话了");
}

function prevChapter() {
  if (state.chapterIndex > 0) {
    return openPath(state.chapters[state.chapterIndex - 1].path);
  }
  toast("已经是第一话了");
}

/* ---------------- 视图切换 ---------------- */
function renderView() {
  const has = state.pages.length > 0;
  $("#library").classList.add("hidden");
  $("#empty").classList.toggle("hidden", has);

  const scrollMode = state.mode === "scroll" && has;
  $("#scroll").classList.toggle("hidden", !scrollMode);
  $("#canvas").classList.toggle("hidden", !has || scrollMode);
  $("#cover-solo-wrap").classList.toggle("hidden", state.mode !== "double" || !has);
  if (scrollMode) buildScroll();
}

/* ---------------- 翻页 ---------------- */
function spreadPair(i) {
  const n = state.pages.length;
  if (!n) return [0, null];
  i = Math.max(0, Math.min(n - 1, i));
  if (!state.coverSolo) {
    const left = i - (i % 2);
    return [left, left + 1 < n ? left + 1 : null];
  }
  if (i === 0) return [0, null];
  const left = 1 + Math.floor((i - 1) / 2) * 2;
  return [left, left + 1 < n ? left + 1 : null];
}

function normalizeIndex(i) {
  return state.mode === "double" ? spreadPair(i)[0] : i;
}

function goTo(i, force) {
  if (!state.pages.length) return;
  const clamped = Math.max(0, Math.min(state.pages.length - 1, i));
  const target = normalizeIndex(clamped);
  if (!force && target === state.index) return;
  state.index = target;
  savePos();
  updateChrome();

  if (state.mode === "scroll") {
    scrollToPage(target, force);
    return;
  }
  return loadPage();
}

function step(dir) {
  if (!state.pages.length) return;
  if (state.mode === "double") {
    const [a, b] = spreadPair(state.index);
    if (dir > 0) return goTo(b == null ? a + 1 : b + 1);
    return goTo(Math.max(0, a - 2));
  }
  return goTo(state.index + dir);
}

function next() {
  if (!state.pages.length) return;
  if ($("#btn-next").disabled) {           // 本话最后一页
    const nxt = state.chapters[state.chapterIndex + 1];
    if (nxt) toast("本话最后一页，按 n 看下一话：" + nxt.name);
    return;
  }
  step(1);
}
function prev() { step(-1); }

function savePos() {
  if (state.path) localStorage.setItem(posKey(state.path), String(state.index));
}

async function loadPage() {
  const pair = state.mode === "double" ? spreadPair(state.index) : [state.index, null];
  const a = $("#img-a");
  const b = $("#img-b");
  const err = $("#page-error");

  err.classList.add("hidden");
  a.classList.remove("loaded");
  b.classList.remove("loaded");
  b.classList.add("hidden");
  setBusy(true, true);

  const jobs = [loadImage(a, pageUrl(pair[0]))];
  if (pair[1] != null) {
    b.classList.remove("hidden");
    jobs.push(loadImage(b, pageUrl(pair[1])));
  }

  try {
    await Promise.all(jobs);
    setBusy(false, true);
    applySizing();
    a.classList.add("loaded");
    if (pair[1] != null) b.classList.add("loaded");
    updateChrome();
    preload(pair[0] + 1);
    preload(pair[0] - 1);
    preload(pair[1] != null ? pair[1] + 1 : pair[0] + 2);
  } catch (e) {
    setBusy(false, true);
    b.classList.add("hidden");
    const meta = state.pages[pair[0]];
    err.textContent = `第 ${pair[0] + 1} 页读不出来${meta ? "（" + meta.name + "）" : ""}，文件可能已损坏或被占用。`;
    err.classList.remove("hidden");
  }
}

function loadImage(img, url) {
  return new Promise((resolve, reject) => {
    img.onload = () => resolve();
    img.onerror = () => reject(new Error("load failed"));
    img.src = url;
  });
}

/* ---------------- 竖屏连续滚动 ---------------- */
function buildScroll() {
  const box = $("#scroll");
  if (state.scrollBuiltFor === state.sid) {
    applyScrollWidth();
    return;
  }
  box.innerHTML = "";
  const frag = document.createDocumentFragment();
  state.pages.forEach((p, i) => {
    const img = document.createElement("img");
    img.className = "scroll-page";
    img.loading = "lazy";
    img.dataset.index = String(i);
    img.alt = `第 ${i + 1} 页`;
    img.addEventListener("load", () => img.classList.add("loaded"));
    img.src = pageUrl(i);
    frag.appendChild(img);
  });
  box.appendChild(frag);
  state.scrollBuiltFor = state.sid;
  applyScrollWidth();
}

function applyScrollWidth() {
  const stage = $("#stage");
  const width = Math.max(120, stage.clientWidth - 20);
  document.querySelectorAll(".scroll-page").forEach((img) => {
    img.style.width = Math.round(width * state.zoom) + "px";
  });
}

function scrollToPage(i, instant) {
  const el = $(`#scroll img[data-index="${i}"]`);
  if (!el) return;
  const stage = $("#stage");
  const jump = () => stage.scrollTo({
    top: Math.max(0, el.offsetTop - 6),
    behavior: instant ? "auto" : "smooth",
  });
  if (el.complete && el.naturalWidth) jump();
  else el.addEventListener("load", jump, { once: true });
}

function onScroll() {
  if (state.mode !== "scroll" || !state.pages.length) return;
  const now = Date.now();
  if (now - state.lastScroll < 120) return;
  state.lastScroll = now;

  const stage = $("#stage");
  const probe = stage.scrollTop + stage.clientHeight * 0.35;
  const imgs = $("#scroll").children;
  let idx = 0;
  for (let i = 0; i < imgs.length; i++) {
    if (imgs[i].offsetTop <= probe) idx = i;
    else break;
  }
  if (idx !== state.index) {
    state.index = idx;
    savePos();
    updateChrome();
  }
}

/* ---------------- 尺寸与缩放 ---------------- */
function applySizing() {
  const stage = $("#stage");
  const label = $("#zoom-label");

  if (state.mode === "scroll") {
    applyScrollWidth();
    label.textContent = Math.round(state.zoom * 100) + "%";
    return;
  }

  const a = $("#img-a");
  const b = $("#img-b");
  const nwA = a.naturalWidth || 1;
  const nhA = a.naturalHeight || 1;
  const two = !b.classList.contains("hidden") && b.naturalWidth > 0;
  const availW = Math.max(120, stage.clientWidth - 28);
  const availH = Math.max(120, stage.clientHeight - 28);

  let scale;
  if (!two) {
    if (state.fit === "actual") scale = 1;
    else if (state.fit === "width") scale = availW / nwA;
    else scale = Math.min(availW / nwA, availH / nhA);
    scale *= state.zoom;
    a.style.width = Math.max(1, Math.round(nwA * scale)) + "px";
    a.style.height = "auto";
  } else {
    const nwB = b.naturalWidth || 1;
    const nhB = b.naturalHeight || 1;
    if (state.fit === "actual") scale = 1;
    else if (state.fit === "width") scale = (availW - SPREAD_GAP) / (nwA + nwB);
    else scale = Math.min((availW - SPREAD_GAP) / (nwA + nwB), availH / Math.max(nhA, nhB));
    scale *= state.zoom;
    a.style.width = Math.max(1, Math.round(nwA * scale)) + "px";
    b.style.width = Math.max(1, Math.round(nwB * scale)) + "px";
    a.style.height = "auto";
    b.style.height = "auto";
  }

  label.textContent = Math.round(scale * 100) + "%";
  document.querySelectorAll(".fit-btn").forEach((btn) => {
    btn.classList.toggle("active",
      (btn.id === "btn-fit-page" && state.fit === "page") ||
      (btn.id === "btn-fit-w" && state.fit === "width") ||
      (btn.id === "btn-actual" && state.fit === "actual"));
  });
}

function setFit(mode) {
  state.fit = mode;
  state.zoom = 1;
  applySizing();
}

function zoomBy(mul) {
  const z = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, state.zoom * mul));
  if (z === state.zoom) return;
  state.zoom = z;
  applySizing();
}

/* ---------------- 预加载 ---------------- */
const preloaded = new Map();

function preload(i) {
  if (!state.sid || i < 0 || i >= state.pages.length) return;
  const url = pageUrl(i);
  if (preloaded.has(url)) return;
  const im = new Image();
  im.onload = () => { preloaded.set(url, im); trimPreload(); };
  im.src = url;
  preloaded.set(url, im);
  trimPreload();
}

function trimPreload() {
  if (preloaded.size <= PRELOAD_KEEP) return;
  for (const key of preloaded.keys()) {
    if (preloaded.size <= PRELOAD_KEEP) break;
    const im = preloaded.get(key);
    if (key === pageUrl(state.index)) continue;
    if (im) im.src = "";
    preloaded.delete(key);
  }
}

/* ---------------- 界面状态 ---------------- */
function updateChrome() {
  const total = state.pages.length;
  const meta = state.pages[state.index];

  $("#page-total").textContent = String(total);
  $("#slider").max = String(Math.max(1, total));
  $("#slider").disabled = !total;
  $("#page-input").value = total ? String(state.index + 1) : "";
  $("#slider").value = String(state.index + 1);

  let range = "";
  if (meta) {
    if (state.mode === "double") {
      const [a, b] = spreadPair(state.index);
      range = b == null ? `${a + 1}` : `${a + 1}–${b + 1}`;
    } else {
      range = String(state.index + 1);
    }
  }
  $("#page-meta").textContent = meta
    ? `${range} / ${total} · ${meta.name} · ${meta.bytes_text || humanSize(meta.size)}`
    : "未打开文件";

  let atEnd = state.index >= total - 1;
  if (state.mode === "double" && total) {
    const [a, b] = spreadPair(state.index);
    atEnd = b === null && a >= total - 1;   // 封面单独时第 1 页不算结尾
  }
  $("#btn-prev").disabled = !total || state.index <= 0;
  $("#btn-next").disabled = !total || atEnd;
}

function setBusy(busy, overlay) {
  if (overlay) $("#spinner").classList.toggle("hidden", !busy);
}

/* ---------------- 全屏 ---------------- */
function toggleFull() {
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  else document.documentElement.requestFullscreen().catch(() => toast("浏览器不允许全屏"));
}

/* ---------------- 模式 ---------------- */
function setMode(mode, force) {
  state.mode = mode;
  localStorage.setItem("comicpack.mode", mode);
  document.querySelectorAll(".mode-btn").forEach((b) => {
    b.classList.toggle("active", b.id === "btn-mode-" + mode);
  });
  $("#cover-solo-wrap").classList.toggle("hidden", mode !== "double" || !state.pages.length);

  if (!state.pages.length) return;
  if (mode === "double") state.index = spreadPair(state.index)[0];
  renderView();
  if (mode === "scroll") {
    buildScroll();
    scrollToPage(state.index, true);
    updateChrome();
  } else {
    loadPage();
  }
}

/* ---------------- 绑定 ---------------- */
$("#btn-open").addEventListener("click", () => openDialog("file"));
$("#btn-empty-open").addEventListener("click", () => openDialog("file"));
$("#btn-open-dir").addEventListener("click", () => openDialog("folder"));
$("#btn-empty-dir").addEventListener("click", () => openDialog("folder"));
$("#btn-lib-up").addEventListener("click", () => {
  const up = parentOf(state.libraryPath);
  if (up) openPath(up);
});

$("#btn-prev").addEventListener("click", prev);
$("#btn-next").addEventListener("click", next);

$("#btn-mode-single").addEventListener("click", () => setMode("single"));
$("#btn-mode-double").addEventListener("click", () => setMode("double"));
$("#btn-mode-scroll").addEventListener("click", () => setMode("scroll"));

$("#btn-chapters").addEventListener("click", () => setSidebar(!state.sidebarOpen));
$("#btn-chapters-hide").addEventListener("click", () => setSidebar(false));
$("#btn-next-chapter").addEventListener("click", nextChapter);
$("#btn-prev-chapter").addEventListener("click", prevChapter);

$("#btn-zoom-in").addEventListener("click", () => zoomBy(ZOOM_STEP));
$("#btn-zoom-out").addEventListener("click", () => zoomBy(1 / ZOOM_STEP));
$("#btn-fit-page").addEventListener("click", () => setFit("page"));
$("#btn-fit-w").addEventListener("click", () => setFit("width"));
$("#btn-actual").addEventListener("click", () => setFit("actual"));

$("#btn-full").addEventListener("click", toggleFull);

$("#cover-solo").addEventListener("change", () => {
  state.coverSolo = $("#cover-solo").checked;
  localStorage.setItem("comicpack.coversolo", state.coverSolo ? "1" : "0");
  if (state.mode === "double" && state.pages.length) {
    state.index = spreadPair(state.index)[0];
    loadPage();
  }
});

$("#page-input").addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  const n = parseInt($("#page-input").value, 10);
  if (Number.isFinite(n)) goTo(n - 1);
  else updateChrome();
  $("#page-input").blur();
});

$("#page-input").addEventListener("blur", updateChrome);

$("#slider").addEventListener("input", () => {
  const n = parseInt($("#slider").value, 10) || 1;
  $("#page-meta").textContent = `${n} / ${state.pages.length} · 松开跳到这一页`;
});

$("#slider").addEventListener("change", () => {
  const n = parseInt($("#slider").value, 10) || 1;
  goTo(n - 1);
});

$("#stage").addEventListener("wheel", (e) => {
  if (!state.pages.length || state.mode === "scroll") return;
  if (e.ctrlKey) {
    e.preventDefault();
    zoomBy(e.deltaY < 0 ? 1.1 : 1 / 1.1);
    return;
  }
  if (state.zoom > 1.02) return;            // 放大后滚轮用于平移
  const now = Date.now();
  if (now - state.wheelAt < 200) return;
  state.wheelAt = now;
  e.preventDefault();
  if (e.deltaY > 0) next();
  else prev();
}, { passive: false });

$("#stage").addEventListener("scroll", onScroll, { passive: true });

window.addEventListener("keydown", (e) => {
  if (e.target && e.target.matches("input, textarea, select")) return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const k = e.key;
  if (k === "ArrowRight" || k === "ArrowDown" || k === "PageDown" || k === " ") {
    e.preventDefault(); next();
  } else if (k === "ArrowLeft" || k === "ArrowUp" || k === "PageUp") {
    e.preventDefault(); prev();
  } else if (k === "Home") {
    e.preventDefault(); goTo(0);
  } else if (k === "End") {
    e.preventDefault(); goTo(state.pages.length - 1);
  } else if (k === "+" || k === "=") {
    e.preventDefault(); zoomBy(ZOOM_STEP);
  } else if (k === "-" || k === "_") {
    e.preventDefault(); zoomBy(1 / ZOOM_STEP);
  } else if (k === "0") {
    e.preventDefault(); setFit("page");
  } else if (k === "f" || k === "F") {
    e.preventDefault(); toggleFull();
  } else if (k === "o" || k === "O") {
    e.preventDefault(); openDialog("file");
  } else if (k === "n" || k === "N") {
    e.preventDefault(); nextChapter();
  }
});

let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { if (state.pages.length) applySizing(); }, 120);
});

document.addEventListener("fullscreenchange", () => {
  if (state.pages.length) applySizing();
});

$("#btn-quit").addEventListener("click", async () => {
  if (!confirm("关闭本地服务？关闭后页面将无法继续使用。")) return;
  try { await post("/api/shutdown", {}); } catch (e) { /* ignore */ }
  document.body.innerHTML =
    '<div style="height:100vh;display:grid;place-items:center;color:#8b94a7;font-size:15px;font-family:Segoe UI,Microsoft YaHei">服务已关闭，可以关掉这个窗口了</div>';
});

/* ---------------- 初始化 ---------------- */
(function init() {
  const params = new URLSearchParams(location.search);
  const wanted = params.get("mode");
  state.mode = ["single", "double", "scroll"].includes(wanted)
    ? wanted
    : (localStorage.getItem("comicpack.mode") || "single");
  if (!["single", "double", "scroll"].includes(state.mode)) state.mode = "single";
  state.coverSolo = localStorage.getItem("comicpack.coversolo") !== "0";
  state.sidebarOpen = localStorage.getItem("comicpack.sidebar") !== "0";
  $("#cover-solo").checked = state.coverSolo;
  document.querySelectorAll(".mode-btn").forEach((b) => {
    b.classList.toggle("active", b.id === "btn-mode-" + state.mode);
  });
  $("#cover-solo-wrap").classList.toggle("hidden", state.mode !== "double");

  $("#btn-prev").disabled = true;
  $("#btn-next").disabled = true;
  updateChrome();

  const fromUrl = params.get("path");
  const last = localStorage.getItem("comicpack.read.last");
  const start = fromUrl || last;
  if (start) {
    $("#doc-name").textContent = "正在打开…";
    openPath(start);
  }
})();
