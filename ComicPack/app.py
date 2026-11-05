# -*- coding: utf-8 -*-
"""
app.py — ComicPack 本地 Web 服务
================================
双击「启动.bat」→ 起一个只监听 127.0.0.1 的小服务 → 自动用浏览器「应用窗口」打开页面。

接口：
  POST /api/browse        弹出 Windows 原生选目录框，返回真实路径
  POST /api/scan          扫描文件夹里的图片（自然排序）
  POST /api/pack          开始打包，立即返回 job id
  GET  /api/job/<id>      查询打包进度
  POST /api/cancel/<id>   取消打包
  POST /api/reveal        在资源管理器中定位生成的文件
  POST /api/shutdown      关闭服务

只依赖 Python 标准库（zipfile / tkinter / http.server）。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import packer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DEFAULT_PORT = 8765

_jobs: dict = {}
_jobs_lock = threading.Lock()
_dialog_lock = threading.Lock()
_server_ref: dict = {"server": None}


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


def _run_job(job, folder, formats, keep_names, on_progress, cancel):
    try:
        res = packer.pack(folder, formats, keep_names=keep_names,
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


def start_job(folder: str, formats: list, keep_names: bool) -> str:
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
                     args=(job, folder, formats, keep_names, on_progress, cancel)).start()
    return job_id


# ---------------------------------------------------------------- HTTP 处理
class Handler(BaseHTTPRequestHandler):
    server_version = "ComicPack/1.0"
    protocol_version = "HTTP/1.1"

    # ---- 基础工具
    def log_message(self, fmt, *args):                # 静默访问日志
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
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
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        if path == "/api/health":
            return self._json({"ok": True, "version": "1.0"})
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
                jid = start_job(folder, formats, bool(data.get("keep_names")))
                return self._json({"ok": True, "job": jid})
            if path.startswith("/api/cancel/"):
                jid = path.rsplit("/", 1)[-1]
                with _jobs_lock:
                    job = _jobs.get(jid)
                    if job:
                        job["cancel"].set()
                return self._json({"ok": bool(job)})
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
    ap = argparse.ArgumentParser(description="ComicPack — 图片文件夹打包成 cbz/zip")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true", help="只起服务，不自动开浏览器")
    args = ap.parse_args()

    port = find_free_port(args.port)
    url = f"http://127.0.0.1:{port}"
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    _server_ref["server"] = srv

    print("=" * 58)
    print("  ComicPack — 图片文件夹打包成 CBZ / ZIP")
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
        srv.server_close()
    print("服务已停止")


if __name__ == "__main__":
    main()
