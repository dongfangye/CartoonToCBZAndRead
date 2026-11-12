/* ComicPack — 播放页
 * 播放列表 + 自定义控制条：进度 / 倍速 / 音量 / 全屏
 */

const $ = (sel) => document.querySelector(sel);
const video = $("#video");

const state = {
  root: "",
  items: [],
  current: null,
  seeking: false,
  query: "",
  probe: new Map(),     // path -> {ok, codec, playable}
};

const BAD_EXTS = new Set([".mkv", ".avi", ".wmv", ".flv", ".rm", ".rmvb",
                          ".mpg", ".mpeg", ".ts", ".m2ts", ".mts"]);

const LS_VOLUME = "comicpack.play.volume";
const LS_SPEED = "comicpack.play.speed";
const LS_PATH = "comicpack.play.path";

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

async function getJson(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error("HTTP " + res.status);
  return res.json();
}

function fmtTime(sec) {
  if (!Number.isFinite(sec) || sec < 0) return "00:00";
  sec = Math.floor(sec);
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const mm = String(m).padStart(2, "0");
  const ss = String(s).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

/* ---------------- 列表 ---------------- */
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
  if (!path) { toast("请先选择文件夹或视频文件"); return; }
  $("#summary").textContent = "正在扫描…";
  try {
    const r = await post("/api/video/list", { path });
    if (!r.ok) throw new Error(r.error || "扫描失败");
    state.root = r.root;
    state.items = r.items;
    state.query = "";
    $("#filter").value = "";
    $("#path").value = r.root;
    localStorage.setItem(LS_PATH, r.root);
    const dirs = new Set(r.items.map((i) => i.dir || ""));
    $("#summary").innerHTML =
      `<span><b>${r.totals.videos}</b> 个视频</span>` +
      `<span>来自 ${dirs.size} 个文件夹</span>` +
      `<span>${r.totals.size_text}</span>`;
    renderList();
    if (r.items.length) {
      const wanted = new URLSearchParams(location.search).get("file");
      const pick = wanted ? r.items.find((i) => i.name === wanted) : null;
      const first = pick || r.items.find((i) => !unplayableByExt(i)) || r.items[0];
      playItem(first, false);
      probeAll();
    }
  } catch (e) {
    $("#summary").innerHTML = `<span class="bad">扫描失败：${e.message}</span>`;
  }
}

function visibleItems() {
  const q = state.query.trim().toLowerCase();
  if (!q) return state.items;
  return state.items.filter((i) => (i.rel || i.name).toLowerCase().includes(q));
}

/* ---------------- 能不能播 ---------------- */
function unplayableByExt(item) {
  return BAD_EXTS.has((item.ext || "").toLowerCase());
}

function isUnplayable(item) {
  const probed = state.probe.get(item.path);
  if (probed && probed.ok) return probed.playable === false;
  return unplayableByExt(item);
}

function markItem(item) {
  const li = [...document.querySelectorAll(".play-item")]
    .find((el) => el.dataset.path === item.path);
  if (!li) return;
  const probed = state.probe.get(item.path);
  const meta = li.querySelector(".play-item-meta");
  if (meta && probed && probed.ok && probed.codec) {
    meta.textContent = `${item.size_text} · ${probed.codec.toUpperCase()}`;
  }
  if (isUnplayable(item)) {
    li.classList.add("unplayable");
    const codec = (probed && probed.ok && probed.codec)
      ? probed.codec.toUpperCase()
      : (item.ext || "").replace(".", "").toUpperCase();
    li.title = `${item.rel}\n浏览器可能放不了（${codec}），可点右侧 ↗ 用系统播放器打开`;
    if (!li.querySelector(".play-open")) {
      const btn = document.createElement("button");
      btn.className = "play-open";
      btn.type = "button";
      btn.textContent = "↗";
      btn.title = "用系统播放器打开";
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        openInSystem(item);
      });
      li.appendChild(btn);
    }
  } else {
    li.classList.remove("unplayable");
    li.title = item.path;
  }
}

async function openInSystem(item) {
  try {
    const r = await post("/api/video/open", { path: item.path });
    toast(r.ok ? "已用系统播放器打开" : (r.error || "打开失败"));
  } catch (e) {
    toast(e.message);
  }
}

/* 后台探测编码：让 HEVC/MKV 这类置灰（最多探 200 个，避免大目录卡住） */
async function probeAll() {
  const queue = state.items
    .filter((i) => !state.probe.has(i.path) && !unplayableByExt(i))
    .slice(0, 200);
  const worker = async () => {
    while (queue.length) {
      const item = queue.shift();
      try {
        const res = await getJson("/api/video/probe?path=" + encodeURIComponent(item.path));
        state.probe.set(item.path, res);
      } catch (e) {
        state.probe.set(item.path, { ok: false });
      }
      markItem(item);
    }
  };
  await Promise.all([worker(), worker(), worker()]);
}

