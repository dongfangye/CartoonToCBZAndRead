/* ComicPack — 手动改名页
 * 扫描（只列这一层）→ 双击文件名编辑 → 确认改名
 */

const $ = (sel) => document.querySelector(sel);

const state = {
  root: "",
  videos: [],
  edits: new Map(),      // 原文件名 -> 新文件名
};

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
  $("#summary").textContent = "正在扫描…";
  try {
    const r = await post("/api/rename/scan", { path });
    if (!r.ok) throw new Error(r.error || "扫描失败");
    state.root = r.root;
    state.videos = r.videos;
    state.edits.clear();
    $("#path").value = r.root;
    localStorage.setItem("comicpack.rename.path", r.root);
    $("#summary").innerHTML =
      `<span><b>${r.totals.videos}</b> 个视频</span>` +
      `<span>${r.totals.size_text}</span>` +
      (r.ignored ? `<span class="warn">忽略 ${r.ignored} 个非视频文件</span>` : "");
    render();
  } catch (e) {
    state.videos = [];
    $("#summary").innerHTML = `<span class="bad">扫描失败：${e.message}</span>`;
    render();
  }
}

/* ---------------- 渲染 ---------------- */
function render() {
  const tbody = $("#rows");
  const table = $("#table");
  const empty = $("#list-empty");
  tbody.innerHTML = "";

  if (!state.videos.length) {
    table.classList.add("hidden");
    empty.classList.remove("hidden");
    updateStats();
    return;
  }
  table.classList.remove("hidden");
  empty.classList.add("hidden");

  const frag = document.createDocumentFragment();
  state.videos.forEach((v) => {
    const tr = document.createElement("tr");
    const edited = state.edits.has(v.name);
    if (edited) tr.classList.add("edited");

    const name = document.createElement("td");
    name.className = "rt-old";
    name.textContent = edited ? state.edits.get(v.name) : v.name;
    name.title = (edited ? "原名：" + v.name + "　" : "") + "双击改名";
    name.addEventListener("dblclick", () => startEdit(name, v));

    const size = document.createElement("td");
    size.className = "rt-title";
    size.textContent = v.size_text;

    const st = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = "badge " + (edited ? "ok" : "skip");
    badge.textContent = edited ? "已修改" : "未修改";
    st.appendChild(badge);

    tr.append(name, size, st);
    frag.appendChild(tr);
  });
  tbody.appendChild(frag);
  updateStats();
}

function updateStats() {
  $("#st-total").textContent = String(state.videos.length);
  $("#st-edited").textContent = String(state.edits.size);
  $("#btn-apply").disabled = state.edits.size === 0;
}

/* ---------------- 双击改名 ---------------- */
function startEdit(td, video) {
  if (td.querySelector("input")) return;
  const input = document.createElement("input");
  input.type = "text";
  input.spellcheck = false;
  input.value = state.edits.get(video.name) || video.name;
  input.className = "rt-input";

  const commit = () => {
    if (!input.isConnected) return;
    const val = input.value.trim();
    if (!val || val === video.name) state.edits.delete(video.name);
    else state.edits.set(video.name, val);
    render();
  };

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); commit(); }
    if (e.key === "Escape") { e.preventDefault(); input.remove(); }
  });
  input.addEventListener("blur", commit);

  td.textContent = "";
  td.appendChild(input);
  input.focus();
  const dot = input.value.lastIndexOf(".");
  input.setSelectionRange(0, dot > 0 ? dot : input.value.length);
}

/* ---------------- 应用 ---------------- */
async function applyEdits() {
  if (!state.edits.size) return;
  const names = [...state.edits.entries()].map(([from, to]) => ({ from, to }));
  if (!confirm(`确定把 ${names.length} 个文件改成新名字吗？`)) return;

  $("#btn-apply").disabled = true;
  try {
    const r = await post("/api/rename/apply", { path: state.root, names });
    if (!r.ok) { toast(r.error || "改名失败"); return; }
    showResult(r);
    await scan();
  } catch (e) {
    toast(e.message);
  } finally {
    $("#btn-apply").disabled = state.edits.size === 0;
  }
}

function showResult(r) {
  const box = $("#result-box");
  box.classList.remove("hidden", "err");
  box.innerHTML = "";
  const head = document.createElement("div");
  head.className = "result-head";
  head.textContent = `✅ 改名完成：${r.renamed.length} 个` +
    (r.skipped.length ? `，跳过 ${r.skipped.length} 个` : "");
  box.appendChild(head);
  r.skipped.slice(0, 5).forEach((s) => {
    const row = document.createElement("div");
    row.className = "result-note";
    row.textContent = `${s.name}：${s.reason}`;
    box.appendChild(row);
  });
}

/* ---------------- 绑定 ---------------- */
$("#btn-browse").addEventListener("click", browse);
$("#btn-scan").addEventListener("click", scan);
$("#btn-apply").addEventListener("click", applyEdits);
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
  const saved = localStorage.getItem("comicpack.rename.path");
  const fromUrl = new URLSearchParams(location.search).get("path");
  const start = fromUrl || saved;
  if (start) {
    $("#path").value = start;
    scan();
  }
})();
