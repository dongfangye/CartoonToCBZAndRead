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
};

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
    $("#path").value = r.root;
    localStorage.setItem(LS_PATH, r.root);
    $("#summary").innerHTML =
      `<span><b>${r.totals.videos}</b> 个视频</span><span>${r.totals.size_text}</span>`;
    renderList();
    if (r.items.length) {
      const wanted = new URLSearchParams(location.search).get("file");
      const pick = wanted ? r.items.find((i) => i.name === wanted) : null;
      playItem(pick || r.items[0], false);
    }
  } catch (e) {
    $("#summary").innerHTML = `<span class="bad">扫描失败：${e.message}</span>`;
  }
}

function renderList() {
  const ul = $("#playlist");
  ul.innerHTML = "";
  state.items.forEach((item) => {
    const li = document.createElement("li");
    li.className = "play-item";
    li.dataset.path = item.path;
    li.title = item.path;

    const name = document.createElement("span");
    name.className = "play-item-name";
    name.textContent = item.rel || item.name;

    const meta = document.createElement("span");
    meta.className = "play-item-meta";
    meta.textContent = item.size_text;

    li.append(name, meta);
    li.addEventListener("click", () => playItem(item, true));
    ul.appendChild(li);
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
  $("#error").textContent =
    `无法播放这个视频${code}：浏览器可能不支持它的编码/容器。` +
    `建议转成 H.264 + AAC 的 MP4；MKV、HEVC 在部分浏览器里放不了。`;
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
  if (!state.items.length) return;
  const idx = state.current ? state.items.findIndex((i) => i.path === state.current.path) : -1;
  const next = idx < 0 ? 0 : (idx + dir + state.items.length) % state.items.length;
  playItem(state.items[next], true);
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
