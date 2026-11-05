/* ComicPack — 阅读器（原生 JS，无框架） */

const $ = (sel) => document.querySelector(sel);

const state = {
  sid: null,
  path: "",
  name: "",
  pages: [],
  index: 0,
  fit: "page",      // page（整页）| width（宽度）| actual（原始）
  zoom: 1,          // 在 fit 基础上的倍率
  base: 1,          // fit 换算出的缩放基准
  wheelAt: 0,
  loading: false,
};

const ZOOM_STEP = 1.25;
const ZOOM_MIN = 0.15;
const ZOOM_MAX = 8;
const PRELOAD_KEEP = 6;

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

/* ---------------- 打开 ---------------- */
async function openDialog(mode) {
  try {
    const r = await post("/api/reader/browse", { mode, initial: state.path });
    if (!r.ok) throw new Error(r.error || "无法打开选择框");
    if (r.path) await openPath(r.path);
  } catch (e) {
    toast(e.message);
  }
}

async function openPath(path) {
  setBusy(true);
  try {
    const r = await post("/api/reader/open", { path });
    if (!r.ok) throw new Error(r.error || "打开失败");

    if (state.sid) post("/api/reader/close/" + state.sid, {}).catch(() => {});

    state.sid = r.id;
    state.path = r.path;
    state.name = r.name;
    state.pages = r.pages;
    state.fit = "page";
    state.zoom = 1;

    $("#doc-name").textContent = r.name;
    $("#doc-name").title = r.path + " · " + r.page_count + " 页 · " + (r.bytes_text || humanSize(r.bytes));
    document.title = r.name + " · ComicPack";
    $("#page-total").textContent = r.page_count;
    $("#slider").max = String(r.page_count);

    $("#empty").classList.add("hidden");
    $("#canvas").classList.remove("hidden");
    $("#btn-prev").disabled = false;
    $("#btn-next").disabled = false;

    history.replaceState(null, "", "/reader?path=" + encodeURIComponent(r.path));
    localStorage.setItem("comicpack.read.last", r.path);

    const saved = parseInt(localStorage.getItem(posKey(r.path)) || "0", 10) || 0;
    await goTo(Math.min(Math.max(saved, 0), r.page_count - 1), true);
  } catch (e) {
    toast(e.message);
  } finally {
    setBusy(false);
  }
}

/* ---------------- 翻页 ---------------- */
function pageUrl(i) {
  return `/api/reader/page/${state.sid}/${i}`;
}

function goTo(i, force) {
  if (!state.pages.length) return;
  const clamped = Math.max(0, Math.min(state.pages.length - 1, i));
  if (!force && clamped === state.index && $("#page-img").src) return;
  state.index = clamped;
  localStorage.setItem(posKey(state.path), String(clamped));
  updateChrome();
  return loadPage();
}

function next() {
  if (state.index < state.pages.length - 1) return goTo(state.index + 1);
}

function prev() {
  if (state.index > 0) return goTo(state.index - 1);
}

function loadPage() {
  const img = $("#page-img");
  const err = $("#page-error");
  err.classList.add("hidden");
  img.classList.remove("loaded");
  setBusy(true, true);

  const url = pageUrl(state.index);
  img.onload = () => {
    setBusy(false, true);
    applySizing();
    img.classList.add("loaded");
    updateChrome();
    preload(state.index + 1);
    preload(state.index - 1);
  };
  img.onerror = () => {
    setBusy(false, true);
    img.classList.remove("loaded");
    const meta = state.pages[state.index];
    err.textContent = `第 ${state.index + 1} 页读不出来${meta ? "（" + meta.name + "）" : ""}，文件可能已损坏或被占用。`;
    err.classList.remove("hidden");
  };
  img.src = url;
}

