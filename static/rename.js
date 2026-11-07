/* ComicPack — 视频改名页（原生 JS，无框架） */

const $ = (sel) => document.querySelector(sel);

const state = {
  result: null,
  running: false,
};

const STATUS_TEXT = {
  ok: "可改名",
  same: "已是规范名",
  conflict: "冲突",
  skip: "无法识别",
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

/* ---------------- 选择与扫描 ---------------- */
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
    const res = await post("/api/rename/scan", { path });
    if (!res.ok) {
      state.result = null;
      $("#summary").innerHTML = `<span class="bad">${escapeHtml(res.error || "扫描失败")}</span>`;
      render();
      return;
    }
    state.result = res;
    $("#path").value = res.folder;
    localStorage.setItem("comicpack.rename.path", res.folder);
    render();
  } catch (e) {
    state.result = null;
    $("#summary").innerHTML = `<span class="bad">扫描失败：${escapeHtml(e.message)}</span>`;
    render();
  }
}

/* ---------------- 渲染 ---------------- */
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function render() {
  const res = state.result;
  const stats = (res && res.stats) || { total: 0, ok: 0, same: 0, conflict: 0, skip: 0, ignored: 0 };

  $("#st-ok").textContent = String(stats.ok);
  $("#st-same").textContent = String(stats.same);
  $("#st-bad").textContent = String(stats.conflict + stats.skip);

  if (!res) {
    $("#summary").textContent = "尚未选择文件夹";
    $("#table").classList.add("hidden");
    $("#list-empty").classList.remove("hidden");
    $("#btn-apply").disabled = true;
    return;
  }

  const bits = [`<span><b>${stats.total}</b> 个视频</span>`];
  if (stats.ok) bits.push(`<span class="ok-text">${stats.ok} 个可改名</span>`);
  if (stats.same) bits.push(`<span>${stats.same} 个已是规范名</span>`);
  if (stats.conflict) bits.push(`<span class="warn">${stats.conflict} 个冲突</span>`);
  if (stats.skip) bits.push(`<span class="bad">${stats.skip} 个无法识别</span>`);
  if (stats.ignored) bits.push(`<span>忽略 ${stats.ignored} 个非视频文件</span>`);
  $("#summary").innerHTML = bits.join("");

  const table = $("#table");
  const tbody = $("#rows");
  tbody.innerHTML = "";

  if (!res.items.length) {
    table.classList.add("hidden");
    $("#list-empty").classList.remove("hidden");
    $("#btn-apply").disabled = true;
    return;
  }

  table.classList.remove("hidden");
  $("#list-empty").classList.add("hidden");

  const frag = document.createDocumentFragment();
  res.items.forEach((it) => {
    const tr = document.createElement("tr");

    const old = document.createElement("td");
    old.className = "rt-old";
    old.textContent = it.name;
    old.title = it.name;

    const title = document.createElement("td");
    title.className = "rt-title";
    title.textContent = it.title || "—";

    const se = document.createElement("td");
    se.className = "rt-se";
    se.textContent = it.season ? `S${String(it.season).padStart(2, "0")}E${String(it.episode).padStart(2, "0")}` : "—";

    const neu = document.createElement("td");
    neu.className = "rt-new";
    neu.textContent = it.new_name || "—";
    neu.title = it.new_name || "";

    const st = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = "badge " + it.status;
    badge.textContent = STATUS_TEXT[it.status] || it.status;
    st.appendChild(badge);
    if (it.reason) {
      badge.title = it.reason;
      st.title = it.reason;
    }

    tr.append(old, title, se, neu, st);
    frag.appendChild(tr);
  });
  tbody.appendChild(frag);

  $("#btn-apply").disabled = state.running || !stats.ok;
}

/* ---------------- 执行 ---------------- */
async function apply() {
  const res = state.result;
  if (!res || !res.stats.ok) return;

  const ok = confirm(
    `将把 ${res.stats.ok} 个视频改成 Emby 命名。\n` +
    `只改文件名，不移动、不覆盖；确定继续吗？`
  );
  if (!ok) return;

  state.running = true;
  const btn = $("#btn-apply");
  btn.disabled = true;
  btn.textContent = "改名中…";

  try {
    const out = await post("/api/rename/apply", { path: res.folder });
    if (!out.ok) throw new Error(out.error || "改名失败");
    renderResult(out);
    await scan();
  } catch (e) {
    toast(e.message);
  } finally {
    state.running = false;
    btn.textContent = "开始改名";
    render();
  }
}

function renderResult(out) {
  const box = $("#result-box");
  box.classList.remove("hidden", "err");
  box.innerHTML = "";

  const head = document.createElement("div");
  head.className = "result-head";
  head.textContent = `✅ 改名完成：${out.renamed.length} 个`;
  box.appendChild(head);

  out.renamed.slice(0, 8).forEach((r) => {
    const row = document.createElement("div");
    row.className = "result-item";
    const name = document.createElement("span");
    name.className = "result-name";
    name.textContent = r.to;
    name.title = `${r.from} → ${r.to}`;
    row.appendChild(name);
    box.appendChild(row);
  });
  if (out.renamed.length > 8) {
    const more = document.createElement("div");
    more.className = "result-note";
    more.textContent = `… 另外 ${out.renamed.length - 8} 个`;
    box.appendChild(more);
  }
  if (out.failed.length) {
    const note = document.createElement("div");
    note.className = "result-note";
    note.textContent = `${out.failed.length} 个失败：` + out.failed.map((f) => f.name).join("，");
    box.appendChild(note);
  }
}

/* ---------------- 绑定 ---------------- */
$("#btn-browse").addEventListener("click", browse);
$("#btn-scan").addEventListener("click", scan);
$("#btn-apply").addEventListener("click", apply);

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
  render();
  const saved = localStorage.getItem("comicpack.rename.path");
  const fromUrl = new URLSearchParams(location.search).get("path");
  const start = fromUrl || saved;
  if (start) {
    $("#path").value = start;
    scan();
  }
})();
