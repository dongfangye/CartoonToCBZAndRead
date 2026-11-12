# -*- coding: utf-8 -*-
"""
namer.py — 剧集视频按 Emby 规则改名
====================================
只解析现有文件名（离线），把一个文件夹里的视频改成：

    剧名 S01E01.mkv
    剧名 S01E01-E02.mkv      （连播/合集）

规则：
  1. 集数从文件名里找：S01E05 / 第 5 集 / EP05 / [05] / 「剧名 - 05」都认；
  2. 季数找不到就按第 1 季；文件夹名里有「第二季」也会用上；
  3. 剧名取集数之前的文字，去掉 [字幕组]、分辨率、编码等标签；
  4. 剧名实在没有（比如文件就叫「第01集.mp4」）就用所在文件夹名；
  5. 只改名，不移动文件、不覆盖已有文件、不动字幕和 nfo。
"""

from __future__ import annotations

import os
import re
import time

import packer

VIDEO_EXTS = {
    ".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".mts", ".wmv", ".flv",
    ".mov", ".rmvb", ".rm", ".webm", ".mpg", ".mpeg", ".m4v", ".3gp", ".ogv",
}

# 整理剧名时要丢掉的发布/编码标签
_JUNK = re.compile(
    r"(?i)\b(?:2160p|1080p|720p|480p|4k|uhd|x264|x265|h\.?264|h\.?265|hevc|avc|"
    r"aac|ac3|eac3|dts(?:-hd)?|flac|opus|web-?dl|webrip|bluray|blu-ray|bdrip|"
    r"hdtv|dvdrip|remux|10bit|8bit|hi10p|ma10p|hdr|dv|proper|repack|v2)\b"
)

_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

# 集数/季数的各种写法
_SXXEXX = re.compile(r"(?i)(?:^|[^a-z0-9])s(\d{1,2})\s*e(\d{1,3})(?:\s*[-~]\s*e?(\d{1,3}))?")
_CN_EP = re.compile(r"第\s*(\d{1,3})\s*[集话話期]")
_CN_SEASON = re.compile(r"第\s*([0-9一二三四五六七八九十]{1,3})\s*[季部]")
_EN_SEASON = re.compile(r"(?i)\bseason\s*(\d{1,2})\b")
_EN_EP = re.compile(r"(?i)(?:^|[^a-z0-9])ep?\s*(\d{1,3})(?!\d)")
_BRACKET_EP = re.compile(r"[\[【(（]\s*(\d{1,3})(?:v\d+)?\s*[\]】)）]")
_DASH_EP = re.compile(r"[-_~]\s*(\d{1,3})(?:v\d+)?(?=\s*(?:[\[\(【（]|$))")
_BARE_NUM = re.compile(r"^\s*(\d{1,3})(?:v\d+)?\s*$")

_BRACKETS = re.compile(r"[\[\(【（][^\]\)】）]*[\]\)】）]")
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_CJK = re.compile(r"[\u4e00-\u9fff]")


def _bracket_cjk(text: str) -> str:
    """从括号里挑一段含中文的名称，例如「1-【菠萝吹吹 …】(61)」里的「菠萝吹吹 …」。"""
    best = ""
    for m in re.finditer(r"[\[【(（]([^\]\)】）]+)[\]】)）]", text):
        inner = m.group(1).strip()
        if inner and _CJK.search(inner) and len(inner) > len(best):
            best = inner
    return best


class ParseError(Exception):
    """解析不出剧名或集数。"""


