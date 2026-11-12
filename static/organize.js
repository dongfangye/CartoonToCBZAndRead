/* ComicPack — 视频整理页
 * 扫描 → 新建文件夹 → 拖拽归类 → 改名 → 预览 → 执行
 */

const $ = (sel) => document.querySelector(sel);

const state = {
  root: "",
  videos: [],
  dirs: [],
  folders: [],          // [{id, name}]
  assignments: {},      // videoId -> folderId
  renames: {},          // videoId -> new name
  keep: new Set(),      // 保留的旧目录（相对路径）
  selected: new Set(),  // 选中的视频 id
  dragging: [],
  plan: null,
  seq: 0,
};

/* ---------------- 工具 ---------------- */
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

function humanSize(n) {
  n = Number(n) || 0;
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n.toFixed(0) : n.toFixed(1)) + " " + units[i];
}

function folderName(id) {
  const f = state.folders.find((x) => x.id === id);
  return f ? f.name : "";
}

function assignedCount(folderId) {
  return Object.values(state.assignments).filter((v) => v === folderId).length;
}

function unassignedVideos() {
  return state.videos.filter((v) => !state.assignments[v.id]);
}

function invalidatePlan() {
  state.plan = null;
  $("#btn-apply").disabled = true;
  $("#st-delete").textContent = "—";
}

