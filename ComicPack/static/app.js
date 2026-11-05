/* ComicPack — 前端逻辑（原生 JS，无框架） */

const $ = (sel) => document.querySelector(sel);

const state = {
  info: null,          // 扫描结果
  jobId: null,
  timer: null,
  running: false,
};

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

function splitPath(p) {
  const norm = String(p || "").replace(/[\\/]+$/, "");
  const i = Math.max(norm.lastIndexOf("\\"), norm.lastIndexOf("/"));
  return i < 0 ? { dir: "", base: norm } : { dir: norm.slice(0, i), base: norm.slice(i + 1) };
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

function formatsSelected() {
  return [...document.querySelectorAll('input[name="fmt"]:checked')].map((c) => c.value);
}

/* ---------------- 扫描 ---------------- */
async function browse() {
  const btn = $("#btn-browse");
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = "选择中…";
  try {
    const r = await post("/api/browse", { initial: state.info ? state.info.folder : ($("#path").value || "") });
    if (!r.ok) throw new Error(r.error || "无法打开选择框");
    if (r.path) {
      $("#path").value = r.path;
      await scan();
    }
  } catch (e) {
    toast(e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

async function scan() {
  const path = $("#path").value.trim();
  if (!path) { toast("请先选择或输入文件夹路径"); return; }
  $("#summary").textContent = "正在扫描…";
  try {
    const info = await post("/api/scan", { path });
    if (!info.ok) {
      state.info = null;
      $("#summary").innerHTML = `<span class="bad">${info.error || "扫描失败"}</span>`;
      renderList();
      updateUI();
      return;
    }
    state.info = info;
    $("#path").value = info.folder;
    localStorage.setItem("comicpack.path", info.folder);
    renderSummary();
    renderList();
    updateUI();
  } catch (e) {
    state.info = null;
    $("#summary").innerHTML = `<span class="bad">扫描失败：${e.message}（服务是否已停止？）</span>`;
    updateUI();
  }
}

function renderSummary() {
  const s = $("#summary");
  const info = state.info;
  if (!info) {
    s.textContent = "尚未选择文件夹";
    return;
  }
  const bits = [
    `<span><b>${info.count}</b> 张图片</span>`,
    `<span>合计 <b>${info.bytes_text || humanSize(info.bytes)}</b></span>`,
  ];
  if (info.ignored) bits.push(`<span class="warn">忽略 ${info.ignored} 个非图片文件</span>`);
  if (info.subdirs) bits.push(`<span class="warn">跳过 ${info.subdirs} 个子目录</span>`);
  if (!info.count) bits.push(`<span class="bad">没有可打包的图片</span>`);
  s.innerHTML = bits.join("");
}

/* ---------------- 列表 ---------------- */
function renderList() {
  const ol = $("#list");
  const empty = $("#list-empty");
  ol.innerHTML = "";
  const info = state.info;

  if (!info || !info.count) {
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");

  const keep = $("#keep-names").checked;
  const width = Math.max(3, String(info.count).length);
  const frag = document.createDocumentFragment();

  const head = document.createElement("li");
  head.className = "head";
  ["#", "包内文件名", "原文件名", "体积"].forEach((t, i) => {
    const sp = document.createElement("span");
    sp.className = ["col-idx", "col-new", "col-old", "col-size"][i];
    sp.textContent = t;
    head.appendChild(sp);
  });
  frag.appendChild(head);

  info.images.forEach((im, idx) => {
    const li = document.createElement("li");

    const n = document.createElement("span");
    n.className = "col-idx";
    n.textContent = idx + 1;

    const arc = document.createElement("span");
    arc.className = "col-new";
    arc.textContent = keep ? im.name : String(idx + 1).padStart(width, "0") + im.ext;

    const old = document.createElement("span");
    old.className = "col-old";
    old.textContent = im.name;
    old.title = im.name;

    const size = document.createElement("span");
    size.className = "col-size";
    size.textContent = humanSize(im.size);

    li.append(n, arc, old, size);
    frag.appendChild(li);
  });

  ol.appendChild(frag);
}

/* ---------------- 输出预览 ---------------- */
function updateOutput() {
  const box = $("#output-preview");
  const info = state.info;
  if (!info || !info.count) {
    box.classList.add("muted");
    box.textContent = "选择文件夹后显示";
    return;
  }
  box.classList.remove("muted");
  const { dir, base } = splitPath(info.folder);
  const fmts = formatsSelected();
  if (!fmts.length) {
    box.textContent = "（未选择格式）";
    return;
  }
  box.innerHTML = fmts
    .map((f) => `${escapeHtml(dir ? dir + "\\" : "")}${escapeHtml(base)}.${f}`)
    .join("<br>");
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------------- 按钮状态 ---------------- */
function updateUI() {
  const info = state.info;
  const has = !!(info && info.count);
  $("#btn-start").disabled = !has || state.running || formatsSelected().length === 0;
  updateOutput();
}

/* ---------------- 打包 ---------------- */
async function start() {
  if (state.running) return;
  const info = state.info;
  if (!info || !info.count) { toast("这个文件夹里没有可打包的图片"); return; }
  const fmts = formatsSelected();
  if (!fmts.length) { toast("请至少选择一种导出格式"); return; }

  const btn = $("#btn-start");
  btn.disabled = true;
  btn.textContent = "打包中…";
  state.running = true;
  $("#result-box").classList.add("hidden");

  try {
    const r = await post("/api/pack", {
      path: info.folder,
      formats: fmts,
      keep_names: $("#keep-names").checked,
    });
    if (!r.ok) throw new Error(r.error || "启动失败");
    state.jobId = r.job;
    showProgress(0);
    poll();
  } catch (e) {
    toast(e.message);
    resetButtons();
  }
}

function showProgress(done) {
  $("#progress-box").classList.remove("hidden");
  $("#progress-fill").style.width = (done * 100).toFixed(1) + "%";
  $("#progress-pct").textContent = (done * 100).toFixed(0) + "%";
}

async function poll() {
  clearTimeout(state.timer);
  state.timer = null;
  if (!state.jobId) return;

  let job;
  try {
    const res = await fetch("/api/job/" + state.jobId);
    const data = await res.json();
    if (!data.ok || !data.job) throw new Error(data.error || "任务丢失");
    job = data.job;
  } catch (e) {
    $("#progress-box").classList.add("hidden");
    renderError("进度获取失败：" + e.message);
    return;
  }

  if (job.status === "running") {
    const t = job.total || state.info.count || 1;
    showProgress(Math.min(1, job.done / t));
    $("#progress-label").textContent = `${job.done} / ${t}  ${job.current || ""}`;
    const secs = Math.max(0, Math.round(Date.now() / 1000 - job.started));
    $("#progress-sub").textContent =
      `${humanSize(job.bytes)} / ${humanSize(job.total_bytes)} · 已用 ${secs}s`;
    state.timer = setTimeout(poll, 350);
    return;
  }

  if (job.status === "done") {
    showProgress(1);
    $("#progress-label").textContent = "完成";
    $("#progress-sub").textContent = `${job.result.count} 张 · 用时 ${job.result.elapsed}s`;
    renderResult(job.result);
  } else if (job.status === "cancelled") {
    $("#progress-box").classList.add("hidden");
    toast("已取消打包");
    resetButtons();
  } else {
    $("#progress-box").classList.add("hidden");
    renderError(job.error || "打包失败");
  }
}

function resetButtons() {
  state.running = false;
  $("#btn-start").textContent = "开始打包";
  $("#btn-start").disabled = !(state.info && state.info.count) || formatsSelected().length === 0;
}

function renderResult(result) {
  const box = $("#result-box");
  box.classList.remove("hidden", "err");
  box.innerHTML = "";
  const head = document.createElement("div");
  head.className = "result-head";
  head.textContent = "✅ 打包完成";
  box.appendChild(head);

  result.produced.forEach((p) => {
    const row = document.createElement("div");
    row.className = "result-item";

    const name = document.createElement("span");
    name.className = "result-name";
    name.textContent = splitPath(p.path).base;
    name.title = p.path;

    const size = document.createElement("span");
    size.className = "result-size";
    size.textContent = humanSize(p.size);

    const btn = document.createElement("button");
    btn.textContent = "打开位置";
    btn.onclick = () => post("/api/reveal", { path: p.path }).catch(() => {});

    row.append(name, size, btn);
    box.appendChild(row);
  });
  resetButtons();
}

function renderError(msg) {
  const box = $("#result-box");
  box.classList.remove("hidden");
  box.classList.add("err");
  box.innerHTML = "";
  const head = document.createElement("div");
  head.className = "result-head";
  head.textContent = "⚠️ " + msg;
  box.appendChild(head);
  resetButtons();
}

async function cancel() {
  if (!state.jobId) return;
  try { await post("/api/cancel/" + state.jobId, {}); } catch (e) { /* ignore */ }
  toast("正在取消…");
}

/* ---------------- 绑定 ---------------- */
$("#btn-browse").addEventListener("click", browse);
$("#btn-scan").addEventListener("click", scan);
$("#btn-start").addEventListener("click", start);
$("#btn-cancel").addEventListener("click", cancel);
$("#keep-names").addEventListener("change", () => {
  localStorage.setItem("comicpack.keep", $("#keep-names").checked ? "1" : "0");
  renderList();
});

document.querySelectorAll('input[name="fmt"]').forEach((c) => {
  c.addEventListener("change", () => {
    const sel = formatsSelected();
    localStorage.setItem("comicpack.formats", sel.join(","));
    updateUI();
  });
});

$("#path").addEventListener("keydown", (e) => {
  if (e.key === "Enter") scan();
});

let typeTimer = null;
$("#path").addEventListener("input", () => {
  clearTimeout(typeTimer);
  typeTimer = setTimeout(() => {
    if ($("#path").value.trim()) scan();
  }, 700);
});

$("#btn-quit").addEventListener("click", async () => {
  if (!confirm("关闭本地服务？关闭后页面将无法继续使用。")) return;
  try { await post("/api/shutdown", {}); } catch (e) { /* ignore */ }
  document.body.innerHTML =
    '<div style="height:100vh;display:grid;place-items:center;color:#8b94a7;font-size:15px;font-family:Segoe UI,Microsoft YaHei">服务已关闭，可以关掉这个窗口了</div>';
});

/* ---------------- 初始化 ---------------- */
(function init() {
  const savedFmts = (localStorage.getItem("comicpack.formats") || "cbz").split(",").filter(Boolean);
  document.querySelectorAll('input[name="fmt"]').forEach((c) => {
    if (savedFmts.length) c.checked = savedFmts.includes(c.value);
  });
  $("#keep-names").checked = localStorage.getItem("comicpack.keep") === "1";

  const savedPath = localStorage.getItem("comicpack.path");
  const urlPath = new URLSearchParams(location.search).get("path");
  const startPath = urlPath || savedPath;
  if (startPath) {
    $("#path").value = startPath;
    scan();
  }
  updateUI();
})();
