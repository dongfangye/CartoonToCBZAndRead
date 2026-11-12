# -*- coding: utf-8 -*-
"""
emby.py — 把一堆剧集视频整理成 Emby 认的剧集结构
=================================================
流程：扫描（递归找视频 + 同名字幕）→ 用户填剧名/年份/季 → 预览 → 执行

执行结果：

    剧名 (年份)/Season 01/剧名 S01E01.mp4
    剧名 (年份)/Season 01/剧名 S01E01.chs.ass

剧名由用户输入（不猜），集数从原文件名里解析（支持 S01E01 / 第01集 / [01] / (01) / 剧名 - 01）。
移动是同盘 rename；移动阶段失败整单回滚。
"""

from __future__ import annotations

import os

import namer
import organizer
import packer

_ILLEGAL = organizer._ILLEGAL
_RESERVED = organizer._RESERVED


def _valid_show(show: str) -> str:
    show = (show or "").strip().strip(".")
    if not show:
        raise ValueError("剧名不能为空")
    if _ILLEGAL.search(show):
        raise ValueError(f"剧名里有非法字符：{show}")
    if show.casefold() in _RESERVED:
        raise ValueError(f"这是 Windows 保留名，换个剧名：{show}")
    return show


def _unique(candidate: str, taken: set) -> str:
    stem, ext = os.path.splitext(candidate)
    out = candidate
    i = 2
    while out.casefold() in taken:
        out = f"{stem} ({i}){ext}"
        i += 1
    taken.add(out.casefold())
    return out


def scan(root: str) -> dict:
    """递归找视频，解析集数（不要求剧名）。"""
    info = organizer.scan(root)
    if not info.get("ok"):
        return info

    rows = []
    for v in info["videos"]:
        ep = namer.episode_info(v["name"])
        row = dict(v)
        if ep:
            row.update(episode=ep["episode"], episode2=ep["episode2"],
                       season_hint=ep["season"], status="ok", reason="")
        else:
            row.update(episode=None, episode2=None, season_hint=None,
                       status="skip", reason="文件名里找不到集数")
        rows.append(row)

    ok = [r for r in rows if r["status"] == "ok"]
    return {
        "ok": True,
        "root": info["root"],
        "name": info["name"],
        "rows": rows,
        "dirs": info["dirs"],
        "totals": {"videos": len(rows), "ok": len(ok), "skip": len(rows) - len(ok),
                   "bytes": info["totals"]["bytes"],
                   "size_text": info["totals"]["size_text"]},
    }


def build(root: str, payload: dict) -> dict:
    """生成预览：目标目录、每个文件的新名字、缺号、冲突。"""
    info = scan(root)
    if not info.get("ok"):
        return info

    try:
        show = _valid_show(payload.get("show"))
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    year = str(payload.get("year") or "").strip()
    if year and (not year.isdigit() or not 1900 <= int(year) <= 2100):
        return {"ok": False, "error": "年份要写 4 位数，例如 2023（也可以留空）"}

    try:
        season = int(payload.get("season") or 1)
    except (TypeError, ValueError):
        return {"ok": False, "error": "季数要是数字"}

    if season < 1 or season > 99:
        return {"ok": False, "error": "季数范围 1-99"}

    base = info["root"]
    show_dir = f"{show} ({year})" if year else show
    season_dir_name = f"Season {season:02d}"
    target_dir = os.path.join(base, show_dir, season_dir_name)

    taken = set()
    rows = []
    for row in info["rows"]:
        if row["status"] != "ok":
            rows.append(dict(row, new_name="", reason=row["reason"]))
            continue
        tag = f"S{season:02d}E{row['episode']:02d}"
        if row.get("episode2"):
            tag += f"-E{row['episode2']:02d}"
        new_name = _unique(f"{show} {tag}{row['ext']}", taken)
        rows.append(dict(row, new_name=new_name))

    episodes = [r["episode"] for r in info["rows"] if r["status"] == "ok"]
    missing = []
    if episodes:
        missing = [n for n in range(min(episodes), max(episodes) + 1)
                   if n not in set(episodes)]

    return {
        "ok": True,
        "root": base,
        "show": show,
        "year": year,
        "season": season,
        "show_dir": show_dir,
        "season_dir": season_dir_name,
        "target_dir": target_dir,
        "rows": rows,
        "missing": missing,
        "totals": {"videos": len(info["rows"]),
                   "ok": len(episodes),
                   "skip": len(info["rows"]) - len(episodes),
                   "missing": len(missing)},
        "conflict_note": "目标目录里已有同名文件时会自动加 (2)",
    }


def apply(root: str, payload: dict) -> dict:
    """建目录 + 移动/改名（+ 字幕），失败回滚；可选删空目录。"""
    plan = build(root, payload)
    if not plan.get("ok"):
        return plan

    base = plan["root"]
    show_path = os.path.join(base, plan["show_dir"])
    target = plan["target_dir"]
    journal = []
    moved, subs_moved = 0, 0

    try:
        if not os.path.isdir(show_path):
            os.makedirs(show_path)
            journal.append(("mkdir", show_path))
        if not os.path.isdir(target):
            os.makedirs(target)
            journal.append(("mkdir", target))

        for row in plan["rows"]:
            if row["status"] != "ok":
                continue
            dst = os.path.join(target, row["new_name"])
            os.rename(row["path"], dst)
            journal.append(("move", row["path"], dst))
            moved += 1

            new_stem = os.path.splitext(row["new_name"])[0]
            for sub in row.get("subs") or []:
                sub_dst = os.path.join(target, new_stem + sub["extra"] + sub["ext"])
                os.rename(sub["path"], sub_dst)
                journal.append(("move", sub["path"], sub_dst))
                subs_moved += 1
    except OSError as exc:
        errors = organizer._rollback(journal)
        return {"ok": False, "error": f"整理失败，已回滚：{exc}",
                "rolled_back": True, "rollback_errors": errors}

    removed = []
    if payload.get("delete_empty"):
        removed = _remove_empty_dirs(base)

    return {
        "ok": True,
        "root": base,
        "target_dir": target,
        "show_dir": os.path.join(base, plan["show_dir"]),
        "moved": moved,
        "subs": subs_moved,
        "skipped": plan["totals"]["skip"],
        "removed_dirs": removed,
    }


def _remove_empty_dirs(base: str) -> list:
    """删掉整理后变空的旧目录（不删根目录）。"""
    removed = []
    try:
        for dirpath, dirnames, _files in os.walk(base, topdown=False):
            if os.path.normcase(dirpath) == os.path.normcase(base):
                continue
            try:
                os.rmdir(dirpath)
                removed.append(os.path.relpath(dirpath, base))
            except OSError:
                continue
    except OSError:
        pass
    return removed