/* ---------------- 扫描 ---------------- */
async function browse() {
  const btn = $("#btn-browse");
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = "选择中…";
  try {
    const r = await post("/api/browse", { initial: $("#path").value.trim() });
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
  $("#scan-summary").textContent = "正在扫描…";
  try {
    const r = await post("/api/organize/scan", { path });
    if (!r.ok) throw new Error(r.error || "扫描失败");
    state.root = r.root;
    state.videos = r.videos;
    state.dirs = r.dirs;
    state.folders = [];
    state.assignments = {};
    state.renames = {};
    state.keep = new Set();
    state.selected = new Set();
    invalidatePlan();
    $("#path").value = r.root;
    localStorage.setItem("comicpack.org.path", r.root);
    $("#scan-summary").textContent =
      `${r.totals.videos} 个视频 · ${r.totals.dirs} 个目录 · ${r.totals.size_text}`;
    render();
  } catch (e) {
    $("#scan-summary").textContent = "扫描失败：" + e.message;
  }
}

/* ---------------- 渲染 ---------------- */
function render() {
  renderFolders();
  renderVideos();
  renderStats();
}

function renderStats() {
  $("#st-videos").textContent = String(state.videos.length);
  $("#st-folders").textContent = String(state.folders.length);
  $("#st-keep").textContent = String(state.keep.size);
  $("#unassigned-count").textContent = String(unassignedVideos().length);
  $("#btn-plan").disabled = !state.videos.length;
}

function renderFolders() {
  const ul = $("#folder-list");
  ul.innerHTML = "";
  state.folders.forEach((f) => {
    const li = document.createElement("li");
    li.className = "org-folder";
    li.dataset.folder = f.id;

    const name = document.createElement("span");
    name.className = "name";
    name.textContent = f.name;
    name.title = f.name;

    const count = document.createElement("span");
    count.className = "count";
    count.textContent = `${assignedCount(f.id)} 个`;

    const edit = document.createElement("button");
    edit.className = "org-icon";
    edit.type = "button";
    edit.textContent = "✎";
    edit.title = "重命名";
    edit.addEventListener("click", (e) => { e.stopPropagation(); renameFolder(f.id); });

    const del = document.createElement("button");
    del.className = "org-icon";
    del.type = "button";
    del.textContent = "×";
    del.title = "删除文件夹（里面的视频回到未归类）";
    del.addEventListener("click", (e) => { e.stopPropagation(); deleteFolder(f.id); });

    li.append(name, count, edit, del);
    ul.appendChild(li);
  });
  if (!state.folders.length) {
    const li = document.createElement("li");
    li.className = "org-folder-empty";
    li.textContent = "还没有新建文件夹，点右上角「＋ 新建」";
    ul.appendChild(li);
  }
}

function renderVideos() {
  const box = $("#dir-groups");
  box.innerHTML = "";
  if (!state.videos.length) {
    $("#org-empty").classList.remove("hidden");
    return;
  }
  $("#org-empty").classList.add("hidden");

  const byDir = new Map();
  state.videos.forEach((v) => {
    if (!byDir.has(v.dir)) byDir.set(v.dir, []);
    byDir.get(v.dir).push(v);
  });

  byDir.forEach((videos, dir) => {
    const group = document.createElement("div");
    group.className = "org-group";

    const head = document.createElement("div");
    head.className = "org-group-head";

    const rel = document.createElement("span");
    rel.className = "rel";
    rel.textContent = dir || "（根目录）";
    rel.title = dir;

    const count = document.createElement("span");
    count.className = "count";
    count.textContent = `${videos.length} 个视频`;

    const keep = document.createElement("label");
    keep.className = "keep";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = state.keep.has(dir);
    cb.addEventListener("change", () => {
      if (cb.checked) state.keep.add(dir);
      else state.keep.delete(dir);
      invalidatePlan();
      renderStats();
    });
    keep.append(cb, document.createTextNode("保留此目录（视频不动）"));

    head.append(rel, count, keep);

    const ul = document.createElement("ul");
    ul.className = "org-videos";
    videos.forEach((v) => ul.appendChild(videoRow(v)));

    group.append(head, ul);
    box.appendChild(group);
  });
}

function videoRow(v) {
  const li = document.createElement("li");
  li.className = "org-video";
  li.draggable = true;
  li.dataset.id = v.id;
  if (state.selected.has(v.id)) li.classList.add("selected");

  const name = document.createElement("span");
  name.className = "name";
  name.textContent = state.renames[v.id] || v.name;
  name.title = v.rel;
  name.addEventListener("click", (e) => { e.stopPropagation(); startRename(li, v); });

  const meta = document.createElement("span");
  meta.className = "meta";
  meta.textContent = `${v.size_text}${v.subs.length ? " · 字幕 " + v.subs.length : ""}`;

  const chip = document.createElement("span");
  chip.className = "chip";
  const fid = state.assignments[v.id];
  chip.textContent = fid ? folderName(fid) : (state.keep.has(v.dir) ? "原地" : "未归类");
  if (!fid) chip.classList.add("muted");

  li.append(name, meta, chip);

  li.addEventListener("click", () => {
    if (state.selected.has(v.id)) state.selected.delete(v.id);
    else state.selected.add(v.id);
    li.classList.toggle("selected");
  });

  li.addEventListener("dragstart", (e) => {
    if (!state.selected.has(v.id)) {
      state.selected.clear();
      state.selected.add(v.id);
      document.querySelectorAll(".org-video.selected").forEach((el) => el.classList.remove("selected"));
      li.classList.add("selected");
    }
    state.dragging = [...state.selected];
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", "video");
    li.classList.add("dragging");
  });

  li.addEventListener("dragend", () => {
    li.classList.remove("dragging");
    state.dragging = [];
    document.querySelectorAll(".org-folder.drop-hover").forEach((el) => el.classList.remove("drop-hover"));
  });

  return li;
}

/* ---------------- 文件夹操作 ---------------- */
function addFolder() {
  const name = prompt("新建文件夹的名字");
  if (name === null) return;
  const clean = name.trim();
  if (!clean) { toast("名字不能为空"); return; }
  if (clean === "其他") { toast("「其他」是自动生成的，别手动建"); return; }
  if (state.folders.some((f) => f.name.toLowerCase() === clean.toLowerCase())) {
    toast("新建文件夹重名了"); return;
  }
  state.folders.push({ id: "f" + (++state.seq), name: clean });
  invalidatePlan();
  render();
}

function renameFolder(id) {
  const f = state.folders.find((x) => x.id === id);
  if (!f) return;
  const name = prompt("新的文件夹名字", f.name);
  if (name === null) return;
  const clean = name.trim();
  if (!clean || clean === f.name) return;
  if (state.folders.some((x) => x.id !== id && x.name.toLowerCase() === clean.toLowerCase())) {
    toast("文件夹重名了"); return;
  }
  f.name = clean;
  invalidatePlan();
  render();
}

function deleteFolder(id) {
  const f = state.folders.find((x) => x.id === id);
  if (!f) return;
  if (!confirm(`删除新建文件夹「${f.name}」？里面的视频会回到未归类。`)) return;
  state.folders = state.folders.filter((x) => x.id !== id);
  Object.keys(state.assignments).forEach((vid) => {
    if (state.assignments[vid] === id) delete state.assignments[vid];
  });
  invalidatePlan();
  render();
}

function assignTo(folderId) {
  if (!state.dragging.length) return;
  state.dragging.forEach((vid) => { state.assignments[vid] = folderId; });
  invalidatePlan();
  render();
}

function unassign() {
  state.dragging.forEach((vid) => { delete state.assignments[vid]; });
  invalidatePlan();
  render();
}

/* ---------------- 行内改名 ---------------- */
function startRename(li, v) {
  if (li.querySelector("input")) return;
  const span = li.querySelector(".name");
  const input = document.createElement("input");
  input.type = "text";
  input.value = state.renames[v.id] || v.name;
  input.spellcheck = false;

  const commit = () => {
    if (!input.isConnected) return;
    const val = input.value.trim();
    const changed = val && val !== v.name;
    if (changed) state.renames[v.id] = val;
    else delete state.renames[v.id];
    invalidatePlan();
    render();
  };

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); commit(); }
    if (e.key === "Escape") { e.preventDefault(); input.remove(); }
  });
  input.addEventListener("blur", commit);
  span.replaceWith(input);
  input.focus();
  const dot = input.value.lastIndexOf(".");
  input.setSelectionRange(0, dot > 0 ? dot : input.value.length);
}