/* ---------------- 尺寸与缩放 ---------------- */
function applySizing() {
  const img = $("#page-img");
  const stage = $("#stage");
  const nw = img.naturalWidth || 1;
  const nh = img.naturalHeight || 1;
  const availW = Math.max(80, stage.clientWidth - 28);
  const availH = Math.max(80, stage.clientHeight - 28);

  if (state.fit === "actual") state.base = 1;
  else if (state.fit === "height") state.base = availH / nh;
  else if (state.fit === "page") state.base = Math.min(availW / nw, availH / nh);
  else state.base = availW / nw;

  const total = state.base * state.zoom;
  img.style.width = Math.max(1, Math.round(nw * total)) + "px";
  img.style.height = "auto";
  $("#zoom-label").textContent = Math.round(total * 100) + "%";

  document.querySelectorAll(".fit-btn").forEach((b) => {
    b.classList.toggle("active",
      (b.id === "btn-fit-page" && state.fit === "page") ||
      (b.id === "btn-fit-w" && state.fit === "width") ||
      (b.id === "btn-actual" && state.fit === "actual"));
  });
}

function setFit(mode) {
  state.fit = mode;
  state.zoom = 1;
  applySizing();
}

function zoomBy(mul) {
  const nextZoom = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, state.zoom * mul));
  if (nextZoom === state.zoom) return;
  state.zoom = nextZoom;
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
  preloaded.set(url, im);           // 先占位，onload 时再整理
  trimPreload();
}

function trimPreload() {
  if (preloaded.size <= PRELOAD_KEEP) return;
  for (const key of preloaded.keys()) {
    if (preloaded.size <= PRELOAD_KEEP) break;
    const im = preloaded.get(key);
    if (key === pageUrl(state.index)) continue;
    im && (im.src = "");
    preloaded.delete(key);
  }
}

/* ---------------- 界面状态 ---------------- */
function updateChrome() {
  const total = state.pages.length;
  const meta = state.pages[state.index];
  $("#page-input").value = total ? String(state.index + 1) : "";
  $("#slider").value = String(state.index + 1);
  $("#slider").max = String(Math.max(1, total));
  $("#slider").disabled = !total;
  $("#page-total").textContent = String(total);
  $("#page-meta").textContent = meta
    ? `${state.index + 1} / ${total} · ${meta.name} · ${meta.bytes_text || humanSize(meta.size)}`
    : "未打开文件";
  $("#btn-prev").disabled = !total || state.index <= 0;
  $("#btn-next").disabled = !total || state.index >= total - 1;
}

function setBusy(busy, overlay) {
  state.loading = busy;
  if (overlay) $("#spinner").classList.toggle("hidden", !busy);
}

/* ---------------- 全屏 ---------------- */
function toggleFull() {
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  else document.documentElement.requestFullscreen().catch(() => toast("浏览器不允许全屏"));
}

/* ---------------- 绑定 ---------------- */
$("#btn-open").addEventListener("click", () => openDialog("file"));
$("#btn-empty-open").addEventListener("click", () => openDialog("file"));
$("#btn-open-dir").addEventListener("click", () => openDialog("folder"));
$("#btn-empty-dir").addEventListener("click", () => openDialog("folder"));

$("#btn-prev").addEventListener("click", prev);
$("#btn-next").addEventListener("click", next);

$("#btn-zoom-in").addEventListener("click", () => zoomBy(ZOOM_STEP));
$("#btn-zoom-out").addEventListener("click", () => zoomBy(1 / ZOOM_STEP));
$("#btn-fit-page").addEventListener("click", () => setFit("page"));
$("#btn-fit-w").addEventListener("click", () => setFit("width"));
$("#btn-actual").addEventListener("click", () => setFit("actual"));

$("#btn-full").addEventListener("click", toggleFull);

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
  if (e.ctrlKey) {
    e.preventDefault();
    zoomBy(e.deltaY < 0 ? 1.1 : 1 / 1.1);
    return;
  }
  if (state.zoom > 1.02) return;            // 放大后滚轮用于平移
  if (!state.pages.length) return;
  const now = Date.now();
  if (now - state.wheelAt < 200) return;
  state.wheelAt = now;
  e.preventDefault();
  if (e.deltaY > 0) next();
  else prev();
}, { passive: false });

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
  const fromUrl = new URLSearchParams(location.search).get("path");
  const last = localStorage.getItem("comicpack.read.last");
  const start = fromUrl || last;
  $("#btn-prev").disabled = true;
  $("#btn-next").disabled = true;
  updateChrome();
  if (start) {
    $("#doc-name").textContent = "正在打开…";
    openPath(start);
  }
})();