def is_video(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in VIDEO_EXTS


def _cn_number(text: str) -> int | None:
    if text.isdigit():
        return int(text)
    if text == "十":
        return 10
    if text.startswith("十"):
        return 10 + _CN_DIGITS.get(text[1:], 0)
    if text.endswith("十"):
        return _CN_DIGITS.get(text[0], 0) * 10
    if len(text) == 2 and text[1] == "十":
        return _CN_DIGITS.get(text[0], 0) * 10
    return _CN_DIGITS.get(text)


def _clean_title(text: str) -> str:
    """去掉标签、季数、分隔符，留下干净的剧名。"""
    text = _BRACKETS.sub(" ", text)
    text = _CN_SEASON.sub(" ", text)
    text = _EN_SEASON.sub(" ", text)
    text = _JUNK.sub(" ", text)
    text = re.sub(r"(?i)(?:^|\s)s\d{1,2}(?=\s|$)", " ", text)
    text = re.sub(r"[._]+", " ", text)
    text = re.sub(r"\s*[-–—~]+\s*", " ", text)
    text = _ILLEGAL.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" .-_")
    return text


def _season_hint(raw: str) -> int | None:
    m = _CN_SEASON.search(raw)
    if m:
        n = _cn_number(m.group(1))
        if n:
            return n
    m = _EN_SEASON.search(raw)
    if m:
        return int(m.group(1))
    return None


def _find_episode(raw: str) -> dict | None:
    """按优先级找集数，返回 {episode, episode2, season, cut}。"""
    m = _SXXEXX.search(raw)
    if m:
        return {"episode": int(m.group(2)),
                "episode2": int(m.group(3)) if m.group(3) else None,
                "season": int(m.group(1)),
                "cut": m.start()}

    m = _CN_EP.search(raw)
    if m:
        return {"episode": int(m.group(1)), "episode2": None,
                "season": _season_hint(raw), "cut": m.start()}

    m = _EN_EP.search(raw)
    if m:
        return {"episode": int(m.group(1)), "episode2": None,
                "season": _season_hint(raw), "cut": m.start()}

    m = _BRACKET_EP.search(raw)
    if m:
        n = int(m.group(1))
        if not 1900 <= n <= 2100:                    # 括号里的年份不算集数
            return {"episode": n, "episode2": None,
                    "season": _season_hint(raw), "cut": m.start()}

    m = _DASH_EP.search(raw)
    if m:
        return {"episode": int(m.group(1)), "episode2": None,
                "season": _season_hint(raw), "cut": m.start()}

    m = _BARE_NUM.match(raw)                     # 文件名就是一串数字：01.mkv
    if m:
        return {"episode": int(m.group(1)), "episode2": None,
                "season": _season_hint(raw), "cut": 0}

    return None


def parse(name: str, folder_name: str = "") -> dict:
    """从文件名解析出剧名 / 季 / 集；失败抛 ParseError。"""
    base = os.path.splitext(name)[0]
    found = _find_episode(base)
    if not found:
        raise ParseError("没找到集数（支持 S01E01 / 第01集 / EP01 / [01] / 剧名 - 01）")

    raw_title = base[:found["cut"]] if found["cut"] > 0 else ""
    title = _clean_title(raw_title)
    # 「1-【剧名】(61)」这种：括号里的中文才是剧名
    if not title or title.isdigit():
        inner = _bracket_cjk(raw_title)
        if inner:
            title = _clean_title(inner)
    if not title:
        title = _clean_title(folder_name)
    if not title:
        raise ParseError("认不出剧名，文件名里也没有，文件夹名也不像剧名")

    season = found["season"] or _season_hint(base) or 1
    if season < 1 or season > 99:
        season = 1

    return {
        "title": title,
        "season": season,
        "episode": found["episode"],
        "episode2": found["episode2"],
    }


def episode_info(name: str) -> dict | None:
    """只取季/集，不要求能认出剧名（给「剧集名用户自己填」的场景用）。"""
    base = os.path.splitext(name)[0]
    found = _find_episode(base)
    if not found:
        return None
    season = found["season"] or 1
    if season < 1 or season > 99:
        season = 1
    return {"season": season, "episode": found["episode"],
            "episode2": found["episode2"]}


def build_name(parsed: dict, ext: str) -> str:
    """Emby 风格：剧名 S01E01.mkv（连播写成 S01E01-E02）。"""
    tag = f"S{parsed['season']:02d}E{parsed['episode']:02d}"
    if parsed.get("episode2"):
        tag += f"-E{parsed['episode2']:02d}"
    return f"{parsed['title']} {tag}{ext.lower()}"


