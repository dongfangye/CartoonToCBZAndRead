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

# 书架扫描：常见的“这本书的封面”文件名
_COVER_NAMES = ("cover", "folder", "poster", "front", "封面", "封面图")
_COVER_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif")
_SKIP_DIRS = {"$recycle.bin", "system volume information", "recycler", "lost+found"}

_sessions: dict = {}
_lock = threading.Lock()


class ReaderError(Exception):
    """读取失败（文件损坏、页码越界等），由 HTTP 层转成 4xx/5xx。"""


def _ctype(name: str) -> str:
    return CONTENT_TYPES.get(os.path.splitext(name)[1].lower(), "application/octet-stream")


def _sibling_cover(archive_path: str) -> str:
    """同目录下和漫画包配套的封面图（同名优先，其次 cover/poster/封面）。"""
    base = os.path.splitext(archive_path)[0]
    for ext in _COVER_EXTS:
        cand = base + ext
        if os.path.isfile(cand):
            return cand
    folder = os.path.dirname(archive_path)
    for name in _COVER_NAMES:
        for ext in _COVER_EXTS:
            cand = os.path.join(folder, name + ext)
            if os.path.isfile(cand):
                return cand
    return ""


def _zip_first_image_info(archive_path: str):
    """包内的第一张图（按自然排序），没有就 None。"""
    try:
        with zipfile.ZipFile(archive_path) as zf:
            infos = [i for i in zf.infolist()
                     if not i.is_dir() and packer.is_image_name(i.filename)]
            if not infos:
                return None
            infos.sort(key=lambda i: packer.natural_key(i.filename))
            return infos[0]
    except (OSError, zipfile.BadZipFile):
        return None


def cover_bytes(archive_path: str):
    """取封面图片字节：(bytes, content_type)；没有封面返回 None。

    参数既可以是漫画包（取配套封面或包内首图），也可以是直接的封面图片。
    """
    path = packer.normalize_path(archive_path)
    if not os.path.isfile(path):
        return None

    ext = os.path.splitext(path)[1].lower()
    if ext in CONTENT_TYPES:                          # 已经是图片文件
        try:
            with open(path, "rb") as fh:
                return fh.read(), _ctype(path)
        except OSError:
            return None
    if ext not in ARCHIVE_EXTS:
        return None

    sib = _sibling_cover(path)
    if sib:
        try:
            with open(sib, "rb") as fh:
                return fh.read(), _ctype(sib)
        except OSError:
            return None

    info = _zip_first_image_info(path)
    if not info:
        return None
    try:
        with zipfile.ZipFile(path) as zf:
            return zf.read(info.filename), _ctype(info.filename)
    except (OSError, zipfile.BadZipFile, RuntimeError, KeyError):
        return None


def _zip_image_count(archive_path: str) -> int:
    try:
        with zipfile.ZipFile(archive_path) as zf:
            return sum(1 for i in zf.infolist()
                       if not i.is_dir() and packer.is_image_name(i.filename))
    except (OSError, zipfile.BadZipFile):
        return 0


def _group_cover(dirpath: str, books: list, cover_archive: str) -> tuple:
    """给一组章节挑封面：同名封面包 > 目录里 cover/poster/封面 > 父目录同名图 > 第一话首图。"""
    if cover_archive:
        return "zip", cover_archive

    for name in _COVER_NAMES:
        for ext in _COVER_EXTS:
            cand = os.path.join(dirpath, name + ext)
            if os.path.isfile(cand):
                return "file", cand

    parent = os.path.dirname(dirpath)
    dname = os.path.basename(dirpath)
    for ext in _COVER_EXTS:
        cand = os.path.join(parent, dname + ext)
        if os.path.isfile(cand):
            return "file", cand

    first = books[0]
    if first["cover"] == "file" and first["cover_path"]:
        return "file", first["cover_path"]
    if first["cover"] == "zip":
        return "zip", first["path"]
    return "", ""


