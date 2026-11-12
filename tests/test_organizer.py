# -*- coding: utf-8 -*-
"""tests/test_organizer.py — 视频整理：扫描 / 计划 / 执行 / 回滚"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import organizer  # noqa: E402


@contextlib.contextmanager
def workdir():
    base = os.path.join(tempfile.gettempdir(), "comicpack-tests")
    path = os.path.join(base, uuid.uuid4().hex[:8])
    os.makedirs(path, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def touch(path, data=b"video"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


class ScanTest(unittest.TestCase):
    def test_scan_finds_videos_and_subs(self):
        with workdir() as tmp:
            touch(os.path.join(tmp, "第1话", "01.mp4"))
            touch(os.path.join(tmp, "第1话", "01.chs.ass"))
            touch(os.path.join(tmp, "第1话", "01.srt"))
            touch(os.path.join(tmp, "第2话", "02.mkv"))
            touch(os.path.join(tmp, "第2话", "note.txt"))

            res = organizer.scan(tmp)
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["totals"]["videos"], 2)
            self.assertEqual(res["totals"]["dirs"], 2)
            names = {v["name"]: len(v["subs"]) for v in res["videos"]}
            self.assertEqual(names["01.mp4"], 2)
            self.assertEqual(names["02.mkv"], 0)

    def test_scan_rejects_missing(self):
        res = organizer.scan(r"C:\this\does\not\exist")
        self.assertFalse(res["ok"])


class PlanTest(unittest.TestCase):
    def _tree(self, tmp):
        touch(os.path.join(tmp, "A", "1.mp4"))
        touch(os.path.join(tmp, "A", "2.mp4"))
        touch(os.path.join(tmp, "B", "3.mp4"))

    def test_unassigned_goes_to_other(self):
        with workdir() as tmp:
            self._tree(tmp)
            p = organizer.plan(tmp, {"folders": ["新"], "assignments": {}, "keep": []})
            self.assertTrue(p["ok"], p)
            self.assertTrue(p["other"])
            self.assertIn("其他", p["folders"])
            self.assertEqual(len(p["moves"]), 3)

    def test_keep_dir_videos_stay(self):
        with workdir() as tmp:
            self._tree(tmp)
            p = organizer.plan(tmp, {"folders": ["新"], "assignments": {}, "keep": ["B"]})
            self.assertTrue(p["ok"], p)
            self.assertEqual([s["from_dir"] for s in p["stays"]], ["B"])
            self.assertTrue(p["other"])                     # A 里没归类的进「其他」
            self.assertTrue(all(m["from_dir"] == "A" for m in p["moves"]))

    def test_folder_name_rules(self):
        with workdir() as tmp:
            self._tree(tmp)
            os.makedirs(os.path.join(tmp, "已存在"))
            for bad in ("", "  ", "a/b", "已存在", "其他"):
                p = organizer.plan(tmp, {"folders": [bad], "assignments": {}})
                self.assertFalse(p["ok"], f"应当拒绝：{bad!r}")
            p = organizer.plan(tmp, {"folders": ["新", "新"], "assignments": {}})
            self.assertFalse(p["ok"])

    def test_rename_rules(self):
        with workdir() as tmp:
            self._tree(tmp)
            p = organizer.plan(tmp, {"folders": ["新"], "assignments": {},
                                     "renames": {"A\\1.mp4": "改个名.mp4"}})
            self.assertTrue(p["ok"], p)
            moved = [m for m in p["moves"] if m["name"] == "1.mp4"][0]
            self.assertEqual(moved["final"], "改个名.mp4")

            for bad in ("a/b.mp4", "换后缀.mkv"):
                p = organizer.plan(tmp, {"folders": ["新"], "assignments": {},
                                         "renames": {"A\\1.mp4": bad}})
                self.assertFalse(p["ok"], f"应当拒绝：{bad}")

    def test_stay_subtitle_keeps_name(self):
        """保留目录里的视频和字幕都不改名时，不能被自己挤成 (2)。"""
        with workdir() as tmp:
            touch(os.path.join(tmp, "A", "1.mp4"))
            touch(os.path.join(tmp, "A", "1.chs.ass"))
            p = organizer.plan(tmp, {"folders": [], "assignments": {}, "keep": ["A"]})
            self.assertTrue(p["ok"], p)
            stay = p["stays"][0]
            self.assertEqual(stay["final"], "1.mp4")
            self.assertEqual(stay["subs"][0]["dst"], os.path.join(tmp, "A", "1.chs.ass"))

    def test_conflict_adds_index(self):
        with workdir() as tmp:
            touch(os.path.join(tmp, "A", "剧集.mp4"))
            touch(os.path.join(tmp, "B", "剧集.mp4"))
            p = organizer.plan(tmp, {"folders": ["新"], "assignments": {}, "keep": []})
            self.assertTrue(p["ok"], p)
            names = sorted(m["final"] for m in p["moves"])
            self.assertEqual(names, ["剧集 (2).mp4", "剧集.mp4"])


class ApplyTest(unittest.TestCase):
    def _tree(self, tmp):
        touch(os.path.join(tmp, "A", "1.mp4"), b"one")
        touch(os.path.join(tmp, "A", "1.chs.ass"), b"sub")
        touch(os.path.join(tmp, "A", "2.mp4"), b"two")
        touch(os.path.join(tmp, "B", "3.mp4"), b"three")

    def test_apply_moves_and_cleans(self):
        with workdir() as tmp:
            self._tree(tmp)
            res = organizer.apply(tmp, {
                "folders": ["新"],
                "assignments": {"A\\1.mp4": "新", "A\\2.mp4": "新"},
                "keep": ["B"],
            })
            self.assertTrue(res["ok"], res)
            self.assertTrue(os.path.isfile(os.path.join(tmp, "新", "1.mp4")))
            self.assertTrue(os.path.isfile(os.path.join(tmp, "新", "1.chs.ass")))
            self.assertTrue(os.path.isfile(os.path.join(tmp, "新", "2.mp4")))
            self.assertFalse(os.path.exists(os.path.join(tmp, "A")))
            self.assertTrue(os.path.isfile(os.path.join(tmp, "B", "3.mp4")))
            self.assertFalse(os.path.exists(os.path.join(tmp, "其他")))

    def test_apply_renames_and_subs(self):
        with workdir() as tmp:
            self._tree(tmp)
            res = organizer.apply(tmp, {
                "folders": ["新"],
                "assignments": {"A\\1.mp4": "新"},
                "renames": {"A\\1.mp4": "改名了.mp4"},
                "keep": [],
            })
            self.assertTrue(res["ok"], res)
            self.assertTrue(os.path.isfile(os.path.join(tmp, "新", "改名了.mp4")))
            self.assertTrue(os.path.isfile(os.path.join(tmp, "新", "改名了.chs.ass")))

    def test_delete_failure_is_reported(self):
        with workdir() as tmp:
            self._tree(tmp)
            touch(os.path.join(tmp, "A", "keepme.txt"))
            res = organizer.apply(tmp, {"folders": ["新"], "assignments": {}, "keep": []})
            self.assertTrue(res["ok"], res)
            self.assertTrue(os.path.isdir(os.path.join(tmp, "A")))     # 非空，删不掉
            self.assertTrue(any(f["rel"] == "A" for f in res["failed"]))
            self.assertFalse(os.path.exists(os.path.join(tmp, "B")))

    def test_move_failure_rolls_back(self):
        with workdir() as tmp:
            self._tree(tmp)
            real_rename = os.rename
            calls = {"n": 0}

            def flaky(src, dst, *a, **kw):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise OSError("模拟占用")
                return real_rename(src, dst, *a, **kw)

            with mock.patch("organizer.os.rename", side_effect=flaky):
                res = organizer.apply(tmp, {"folders": ["新"], "assignments": {}, "keep": []})

            self.assertFalse(res["ok"])
            self.assertTrue(res["rolled_back"])
            self.assertTrue(os.path.isfile(os.path.join(tmp, "A", "1.mp4")))
            self.assertTrue(os.path.isfile(os.path.join(tmp, "B", "3.mp4")))
            self.assertFalse(os.path.isdir(os.path.join(tmp, "新")))     # 新目录已回滚删除


if __name__ == "__main__":
    unittest.main(verbosity=2)