/* ---------------- 预览 ---------------- */
async function preview() {
  const payload = buildPayload();
  try {
    const r = await post("/api/organize/plan", { path: state.root, plan: payload });
    if (!r.ok) { toast(r.error || "预览失败"); return; }
    state.plan = r;
    $("#st-delete").textContent = String(r.totals.deleted);
    renderPlan(r);
    $("#plan-modal").classList.remove("hidden");
  } catch (e) {
    toast(e.message);
  }
}

function buildPayload() {
  const assignments = {};
  Object.keys(state.assignments).forEach((vid) => {
    assignments[vid] = folderName(state.assignments[vid]);
  });
  return {
    folders: state.folders.map((f) => f.name),
    assignments,
    renames: state.renames,
    keep: [...state.keep],
  };
}

function renderPlan(r) {
  const box = $("#plan-body");
  box.innerHTML = "";

  const summary = document.createElement("div");
  summary.className = "org-plan-summary";
  summary.innerHTML = `
    <span>新建 <b>${r.folders.length}</b> 个文件夹</span>
    <span>移动 <b>${r.moves.length}</b> 个视频</span>
    <span>改名 <b>${r.renamed}</b> 个</span>
    <span>字幕 <b>${r.subs}</b> 个</span>
    <span>删除 <b>${r.delete_dirs.length}</b> 个旧目录</span>
    <span>保留 <b>${r.keep.length}</b> 个目录</span>`;
  box.appendChild(summary);

  const list = document.createElement("ul");
  list.className = "org-plan-list";
  r.moves.slice(0, 60).forEach((m) => {
    const li = document.createElement("li");
    li.textContent = `${m.from_dir || "根目录"}\\${m.name}  →  ${m.to_dir}\\${m.final}`;
    if (m.final !== m.name) li.classList.add("renamed");
    list.appendChild(li);
  });
  r.stays.forEach((s) => {
    if (s.final === s.name) return;
    const li = document.createElement("li");
    li.textContent = `（原地改名）${s.name}  →  ${s.final}`;
    li.classList.add("renamed");
    list.appendChild(li);
  });
  if (r.moves.length > 60) {
    const li = document.createElement("li");
    li.textContent = `… 另外 ${r.moves.length - 60} 个移动`;
    list.appendChild(li);
  }
  box.appendChild(list);

  const del = document.createElement("div");
  del.className = "org-plan-del";
  del.textContent = r.delete_dirs.length
    ? "将删除：" + r.delete_dirs.slice(0, 20).map((d) => d.rel).join("、") + (r.delete_dirs.length > 20 ? " …" : "")
    : "没有需要删除的旧目录";
  box.appendChild(del);

  $("#plan-hint").textContent = r.other ? "未归类视频会放进「其他」" : "";
  $("#plan-ok").disabled = false;
}

