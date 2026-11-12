# -*- coding: utf-8 -*-
"""
organizer.py — 视频整理：扫描 → 计划 → 执行
=============================================
用户给一个根目录，递归找出里面的视频（顺带同名字幕），在网页上新建文件夹、
把视频拖进去、随手改名，确认后：

  1. 新建用户在 UI 里建的文件夹（「其他」按需自动建）；
  2. 同盘移动 / 改名视频，字幕跟着走；
  3. 删除整理后不再需要的旧文件夹（用户勾了「保留」的除外）；
  4. 移动阶段任何一步失败 → 逆序回滚，原样恢复；
  5. 删除阶段失败 → 记下来继续，失败的文件夹/文件原样保留。

只处理同一磁盘：移动都是 os.rename（瞬时、可回滚），不做跨盘复制。
"""

from __future__ import annotations

import os
import re
import shutil
import time
import uuid

import packer

VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".ts", ".m2ts", ".mts", ".wmv", ".flv",
    ".mov", ".rmvb", ".rm", ".webm", ".mpg", ".mpeg", ".m4v", ".3gp", ".ogv",
}

SUB_EXTS = {".srt", ".ass", ".ssa", ".vtt"}

OTHER_NAME = "其他"

_SKIP_DIRS = {"$recycle.bin", "system volume information", "recycler", "lost+found"}
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {
    f"lpt{i}" for i in range(1, 10)}

MAX_DEPTH = 12


class OrganizeError(Exception):
    """参数/状态问题，交给 HTTP 层报给用户。"""


def _find_subs(video_path: str) -> list:
    """同名的字幕：第1话.srt / 第1话.chs.ass / 第1话.zh-CN.vtt。"""
    folder = os.path.dirname(video_path)
    stem = os.path.splitext(os.path.basename(video_path))[0]
    out = []
    try:
        with os.scandir(folder) as it:
            for entry in it:
                try:
                    if not entry.is_file(follow_symlinks=True):
                        continue
                except OSError:
                    continue
                ext = os.path.splitext(entry.name)[1].lower()
                if ext not in SUB_EXTS:
                    continue
                sub_stem = os.path.splitext(entry.name)[0]
                if sub_stem == stem or sub_stem.startswith(stem + "."):
                    out.append({"name": entry.name, "path": entry.path,
                                "extra": sub_stem[len(stem):], "ext": ext})
    except OSError:
        return []
    out.sort(key=lambda s: packer.natural_key(s["name"]))
    return out


def scan(root: str) -> dict:
    """递归扫出视频与字幕，返回清单（不落盘）。"""
    root = packer.normalize_path(root)
    if not root:
        return {"ok": False, "error": "请先选择文件夹"}
    if not os.path.isdir(root):
        return {"ok": False, "error": f"文件夹不存在：{root}"}

    base = os.path.abspath(root)
    videos, dirs = [], {}
    try:
        for dirpath, dirnames, filenames in os.walk(base):
            rel = os.path.relpath(dirpath, base)
            depth = 0 if rel == "." else rel.count(os.sep) + 1
            if depth > MAX_DEPTH:
                dirnames[:] = []
                continue
            dirnames[:] = sorted(
                [d for d in dirnames
                 if not d.startswith(".") and d.lower() not in _SKIP_DIRS],
                key=packer.natural_key)

            for fname in sorted(filenames, key=packer.natural_key):
                ext = os.path.splitext(fname)[1].lower()
                if ext not in VIDEO_EXTS:
                    continue
                full = os.path.join(dirpath, fname)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                rel_dir = "" if rel == "." else rel
                rel_file = os.path.relpath(full, base)
                videos.append({
                    "id": rel_file,
                    "rel": rel_file,
                    "path": full,
                    "dir": rel_dir,
                    "name": fname,
                    "ext": ext,
                    "size": st.st_size,
                    "size_text": packer.human_size(st.st_size),
                    "subs": _find_subs(full),
                })
                bucket = dirs.setdefault(rel_dir, {"rel": rel_dir, "path": dirpath,
                                                   "count": 0, "bytes": 0})
                bucket["count"] += 1
                bucket["bytes"] += st.st_size
    except OSError as exc:
        return {"ok": False, "error": f"无法读取文件夹：{exc}"}

    videos.sort(key=lambda v: packer.natural_key(v["rel"]))
    dir_list = sorted(dirs.values(), key=lambda d: packer.natural_key(d["rel"]))
    for d in dir_list:
        d["size_text"] = packer.human_size(d["bytes"])

    total = sum(v["size"] for v in videos)
    return {
        "ok": True,
        "root": base,
        "name": os.path.basename(base.rstrip("\\/")) or base,
        "videos": videos,
        "dirs": dir_list,
        "totals": {"videos": len(videos), "dirs": len(dir_list), "bytes": total,
                   "size_text": packer.human_size(total)},
    }


