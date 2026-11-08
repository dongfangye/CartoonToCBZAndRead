/* ComicPack — 前端逻辑（原生 JS，无框架） */

const $ = (sel) => document.querySelector(sel);

const state = {
  info: null,          // 扫描结果（单层）
  tree: null,          // 递归扫描结果（多个子文件夹）
  jobId: null,
  timer: null,
  running: false,
};

function recursiveOn() { return $("#recursive").checked; }

function packable() {
  if (recursiveOn()) return !!(state.tree && state.tree.folders.length);
  return !!(state.info && state.info.count);
}

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
    const r = await post("/api/browse", {
      initial: (state.info || state.tree) ? ((state.info || state.tree).folder || (state.info || state.tree).root) : ($("#path").value || ""),
    });
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
  const recursive = recursiveOn();
  try {
    const info = await post("/api/scan", { path, recursive });
    if (!info.ok) {
      state.info = null;
      state.tree = null;
      $("#summary").innerHTML = `<span class="bad">${info.error || "扫描失败"}</span>`;
      renderList();
      updateUI();
      return;
    }
    if (recursive) {
      state.tree = info;
      state.info = null;
      $("#path").value = info.root;
      localStorage.setItem("comicpack.path", info.root);
    } else {
      state.info = info;
      state.tree = null;
      $("#path").value = info.folder;
      localStorage.setItem("comicpack.path", info.folder);
    }
    renderSummary();
    renderList();
    updateUI();
  } catch (e) {
    state.info = null;
    state.tree = null;
    $("#summary").innerHTML = `<span class="bad">扫描失败：${e.message}（服务是否已停止？）</span>`;
    renderList();
    updateUI();
  }
}

function renderSummary() {
  const s = $("#summary");
  if (recursiveOn()) {
    const tree = state.tree;
    if (!tree) {
      s.textContent = "尚未选择文件夹";
      return;
    }
    const bits = [
      `<span><b>${tree.total}</b> 个子文件夹</span>`,
      `<span>合计 <b>${tree.images}</b> 张图片</span>`,
      `<span>合计 <b>${tree.bytes_text || humanSize(tree.bytes)}</b></span>`,
    ];
    if (!tree.total) bits.push(`<span class="bad">没有找到含图片的子文件夹</span>`);
    s.innerHTML = bits.join("");
    return;
  }

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
  if (info.covers) bits.push(`<span class="ok-text">含 ${info.covers} 张封面</span>`);
  if (!info.count) bits.push(`<span class="bad">没有可打包的图片</span>`);
  s.innerHTML = bits.join("");
}

/* ---------------- 列表 ---------------- */
function renderList() {
  const ol = $("#list");
  const empty = $("#list-empty");
  ol.innerHTML = "";
  $(".list-wrap").classList.toggle("folders", recursiveOn());
  $("#panel-hint").textContent = recursiveOn()
    ? "递归：每个含图片的子文件夹各打一个包"
    : "只处理这一层，不递归子目录";

  if (recursiveOn()) {
    renderFolderList(ol, empty);
    return;
  }

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
    if (im.cover) {
      const tag = document.createElement("em");
      tag.className = "cover-tag";
      tag.textContent = "封面";
      old.appendChild(tag);
    }

    const size = document.createElement("span");
    size.className = "col-size";
    size.textContent = humanSize(im.size);

    li.append(n, arc, old, size);
    frag.appendChild(li);
  });

  ol.appendChild(frag);
}