function renderList() {
  const ul = $("#playlist");
  ul.innerHTML = "";
  const items = visibleItems();
  if (!items.length) {
    const li = document.createElement("li");
    li.className = "play-empty-item";
    li.textContent = state.query ? "没有匹配的视频" : "这个文件夹里没有视频";
    ul.appendChild(li);
    return;
  }

  let lastDir = null;
  items.forEach((item) => {
    const dir = item.dir || "";
    if (dir !== lastDir) {
      const head = document.createElement("li");
      head.className = "play-folder";
      head.textContent = dir || "（根目录）";
      head.title = dir;
      ul.appendChild(head);
      lastDir = dir;
    }

    const li = document.createElement("li");
    li.className = "play-item";
    li.dataset.path = item.path;
    li.title = item.path;

    const name = document.createElement("span");
    name.className = "play-item-name";
    name.textContent = item.name;

    const meta = document.createElement("span");
    meta.className = "play-item-meta";
    meta.textContent = item.size_text;

    li.append(name, meta);
    li.addEventListener("click", () => {
      if (isUnplayable(item)) {
        toast("浏览器可能放不了这个编码，点右侧 ↗ 用系统播放器打开");
        return;
      }
      playItem(item, true);
    });
    ul.appendChild(li);
    markItem(item);
  });
  highlight();
}

function highlight() {
  document.querySelectorAll(".play-item").forEach((li) => {
    li.classList.toggle("active", !!state.current && li.dataset.path === state.current.path);
  });
}

/* ---------------- 播放 ---------------- */
function playItem(item, autoplay) {
  if (!item) return;
  state.current = item;
  $("#info-title").textContent = item.rel || item.name;
  $("#info-title").title = item.path;
  $("#info-size").textContent = item.size_text;
  $("#info-res").textContent = "";
  $("#empty").classList.add("hidden");
  $("#error").classList.add("hidden");

  video.src = "/api/video/stream?path=" + encodeURIComponent(item.path);
  video.load();
  video.playbackRate = parseFloat($("#speed").value) || 1;
  if (autoplay) video.play().catch(() => {});
  highlight();
  updateTime();
}

function updateTime() {
  const dur = video.duration;
  const cur = video.currentTime;
  $("#time").textContent = `${fmtTime(cur)} / ${Number.isFinite(dur) ? fmtTime(dur) : "--:--"}`;
  if (!state.seeking && Number.isFinite(dur) && dur > 0) {
    $("#seek").value = String(Math.round((cur / dur) * 1000));
  }
}

/* ---------------- 控制条 ---------------- */
$("#btn-play").addEventListener("click", () => {
  if (!state.current) return;
  if (video.paused) video.play().catch(() => {});
  else video.pause();
});

video.addEventListener("play", () => { $("#btn-play").textContent = "❚❚"; });
video.addEventListener("pause", () => { $("#btn-play").textContent = "▶"; });
video.addEventListener("timeupdate", updateTime);
video.addEventListener("durationchange", updateTime);

video.addEventListener("loadedmetadata", () => {
  const w = video.videoWidth;
  const h = video.videoHeight;
  if (w && h) {
    const is4k = w >= 3800 || h >= 2000;
    const badge = $("#info-res");
    badge.textContent = `${w}×${h}${is4k ? " · 4K" : ""}`;
    badge.classList.toggle("is-4k", is4k);
  }
  updateTime();
});

video.addEventListener("error", () => {
  if (!video.currentSrc) return;
  const err = video.error;
  const code = err ? `（code ${err.code}）` : "";
  const item = state.current;
  const probed = item ? state.probe.get(item.path) : null;
  const codec = probed && probed.ok && probed.codec ? `（${probed.codec.toUpperCase()}）` : "";
  $("#error").textContent =
    `无法播放这个视频${code}${codec}：浏览器可能不支持它的编码/容器。` +
    `可以点列表里该视频右侧的 ↗ 用系统播放器打开；` +
    `也可以转成 H.264 + AAC 的 MP4。`;
  $("#error").classList.remove("hidden");
});

$("#seek").addEventListener("input", () => {
  const dur = video.duration;
  if (!Number.isFinite(dur) || dur <= 0) return;
  const ratio = Number($("#seek").value) / 1000;
  video.currentTime = Math.min(dur - 0.05, Math.max(0, ratio * dur));
  $("#time").textContent = `${fmtTime(video.currentTime)} / ${fmtTime(dur)}`;
});
$("#seek").addEventListener("pointerdown", () => { state.seeking = true; });
$("#seek").addEventListener("pointerup", () => { state.seeking = false; });
$("#seek").addEventListener("change", () => { state.seeking = false; });