# ---------------------------------------------------------------- 计划
def _valid_folder_name(name: str) -> str:
    name = (name or "").strip().strip(".")
    if not name:
        raise OrganizeError("文件夹名不能为空")
    if _ILLEGAL.search(name):
        raise OrganizeError(f"文件夹名里有非法字符：{name}")
    if name.casefold() in _RESERVED:
        raise OrganizeError(f"这是 Windows 保留名，换个名字：{name}")
    if name == OTHER_NAME:
        raise OrganizeError("「其他」是自动生成的，别手动建")
    return name


def _valid_video_name(name: str, old_ext: str) -> str:
    name = (name or "").strip()
    if not name:
        raise OrganizeError("文件名不能为空")
    if _ILLEGAL.search(name):
        raise OrganizeError(f"文件名里有非法字符：{name}")
    stem, ext = os.path.splitext(name)
    if not ext:
        return name + old_ext
    if ext.lower() != old_ext:
        raise OrganizeError(f"只能改名字，不能改扩展名：{name}")
    if name.casefold() in _RESERVED:
        raise OrganizeError(f"这是 Windows 保留名，换个名字：{name}")
    return name


def _unique_name(candidate: str, taken: set) -> str:
    """目标目录里重名就自动加 (2)、(3)…"""
    stem, ext = os.path.splitext(candidate)
    out = candidate
    i = 2
    while out.casefold() in taken:
        out = f"{stem} ({i}){ext}"
        i += 1
    taken.add(out.casefold())
    return out


def _is_under(rel: str, parents: list) -> bool:
    for p in parents:
        if rel == p or rel.startswith(p + os.sep):
            return True
    return False


def plan(root: str, payload: dict) -> dict:
    """校验并生成计划：不创建、不移动、不删除。"""
    info = scan(root)
    if not info.get("ok"):
        return info

    base = info["root"]
    videos = {v["id"]: v for v in info["videos"]}
    if not videos:
        return {"ok": False, "error": "这个文件夹里没有找到视频"}

    raw_folders = payload.get("folders") or []
    if not isinstance(raw_folders, list):
        return {"ok": False, "error": "folders 参数不对"}

    folders, seen = [], set()
    try:
        for raw in raw_folders:
            name = _valid_folder_name(raw)
            if name.casefold() in seen:
                raise OrganizeError(f"新建文件夹重名：{name}")
            seen.add(name.casefold())
            if os.path.isdir(os.path.join(base, name)):
                raise OrganizeError(f"根目录下已经有同名文件夹「{name}」，请换个名字")
            folders.append(name)
    except OrganizeError as exc:
        return {"ok": False, "error": str(exc)}

    keep = [str(k).strip() for k in (payload.get("keep") or []) if str(k).strip()]
    for rel in keep:
        if not os.path.isdir(os.path.join(base, rel)):
            return {"ok": False, "error": f"要保留的文件夹不存在：{rel}"}

    assignments = payload.get("assignments") or {}
    renames = payload.get("renames") or {}
    if not isinstance(assignments, dict) or not isinstance(renames, dict):
        return {"ok": False, "error": "assignments / renames 参数不对"}

    moves, stays, planned = [], [], {}
    need_other = False
    try:
        for vid, video in videos.items():
            target = assignments.get(vid)
            if target is not None:
                target = str(target)
                if target not in folders:
                    raise OrganizeError(f"目标文件夹不存在：{target}")
                to_dir = target
                stays_put = False
            elif _is_under(video["dir"], keep):
                to_dir = None
                stays_put = True
            else:
                to_dir = OTHER_NAME
                stays_put = False
                need_other = True

            new_name = renames.get(vid)
            final = _valid_video_name(new_name, video["ext"]) if new_name else video["name"]
            final = os.path.basename(final)

            entry = {
                "id": vid,
                "src": video["path"],
                "from_dir": video["dir"],
                "name": video["name"],
                "final": final,
                "ext": video["ext"],
                "subs": video["subs"],
                "to_dir": to_dir,
                "stay": stays_put,
            }
            (stays if stays_put else moves).append(entry)
            planned[vid] = entry
    except OrganizeError as exc:
        return {"ok": False, "error": str(exc)}

    if need_other:
        if os.path.isdir(os.path.join(base, OTHER_NAME)):
            return {"ok": False,
                    "error": f"根目录下已经有「{OTHER_NAME}」文件夹，请先改名或删掉它"}
        folders.append(OTHER_NAME)

    # 目标目录里已有的名字（保留不动的、原地改名的）
    taken_by_dir = {}
    for entry in stays:
        d = os.path.join(base, entry["from_dir"])
        taken_by_dir.setdefault(entry["from_dir"], set())
        for name in os.listdir(d) if os.path.isdir(d) else []:
            taken_by_dir[entry["from_dir"]].add(name.casefold())
    # 原地不动的文件（含字幕）自己占着旧名：先释放，改名后再争用
    for entry in stays:
        taken = taken_by_dir[entry["from_dir"]]
        taken.discard(entry["name"].casefold())
        for sub in entry["subs"]:
            taken.discard(sub["name"].casefold())

    created = []
    for entry in moves:
        taken = taken_by_dir.setdefault(entry["to_dir"], set())
        final = _unique_name(entry["final"], taken)
        entry["final"] = final
        target_dir = os.path.join(base, entry["to_dir"])
        entry["dst"] = os.path.join(target_dir, final)
        entry["subs"] = _sub_targets(entry, target_dir, final, taken)
        if entry["to_dir"] not in created:
            created.append(entry["to_dir"])

    for entry in stays:
        taken = taken_by_dir[entry["from_dir"]]
        final = _unique_name(entry["final"], taken)
        entry["final"] = final
        target_dir = os.path.join(base, entry["from_dir"])
        entry["dst"] = os.path.join(target_dir, final)
        entry["subs"] = _sub_targets(entry, target_dir, final, taken)

    # 要删的旧目录：根目录下所有子目录，排除保留目录（及其上下级）和新文件夹
    delete_dirs = _delete_candidates(base, keep, folders)

    changed = [e for e in moves if e["dst"] != e["src"]]
    return {
        "ok": True,
        "root": base,
        "folders": folders,
        "other": need_other,
        "keep": keep,
        "moves": moves,
        "stays": stays,
        "changed": len(changed),
        "renamed": sum(1 for e in stays + moves if e["final"] != e["name"]),
        "subs": sum(len(e["subs"]) for e in moves + stays),
        "delete_dirs": delete_dirs,
        "totals": {"videos": len(videos), "created": len(created),
                   "moved": len(moves), "stayed": len(stays),
                   "deleted": len(delete_dirs)},
    }