/* ---------------- 执行 ---------------- */
async function applyPlan() {
  if (!state.plan) return;
  const ok = confirm(
    `确定执行吗？\n\n会移动/改名 ${state.plan.moves.length} 个视频，` +
    `并永久删除 ${state.plan.delete_dirs.length} 个旧文件夹（勾选保留的除外）。\n此操作不可撤销。`
  );
  if (!ok) return;

  $("#plan-ok").disabled = true;
  $("#btn-apply").disabled = true;
  try {
    const r = await post("/api/organize/apply", { path: state.root, plan: buildPayload() });
    if (!r.ok) {
      toast(r.error || "执行失败");
      if (r.rolled_back) toast("已回滚，原文件保持原样");
      return;
    }
    renderResult(r);
    $("#plan-modal").classList.add("hidden");
    await scan();
  } catch (e) {
    toast(e.message);
  }
}

function renderResult(r) {
  const box = $("#result-box");
  box.classList.remove("hidden", "err");
  box.innerHTML = "";
  const head = document.createElement("div");
  head.className = "result-head";
  head.textContent = `✅ 整理完成：移动 ${r.moved}、改名 ${r.renamed}、删除 ${r.deleted.length} 个目录`;
  box.appendChild(head);
  if (r.failed.length) {
    const note = document.createElement("div");
    note.className = "result-note";
    note.textContent = `${r.failed.length} 个目录没删掉：` +
      r.failed.slice(0, 5).map((f) => f.rel).join("、") + (r.failed.length > 5 ? " …" : "");
    note.title = r.failed.map((f) => `${f.rel}: ${f.error}`).join("\n");
    box.appendChild(note);
  }
}

/* ---------------- 绑定 ---------------- */
$("#btn-browse").addEventListener("click", browse);
$("#btn-scan").addEventListener("click", scan);
$("#btn-add-folder").addEventListener("click", addFolder);
$("#btn-plan").addEventListener("click", preview);
$("#btn-apply").addEventListener("click", () => {
  if (state.plan) $("#plan-modal").classList.remove("hidden");
  else toast("请先预览");
});

$("#unassigned-zone").addEventListener("dragover", (e) => {
  e.preventDefault();
  e.currentTarget.classList.add("drop-hover");
});
$("#unassigned-zone").addEventListener("dragleave", (e) => {
  e.currentTarget.classList.remove("drop-hover");
});
$("#unassigned-zone").addEventListener("drop", (e) => {
  e.preventDefault();
  e.currentTarget.classList.remove("drop-hover");
  unassign();
});

$("#folder-list").addEventListener("dragover", (e) => {
  const card = e.target.closest(".org-folder");
  if (!card) return;
  e.preventDefault();
  card.classList.add("drop-hover");
});
$("#folder-list").addEventListener("dragleave", (e) => {
  const card = e.target.closest(".org-folder");
  if (card) card.classList.remove("drop-hover");
});
$("#folder-list").addEventListener("drop", (e) => {
  const card = e.target.closest(".org-folder");
  if (!card) return;
  e.preventDefault();
  card.classList.remove("drop-hover");
  assignTo(card.dataset.folder);
});

$("#plan-close").addEventListener("click", () => $("#plan-modal").classList.add("hidden"));
$("#plan-cancel").addEventListener("click", () => $("#plan-modal").classList.add("hidden"));
$("#plan-ok").addEventListener("click", applyPlan);
$("#plan-modal").addEventListener("click", (e) => {
  if (e.target === $("#plan-modal")) $("#plan-modal").classList.add("hidden");
});

$("#path").addEventListener("keydown", (e) => { if (e.key === "Enter") scan(); });

$("#btn-quit").addEventListener("click", async () => {
  if (!confirm("关闭本地服务？关闭后页面将无法继续使用。")) return;
  try { await post("/api/shutdown", {}); } catch (e) { /* ignore */ }
  document.body.innerHTML =
    '<div style="height:100vh;display:grid;place-items:center;color:#8b94a7;font-size:15px;font-family:Segoe UI,Microsoft YaHei">服务已关闭，可以关掉这个窗口了</div>';
});

/* ---------------- 初始化 ---------------- */
(function init() {
  render();
  const saved = localStorage.getItem("comicpack.org.path");
  const fromUrl = new URLSearchParams(location.search).get("path");
  const start = fromUrl || saved;
  if (start) {
    $("#path").value = start;
    scan();
  }
})();
