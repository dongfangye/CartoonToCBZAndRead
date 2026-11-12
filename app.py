# -*- coding: utf-8 -*-
"""
app.py — ComicPack 本地 Web 服务
================================
双击「启动.bat」→ 起一个只监听 127.0.0.1 的小服务 → 自动用浏览器「应用窗口」打开页面。

两个页面：
  /        打包：图片文件夹 → CBZ / ZIP
  /reader  阅读：直接翻看 CBZ / ZIP（或图片文件夹）
  /rename  改名：剧集视频按 Emby 规则改名（先预览再执行）
  /organize 整理：按文件夹归置视频、改名，并清理旧目录
  /emby    剧集：填剧名/年份/季，整理成 Emby 剧集结构
  /play    播放：直接播放视频（倍速、音量、拖动进度）

接口：
  POST /api/browse             弹出 Windows 原生选目录框，返回真实路径
  POST /api/scan               扫描文件夹里的图片（自然排序）
  POST /api/pack               开始打包，立即返回 job id
  GET  /api/job/<id>           查询打包进度
  POST /api/cancel/<id>        取消打包
  POST /api/reveal             在资源管理器中定位生成的文件
  POST /api/reader/list        给选择弹窗列目录（子文件夹 + cbz，path 空则从主目录开始）
  POST /api/reader/open        打开漫画包或文件夹，返回阅读会话
  GET  /api/reader/page/<sid>/<n>  取第 n 页图片（0 基）
  POST /api/reader/close/<sid> 关闭阅读会话
  POST /api/rename/scan        预览视频改名（只算不改）
  POST /api/rename/apply       执行改名，绝不覆盖
  POST /api/organize/scan      扫描视频整理：递归找视频 + 同名字幕
  POST /api/organize/plan      预演整理计划（不落盘）
  POST /api/organize/apply     执行整理：同盘移动/改名，失败回滚
  POST /api/emby/scan          扫描剧集：递归找视频并解析集数
  POST /api/emby/build         预演 Emby 剧集结构（剧名/年份/季 由用户填）
  POST /api/emby/apply         建 剧名/Season 01/ 并移动改名
  POST /api/video/list         列出文件夹里的视频（播放页用）
  GET  /api/video/stream       视频流（支持 Range，可拖动进度条，适合 4K）
  GET  /api/video/probe        探测编码/容器，判断浏览器能不能播
  POST /api/video/open         用系统默认播放器打开（MKV/HEVC 兜底）
  POST /api/shutdown           关闭服务

只依赖 Python 标准库（zipfile / tkinter / http.server）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import emby
import namer
import organizer
import packer
import reader

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DEFAULT_PORT = 8765

_jobs: dict = {}
_jobs_lock = threading.Lock()
_dialog_lock = threading.Lock()
_server_ref: dict = {"server": None}


_BAD_CODECS = {"hevc", "h265", "mpeg2video", "mpeg4", "msmpeg4v3", "vc1",
               "wmv3", "rv30", "rv40", "theora", "prores", "dnxhd"}
_BAD_CONTAINERS = {"mkv", "avi", "wmv", "flv", "rm", "rmvb"}
_BAD_EXTS = {".mkv", ".avi", ".wmv", ".flv", ".rm", ".rmvb",
             ".mpg", ".mpeg", ".ts", ".m2ts", ".mts"}


def probe_video(path: str) -> dict:
    """用 ffprobe 看编码/容器，判断浏览器能不能播（找不到 ffprobe 就退回扩展名判断）。"""
    p = packer.normalize_path(path)
    if not p or not os.path.isfile(p):
        return {"ok": False, "error": "文件不存在"}
    ext = os.path.splitext(p)[1].lower()
    if ext not in namer.VIDEO_EXTS:
        return {"ok": False, "error": "不是视频文件"}
    if ext in _BAD_EXTS:
        return {"ok": True, "codec": ext.lstrip("."), "container": ext.lstrip("."),
                "playable": False}

    import shutil
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {"ok": False, "error": "没找到 ffprobe", "playable": None}
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,width,height:format=format_name,duration",
             "-of", "json", p],
            capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": str(exc), "playable": None}
    if out.returncode != 0:
        return {"ok": False, "error": "ffprobe 解析失败", "playable": None}
    try:
        data = json.loads(out.stdout.decode("utf-8", "replace") or "{}")
    except ValueError:
        return {"ok": False, "error": "ffprobe 输出无效", "playable": None}

    streams = data.get("streams") or []
    if not streams:
        return {"ok": False, "error": "没有视频流", "playable": False}
    stream = streams[0]
    fmt = (data.get("format") or {}).get("format_name", "") or ""
    codec = (stream.get("codec_name") or "").lower()

    container = ext.lstrip(".")
    for key, name in (("matroska", "mkv"), ("avi", "avi"), ("mpegts", "ts"),
                      ("flv", "flv"), ("asf", "wmv"), ("realmedia", "rm")):
        if key in fmt:
            container = name
            break

    playable = (codec not in _BAD_CODECS
                and container not in _BAD_CONTAINERS
                and ext not in _BAD_EXTS)
    return {"ok": True, "codec": codec, "container": container,
            "width": stream.get("width"), "height": stream.get("height"),
            "duration": (data.get("format") or {}).get("duration"),
            "playable": playable}


def open_video(path: str) -> dict:
    """用系统默认播放器打开（网页放不了的 MKV/HEVC 用）。"""
    p = packer.normalize_path(path)
    if not p or not os.path.isfile(p):
        return {"ok": False, "error": "文件不存在"}
    if os.path.splitext(p)[1].lower() not in namer.VIDEO_EXTS:
        return {"ok": False, "error": "不是视频文件"}
    try:
        if sys.platform.startswith("win"):
            os.startfile(p)                       # noqa: S606  仅本地工具
        else:
            subprocess.Popen(["xdg-open", p])
    except OSError as exc:
        return {"ok": False, "error": f"无法打开：{exc}"}
    return {"ok": True}


def video_list(raw: str) -> dict:
    """列出视频（播放页用）：文件夹会**递归**找，兼容 剧名/Season 01/ 这类结构。

    只扫描一层的话，标准平铺文件夹能用，但 Emby 那种套了几层的文件夹就什么都找不到。
    """
    p = packer.normalize_path(raw)
    if not p:
        return {"ok": False, "error": "请先选择文件夹"}

    if os.path.isfile(p):
        if os.path.splitext(p)[1].lower() not in namer.VIDEO_EXTS:
            return {"ok": False, "error": "不是视频文件"}
        try:
            st = os.stat(p)
        except OSError as exc:
            return {"ok": False, "error": f"无法读取：{exc}"}
        name = os.path.basename(p)
        items = [{
            "name": name, "rel": name, "dir": "", "path": p,
            "size": st.st_size, "size_text": packer.human_size(st.st_size),
            "ext": os.path.splitext(name)[1].lower(),
        }]
        root = os.path.dirname(p)
        total = st.st_size
    elif os.path.isdir(p):
        info = organizer.scan(p)
        if not info.get("ok"):
            return info
        root = info["root"]
        items = [{
            "name": v["name"], "rel": v["rel"], "dir": v["dir"], "path": v["path"],
            "size": v["size"], "size_text": v["size_text"], "ext": v["ext"],
        } for v in info["videos"]]
        total = info["totals"]["bytes"]
    else:
        return {"ok": False, "error": f"文件夹不存在：{p}"}

    return {
        "ok": True,
        "root": root,
        "items": items,
        "totals": {"videos": len(items), "bytes": total,
                   "size_text": packer.human_size(total)},
    }


# ---------------------------------------------------------------- 原生选目录
def ask_folder(initial: str = "") -> dict:
    """在服务端进程弹 Windows 原生目录选择框（Tk 在请求线程内创建并销毁）。"""
    with _dialog_lock:
        try:
            import tkinter as tk
            from tkinter import filedialog
        except Exception as exc:                      # 极少见：精简版 Python
            return {"ok": False, "error": f"当前 Python 缺少 tkinter，无法弹出选择框：{exc}"}
        root = None
        try:
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            root.update()
            kw = {"parent": root, "title": "选择图片文件夹", "mustexist": True}
            if initial and os.path.isdir(initial):
                kw["initialdir"] = initial
            chosen = filedialog.askdirectory(**kw)
            return {"ok": True, "path": packer.normalize_path(chosen) if chosen else "",
                    "cancelled": not chosen}
        except Exception as exc:
            return {"ok": False, "error": f"打开选择框失败：{exc}"}
        finally:
            if root is not None:
                try:
                    root.destroy()
                except Exception:
                    pass


# ---------------------------------------------------------------- 打包任务
def _public_job(job: dict) -> dict:
    return {k: v for k, v in job.items() if k != "cancel"}


def _run_job(job, folder, formats, keep_names, delete_source, recursive,
             on_progress, cancel):
    try:
        if recursive:
            tree = packer.scan_tree(folder)
            if not tree.get("ok"):
                raise RuntimeError(tree.get("error") or "扫描子文件夹失败")
            with _jobs_lock:
                job["total"] = tree.get("images", 0)
            res = packer.pack_many(tree["folders"], formats, keep_names=keep_names,
                                   delete_source=delete_source,
                                   progress=on_progress, cancel_event=cancel)
        else:
            res = packer.pack(folder, formats, keep_names=keep_names,
                              delete_source=delete_source,
                              progress=on_progress, cancel_event=cancel)
        with _jobs_lock:
            if res.get("cancelled"):
                job.update(status="cancelled", error="已取消")
            elif res.get("ok"):
                job.update(status="done", result=res, done=res.get("count", job["done"]))
            else:
                job.update(status="error", error=res.get("error", "打包失败"))
    except Exception as exc:                          # 防御：任何异常都要落到任务状态里
        with _jobs_lock:
            job.update(status="error", error=f"打包异常：{exc}")
    finally:
        with _jobs_lock:
            job["finished"] = time.time()


def start_job(folder: str, formats: list, keep_names: bool,
              delete_source: bool = False, recursive: bool = False) -> str:
    job_id = uuid.uuid4().hex[:12]
    cancel = threading.Event()
    job = {
        "id": job_id, "status": "running", "done": 0, "total": 0, "current": "",
        "bytes": 0, "total_bytes": 0, "total_files": 0,
        "started": time.time(), "finished": None, "result": None, "error": "",
        "folder": folder, "formats": formats, "cancel": cancel,
    }
    with _jobs_lock:
        _jobs[job_id] = job

    def on_progress(p: dict):
        with _jobs_lock:
            job["done"] = p.get("done", job["done"])
            job["total"] = p.get("total", job["total"])
            job["total_files"] = p.get("total", job["total"])
            job["current"] = p.get("current", "")
            job["bytes"] = p.get("bytes", job["bytes"])
            job["total_bytes"] = p.get("total_bytes", job["total_bytes"])

    threading.Thread(target=_run_job, daemon=True,
                     args=(job, folder, formats, keep_names, delete_source,
                           recursive, on_progress, cancel)).start()
    return job_id


# ---------------------------------------------------------------- HTTP 处理
class Handler(BaseHTTPRequestHandler):
    server_version = "ComicPack/1.1"
    protocol_version = "HTTP/1.1"

    # ---- 基础工具
    def log_message(self, fmt, *args):                # 静默访问日志
        pass

    def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, data, "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > 4 << 20:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    _VIDEO_MIME = {
        ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
        ".webm": "video/webm", ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
        ".ts": "video/mp2t", ".m2ts": "video/mp2t", ".mts": "video/mp2t",
        ".wmv": "video/x-ms-wmv", ".flv": "video/x-flv", ".mpg": "video/mpeg",
        ".mpeg": "video/mpeg", ".3gp": "video/3gpp", ".ogv": "video/ogg",
        ".rmvb": "application/vnd.rn-realmedia-vbr",
        ".rm": "application/vnd.rn-realmedia",
    }

    def _video_stream(self, target: str):
        """带 Range 的视频流：4K 大文件也能边下边播、拖动进度。"""
        target = packer.normalize_path(target)
        if not target or not os.path.isfile(target):
            return self._json({"ok": False, "error": "视频不存在"}, 404)
        ext = os.path.splitext(target)[1].lower()
        if ext not in namer.VIDEO_EXTS:
            return self._json({"ok": False, "error": "不是视频文件"}, 400)

        size = os.path.getsize(target)
        start, end, status = 0, max(0, size - 1), 200
        rng = (self.headers.get("Range") or "").strip()
        if rng:
            m = re.match(r"^bytes=(\d*)-(\d*)$", rng)
            if not m or (not m.group(1) and not m.group(2)):
                return self._range_error(size)
            a, b = m.group(1), m.group(2)
            if a:
                start = int(a)
                end = int(b) if b else size - 1
            else:                                  # bytes=-N 取最后 N 字节
                start = max(0, size - int(b))
                end = size - 1
            end = min(end, size - 1)
            if size == 0 or start >= size or start > end:
                return self._range_error(size)
            status = 206

        length = end - start + 1 if size else 0
        self.send_response(status)
        self.send_header("Content-Type", self._VIDEO_MIME.get(ext, "application/octet-stream"))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        if not length:
            return
        try:
            with open(target, "rb") as fh:
                fh.seek(start)
                remain = length
                while remain > 0:
                    chunk = fh.read(min(256 * 1024, remain))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remain -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass                                    # 用户拖动/关闭页面，正常现象
        except OSError:
            pass

    def _range_error(self, size: int):
        self.send_response(416)
        self.send_header("Content-Range", f"bytes */{size}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _static(self, rel: str):
        rel = rel.lstrip("/").replace("\\", "/")
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(STATIC_DIR + os.sep) and full != STATIC_DIR:
            return self._json({"ok": False, "error": "forbidden"}, 403)
        if not os.path.isfile(full):
            return self._json({"ok": False, "error": "not found"}, 404)
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".ico": "image/x-icon",
        }.get(os.path.splitext(full)[1].lower(), "application/octet-stream")
        try:
            with open(full, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            return self._json({"ok": False, "error": str(exc)}, 500)
        self._send(200, data, ctype)

    # ---- 路由
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path in ("/reader", "/reader.html"):
            return self._static("reader.html")
        if path in ("/rename", "/rename.html"):
            return self._static("rename.html")
        if path in ("/organize", "/organize.html"):
            return self._static("organize.html")
        if path in ("/emby", "/emby.html"):
            return self._static("emby.html")
        if path in ("/play", "/play.html"):
            return self._static("play.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        if path == "/api/health":
            return self._json({"ok": True, "version": "1.1"})
        if path == "/api/video/stream":
            query = parse_qs(urlparse(self.path).query)
            return self._video_stream((query.get("path") or [""])[0])
        if path == "/api/video/probe":
            query = parse_qs(urlparse(self.path).query)
            return self._json(probe_video((query.get("path") or [""])[0]))
        if path == "/api/library/cover":
            query = parse_qs(urlparse(self.path).query)
            target = (query.get("path") or [""])[0]
            data = reader.cover_bytes(target)
            if not data:
                return self._json({"ok": False, "error": "没有封面"}, 404)
            body, ctype = data
            return self._send(200, body, ctype, cache="public, max-age=3600")
        if path.startswith("/api/reader/page/"):
            parts = [p for p in path.split("/") if p]
            if len(parts) != 5:
                return self._json({"ok": False, "error": "not found"}, 404)
            sid, raw_index = parts[3], parts[4]
            try:
                index = int(raw_index)
            except ValueError:
                return self._json({"ok": False, "error": "页码无效"}, 400)
            try:
                data, ctype, _name = reader.page(sid, index)
            except reader.ReaderError as exc:
                return self._json({"ok": False, "error": str(exc)}, 404)
            # 同一会话同一页内容不会变，放心让浏览器缓存
            return self._send(200, data, ctype, cache="public, max-age=86400")
        if path.startswith("/api/job/"):
            jid = path.rsplit("/", 1)[-1]
            with _jobs_lock:
                job = _jobs.get(jid)
                snap = _public_job(job) if job else None
            if not snap:
                return self._json({"ok": False, "error": "任务不存在"}, 404)
            return self._json({"ok": True, "job": snap})
        return self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        data = self._read_json()
        try:
            if path == "/api/browse":
                return self._json(ask_folder(data.get("initial") or ""))
            if path == "/api/scan":
                folder = packer.normalize_path(data.get("path") or "")
                if data.get("recursive"):
                    return self._json(packer.scan_tree(folder))
                info = packer.scan(folder)
                if info.get("ok"):
                    info["bytes_text"] = packer.human_size(info["bytes"])
                return self._json(info)
            if path == "/api/pack":
                folder = packer.normalize_path(data.get("path") or "")
                formats = [f for f in (data.get("formats") or []) if f in packer.FORMATS]
                if not os.path.isdir(folder):
                    return self._json({"ok": False, "error": "请选择有效的文件夹"})
                if not formats:
                    return self._json({"ok": False, "error": "请至少选择一种导出格式"})
                jid = start_job(folder, formats, bool(data.get("keep_names")),
                                bool(data.get("delete_source")),
                                bool(data.get("recursive")))
                return self._json({"ok": True, "job": jid})
            if path.startswith("/api/cancel/"):
                jid = path.rsplit("/", 1)[-1]
                with _jobs_lock:
                    job = _jobs.get(jid)
                    if job:
                        job["cancel"].set()
                return self._json({"ok": bool(job)})
            if path == "/api/reader/list":
                return self._json(reader.list_dir(data.get("path") or ""))
            if path == "/api/reader/open":
                return self._json(reader.open_path(data.get("path") or "",
                                                   bool(data.get("prefer_images"))))
            if path.startswith("/api/reader/close/"):
                sid = path.rsplit("/", 1)[-1]
                return self._json({"ok": reader.close(sid)})
            if path == "/api/library/scan":
                return self._json(reader.scan_library(data.get("path") or ""))
            if path == "/api/rename/scan":
                return self._json(namer.scan(data.get("path") or ""))
            if path == "/api/rename/apply":
                names = data.get("names")
                if names is not None and not isinstance(names, list):
                    names = None
                return self._json(namer.apply(data.get("path") or "", names))
            if path == "/api/organize/scan":
                return self._json(organizer.scan(data.get("path") or ""))
            if path == "/api/organize/plan":
                return self._json(organizer.plan(data.get("path") or "",
                                                 data.get("plan") or {}))
            if path == "/api/organize/apply":
                return self._json(organizer.apply(data.get("path") or "",
                                                  data.get("plan") or {}))
            if path == "/api/video/open":
                return self._json(open_video(data.get("path") or ""))
            if path == "/api/video/list":
                return self._json(video_list(data.get("path") or ""))
            if path == "/api/emby/scan":
                return self._json(emby.scan(data.get("path") or ""))
            if path == "/api/emby/build":
                return self._json(emby.build(data.get("path") or "",
                                             data.get("plan") or {}))
            if path == "/api/emby/apply":
                return self._json(emby.apply(data.get("path") or "",
                                             data.get("plan") or {}))
            if path == "/api/reveal":
                target = packer.normalize_path(data.get("path") or "")
                if target and os.path.exists(target):
                    subprocess.Popen(["explorer", f"/select,{target}"])
                    return self._json({"ok": True})
                return self._json({"ok": False, "error": "文件不存在"})
            if path == "/api/shutdown":
                threading.Thread(target=_shutdown, daemon=True).start()
                return self._json({"ok": True})
        except Exception as exc:
            return self._json({"ok": False, "error": f"服务端异常：{exc}"}, 500)
        return self._json({"ok": False, "error": "not found"}, 404)


def _shutdown():
    time.sleep(0.25)
    reader.close_all()
    srv = _server_ref.get("server")
    if srv:
        srv.shutdown()


# ---------------------------------------------------------------- 启动
def find_free_port(start: int) -> int:
    for port in range(start, start + 60):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start


def _find_browser():
    exe = os.environ.get("MEDIA_BROWSER")
    if exe and os.path.isfile(exe):
        return exe
    roots = [os.environ.get(p) for p in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
    cands = []
    for root in roots:
        if root:
            cands.append(os.path.join(root, "Google", "Chrome", "Application", "chrome.exe"))
    for root in roots:
        if root:
            cands.append(os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe"))
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


def _open_app(url: str):
    exe = _find_browser()
    if exe:
        try:
            subprocess.Popen([exe, "--app=" + url, "--window-size=1440,880"])
            return
        except OSError:
            pass
    webbrowser.open(url)


def main():
    ap = argparse.ArgumentParser(description="ComicPack — 图片打包成 cbz/zip，并在网页里直接阅读")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true", help="只起服务，不自动开浏览器")
    args = ap.parse_args()

    port = find_free_port(args.port)
    url = f"http://127.0.0.1:{port}"
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    _server_ref["server"] = srv

    print("=" * 58)
    print("  ComicPack — 图片打包 + CBZ 阅读")
    print(f"  服务地址：{url}")
    print("  关闭方式：页面右上角「关闭服务」／本窗口 Ctrl+C／直接关窗口")
    print("=" * 58, flush=True)

    if not args.no_browser:
        threading.Timer(0.8, lambda: _open_app(url)).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        reader.close_all()
        srv.server_close()
    print("服务已停止")


if __name__ == "__main__":
    main()
