# -*- coding: utf-8 -*-
"""tests/test_emby.py — Emby 剧集整理：扫描 / 预览 / 执行 / 回滚"""

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

import emby  # noqa: E402
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


def touch(path, data=b"video"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


class NamerBracketTest(unittest.TestCase):
    def test_parenthesised_episode(self):
        p = namer.parse("1-【菠萝吹吹 合集】 (61).mp4", "其他")
        self.assertEqual(namer.build_name(p, ".mp4"), "菠萝吹吹 合集 S01E61.mp4")

    def test_episode_info_without_title(self):
        info = namer.episode_info("1-【菠萝吹吹 合集】 (61).mp4")
        self.assertEqual(info["episode"], 61)
        self.assertIsNone(namer.episode_info("花絮.mp4"))


class EmbyTest(unittest.TestCase):
    def _tree(self, tmp):
        touch(os.path.join(tmp, "第1集", "剧名 - 01.mp4"), b"one")
        touch(os.path.join(tmp, "第1集", "剧名 - 01.chs.ass"), b"sub")
        touch(os.path.join(tmp, "第2集", "剧名 - 02.mp4"), b"two")
        touch(os.path.join(tmp, "第3集", "剧名 - 04.mp4"), b"four")
        touch(os.path.join(tmp, "第3集", "note.txt"), b"keep")

    def payload(self, **kw):
        p = {"show": "测试剧", "year": "2023", "season": "1", "delete_empty": True}
        p.update(kw)
        return p

    def test_scan_parses_episodes(self):
        with workdir() as tmp:
            self._tree(tmp)
            res = emby.scan(tmp)
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["totals"]["videos"], 3)
            self.assertEqual([r["episode"] for r in res["rows"]], [1, 2, 4])

    def test_build_preview(self):
        with workdir() as tmp:
            self._tree(tmp)
            p = emby.build(tmp, self.payload())
            self.assertTrue(p["ok"], p)
            self.assertEqual(p["show_dir"], "测试剧 (2023)")
            self.assertEqual(p["season_dir"], "Season 01")
            names = [r["new_name"] for r in p["rows"]]
            self.assertEqual(names, ["测试剧 S01E01.mp4", "测试剧 S01E02.mp4", "测试剧 S01E04.mp4"])
            self.assertEqual(p["missing"], [3])

    def test_invalid_inputs(self):
        with workdir() as tmp:
            self._tree(tmp)
            for bad in ({"show": ""}, {"show": "a/b"}, {"year": "20"}, {"season": "0"}):
                p = emby.build(tmp, self.payload(**bad))
                self.assertFalse(p["ok"], f"应当拒绝：{bad}")

    def test_apply_moves_and_cleans(self):
        with workdir() as tmp:
            self._tree(tmp)
            res = emby.apply(tmp, self.payload())
            self.assertTrue(res["ok"], res)
            target = os.path.join(tmp, "测试剧 (2023)", "Season 01")
            self.assertTrue(os.path.isfile(os.path.join(target, "测试剧 S01E01.mp4")))
            self.assertTrue(os.path.isfile(os.path.join(target, "测试剧 S01E01.chs.ass")))
            self.assertTrue(os.path.isfile(os.path.join(target, "测试剧 S01E02.mp4")))
            self.assertTrue(os.path.isfile(os.path.join(target, "测试剧 S01E04.mp4")))
            # 第3集里有 note.txt，目录非空 → 不删
            self.assertTrue(os.path.isdir(os.path.join(tmp, "第3集")))
            self.assertFalse(os.path.isdir(os.path.join(tmp, "第1集")))

    def test_keep_nonempty_when_delete_empty_off(self):
        with workdir() as tmp:
            self._tree(tmp)
            res = emby.apply(tmp, self.payload(delete_empty=False))
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["removed_dirs"], [])
            self.assertTrue(os.path.isdir(os.path.join(tmp, "第1集")))

    def test_move_failure_rolls_back(self):
        with workdir() as tmp:
            self._tree(tmp)
            real = os.rename
            calls = {"n": 0}

            def flaky(src, dst, *a, **kw):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise OSError("模拟占用")
                return real(src, dst, *a, **kw)

            with mock.patch("emby.os.rename", side_effect=flaky):
                res = emby.apply(tmp, self.payload())

            self.assertFalse(res["ok"])
            self.assertTrue(res["rolled_back"])
            self.assertTrue(os.path.isfile(os.path.join(tmp, "第1集", "剧名 - 01.mp4")))
            self.assertFalse(os.path.isdir(os.path.join(tmp, "测试剧 (2023)")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