def scan(folder: str) -> dict:
    """扫描文件夹里的视频，给出改名建议（不落盘）。"""
    folder = packer.normalize_path(folder)
    if not folder:
        return {"ok": False, "error": "请先选择文件夹"}
    if not os.path.isdir(folder):
        return {"ok": False, "error": f"文件夹不存在：{folder}"}

    folder_name = os.path.basename(folder.rstrip("\\/"))
    files, ignored = [], 0
    try:
        with os.scandir(folder) as it:
            for entry in it:
                try:
                    if not entry.is_file(follow_symlinks=True):
                        continue
                except OSError:
                    continue
                if not is_video(entry.name):
                    ignored += 1
                    continue
                try:
                    st = entry.stat(follow_symlinks=True)
                except OSError:
                    continue
                files.append({"name": entry.name, "size": st.st_size, "ext": os.path.splitext(entry.name)[1]})
    except OSError as exc:
        return {"ok": False, "error": f"无法读取文件夹：{exc}"}

    files.sort(key=lambda f: packer.natural_key(f["name"]))
    items = []
    taken = {}
    for info in files:
        item = {
            "name": info["name"],
            "size": info["size"],
            "size_text": packer.human_size(info["size"]),
            "title": "",
            "season": None,
            "episode": None,
            "new_name": "",
            "status": "ok",
            "reason": "",
        }
        try:
            parsed = parse(info["name"], folder_name)
        except ParseError as exc:
            item.update(status="skip", reason=str(exc))
            items.append(item)
            continue

        item.update(title=parsed["title"], season=parsed["season"], episode=parsed["episode"])
        new_name = build_name(parsed, info["ext"])

        if new_name == info["name"]:
            item.update(status="same", new_name=new_name, reason="已经是规范名字")
        else:
            item["new_name"] = new_name
            key = new_name.casefold()
            if key in taken:
                item.update(status="conflict", reason=f"和「{taken[key]}」改名后重名")
            elif os.path.exists(os.path.join(folder, new_name)):
                item.update(status="conflict", reason="目标文件名已存在")
            else:
                taken[key] = info["name"]
        items.append(item)

    stats = {"total": len(items), "ok": 0, "same": 0, "conflict": 0, "skip": 0}
    for item in items:
        stats[item["status"]] += 1
    stats["ignored"] = ignored

    return {
        "ok": True,
        "folder": folder,
        "name": folder_name or folder,
        "items": items,
        "stats": stats,
    }


def _rename(src: str, dst: str):
    """改名；只有大小写不同时走中转名，避免 Windows 认为「已存在」。"""
    if os.path.normcase(src) == os.path.normcase(dst):
        tmp = dst + ".renaming"
        os.rename(src, tmp)
        os.rename(tmp, dst)
    else:
        os.rename(src, dst)


def apply(folder: str, names: list | None = None) -> dict:
    """执行改名：只动扫描结果为 ok 的文件，绝不覆盖。"""
    folder = packer.normalize_path(folder)
    result = scan(folder)
    if not result.get("ok"):
        return result

    wanted = None if names is None else {str(n) for n in names}
    done, failed = [], []
    started = time.time()

    for item in result["items"]:
        if item["status"] != "ok":
            continue
        if wanted is not None and item["name"] not in wanted:
            continue
        src = os.path.join(folder, item["name"])
        dst = os.path.join(folder, item["new_name"])
        if os.path.exists(dst) and os.path.normcase(src) != os.path.normcase(dst):
            failed.append({"name": item["name"], "error": "目标文件名已存在"})
            continue
        try:
            _rename(src, dst)
            done.append({"from": item["name"], "to": item["new_name"]})
        except OSError as exc:
            failed.append({"name": item["name"], "error": str(exc)})

    return {
        "ok": True,
        "folder": folder,
        "renamed": done,
        "failed": failed,
        "elapsed": round(time.time() - started, 2),
    }
