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


ARCHIVE_EXTS = {".cbz", ".zip"}


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


def _chapters(folder: str, current: str) -> tuple:
    """同一目录下的所有漫画包，按名字自然排序；并给出当前这本的序号。

    阅读器用它做「章节侧边栏 / 下一话」：打开一本，就知道兄弟章节有哪些。
    """
    items = []
    try:
        with os.scandir(folder) as it:
            for entry in it:
                try:
                    if not entry.is_file(follow_symlinks=True):
                        continue
                except OSError:
                    continue
                if os.path.splitext(entry.name)[1].lower() not in ARCHIVE_EXTS:
                    continue
                try:
                    st = entry.stat(follow_symlinks=True)
                except OSError:
                    continue
                items.append({
                    "name": entry.name,
                    "path": entry.path,
                    "size": st.st_size,
                    "bytes_text": packer.human_size(st.st_size),
                })
    except OSError:
        return [], -1

    items.sort(key=lambda x: packer.natural_key(x["name"]))
    current_norm = os.path.normcase(os.path.abspath(current))
    index = next((i for i, it in enumerate(items)
                  if os.path.normcase(os.path.abspath(it["path"])) == current_norm), -1)
    return items, index


def _shelf(folder: str) -> dict:
    """列出一个文件夹里的漫画包与子文件夹（不递归）。

    阅读器选文件夹时，如果里面没有散图，就把这些包列成“书架”，
    解决“选了放满 cbz 的文件夹却提示没有图片”的问题。
    """
    archives, folders = [], []
    try:
        with os.scandir(folder) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        folders.append({"kind": "folder", "name": entry.name,
                                        "path": entry.path})
                        continue
                    if not entry.is_file(follow_symlinks=True):
                        continue
                except OSError:
                    continue
                ext = os.path.splitext(entry.name)[1].lower()
                if ext not in ARCHIVE_EXTS:
                    continue
                try:
                    st = entry.stat(follow_symlinks=True)
                except OSError:
                    continue
                archives.append({
                    "kind": "archive",
                    "name": entry.name,
                    "path": entry.path,
                    "size": st.st_size,
                    "bytes_text": packer.human_size(st.st_size),
                })
    except OSError as exc:
        raise ReaderError(f"无法读取文件夹：{exc}") from exc

    archives.sort(key=lambda x: packer.natural_key(x["name"]))
    folders.sort(key=lambda x: packer.natural_key(x["name"]))
    return {
        "archives": archives,
        "folders": folders,
        "items": archives + folders,
        "archive_count": len(archives),
    }


def _trim_sessions():
    """会话过多时关掉最久没用的那些，释放 zip 句柄。

    注意：先在同一把锁里选出要关的会话，出锁后再逐个 _drop。
    不能在持有 _lock 时调 _drop（它会再拿一次锁），否则第 9 次打开就死锁。
    """
    with _lock:
        excess = len(_sessions) - MAX_SESSIONS
        if excess <= 0:
            return
        ordered = sorted(_sessions.values(), key=lambda s: s["touched"])
        victims = [sess["id"] for sess in ordered[:excess]]

    for sid in victims:
        _drop(sid)


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
            shelf = _shelf(path)
        except ReaderError as exc:
            return {"ok": False, "error": str(exc)}

        # 文件夹里有漫画包：直接打开第一本，同目录的其它包作为章节返回
        if shelf["archives"]:
            return open_path(shelf["archives"][0]["path"])

        info = packer.scan(path)
        if not info.get("ok"):
            return {"ok": False, "error": info.get("error") or "无法读取文件夹"}
        if info["images"]:
            pages = [{"name": im["name"], "size": im["size"]} for im in info["images"]]
            sid = _remember({"kind": "folder", "path": path,
                             "name": os.path.basename(path) or path,
                             "pages": pages, "folder": path, "zf": None})
            return _public(sid, pages, path, "folder")

        # 只有子文件夹：列成书架继续往下找
        if shelf["folders"]:
            return {
                "ok": True,
                "kind": "library",
                "path": path,
                "name": os.path.basename(path.rstrip("\\/")) or path,
                "parent": os.path.dirname(path.rstrip("\\/")),
                "items": shelf["folders"],
                "archive_count": 0,
                "folder_count": len(shelf["folders"]),
            }
        return {"ok": False, "error": "这个文件夹里没有图片，也没有 CBZ / ZIP 漫画包"}

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

    chapters, chapter_index = _chapters(os.path.dirname(path), path)
    sid = _remember({"kind": "zip", "path": path, "name": os.path.basename(path),
                     "pages": pages, "folder": "", "zf": zf})
    return _public(sid, pages, path, "zip", chapters, chapter_index)


def _remember(sess: dict) -> str:
    sid = uuid.uuid4().hex[:12]
    sess.update({"id": sid, "touched": time.time(), "lock": threading.Lock()})
    with _lock:
        _sessions[sid] = sess
    _trim_sessions()
    return sid


def _public(sid: str, pages: list, path: str, kind: str,
            chapters: list | None = None, chapter_index: int = -1) -> dict:
    total = sum(p["size"] for p in pages)
    chapters = chapters or []
    return {
        "ok": True,
        "id": sid,
        "kind": kind,
        "path": path,
        "name": os.path.basename(path) or path,
        "folder": os.path.dirname(path),
        "page_count": len(pages),
        "bytes": total,
        "bytes_text": packer.human_size(total),
        "chapters": chapters,
        "chapter_count": len(chapters),
        "chapter_index": chapter_index,
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