def _sub_targets(entry: dict, target_dir: str, final_name: str, taken: set) -> list:
    """字幕跟着视频改名：第1话.chs.ass → 新名字.chs.ass。"""
    if not entry["subs"]:
        return []
    new_stem = os.path.splitext(final_name)[0]
    out = []
    for sub in entry["subs"]:
        cand = new_stem + sub["extra"] + sub["ext"]
        cand = _unique_name(cand, taken)
        out.append({"name": sub["name"], "src": sub["path"],
                    "dst": os.path.join(target_dir, cand)})
    return out


def _delete_candidates(base: str, keep: list, new_folders: list) -> list:
    protected = {f.casefold() for f in new_folders}
    out = []
    try:
        for dirpath, dirnames, _files in os.walk(base):
            rel = os.path.relpath(dirpath, base)
            if rel == ".":
                continue
            rel = rel
            if _is_under(rel, keep):
                dirnames[:] = []
                continue
            if any(rel == p or rel.startswith(p + os.sep) for p in keep):
                continue
            if rel.count(os.sep) == 0 and rel.casefold() in protected:
                dirnames[:] = []
                continue
            # 保留目录的上级也别删（否则会连带删掉保留目录）
            if any(p.startswith(rel + os.sep) for p in keep):
                continue
            out.append({"rel": rel, "path": dirpath})
    except OSError:
        return []
    out.sort(key=lambda d: d["rel"].count(os.sep), reverse=True)
    return out


# ---------------------------------------------------------------- 执行
def _rollback(journal: list) -> list:
    errors = []
    for entry in reversed(journal):
        kind = entry[0]
        try:
            if kind == "move":
                os.rename(entry[2], entry[1])
            elif kind == "mkdir":
                os.rmdir(entry[1])
        except OSError as exc:
            errors.append(f"{entry[1]}：{exc}")
    return errors


def apply(root: str, payload: dict) -> dict:
    """按计划执行；移动阶段失败整体回滚，删除阶段失败只记录。"""
    plan_result = plan(root, payload)
    if not plan_result.get("ok"):
        return plan_result

    base = plan_result["root"]
    jid = uuid.uuid4().hex[:12]
    journal = []
    started = time.time()

    try:
        for name in plan_result["folders"]:
            path = os.path.join(base, name)
            os.makedirs(path)                     # 已存在会抛错 → 回滚
            journal.append(("mkdir", path))

        for entry in plan_result["moves"] + plan_result["stays"]:
            if entry["dst"] != entry["src"]:
                os.rename(entry["src"], entry["dst"])
                journal.append(("move", entry["src"], entry["dst"]))
            for sub in entry["subs"]:
                if sub["dst"] != sub["src"]:
                    os.rename(sub["src"], sub["dst"])
                    journal.append(("move", sub["src"], sub["dst"]))
    except OSError as exc:
        rollback_errors = _rollback(journal)
        return {"ok": False, "error": f"移动失败，已回滚：{exc}",
                "rolled_back": True, "rollback_errors": rollback_errors}

    deleted, failed = [], []
    for item in plan_result["delete_dirs"]:
        try:
            os.rmdir(item["path"])
            deleted.append(item["rel"])
        except OSError as exc:
            failed.append({"rel": item["rel"], "error": str(exc)})

    return {
        "ok": True,
        "journal": jid,
        "root": base,
        "moved": len(plan_result["moves"]),
        "renamed": plan_result["renamed"],
        "subs": plan_result["subs"],
        "created": plan_result["folders"],
        "deleted": deleted,
        "failed": failed,
        "stayed": len(plan_result["stays"]),
        "elapsed": round(time.time() - started, 2),
    }