/* ---------------- 递归：子文件夹列表 ---------------- */
function renderFolderList(ol, empty) {
  const tree = state.tree;
  if (!tree || !tree.folders.length) {
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");

  const frag = document.createDocumentFragment();
  const head = document.createElement("li");
  head.className = "head";
  ["#", "子文件夹", "相对路径", "图片 / 体积"].forEach((t, i) => {
    const sp = document.createElement("span");
    sp.className = ["col-idx", "col-new", "col-old", "col-size"][i];
    sp.textContent = t;
    head.appendChild(sp);
  });
  frag.appendChild(head);

  tree.folders.forEach((f, idx) => {
    const li = document.createElement("li");

    const n = document.createElement("span");
    n.className = "col-idx";
    n.textContent = idx + 1;

    const name = document.createElement("span");
    name.className = "col-new";
    name.textContent = f.name;
    name.title = f.folder;

    const rel = document.createElement("span");
    rel.className = "col-old";
    rel.textContent = f.rel || "（根目录）";
    rel.title = f.folder;

    const size = document.createElement("span");
    size.className = "col-size";
    size.textContent = `${f.count} 张 · ${f.bytes_text || humanSize(f.bytes)}`;

    li.append(n, name, rel, size);
    frag.appendChild(li);
  });

  ol.appendChild(frag);
}

/* ---------------- 输出预览 ---------------- */
function updateOutput() {
  const box = $("#output-preview");
  const fmts = formatsSelected();

  if (recursiveOn()) {
    const tree = state.tree;
    if (!tree || !tree.folders.length) {
      box.classList.add("muted");
      box.textContent = "选择文件夹后显示";
      return;
    }
    box.classList.remove("muted");
    if (!fmts.length) {
      box.textContent = "（未选择格式）";
      return;
    }
    const sample = tree.folders[0];
    box.innerHTML =
      `每个子文件夹旁生成：<br>${escapeHtml(sample.name)}.${fmts[0]}` +
      `<br>（共 ${tree.folders.length} 个包）`;
    return;
  }

  const info = state.info;
  if (!info || !info.count) {
    box.classList.add("muted");
    box.textContent = "选择文件夹后显示";
    return;
  }
  box.classList.remove("muted");
  const { dir, base } = splitPath(info.folder);
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
  $("#btn-start").disabled = !packable() || state.running || formatsSelected().length === 0;
  updateOutput();
}

/* ---------------- 打包 ---------------- */
async function start() {
  if (state.running) return;
  const recursive = recursiveOn();
  const folder = recursive ? (state.tree ? state.tree.root : "") : (state.info ? state.info.folder : "");
  if (!folder || !packable()) {
    toast(recursive ? "这个文件夹里没有找到含图片的子文件夹" : "这个文件夹里没有可打包的图片");
    return;
  }
  const fmts = formatsSelected();
  if (!fmts.length) { toast("请至少选择一种导出格式"); return; }

  const del = $("#delete-source").checked;
  if (del) {
    const images = recursive ? state.tree.images : state.info.count;
    const where = recursive ? `这 ${state.tree.folders.length} 个子文件夹里的 ${images} 张原图` : `「${folder}」里的 ${images} 张原图`;
    const ok = confirm(
      `打包成功后会删除${where}。\n文件夹空了会一并删除，此操作不可撤销。\n\n确定继续吗？`
    );
    if (!ok) return;
  }

  const btn = $("#btn-start");
  btn.disabled = true;
  btn.textContent = "打包中…";
  state.running = true;
  $("#result-box").classList.add("hidden");

  try {
    const r = await post("/api/pack", {
      path: folder,
      formats: fmts,
      keep_names: $("#keep-names").checked,
      delete_source: $("#delete-source").checked,
      recursive,
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
    const t = job.total || (state.tree ? state.tree.images : (state.info ? state.info.count : 1)) || 1;
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
  $("#btn-start").disabled = !packable() || formatsSelected().length === 0;
}

function renderResult(result) {
  const box = $("#result-box");
  box.classList.remove("hidden", "err");
  box.innerHTML = "";

  const head = document.createElement("div");
  head.className = "result-head";
  head.textContent = result.recursive
    ? `✅ 打包完成：${result.ok_folders}/${result.folders} 个子文件夹`
    : "✅ 打包完成";
  box.appendChild(head);

  const shown = result.produced.slice(0, 12);
  shown.forEach((p) => {
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

    const read = document.createElement("a");
    read.className = "result-link";
    read.href = "/reader?path=" + encodeURIComponent(p.path);
    read.textContent = "阅读";
    read.title = "在阅读器里打开这个包";

    row.append(name, size, btn, read);
    box.appendChild(row);
  });

  if (result.produced.length > shown.length) {
    const more = document.createElement("div");
    more.className = "result-note";
    more.textContent = `… 另外 ${result.produced.length - shown.length} 个包`;
    box.appendChild(more);
  }

  if (result.failed_folders) {
    const bad = (result.results || []).filter((r) => !r.ok);
    const note = document.createElement("div");
    note.className = "result-note";
    note.textContent = `${result.failed_folders} 个子文件夹失败：` +
      bad.slice(0, 5).map((r) => r.name).join("，") +
      (bad.length > 5 ? " …" : "");
    note.title = bad.map((r) => `${r.name}: ${r.error}`).join("\n");
    box.appendChild(note);
  }

  if (result.deleted || (result.delete_errors || []).length || result.removed_folder) {
    const note = document.createElement("div");
    note.className = "result-note";
    const bits = [];
    if (result.deleted) bits.push(`已删除 ${result.deleted} 张原图`);
    if (result.removed_folder) bits.push("空文件夹已一并删除");
    if ((result.delete_errors || []).length) {
      bits.push(`${result.delete_errors.length} 张删除失败`);
      note.title = result.delete_errors.join("\n");
    }
    note.textContent = bits.join(" · ");
    box.appendChild(note);

    // 原图已经没了，扫描结果不再有效
    state.info = null;
    state.tree = null;
    $("#path").value = "";
    $("#summary").textContent = "原图已删除，可以重新选择文件夹";
    localStorage.removeItem("comicpack.path");
    renderList();
  }

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

$("#delete-source").addEventListener("change", () => {
  localStorage.setItem("comicpack.delete", $("#delete-source").checked ? "1" : "0");
});

$("#recursive").addEventListener("change", () => {
  localStorage.setItem("comicpack.recursive", $("#recursive").checked ? "1" : "0");
  if ($("#path").value.trim()) scan();
  else updateUI();
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
  $("#delete-source").checked = localStorage.getItem("comicpack.delete") === "1";
  $("#recursive").checked = localStorage.getItem("comicpack.recursive") === "1";

  const savedPath = localStorage.getItem("comicpack.path");
  const urlPath = new URLSearchParams(location.search).get("path");
  const startPath = urlPath || savedPath;
  if (startPath) {
    $("#path").value = startPath;
    scan();
  }
  updateUI();
})();
