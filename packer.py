# -*- coding: utf-8 -*-
"""
packer.py — 图片文件夹 → cbz / zip 打包核心
============================================
职责：扫描文件夹内图片 → 自然排序（1 < 2 < 10）→ 按需零填充重编号
→ 以 ZIP_STORED 写入临时文件 → 原子重命名成 .cbz/.zip。

不依赖任何第三方库（纯标准库），与主项目 server/model/chapter_download.py
的打包约定保持一致（ZIP_STORED + .part 半成品 + os.replace 原子替换）。
"""

from __future__ import annotations

import os
import re
import shutil
import threading
import time
import zipfile

# 视为图片的扩展名（小写）
IMAGE_EXTS = {
    ".jpg", ".jpeg", ".jpe", ".jfif",
    ".png", ".webp", ".gif", ".bmp",
    ".avif", ".tif", ".tiff",
}

# 目录里常见但要忽略的杂物（不提示、不打包）
_IGNORE_NAMES = {"thumbs.db", "desktop.ini", ".ds_store", "~$index.db"}

# 支持的导出格式 -> 扩展名
FORMATS = {
    "cbz": ".cbz",
    "zip": ".zip",
}


class Cancelled(Exception):
    """用户取消打包。"""


def natural_key(name: str):
    """自然排序键：数字段按数值比较（p2 < p10），其余按小写字符串比较。"""
    return [(0, int(t)) if t.isdigit() else (1, t.casefold())
            for t in re.split(r"(\d+)", name)]


def normalize_path(raw: str) -> str:
    """把用户输入/粘贴的路径规整成绝对路径。

    兼容 Windows「复制文件地址」带引号的情况、环境变量、正斜杠、末尾反斜杠。
    """
    p = (raw or "").strip().strip('"').strip("'").strip()
    if not p:
        return ""
    p = os.path.expandvars(os.path.expanduser(p))
    p = p.replace("/", os.sep)
    # 去掉末尾多余分隔符，但保留盘符根目录（C:\）
    if len(p) > 3 and p.endswith(os.sep):
        p = p.rstrip(os.sep) or p
    return os.path.normpath(p)


def is_image_name(name: str) -> bool:
    """文件名是否是受支持的图片（按扩展名判断，不看文件内容）。"""
    return os.path.splitext(name)[1].lower() in IMAGE_EXTS


def _is_image_name(name: str) -> bool:      # 兼容旧调用
    return is_image_name(name)


def scan(folder: str) -> dict:
    """扫描单层文件夹里的图片，返回清单（自然排序）。

    只处理该文件夹内的直接子文件，不进子目录。
    """
    if not folder:
        return {"ok": False, "error": "请先选择文件夹"}
    if not os.path.isdir(folder):
        return {"ok": False, "error": f"文件夹不存在：{folder}"}

    images = []
    ignored = 0
    skipped_dirs = 0
    try:
        with os.scandir(folder) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        skipped_dirs += 1
                        continue
                    if not entry.is_file(follow_symlinks=True):
                        continue
                except OSError:
                    continue
                name = entry.name
                if name.lower() in _IGNORE_NAMES or name.startswith("~$"):
                    continue
                if not _is_image_name(name):
                    ignored += 1
                    continue
                try:
                    st = entry.stat(follow_symlinks=True)
                except OSError:
                    continue
                images.append({
                    "name": name,
                    "size": st.st_size,
                    "ext": os.path.splitext(name)[1].lower(),
                    "mtime": int(st.st_mtime),
                })
    except OSError as exc:
        return {"ok": False, "error": f"无法读取文件夹：{exc}"}

    images.sort(key=lambda im: natural_key(im["name"]))
    total = sum(im["size"] for im in images)
    return {
        "ok": True,
        "folder": folder,
        "name": os.path.basename(folder) or folder,
        "count": len(images),
        "bytes": total,
        "ignored": ignored,
        "subdirs": skipped_dirs,
        "images": images,
    }


def build_arcnames(images: list, keep_names: bool = False) -> list:
    """生成包内文件名：默认零填充重编号（001.jpg），可选保留原名。

    返回与 images 等长的新名字列表；同名冲突自动加 _2、_3。
    """
    n = len(images)
    width = max(3, len(str(n)))
    used = set()
    out = []
    for idx, im in enumerate(images, 1):
        if keep_names:
            arc = im["name"]
        else:
            arc = f"{idx:0{width}d}{im['ext']}"
        low = arc.casefold()
        if low in used:
            stem, ext = os.path.splitext(arc)
            k = 2
            while f"{stem}_{k}{ext}".casefold() in used:
                k += 1
            arc = f"{stem}_{k}{ext}"
            low = arc.casefold()
        used.add(low)
        out.append(arc)
    return out