def scan_library(root: str, max_depth: int = 8, max_books: int = 5000) -> dict:
    """递归扫描文件夹里的 cbz/zip，返回书架条目（带封面标记）。

    用户选中的文件夹可能只有一堆包，也可能有很多层子文件夹；
    这里统一递归收集，客户端只负责按封面分页展示。
    """
    path = packer.normalize_path(root)
    if not path:
        return {"ok": False, "error": "请选择一个文件夹"}

    if os.path.isfile(path):
        if os.path.splitext(path)[1].lower() in ARCHIVE_EXTS:
            return {"ok": True, "kind": "book", "path": path,
                    "name": os.path.basename(path)}
        return {"ok": False, "error": "这看起来不是 cbz / zip 文件"}
    if not os.path.isdir(path):
        return {"ok": False, "error": f"文件夹不存在：{path}"}

    base = os.path.abspath(path)
    books, folders = [], 0
    try:
        for dirpath, dirnames, filenames in os.walk(base):
            rel_dir = os.path.relpath(dirpath, base)
            depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
            if depth > max_depth:
                dirnames[:] = []
                continue

            dirnames[:] = sorted(
                [d for d in dirnames
                 if not d.startswith(".") and d.lower() not in _SKIP_DIRS],
                key=packer.natural_key)
            folders += 1

            for fname in sorted(filenames, key=packer.natural_key):
                if os.path.splitext(fname)[1].lower() not in ARCHIVE_EXTS:
                    continue
                full = os.path.join(dirpath, fname)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                cover_path = _sibling_cover(full)
                if cover_path:
                    cover = "file"
                elif _zip_first_image_info(full):
                    cover = "zip"
                else:
                    cover = ""
                books.append({
                    "name": fname,
                    "path": full,
                    "dirpath": dirpath,
                    "rel": os.path.relpath(full, base),
                    "size": st.st_size,
                    "size_text": packer.human_size(st.st_size),
                    "cover": cover,
                    "cover_path": cover_path,
                })
                if len(books) >= max_books:
                    break
            if len(books) >= max_books:
                break
    except OSError as exc:
        return {"ok": False, "error": f"无法读取文件夹：{exc}"}

    # “封面包”：与某个章节文件夹同名的 zip（例如 [Series].zip 对应 [Series]/第1话.zip）
    # 它只是封面，不该单独算一本书。
    cover_archives = {}
    for dirpath in {b["dirpath"] for b in books}:
        if os.path.normcase(dirpath) == os.path.normcase(base):
            continue
        parent = os.path.dirname(dirpath)
        dname = os.path.basename(dirpath)
        for ext in (".zip", ".cbz"):
            cand = os.path.join(parent, dname + ext)
            # 封面包通常只有 1~2 张图；图多的是真书，不能当封面吃掉
            if os.path.isfile(cand) and _zip_image_count(cand) <= 2:
                cover_archives[os.path.normcase(os.path.abspath(cand))] = cand

    books = [b for b in books
             if os.path.normcase(os.path.abspath(b["path"])) not in cover_archives]

    groups = {}
    for book in books:
        # 根部目录里的包各自算一本；子文件夹里的包按“一部漫画的多话”合并
        if os.path.normcase(book["dirpath"]) == os.path.normcase(base):
            key = ("book", os.path.normcase(book["path"]))
        else:
            key = ("dir", os.path.normcase(book["dirpath"]))
        groups.setdefault(key, []).append(book)

    shelf = []
    for group_books in groups.values():
        dirpath = group_books[0]["dirpath"]
        group_books.sort(key=lambda b: packer.natural_key(b["name"]))
        first = group_books[0]

        own_cover_archive = ""
        if os.path.normcase(dirpath) != os.path.normcase(base):
            parent = os.path.dirname(dirpath)
            dname = os.path.basename(dirpath)
            for ext in (".zip", ".cbz"):
                cand = os.path.join(parent, dname + ext)
                hit = cover_archives.get(os.path.normcase(os.path.abspath(cand)), "")
                if hit:
                    own_cover_archive = hit
                    break

        series = len(group_books) > 1 or bool(own_cover_archive)
        rel_dir = os.path.relpath(dirpath, base)
        display_dir = "" if rel_dir == "." else rel_dir

        if series:
            name = os.path.basename(dirpath)
            cover, cover_path = _group_cover(dirpath, group_books, own_cover_archive)
            rel = display_dir or first["rel"]
        else:
            name = first["name"]
            cover = first["cover"]
            cover_path = first["cover_path"] or first["path"]
            rel = first["rel"]

        total = sum(b["size"] for b in group_books)
        shelf.append({
            "name": name,
            "dir": display_dir,
            "rel": rel,
            "path": first["path"],                 # 点开先读第一话
            "cover": cover,
            "cover_path": cover_path,
            "count": len(group_books),             # 这一部有几话
            "size": total,
            "size_text": packer.human_size(total),
            "chapters": [{"name": b["name"], "path": b["path"]} for b in group_books],
        })

    shelf.sort(key=lambda it: (
        0 if not it["dir"] else it["dir"].count(os.sep) + 1,
        packer.natural_key(it["dir"]),
        packer.natural_key(it["name"])))

    return {
        "ok": True,
        "kind": "shelf",
        "path": path,
        "name": os.path.basename(path.rstrip("\\/")) or path,
        "items": shelf,
        "count": len(shelf),
        "chapters": sum(it["count"] for it in shelf),
        "folders": folders,
    }


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


