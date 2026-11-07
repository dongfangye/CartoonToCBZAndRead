# -*- coding: utf-8 -*-
"""tests/test_namer.py — 视频重命名的解析与执行测试"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
import unittest
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import namer  # noqa: E402


@contextlib.contextmanager
def workdir():
    base = os.path.join(tempfile.gettempdir(), "comicpack-tests")
    path = os.path.join(base, uuid.uuid4().hex[:8])
    os.makedirs(path, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class ParseTest(unittest.TestCase):
    def check(self, name, folder, expected):
        parsed = namer.parse(name, folder)
        self.assertEqual(namer.build_name(parsed, ".mkv"), expected, name)

    def test_release_tags(self):
        self.check("[Lilith-Raws] 葬送的芙莉莲 - 01 [Baha][WEB-DL][1080p][AVC AAC][CHT][MP4].mp4",
                   "下载", "葬送的芙莉莲 S01E01.mkv")

    def test_sxxexx(self):
        self.check("葬送的芙莉莲.S01E05.1080p.WEB-DL.mkv", "x", "葬送的芙莉莲 S01E05.mkv")
        self.check("Show.Name.S02E10.720p.WEB-DL.mkv", "x", "Show Name S02E10.mkv")

    def test_chinese_episode_and_season(self):
        self.check("进击的巨人 第二季 第03集.mp4", "x", "进击的巨人 S02E03.mkv")
        self.check("进击的巨人 第10话.mp4", "x", "进击的巨人 S01E10.mkv")

    def test_bracket_episode(self):
        self.check("[VCB-Studio] 86 - 08 [Ma10p_1080p].mkv", "x", "86 S01E08.mkv")
        self.check("[ANi] 孤独摇滚 [05][1080P][Baha].mp4", "x", "孤独摇滚 S01E05.mkv")

    def test_title_falls_back_to_folder(self):
        self.check("第01集.mp4", "孤独摇滚", "孤独摇滚 S01E01.mkv")
        self.check("01.mkv", "某部番", "某部番 S01E01.mkv")

    def test_multi_episode(self):
        self.check("剧名 S01E01-E02.mkv", "x", "剧名 S01E01-E02.mkv")

    def test_skips(self):
        for name in ("花絮.mp4", "making-of.mkv"):
            with self.assertRaises(namer.ParseError):
                namer.parse(name, "某剧")


class ScanApplyTest(unittest.TestCase):
    def _touch(self, folder, name, data=b"x"):
        path = os.path.join(folder, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def test_scan_preview(self):
        with workdir() as tmp:
            self._touch(tmp, "[Group] 剧名 - 01 [1080p].mkv")
            self._touch(tmp, "剧名 - 02 [1080p].mkv")
            self._touch(tmp, "note.txt")

            res = namer.scan(tmp)
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["stats"]["total"], 2)
            self.assertEqual(res["stats"]["ignored"], 1)
            self.assertEqual([i["new_name"] for i in res["items"]],
                             ["剧名 S01E01.mkv", "剧名 S01E02.mkv"])
            self.assertTrue(all(i["status"] == "ok" for i in res["items"]))
            # 预览不应落盘
            self.assertTrue(os.path.exists(os.path.join(tmp, "[Group] 剧名 - 01 [1080p].mkv")))

    def test_apply_renames_and_keeps_content(self):
        with workdir() as tmp:
            self._touch(tmp, "剧名 - 01 [1080p].mkv", b"one")
            res = namer.apply(tmp)
            self.assertTrue(res["ok"], res)
            self.assertEqual(len(res["renamed"]), 1)
            self.assertEqual(os.listdir(tmp), ["剧名 S01E01.mkv"])
            with open(os.path.join(tmp, "剧名 S01E01.mkv"), "rb") as fh:
                self.assertEqual(fh.read(), b"one")

    def test_apply_skips_conflicts(self):
        with workdir() as tmp:
            self._touch(tmp, "剧名 - 01.mkv", b"old")
            self._touch(tmp, "剧名 S01E01.mkv", b"exists")
            res = namer.scan(tmp)
            statuses = {i["name"]: i["status"] for i in res["items"]}
            self.assertEqual(statuses["剧名 S01E01.mkv"], "same")
            self.assertEqual(statuses["剧名 - 01.mkv"], "conflict")

            out = namer.apply(tmp)
            self.assertEqual(out["renamed"], [])
            # 两个文件都还在，没被覆盖
            self.assertEqual(sorted(os.listdir(tmp)), sorted(["剧名 - 01.mkv", "剧名 S01E01.mkv"]))

    def test_apply_only_selected(self):
        with workdir() as tmp:
            self._touch(tmp, "剧名 - 01.mkv")
            self._touch(tmp, "剧名 - 02.mkv")
            out = namer.apply(tmp, names=["剧名 - 02.mkv"])
            self.assertEqual([d["to"] for d in out["renamed"]], ["剧名 S01E02.mkv"])
            self.assertIn("剧名 - 01.mkv", os.listdir(tmp))

    def test_empty_folder(self):
        with workdir() as tmp:
            res = namer.scan(tmp)
            self.assertTrue(res["ok"])
            self.assertEqual(res["stats"]["total"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