def default_output_dir(folder: str) -> str:
    """默认输出位置：源文件夹的同级目录。"""
    folder = os.path.normpath(folder)
    parent, base = os.path.split(folder)
    return parent or folder


def _targets(folder: str, formats: list) -> list:
    """各格式的目标路径。

    多种格式共用同一个序号后缀：若 base.cbz 已存在，则两种格式一起变成
    base_1.cbz / base_1.zip，避免出现 base_3.cbz + base_1.zip 这种错位。
    """
    out_dir = default_output_dir(folder)
    base = os.path.basename(os.path.normpath(folder))
    if not base:                       # 盘符根目录之类的极端情况
        base = os.path.splitdrive(folder)[0].replace(":", "") + "盘" or "comic"

    exts = [FORMATS[f] for f in formats if f in FORMATS]
    stem = base
    i = 1
    while any(os.path.exists(os.path.join(out_dir, stem + ext)) for ext in exts):
        stem = f"{base}_{i}"
        i += 1
    return [(f, os.path.join(out_dir, stem + FORMATS[f]))
            for f in formats if f in FORMATS]


def free_space(path: str) -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 1 << 62


def pack(folder: str, formats: list, keep_names: bool = False,
         progress=None, cancel_event: threading.Event | None = None) -> dict:
    """打包文件夹为一种或多种格式。

    formats      例：["cbz", "zip"]；内容完全一致时只写一次、再复制，
                 避免同一份数据写两遍。
    progress     callback(dict)，字段：done / total / current / bytes
    cancel_event 置位后在下个文件处中止并删除半成品
    """
    info = scan(folder)
    if not info.get("ok"):
        return info
    images = info["images"]
    if not images:
        return {"ok": False, "error": "该文件夹里没有找到图片（支持 jpg/png/webp/gif/bmp/avif 等）"}

    formats = [f for f in (formats or []) if f in FORMATS]
    if not formats:
        return {"ok": False, "error": "请至少选择一种导出格式"}

    targets = _targets(folder, formats)
    total_bytes = info["bytes"]
    need = int(total_bytes * 1.02 * max(1, len(targets))) + (16 << 20)
    if free_space(default_output_dir(folder)) < need:
        return {"ok": False, "error": "目标磁盘剩余空间不足，无法完成打包"}

    arcnames = build_arcnames(images, keep_names=keep_names)
    started = time.time()
    first = targets[0][1]
    part = first + ".part"
    written = 0
    n = len(images)

    def beat(done, current):
        if progress:
            progress({"done": done, "total": n, "current": current,
                      "bytes": written, "total_bytes": total_bytes})

    try:
        with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_STORED,
                             allowZip64=True) as zf:
            for i, (im, arc) in enumerate(zip(images, arcnames), 1):
                if cancel_event is not None and cancel_event.is_set():
                    raise Cancelled()
                src = os.path.join(folder, im["name"])
                beat(i - 1, im["name"])
                try:
                    zf.write(src, arc)
                except (ValueError, OSError):
                    # 极少数 mtime 早于 1980 的文件会被 zipfile 拒绝：带回退时间戳
                    with open(src, "rb") as fh:
                        data = fh.read()
                    zi = zipfile.ZipInfo(arc, date_time=(1980, 1, 1, 0, 0, 0))
                    zi.compress_type = zipfile.ZIP_STORED
                    zf.writestr(zi, data)
                written += im["size"]
                beat(i, im["name"])
        os.replace(part, first)        # 原子替换：半成品不会冒充成品
    except Cancelled:
        _silent_remove(part)
        return {"ok": False, "cancelled": True, "error": "已取消"}
    except (OSError, zipfile.BadZipFile) as exc:
        _silent_remove(part)
        return {"ok": False, "error": f"打包失败：{exc}"}

    produced = [{"format": targets[0][0], "path": first,
                 "size": os.path.getsize(first)}]

    # 其余格式：内容完全相同，直接复制再改扩展名
    for fmt, dst in targets[1:]:
        tmp = dst + ".part"
        try:
            shutil.copyfile(first, tmp)
            os.replace(tmp, dst)
        except OSError as exc:
            _silent_remove(tmp)
            _silent_remove(dst)
            return {"ok": False, "error": f"生成 {fmt} 失败：{exc}",
                    "produced": produced}
        produced.append({"format": fmt, "path": dst, "size": os.path.getsize(dst)})

    elapsed = time.time() - started
    return {
        "ok": True,
        "produced": produced,
        "count": n,
        "bytes": total_bytes,
        "elapsed": round(elapsed, 2),
        "folder": folder,
    }


def _silent_remove(path: str):
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def human_size(n: int) -> str:
    """字节数转可读体积。"""
    step = 1024.0
    v = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < step or unit == "TB":
            return (f"{v:.0f} {unit}" if unit == "B" else f"{v:.1f} {unit}")
        v /= step
    return f"{n} B"