def _drives() -> list:
    """可选磁盘根目录。"""
    roots = []
    try:
        for d in os.listdrives():                       # Python 3.12+
            roots.append({"name": d.rstrip("\\/"), "path": d})
    except AttributeError:
        for letter in "CDEFGHIJK":
            path = f"{letter}:\\"
            if os.path.exists(path):
                roots.append({"name": f"{letter}:", "path": path})
    return roots


def list_dir(raw_path: str = "") -> dict:
    """给阅读器的选择弹窗列目录：子文件夹 + cbz/zip 文件。

    path 为空时从用户主目录开始（同时返回磁盘列表，方便跳到别的盘）。
    """
    roots = _drives()
    path = packer.normalize_path(raw_path)
    if not path:
        home = os.path.expanduser("~")
        if not os.path.isdir(home):
            return {"ok": True, "path": "", "parent": "", "entries": [], "roots": roots,
                    "dir_count": 0, "file_count": 0}
        path = home                                              # 从主目录开始，别对着空列表发愣
    if os.path.isfile(path):                            # 传进来的是文件：退到它的目录
        path = os.path.dirname(path)
    if not os.path.isdir(path):
        return {"ok": False, "error": f"文件夹不存在：{path}", "roots": roots}

    dirs, files = [], []
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        dirs.append({"kind": "dir", "name": entry.name, "path": entry.path})
                    elif entry.is_file(follow_symlinks=True):
                        if os.path.splitext(entry.name)[1].lower() not in ARCHIVE_EXTS:
                            continue
                        st = entry.stat(follow_symlinks=True)
                        files.append({"kind": "file", "name": entry.name, "path": entry.path,
                                      "size": st.st_size,
                                      "bytes_text": packer.human_size(st.st_size)})
                except OSError:
                    continue
    except OSError as exc:
        return {"ok": False, "error": f"无法读取文件夹：{exc}", "roots": roots}

    dirs.sort(key=lambda x: packer.natural_key(x["name"]))
    files.sort(key=lambda x: packer.natural_key(x["name"]))
    trimmed = path.rstrip("\\/")
    parent = os.path.dirname(trimmed)
    if parent == trimmed or not parent:
        parent = ""
    return {
        "ok": True,
        "path": path,
        "parent": parent,
        "entries": dirs + files,
        "roots": roots,
        "dir_count": len(dirs),
        "file_count": len(files),
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


def _folder_session(path: str, images: list) -> dict:
    """把「一层散图」当一本漫画读。"""
    pages = [{"name": im["name"], "size": im["size"]} for im in images]
    sid = _remember({"kind": "folder", "path": path,
                     "name": os.path.basename(path) or path,
                     "pages": pages, "folder": path, "zf": None})
    return _public(sid, pages, path, "folder")


def open_path(raw_path: str, prefer_images: bool = False) -> dict:
    """打开漫画包或文件夹。

    文件夹：列成书架（子文件夹 + 漫画包 + 可选的“本层图片”入口），
    让用户自己挑；只有一层散图（或 prefer_images=True）时才直接读图。
    """
    path = packer.normalize_path(raw_path)
    if not path:
        return {"ok": False, "error": "请选择一个 CBZ / ZIP 文件"}
    if os.path.isdir(path):
        try:
            shelf = _shelf(path)
        except ReaderError as exc:
            return {"ok": False, "error": str(exc)}

        info = packer.scan(path)
        images = info["images"] if info.get("ok") else []

        if prefer_images and images:
            return _folder_session(path, images)

        archives = shelf["archives"]
        folders = shelf["folders"]

        # 只有一个包、别的什么都没有：直接打开，省一次点击
        if len(archives) == 1 and not folders and not images:
            return open_path(archives[0]["path"])

        items = list(archives) + list(folders)
        if images and (archives or folders):
            items.append({
                "kind": "images",
                "name": f"直接阅读本层 {len(images)} 张图片",
                "path": path,
                "count": len(images),
            })

        if items:
            return {
                "ok": True,
                "kind": "library",
                "path": path,
                "name": os.path.basename(path.rstrip("\\/")) or path,
                "parent": os.path.dirname(path.rstrip("\\/")),
                "items": items,
                "archive_count": len(archives),
                "folder_count": len(folders),
                "image_count": len(images),
            }

        if images:
            return _folder_session(path, images)
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