/* 倍速 */
$("#speed").addEventListener("change", () => {
  video.playbackRate = parseFloat($("#speed").value) || 1;
  localStorage.setItem(LS_SPEED, String(video.playbackRate));
  toast(`倍速 ${video.playbackRate}×`);
});

/* 音量 */
function applyVolume(percent, save = true) {
  const v = Math.max(0, Math.min(100, percent));
  video.volume = v / 100;
  video.muted = v === 0;
  $("#volume").value = String(v);
  $("#vol-label").textContent = `${v}%`;
  $("#btn-mute").textContent = v === 0 ? "🔇" : "🔊";
  if (save) localStorage.setItem(LS_VOLUME, String(v));
}

$("#volume").addEventListener("input", () => applyVolume(Number($("#volume").value)));
$("#btn-mute").addEventListener("click", () => {
  const cur = Number($("#volume").value);
  applyVolume(cur > 0 ? 0 : (Number(localStorage.getItem(LS_VOLUME)) || 100));
});

/* 全屏 */
$("#btn-full").addEventListener("click", () => {
  const stage = document.querySelector(".play-stage");
  if (document.fullscreenElement) document.exitFullscreen();
  else stage.requestFullscreen && stage.requestFullscreen();
});

/* 键盘 */
document.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT" || e.target.tagName === "TEXTAREA") return;
  if (e.code === "Space") { e.preventDefault(); $("#btn-play").click(); }
  else if (e.code === "ArrowRight") { video.currentTime = Math.min(video.duration || 0, video.currentTime + 5); }
  else if (e.code === "ArrowLeft") { video.currentTime = Math.max(0, video.currentTime - 5); }
  else if (e.code === "ArrowUp") { e.preventDefault(); applyVolume(Number($("#volume").value) + 5); }
  else if (e.code === "ArrowDown") { e.preventDefault(); applyVolume(Number($("#volume").value) - 5); }
  else if (e.code === "KeyF") { $("#btn-full").click(); }
  else if (e.code === "KeyM") { $("#btn-mute").click(); }
});

/* 滚轮切换上/下一个视频（只在播放器区域；左侧列表保留自己的滚动） */
let wheelAcc = 0;
let wheelLock = 0;

function stepItem(dir) {
  const items = visibleItems();
  if (!items.length) return;
  const idx = state.current ? items.findIndex((i) => i.path === state.current.path) : -1;
  const next = idx < 0 ? 0 : (idx + dir + items.length) % items.length;
  playItem(items[next], true);
}

document.querySelector(".play-stage").addEventListener("wheel", (e) => {
  if (!state.items.length) return;
  e.preventDefault();
  const now = Date.now();
  if (now - wheelLock < 280) return;
  wheelAcc += e.deltaY;
  if (Math.abs(wheelAcc) < 60) return;
  const dir = wheelAcc > 0 ? 1 : -1;
  wheelAcc = 0;
  wheelLock = now;
  stepItem(dir);
}, { passive: false });

/* 记住上次的倍速和音量 */
(function restorePrefs() {
  const speed = localStorage.getItem(LS_SPEED);
  if (speed && [...$("#speed").options].some((o) => o.value === speed)) {
    $("#speed").value = speed;
  }
  const vol = localStorage.getItem(LS_VOLUME);
  applyVolume(vol == null ? 100 : Number(vol), false);
  video.playbackRate = parseFloat($("#speed").value) || 1;
})();

/* ---------------- 绑定与初始化 ---------------- */
$("#btn-browse").addEventListener("click", browse);
$("#btn-scan").addEventListener("click", scan);
$("#path").addEventListener("keydown", (e) => { if (e.key === "Enter") scan(); });
$("#filter").addEventListener("input", () => {
  state.query = $("#filter").value;
  renderList();
});

$("#btn-quit").addEventListener("click", async () => {
  if (!confirm("关闭本地服务？关闭后页面将无法继续使用。")) return;
  try { await post("/api/shutdown", {}); } catch (e) { /* ignore */ }
  document.body.innerHTML =
    '<div style="height:100vh;display:grid;place-items:center;color:#8b94a7;font-size:15px;font-family:Segoe UI,Microsoft YaHei">服务已关闭，可以关掉这个窗口了</div>';
});

(function init() {
  const saved = localStorage.getItem(LS_PATH);
  const fromUrl = new URLSearchParams(location.search).get("path");
  const start = fromUrl || saved;
  if (start) {
    $("#path").value = start;
    scan();
  }
})();
