# -*- coding: utf-8 -*-
"""
reader.py — CBZ / ZIP 漫画包读取核心
=====================================
职责：打开一个漫画包（CBZ/ZIP，本质都是 zip），列出内页图片（自然排序），
按页取出图片字节交给 HTTP 层；也支持直接打开一个图片文件夹当“虚拟包”阅读。

不依赖第三方库：zipfile 直接读，ZipFile 句柄按会话缓存，避免每次翻页都重扫目录。
"""

from __future__ import annotations

import os
import threading
import time
import uuid
import zipfile

import packer

# 扩展名 -> Content-Type（与 packer.IMAGE_EXTS 对应）
CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".jpe": "image/jpeg",
    ".jfif": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".avif": "image/avif",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}

# 同时保留的会话数（每个会话占一个 zip 句柄）
MAX_SESSIONS = 8

_sessions: dict = {}
_lock = threading.Lock()


class ReaderError(Exception):
    """读取失败（文件损坏、页码越界等），由 HTTP 层转成 4xx/5xx。"""


def _ctype(name: str) -> str:
    return CONTENT_TYPES.get(os.path.splitext(name)[1].lower(), "application/octet-stream")


def _archive_pages(zf: zipfile.ZipFile) -> list:
    """包内的图片条目，按自然顺序排列（001.jpg < 002.jpg < 010.jpg）。"""
    infos = [i for i in zf.infolist()
             if not i.is_dir() and packer.is_image_name(i.filename)]
    infos.sort(key=lambda i: packer.natural_key(i.filename))
    return [{"name": i.filename, "size": i.file_size} for i in infos]


def _folder_pages(folder: str) -> list:
    info = packer.scan(folder)
    if not info.get("ok"):
        raise ReaderError(info.get("error") or "无法读取文件夹")
    return [{"name": im["name"], "size": im["size"]} for im in info["images"]]


def _trim_sessions():
    """会话过多时关掉最久没用的那些，释放 zip 句柄。"""
    with _lock:
        if len(_sessions) <= MAX_SESSIONS:
            return
        ordered = sorted(_sessions.values(), key=lambda s: s["touched"])
        for sess in ordered[:len(_sessions) - MAX_SESSIONS]:
            _drop(sess["id"])


def _drop(sid: str):
    with _lock:
        sess = _sessions.pop(sid, None)
    if sess:
        try:
            if sess.get("zf") is not None:
                sess["zf"].close()
        except OSError:
            pass


def open_path(raw_path: str) -> dict:
    """打开漫画包或图片文件夹，返回阅读会话信息。"""
    path = packer.normalize_path(raw_path)
    if not path:
        return {"ok": False, "error": "请选择一个 CBZ / ZIP 文件"}
    if os.path.isdir(path):
        try:
            pages = _folder_pages(path)
        except ReaderError as exc:
            return {"ok": False, "error": str(exc)}
        if not pages:
            return {"ok": False, "error": "这个文件夹里没有图片"}
        sid = _remember({"kind": "folder", "path": path, "name": os.path.basename(path) or path,
                         "pages": pages, "folder": path, "zf": None})
        return _public(sid, pages, path, "folder")

    if not os.path.isfile(path):
        return {"ok": False, "error": f"文件不存在：{path}"}
    if not zipfile.is_zipfile(path):
        return {"ok": False, "error": "不是有效的 CBZ / ZIP（CBZ 本质是改了扩展名的 zip）"}
    try:
        zf = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        return {"ok": False, "error": f"打开失败：{exc}"}
    try:
        pages = _archive_pages(zf)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        zf.close()
        return {"ok": False, "error": f"读取目录失败：{exc}"}
    if not pages:
        zf.close()
        return {"ok": False, "error": "这个包里没有图片"}

    sid = _remember({"kind": "zip", "path": path, "name": os.path.basename(path),
                     "pages": pages, "folder": "", "zf": zf})
    return _public(sid, pages, path, "zip")


def _remember(sess: dict) -> str:
    sid = uuid.uuid4().hex[:12]
    sess.update({"id": sid, "touched": time.time(), "lock": threading.Lock()})
    with _lock:
        _sessions[sid] = sess
    _trim_sessions()
    return sid


def _public(sid: str, pages: list, path: str, kind: str) -> dict:
    total = sum(p["size"] for p in pages)
    return {
        "ok": True,
        "id": sid,
        "kind": kind,
        "path": path,
        "name": os.path.basename(path) or path,
        "page_count": len(pages),
        "bytes": total,
        "bytes_text": packer.human_size(total),
        "pages": [{"index": i, "name": p["name"], "size": p["size"],
                   "bytes_text": packer.human_size(p["size"])}
                  for i, p in enumerate(pages)],
    }


def page(sid: str, index: int) -> tuple:
    """取第 index 页（0 基）的 (bytes, content_type, 文件名)。"""
    with _lock:
        sess = _sessions.get(sid)
    if not sess:
        raise ReaderError("阅读会话已失效，请重新打开文件")
    pages = sess["pages"]
    if index < 0 or index >= len(pages):
        raise ReaderError(f"页码超出范围（共 {len(pages)} 页）")

    sess["touched"] = time.time()
    entry = pages[index]
    if sess["kind"] == "folder":
        full = os.path.join(sess["folder"], entry["name"])
        try:
            with open(full, "rb") as fh:
                return fh.read(), _ctype(entry["name"]), entry["name"]
        except OSError as exc:
            raise ReaderError(f"读取图片失败：{exc}") from exc

    zf = sess["zf"]
    try:
        with sess["lock"]:
            data = zf.read(entry["name"])
    except KeyError as exc:
        raise ReaderError("包内找不到这一页") from exc
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ReaderError(f"读取失败：{exc}") from exc
    return data, _ctype(entry["name"]), entry["name"]


def close(sid: str) -> bool:
    with _lock:
        exists = sid in _sessions
    _drop(sid)
    return exists


def close_all():
    with _lock:
        ids = list(_sessions)
    for sid in ids:
        _drop(sid)
