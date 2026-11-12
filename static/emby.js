/* ComicPack — Emby 剧集整理页
 * 扫描 → 填剧名/年份/季 → 预览 → 整理
 */

const $ = (sel) => document.querySelector(sel);

const state = {
  root: "",
  scan: null,
  preview: null,
  excluded: new Set(),
  force: false,
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
    state.excluded = new Set();
    state.force = false;
    $("#path").value = r.root;
    localStorage.setItem("comicpack.emby.path", r.root);
    updateForceUI();
    updateSummary();
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
    const pv = byId[r.id] || null;
    if (state.excluded.has(r.id)) tr.classList.add("excluded");

    const pick = document.createElement("td");
    pick.className = "rt-pick";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    const canPick = state.force || r.status === "ok";
    cb.checked = canPick && !state.excluded.has(r.id);
    cb.disabled = !canPick;
    cb.title = !canPick
      ? "识别不出集数；点上方「全选并强制编号」可以按顺序强行编号"
      : "取消勾选就跳过这个文件";
    cb.addEventListener("change", () => {
      if (cb.checked) state.excluded.delete(r.id);
      else state.excluded.add(r.id);
      state.preview = null;
      $("#btn-apply").disabled = true;
      renderRows(state.scan.rows, null);
      updateSummary();
    });
    pick.appendChild(cb);

    const old = document.createElement("td");
    old.className = "rt-old";
    old.textContent = r.name;
    old.title = r.rel;

    const se = document.createElement("td");
    se.className = "rt-se";
    const epi = pv && pv.episode != null ? pv.episode : r.episode;
    se.textContent = epi != null ? String(epi) : "—";

    const neu = document.createElement("td");
    neu.className = "rt-new";
    neu.textContent = pv ? pv.new_name || "—" : "—";

    const st = document.createElement("td");
    const badge = document.createElement("span");
    if (state.excluded.has(r.id)) {
      badge.className = "badge skip";
      badge.textContent = "已取消";
    } else if (pv && pv.status === "ok") {
      badge.className = "badge ok";
      badge.textContent = pv.forced ? "强制编号" : "可整理";
    } else if (state.force) {
      badge.className = "badge ok";
      badge.textContent = "将按顺序编号";
    } else if (r.status === "ok") {
      badge.className = "badge ok";
      badge.textContent = "识别到";
    } else {
      badge.className = "badge skip";
      badge.textContent = "无法识别";
    }
    const reason = pv ? pv.reason : r.reason;
    if (reason) badge.title = reason;
    st.appendChild(badge);

    tr.append(pick, old, se, neu, st);
    frag.appendChild(tr);
  });
  tbody.appendChild(frag);
}

/* 顶部汇总：参与 / 已取消 / 无法识别 */
function updateSummary() {
  const scan = state.scan;
  if (!scan) return;
  const excluded = state.excluded.size;
  const joined = Math.max(0, scan.totals.videos - excluded);
  if (state.force) {
    $("#summary").innerHTML =
      `<span><b>${scan.totals.videos}</b> 个视频</span>` +
      `<span class="ok-text">强制编号：${joined} 个按当前顺序编号</span>` +
      (excluded ? `<span class="warn">${excluded} 个已取消</span>` : "");
    $("#btn-preview").disabled = joined === 0;
    return;
  }
  const parsed = scan.totals.ok - excluded;
  $("#summary").innerHTML =
    `<span><b>${scan.totals.videos}</b> 个视频</span>` +
    `<span class="ok-text">${Math.max(0, parsed)} 个参与</span>` +
    (excluded ? `<span class="warn">${excluded} 个已取消</span>` : "") +
    (scan.totals.skip ? `<span class="bad">${scan.totals.skip} 个无法识别</span>` : "");
  $("#btn-preview").disabled = parsed <= 0;
}

/* 强制编号按钮的文案与提示 */
function updateForceUI() {
  const btn = $("#btn-force");
  btn.textContent = state.force ? "取消强制编号" : "全选并强制编号";
  btn.classList.toggle("btn-primary", state.force);
  $("#force-hint").textContent = state.force
    ? "已开启：忽略文件名，按当前列表顺序从 E01 开始编号"
    : "默认规则认不出集数时，点左边按钮按顺序编号";
}

/* ---------------- 预览与执行 ---------------- */
function payload() {
  return {
    show: $("#show").value.trim(),
    year: $("#year").value.trim(),
    season: $("#season").value.trim() || "1",
    exclude: [...state.excluded],
    force: state.force,
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
      `<span class="ok-text">${r.totals.ok} 个参与${r.force ? "（强制编号）" : ""}</span>` +
      (r.totals.excluded ? `<span class="warn">${r.totals.excluded} 个已取消</span>` : "") +
      (r.missing.length ? `<span class="warn">缺号：${r.missing.join(", ")}</span>` : "");
    renderRows(state.scan.rows, r.rows);
    $("#btn-apply").disabled = r.totals.ok === 0;
  } catch (e) {
    toast(e.message);
  }
}

async function applyPlan() {
  if (!state.preview) return;
  const ok = confirm(
    `将把 ${state.preview.totals.ok} 个视频整理到：\n${state.preview.target_dir}\n\n` +
    `没勾选「参与」的视频会留在原文件夹。\n确定继续吗？`
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
  if (r.excluded) {
    const more = document.createElement("div");
    more.className = "result-note";
    more.textContent = `跳过 ${r.excluded} 个（已取消参与）`;
    box.appendChild(more);
  }
}

/* ---------------- 绑定 ---------------- */
$("#btn-browse").addEventListener("click", browse);
$("#btn-scan").addEventListener("click", scan);
$("#btn-preview").addEventListener("click", preview);
$("#btn-apply").addEventListener("click", applyPlan);
$("#btn-force").addEventListener("click", () => {
  if (!state.scan) { toast("请先扫描文件夹"); return; }
  state.force = !state.force;
  if (state.force) state.excluded.clear();
  state.preview = null;
  $("#btn-apply").disabled = true;
  updateForceUI();
  updateSummary();
  renderRows(state.scan.rows, null);
});

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
  updateForceUI();
  renderRows([], null);
  const saved = localStorage.getItem("comicpack.emby.path");
  const fromUrl = new URLSearchParams(location.search).get("path");
  const start = fromUrl || saved;
  if (start) {
    $("#path").value = start;
    scan();
  }
})();
