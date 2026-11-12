/* ComicPack — Emby 剧集整理页
 * 扫描 → 填剧名/年份/季 → 预览 → 整理
 */

const $ = (sel) => document.querySelector(sel);

const state = {
  root: "",
  scan: null,
  preview: null,
  running: false,
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

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
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
    const r = await post("/api/emby/scan", { path });
    if (!r.ok) throw new Error(r.error || "扫描失败");
    state.root = r.root;
    state.scan = r;
    state.preview = null;
    $("#path").value = r.root;
    localStorage.setItem("comicpack.emby.path", r.root);
    $("#summary").innerHTML =
      `<span><b>${r.totals.videos}</b> 个视频</span>` +
      `<span class="ok-text">${r.totals.ok} 个识别出集数</span>` +
      (r.totals.skip ? `<span class="bad">${r.totals.skip} 个无法识别</span>` : "");
    $("#btn-preview").disabled = !r.totals.ok;
    $("#btn-apply").disabled = true;
    renderRows(r.rows, null);
  } catch (e) {
    state.scan = null;
    $("#summary").innerHTML = `<span class="bad">扫描失败：${escapeHtml(e.message)}</span>`;
    renderRows([], null);
  }
}

/* ---------------- 渲染 ---------------- */
function renderRows(rows, previewRows) {
  const table = $("#table");
  const tbody = $("#rows");
  const empty = $("#list-empty");
  tbody.innerHTML = "";

  if (!rows.length) {
    table.classList.add("hidden");
    empty.classList.remove("hidden");
    return;
  }
  table.classList.remove("hidden");
  empty.classList.add("hidden");

  const byId = {};
  (previewRows || []).forEach((r) => { byId[r.id] = r; });

  const frag = document.createDocumentFragment();
  rows.forEach((r) => {
    const tr = document.createElement("tr");

    const old = document.createElement("td");
    old.className = "rt-old";
    old.textContent = r.name;
    old.title = r.rel;

    const se = document.createElement("td");
    se.className = "rt-se";
    se.textContent = r.episode != null ? String(r.episode) : "—";

    const neu = document.createElement("td");
    neu.className = "rt-new";
    const p = byId[r.id];
    neu.textContent = p ? p.new_name || "—" : "—";

    const st = document.createElement("td");
    const badge = document.createElement("span");
    const status = p ? p.status : r.status;
    badge.className = "badge " + status;
    badge.textContent = status === "ok" ? (p ? "可整理" : "识别到") : "无法识别";
    if (r.reason) badge.title = r.reason;
    st.appendChild(badge);

    tr.append(old, se, neu, st);
    frag.appendChild(tr);
  });
  tbody.appendChild(frag);
}

/* ---------------- 预览与执行 ---------------- */
function payload() {
  return {
    show: $("#show").value.trim(),
    year: $("#year").value.trim(),
    season: $("#season").value.trim() || "1",
    delete_empty: $("#delete-empty").checked,
  };
}

async function preview() {
  const show = $("#show").value.trim();
  if (!show) { toast("请先填剧名"); $("#show").focus(); return; }
  try {
    const r = await post("/api/emby/build", { path: state.root, plan: payload() });
    if (!r.ok) { toast(r.error || "预览失败"); return; }
    state.preview = r;
    $("#summary").innerHTML =
      `<span>目标：<b>${escapeHtml(r.show_dir)}\\${escapeHtml(r.season_dir)}</b></span>` +
      `<span class="ok-text">${r.totals.ok} 个视频</span>` +
      (r.missing.length ? `<span class="warn">缺号：${r.missing.join(", ")}</span>` : "");
    renderRows(state.scan.rows, r.rows);
    $("#btn-apply").disabled = false;
  } catch (e) {
    toast(e.message);
  }
}

async function applyPlan() {
  if (!state.preview) return;
  const ok = confirm(
    `将把 ${state.preview.totals.ok} 个视频整理到：\n${state.preview.target_dir}\n\n` +
    `原目录里被移空的文件夹会被删除（不删非空目录）。\n确定继续吗？`
  );
  if (!ok) return;

  state.running = true;
  $("#btn-apply").disabled = true;
  $("#btn-preview").disabled = true;
  try {
    const r = await post("/api/emby/apply", { path: state.root, plan: payload() });
    if (!r.ok) {
      toast(r.error || "整理失败");
      if (r.rolled_back) toast("已回滚，原文件保持原样");
      return;
    }
    renderResult(r);
    await scan();
  } catch (e) {
    toast(e.message);
  } finally {
    state.running = false;
    $("#btn-preview").disabled = !(state.scan && state.scan.totals.ok);
  }
}

function renderResult(r) {
  const box = $("#result-box");
  box.classList.remove("hidden", "err");
  box.innerHTML = "";
  const head = document.createElement("div");
  head.className = "result-head";
  head.textContent = `✅ 整理完成：${r.moved} 个视频${r.subs ? " + " + r.subs + " 个字幕" : ""}`;
  box.appendChild(head);
  const note = document.createElement("div");
  note.className = "result-note";
  note.textContent = `输出：${r.target_dir}`;
  note.title = r.target_dir;
  box.appendChild(note);
  if (r.removed_dirs && r.removed_dirs.length) {
    const more = document.createElement("div");
    more.className = "result-note";
    more.textContent = `清理空目录 ${r.removed_dirs.length} 个`;
    box.appendChild(more);
  }
}

/* ---------------- 绑定 ---------------- */
$("#btn-browse").addEventListener("click", browse);
$("#btn-scan").addEventListener("click", scan);
$("#btn-preview").addEventListener("click", preview);
$("#btn-apply").addEventListener("click", applyPlan);

$("#path").addEventListener("keydown", (e) => { if (e.key === "Enter") scan(); });

["show", "year", "season"].forEach((id) => {
  $("#" + id).addEventListener("input", () => {
    state.preview = null;
    $("#btn-apply").disabled = true;
  });
});

$("#btn-quit").addEventListener("click", async () => {
  if (!confirm("关闭本地服务？关闭后页面将无法继续使用。")) return;
  try { await post("/api/shutdown", {}); } catch (e) { /* ignore */ }
  document.body.innerHTML =
    '<div style="height:100vh;display:grid;place-items:center;color:#8b94a7;font-size:15px;font-family:Segoe UI,Microsoft YaHei">服务已关闭，可以关掉这个窗口了</div>';
});

/* ---------------- 初始化 ---------------- */
(function init() {
  renderRows([], null);
  const saved = localStorage.getItem("comicpack.emby.path");
  const fromUrl = new URLSearchParams(location.search).get("path");
  const start = fromUrl || saved;
  if (start) {
    $("#path").value = start;
    scan();
  }
})();
