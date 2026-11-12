# -*- coding: utf-8 -*-
"""tests/test_core.py — 打包与阅读核心的单元测试（只用标准库）

运行：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app        # noqa: E402
import packer     # noqa: E402
import reader     # noqa: E402

# 1x1 的最小 JPEG，够图片识别与 zip 读取用
JPG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAA"
    "AAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q==")

# 一个 1x1 PNG，用来验证混合扩展名的排序
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmM"
    "IQAAAABJRU5ErkJggg==")


@contextlib.contextmanager
def workdir():
    """测试用临时目录。

    不用 tempfile.TemporaryDirectory：它会以 0700 建目录，在 Windows 的受限
    令牌下后续读写会被 ACL 拒绝；这里用普通 makedirs，退出时尽力清理。
    """
    base = os.path.join(tempfile.gettempdir(), "comicpack-tests")
    path = os.path.join(base, uuid.uuid4().hex[:8])
    os.makedirs(path, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class PackerTest(unittest.TestCase):
    def test_natural_key(self):
        names = ["10.jpg", "2.jpg", "1.jpg", "第3话.jpg", "010.jpg"]
        self.assertEqual(
            sorted(names, key=packer.natural_key),
            ["1.jpg", "2.jpg", "10.jpg", "010.jpg", "第3话.jpg"],
        )

    def test_normalize_path(self):
        self.assertEqual(packer.normalize_path('  "C:\\\\comic\\\\a"  '), "C:\\comic\\a")
        self.assertEqual(packer.normalize_path(""), "")

    def test_arcnames_zero_pad(self):
        images = [{"name": "a.jpg", "ext": ".jpg", "size": 1, "mtime": 0},
                  {"name": "b.png", "ext": ".png", "size": 1, "mtime": 0}]
        self.assertEqual(packer.build_arcnames(images), ["001.jpg", "002.png"])
        self.assertEqual(packer.build_arcnames(images, keep_names=True), ["a.jpg", "b.png"])

    def test_scan_and_pack(self):
        with workdir() as tmp:
            src = os.path.join(tmp, "第01话")
            os.makedirs(src)
            for name, data in (("1.jpg", JPG), ("2.jpg", JPG), ("10.jpg", JPG),
                               ("3.jpg", JPG), ("notes.txt", b"skip me")):
                with open(os.path.join(src, name), "wb") as fh:
                    fh.write(data)

            info = packer.scan(src)
            self.assertTrue(info["ok"])
            self.assertEqual(info["count"], 4)
            self.assertEqual([im["name"] for im in info["images"]],
                             ["1.jpg", "2.jpg", "3.jpg", "10.jpg"])
            self.assertEqual(info["ignored"], 1)

            res = packer.pack(src, ["cbz", "zip"])
            self.assertTrue(res["ok"], res)
            self.assertEqual(len(res["produced"]), 2)
            cbz = res["produced"][0]["path"]
            self.assertTrue(os.path.isfile(cbz))
            with zipfile.ZipFile(cbz) as zf:
                self.assertIsNone(zf.testzip())
                self.assertEqual([i.filename for i in zf.infolist()],
                                 ["001.jpg", "002.jpg", "003.jpg", "004.jpg"])
                self.assertTrue(all(i.compress_type == zipfile.ZIP_STORED
                                    for i in zf.infolist()))

    def test_scan_tree_and_pack_many(self):
        with workdir() as tmp:
            for rel, pages in (("第01话", 2), ("第02话", 3), ("卷2/第03话", 1)):
                d = os.path.join(tmp, *rel.split("/"))
                os.makedirs(d)
                for i in range(pages):
                    with open(os.path.join(d, f"{i + 1}.jpg"), "wb") as fh:
                        fh.write(JPG)
            os.makedirs(os.path.join(tmp, "空文件夹"))

            tree = packer.scan_tree(tmp)
            self.assertTrue(tree["ok"], tree)
            self.assertEqual(tree["total"], 3)          # 空文件夹不算
            self.assertEqual(tree["images"], 6)
            self.assertEqual([f["name"] for f in tree["folders"]],
                             ["第01话", "第02话", "第03话"])

            res = packer.pack_many(tree["folders"], ["cbz"])
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["folders"], 3)
            self.assertEqual(res["ok_folders"], 3)
            self.assertEqual(len(res["produced"]), 3)
            for p in res["produced"]:
                self.assertTrue(os.path.isfile(p["path"]), p)
            # 每个包都在对应子文件夹的同级
            self.assertTrue(os.path.isfile(os.path.join(tmp, "第01话.cbz")))
            self.assertTrue(os.path.isfile(os.path.join(tmp, "卷2", "第03话.cbz")))

    def test_pack_many_delete_source(self):
        with workdir() as tmp:
            for rel in ("第01话", "第02话"):
                d = os.path.join(tmp, rel)
                os.makedirs(d)
                with open(os.path.join(d, "1.jpg"), "wb") as fh:
                    fh.write(JPG)
            tree = packer.scan_tree(tmp)
            res = packer.pack_many(tree["folders"], ["cbz"], delete_source=True)
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["deleted"], 2)
            self.assertFalse(os.path.exists(os.path.join(tmp, "第01话")))
            self.assertTrue(os.path.isfile(os.path.join(tmp, "第01话.cbz")))

    def test_parent_cover_is_packed(self):
        """图片在 第1话/，封面在上一级 系列/cover.jpg —— 封面要进包，且删原图时不能删它。"""
        with workdir() as tmp:
            series = os.path.join(tmp, "Series")
            chapter = os.path.join(series, "第1话")
            os.makedirs(chapter)
            for name in ("1.jpg", "2.jpg"):
                with open(os.path.join(chapter, name), "wb") as fh:
                    fh.write(JPG)
            with open(os.path.join(series, "cover.jpg"), "wb") as fh:
                fh.write(PNG)

            info = packer.scan(chapter)
            self.assertEqual(info["covers"], 1)
            self.assertEqual(info["images"][0]["name"], "cover.jpg")
            self.assertTrue(info["images"][0]["external"])

            res = packer.pack(chapter, ["cbz"], delete_source=True)
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["deleted"], 2)            # 只删章节里的两张
            self.assertTrue(os.path.isfile(os.path.join(series, "cover.jpg")))

            with zipfile.ZipFile(res["produced"][0]["path"]) as zf:
                names = [i.filename for i in zf.infolist()]
                first = zf.read(names[0])
            self.assertEqual(len(names), 3)                # 封面 + 2 页
            self.assertEqual(first, PNG)                   # 封面在包内第一张

    def test_recursive_pack_includes_parent_cover(self):
        with workdir() as tmp:
            for vol in ("第1话", "第2话"):
                d = os.path.join(tmp, "Series", vol)
                os.makedirs(d)
                with open(os.path.join(d, "1.jpg"), "wb") as fh:
                    fh.write(JPG)
            with open(os.path.join(tmp, "Series", "cover.jpg"), "wb") as fh:
                fh.write(PNG)

            tree = packer.scan_tree(os.path.join(tmp, "Series"))
            self.assertTrue(tree["ok"], tree)
            self.assertEqual(tree["total"], 2)

            res = packer.pack_many(tree["folders"], ["cbz"])
            self.assertTrue(res["ok"], res)
            self.assertEqual(len(res["produced"]), 2)
            for item in res["produced"]:
                with zipfile.ZipFile(item["path"]) as zf:
                    self.assertEqual(len(zf.infolist()), 2)   # 封面 + 1 页

    def test_cover_image_goes_first(self):
        with workdir() as tmp:
            src = os.path.join(tmp, "第07话")
            os.makedirs(src)
            for name in ("1.jpg", "2.jpg", "10.jpg"):
                with open(os.path.join(src, name), "wb") as fh:
                    fh.write(JPG)
            with open(os.path.join(src, "cover.png"), "wb") as fh:
                fh.write(PNG)

            info = packer.scan(src)
            self.assertEqual([im["name"] for im in info["images"]],
                             ["cover.png", "1.jpg", "2.jpg", "10.jpg"])
            self.assertEqual(info["covers"], 1)

            res = packer.pack(src, ["cbz"])
            self.assertTrue(res["ok"], res)
            with zipfile.ZipFile(res["produced"][0]["path"]) as zf:
                names = [i.filename for i in zf.infolist()]
                first = zf.read(names[0])
            self.assertEqual(names, ["001.png", "002.jpg", "003.jpg", "004.jpg"])
            self.assertEqual(first, PNG)             # 封面成了包内第一张

    def test_cover_deleted_with_sources(self):
        with workdir() as tmp:
            src = os.path.join(tmp, "第08话")
            os.makedirs(src)
            for name in ("1.jpg", "2.jpg"):
                with open(os.path.join(src, name), "wb") as fh:
                    fh.write(JPG)
            with open(os.path.join(src, "cover.jpg"), "wb") as fh:
                fh.write(JPG)

            res = packer.pack(src, ["cbz"], delete_source=True)
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["deleted"], 3)      # 封面也进包了，才会一起删
            self.assertTrue(res["removed_folder"])
            self.assertFalse(os.path.exists(src))
            with zipfile.ZipFile(res["produced"][0]["path"]) as zf:
                self.assertEqual(len(zf.infolist()), 3)

    def test_pack_delete_source(self):
        with workdir() as tmp:
            src = os.path.join(tmp, "第04话")
            os.makedirs(src)
            for name in ("1.jpg", "2.jpg", "10.jpg"):
                with open(os.path.join(src, name), "wb") as fh:
                    fh.write(JPG)

            res = packer.pack(src, ["cbz"], delete_source=True)
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["deleted"], 3)
            self.assertFalse(res["delete_errors"])
            self.assertTrue(res["removed_folder"])
            self.assertFalse(os.path.exists(src), "空文件夹应被一并删除")
            self.assertTrue(os.path.isfile(res["produced"][0]["path"]))

    def test_pack_delete_keeps_other_files(self):
        with workdir() as tmp:
            src = os.path.join(tmp, "第05话")
            os.makedirs(src)
            for name in ("1.jpg", "2.jpg"):
                with open(os.path.join(src, name), "wb") as fh:
                    fh.write(JPG)
            with open(os.path.join(src, "notes.txt"), "wb") as fh:
                fh.write(b"keep me")

            res = packer.pack(src, ["cbz"], delete_source=True)
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["deleted"], 2)
            self.assertFalse(res["removed_folder"])
            self.assertEqual(os.listdir(src), ["notes.txt"])

    def test_pack_without_delete_keeps_sources(self):
        with workdir() as tmp:
            src = os.path.join(tmp, "第06话")
            os.makedirs(src)
            with open(os.path.join(src, "1.jpg"), "wb") as fh:
                fh.write(JPG)
            res = packer.pack(src, ["cbz"])
            self.assertEqual(res["deleted"], 0)
            self.assertTrue(os.path.isfile(os.path.join(src, "1.jpg")))


class LibraryTest(unittest.TestCase):
    """封面墙扫描：递归找包、封面来源、封面字节。"""

    def _make_cbz(self, folder, name, payload=JPG):
        src = os.path.join(folder, "_tmp_" + name)
        os.makedirs(src, exist_ok=True)
        for page in ("1.jpg", "2.jpg"):
            with open(os.path.join(src, page), "wb") as fh:
                fh.write(payload)
        out = packer.pack(src, ["cbz"])["produced"][0]["path"]
        os.replace(out, os.path.join(folder, name))
        shutil.rmtree(src)
        return os.path.join(folder, name)

    def test_scan_recursive_with_covers(self):
        with workdir() as tmp:
            root_book = self._make_cbz(tmp, "第01话.cbz")
            vol = os.path.join(tmp, "卷2")
            os.makedirs(vol)
            nested_book = self._make_cbz(vol, "第02话.cbz", PNG)
            with open(os.path.join(vol, "第02话.jpg"), "wb") as fh:
                fh.write(JPG)

            res = reader.scan_library(tmp)
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["kind"], "shelf")
            self.assertEqual(res["count"], 2)

            by_name = {it["name"]: it for it in res["items"]}
            self.assertEqual(by_name["第01话.cbz"]["cover"], "zip")   # 包内首图
            self.assertEqual(by_name["第02话.cbz"]["cover"], "file")  # 同名封面
            self.assertTrue(by_name["第02话.cbz"]["dir"])             # 子文件夹
            self.assertEqual(by_name["第01话.cbz"]["dir"], "")

            # 新增测试里的封面文件不是 cbz，不该被当成书
            self.assertEqual([i["rel"] for i in res["items"]].__len__(), 2)
            self.assertEqual(reader.scan_library(root_book)["kind"], "book")

    def test_cover_bytes(self):
        with workdir() as tmp:
            book = self._make_cbz(tmp, "第01话.cbz")
            data, ctype = reader.cover_bytes(book)
            self.assertEqual(data, JPG)
            self.assertEqual(ctype, "image/jpeg")

            with open(os.path.join(tmp, "封面.png"), "wb") as fh:
                fh.write(PNG)
            data, ctype = reader.cover_bytes(book)
            self.assertEqual(data, PNG)
            self.assertEqual(ctype, "image/png")

            self.assertIsNone(reader.cover_bytes(os.path.join(tmp, "note.txt")))

    def test_same_cover_groups_chapters(self):
        """一个封面对应多本 cbz：封面墙只出现一张，count 就是话数。"""
        with workdir() as tmp:
            folder = os.path.join(tmp, "葬送的芙莉莲")
            os.makedirs(folder)
            for name in ("第01话.cbz", "第02话.cbz", "第10话.cbz"):
                self._make_cbz(folder, name)
            with open(os.path.join(folder, "cover.jpg"), "wb") as fh:
                fh.write(JPG)

            res = reader.scan_library(tmp)
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["count"], 1)          # 只出现一张封面
            self.assertEqual(res["chapters"], 3)

            group = res["items"][0]
            self.assertEqual(group["name"], "葬送的芙莉莲")
            self.assertEqual(group["count"], 3)
            self.assertEqual([c["name"] for c in group["chapters"]],
                             ["第01话.cbz", "第02话.cbz", "第10话.cbz"])
            self.assertTrue(group["path"].endswith("第01话.cbz"))   # 点开先读第一话

    def test_series_folder_with_cover_zip(self):
        """封面单独打成一个 zip 放在系列文件夹旁边：只出一张卡，点开读正片。"""
        with workdir() as tmp:
            series = os.path.join(tmp, "[Series] Title")
            os.makedirs(series)
            for name in ("第1话.zip", "第2话.zip"):
                with zipfile.ZipFile(os.path.join(series, name), "w") as zf:
                    for i in range(1, 4):
                        zf.writestr(f"{i:03d}.jpg", PNG)

            cover_zip = os.path.join(tmp, "[Series] Title.zip")
            with zipfile.ZipFile(cover_zip, "w") as zf:
                zf.writestr("cover.jpg", PNG)

            res = reader.scan_library(tmp)
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["count"], 1)                  # 封面包不单独成卡
            group = res["items"][0]
            self.assertEqual(group["name"], "[Series] Title")
            self.assertEqual(group["count"], 2)
            self.assertEqual(group["cover"], "zip")
            self.assertEqual(os.path.normcase(group["cover_path"]),
                             os.path.normcase(cover_zip))
            self.assertTrue(group["path"].endswith("第1话.zip"))

            # 点开应该是正片 3 页，而不是封面 1 页
            info = reader.open_path(group["path"])
            self.assertTrue(info["ok"], info)
            self.assertEqual(info["page_count"], 3)
            reader.close(info["id"])

            cover = reader.cover_bytes(group["cover_path"])
            self.assertIsNotNone(cover)

    def test_cover_zip_with_many_pages_stays_a_book(self):
        """同名 zip 里是一整本书时，不能当封面吃掉。"""
        with workdir() as tmp:
            series = os.path.join(tmp, "Series")
            os.makedirs(series)
            with zipfile.ZipFile(os.path.join(series, "第1话.zip"), "w") as zf:
                zf.writestr("001.jpg", PNG)
            with zipfile.ZipFile(os.path.join(tmp, "Series.zip"), "w") as zf:
                for i in range(5):
                    zf.writestr(f"{i:03d}.jpg", PNG)

            res = reader.scan_library(tmp)
            self.assertEqual(res["count"], 2)
            names = {it["name"] for it in res["items"]}
            self.assertIn("Series.zip", names)
            self.assertIn("第1话.zip", names)

    def test_empty_shelf(self):
        with workdir() as tmp:
            res = reader.scan_library(tmp)
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["count"], 0)


class ReaderTest(unittest.TestCase):
    def _make_cbz(self, folder: str) -> tuple:
        src = os.path.join(folder, "第02话")
        os.makedirs(src, exist_ok=True)
        for name, data in (("1.jpg", JPG), ("2.png", PNG), ("10.jpg", JPG)):
            with open(os.path.join(src, name), "wb") as fh:
                fh.write(data)
        res = packer.pack(src, ["cbz"])
        return res["produced"][0]["path"], src

    def test_open_archive(self):
        with workdir() as tmp:
            cbz, _src = self._make_cbz(tmp)
            info = reader.open_path(cbz)
            self.assertTrue(info["ok"], info)
            self.assertEqual(info["page_count"], 3)
            self.assertEqual([p["name"] for p in info["pages"]],
                             ["001.jpg", "002.png", "003.jpg"])
            data, ctype, _ = reader.page(info["id"], 1)
            self.assertEqual(data, PNG)
            self.assertEqual(ctype, "image/png")
            with self.assertRaises(reader.ReaderError):
                reader.page(info["id"], 99)
            reader.close(info["id"])
            with self.assertRaises(reader.ReaderError):
                reader.page(info["id"], 0)

    def test_open_folder(self):
        with workdir() as tmp:
            src = os.path.join(tmp, "单行本")
            os.makedirs(src)
            for name in ("1.jpg", "2.jpg"):
                with open(os.path.join(src, name), "wb") as fh:
                    fh.write(JPG)
            info = reader.open_path(src)
            self.assertTrue(info["ok"], info)
            self.assertEqual(info["kind"], "folder")
            self.assertEqual(info["page_count"], 2)
            data, ctype, _ = reader.page(info["id"], 0)
            self.assertEqual((data, ctype), (JPG, "image/jpeg"))
            reader.close(info["id"])

    def test_folder_with_archives_lists_them(self):
        """选一个装有多本 cbz 的文件夹：列成书架，文件夹和包都在列表里。"""
        with workdir() as tmp:
            for name in ("第01话", "第02话", "第10话"):
                src = os.path.join(tmp, name)
                os.makedirs(src)
                for fname in ("1.jpg", "2.jpg"):
                    with open(os.path.join(src, fname), "wb") as fh:
                        fh.write(JPG)
                packer.pack(src, ["cbz"])

            info = reader.open_path(tmp)
            self.assertTrue(info["ok"], info)
            self.assertEqual(info["kind"], "library")
            self.assertEqual(info["archive_count"], 3)
            self.assertEqual(info["folder_count"], 3)      # 打包后的源文件夹也在
            self.assertEqual([i["name"] for i in info["items"]],
                             ["第01话.cbz", "第02话.cbz", "第10话.cbz",
                              "第01话", "第02话", "第10话"])
            self.assertEqual([i["kind"] for i in info["items"]],
                             ["archive", "archive", "archive", "folder", "folder", "folder"])

    def test_chapters_sorted_and_indexed(self):
        with workdir() as tmp:
            for name in ("第1话", "第2话", "第10话"):
                src = os.path.join(tmp, name)
                os.makedirs(src)
                with open(os.path.join(src, "1.jpg"), "wb") as fh:
                    fh.write(JPG)
                packer.pack(src, ["cbz"])
                shutil.rmtree(src)

            # 直接打开中间那本，章节列表应仍是自然排序，且当前序号正确
            info = reader.open_path(os.path.join(tmp, "第2话.cbz"))
            self.assertTrue(info["ok"], info)
            self.assertEqual([c["name"] for c in info["chapters"]],
                             ["第1话.cbz", "第2话.cbz", "第10话.cbz"])
            self.assertEqual(info["chapter_index"], 1)
            reader.close(info["id"])

    def test_mixed_folder_lists_images_entry(self):
        """文件夹里同时有散图和 cbz：列表里既有包，也有“直接读本层图片”的入口。"""
        with workdir() as tmp:
            with open(os.path.join(tmp, "1.jpg"), "wb") as fh:
                fh.write(JPG)
            with open(os.path.join(tmp, "2.jpg"), "wb") as fh:
                fh.write(JPG)
            src = os.path.join(tmp, "书")
            os.makedirs(src)
            with open(os.path.join(src, "1.jpg"), "wb") as fh:
                fh.write(JPG)
            packer.pack(src, ["cbz"])
            shutil.rmtree(src)

            info = reader.open_path(tmp)
            self.assertTrue(info["ok"], info)
            self.assertEqual(info["kind"], "library")
            self.assertEqual(info["image_count"], 2)
            kinds = [i["kind"] for i in info["items"]]
            self.assertEqual(kinds, ["archive", "images"])
            self.assertEqual(info["items"][1]["count"], 2)

            # 走“直接读本层图片”入口
            pics = reader.open_path(tmp, prefer_images=True)
            self.assertTrue(pics["ok"], pics)
            self.assertEqual(pics["kind"], "folder")
            self.assertEqual(pics["page_count"], 2)
            reader.close(pics["id"])

    def test_folder_with_single_archive_opens_it(self):
        with workdir() as tmp:
            src = os.path.join(tmp, "第03话")
            os.makedirs(src)
            with open(os.path.join(src, "1.jpg"), "wb") as fh:
                fh.write(JPG)
            packer.pack(src, ["cbz"])
            shutil.rmtree(src)          # 只剩一个包

            info = reader.open_path(tmp)
            self.assertTrue(info["ok"], info)
            self.assertEqual(info["kind"], "zip")
            self.assertEqual(info["page_count"], 1)
            self.assertEqual(info["chapter_count"], 1)
            self.assertEqual(info["chapter_index"], 0)
            reader.close(info["id"])

    def test_folder_with_only_subfolders_is_library(self):
        with workdir() as tmp:
            for name in ("卷1", "卷2"):
                os.makedirs(os.path.join(tmp, name))
            info = reader.open_path(tmp)
            self.assertTrue(info["ok"], info)
            self.assertEqual(info["kind"], "library")
            self.assertEqual([i["kind"] for i in info["items"]], ["folder", "folder"])

    def test_empty_folder_reports_clearly(self):
        with workdir() as tmp:
            info = reader.open_path(tmp)
            self.assertFalse(info["ok"])
            self.assertIn("漫画包", info["error"])

    def test_many_sessions_no_deadlock(self):
        """超过会话上限要能继续打开（曾因重复拿锁而卡死第 9 个）。"""
        with workdir() as tmp:
            cbz, _src = self._make_cbz(tmp)
            result = {}

            def work():
                ids = []
                for _ in range(reader.MAX_SESSIONS + 3):
                    info = reader.open_path(cbz)
                    self.assertTrue(info["ok"], info)
                    ids.append(info["id"])
                result["ids"] = ids

            worker = threading.Thread(target=work, daemon=True)
            worker.start()
            worker.join(10)
            self.assertFalse(worker.is_alive(), "打开超过会话上限时死锁了")
            self.assertEqual(len(result["ids"]), reader.MAX_SESSIONS + 3)
            self.assertLessEqual(len(reader._sessions), reader.MAX_SESSIONS)

    def test_list_dir_shows_folders_and_cbz(self):
        with workdir() as tmp:
            os.makedirs(os.path.join(tmp, "卷2"))
            for name in ("第01话.cbz", "第02话.cbz"):
                with open(os.path.join(tmp, name), "wb") as fh:
                    fh.write(JPG)
            with open(os.path.join(tmp, "notes.txt"), "wb") as fh:
                fh.write(b"skip me")

            res = reader.list_dir(tmp)
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["dir_count"], 1)
            self.assertEqual(res["file_count"], 2)
            self.assertEqual([e["name"] for e in res["entries"]],
                             ["卷2", "第01话.cbz", "第02话.cbz"])
            self.assertEqual([e["kind"] for e in res["entries"]], ["dir", "file", "file"])

    def test_list_dir_roots_and_errors(self):
        roots = reader.list_dir("")
        self.assertTrue(roots["ok"])
        self.assertGreaterEqual(len(roots["roots"]), 1)      # 至少一个盘符

        missing = reader.list_dir(r"C:\this\path\does\not\exist")
        self.assertFalse(missing["ok"])
        self.assertIn("不存在", missing["error"])

    def test_list_dir_accepts_file_path(self):
        with workdir() as tmp:
            cbz = os.path.join(tmp, "第01话.cbz")
            with open(cbz, "wb") as fh:
                fh.write(JPG)
            res = reader.list_dir(cbz)      # 传文件时退到它所在目录
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["path"], tmp)
            self.assertEqual([e["name"] for e in res["entries"]], ["第01话.cbz"])

    def test_rejects_non_zip(self):
        with workdir() as tmp:
            bad = os.path.join(tmp, "fake.cbz")
            with open(bad, "wb") as fh:
                fh.write(b"not a zip at all")
            info = reader.open_path(bad)
            self.assertFalse(info["ok"])
            self.assertIn("CBZ", info["error"])

    def test_empty_archive(self):
        with workdir() as tmp:
            empty = os.path.join(tmp, "empty.cbz")
            with zipfile.ZipFile(empty, "w") as zf:
                zf.writestr("readme.txt", "no images")
            info = reader.open_path(empty)
            self.assertFalse(info["ok"])
            self.assertIn("没有图片", info["error"])


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = os.path.join(tempfile.gettempdir(), "comicpack-tests")
        cls.tmp = os.path.join(cls.base, uuid.uuid4().hex[:8])
        src = os.path.join(cls.tmp, "第03话")
        os.makedirs(src)
        for name in ("1.jpg", "2.jpg", "10.jpg"):
            with open(os.path.join(src, name), "wb") as fh:
                fh.write(JPG)
        cls.cbz = packer.pack(src, ["cbz"])["produced"][0]["path"]

        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.srv.daemon_threads = True
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        reader.close_all()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path: str):
        with urllib.request.urlopen(self.url(path), timeout=10) as res:
            return res.status, res.headers.get("Content-Type"), res.read()

    def post(self, path: str, payload: dict):
        req = urllib.request.Request(
            self.url(path), data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as res:
            return json.loads(res.read().decode("utf-8"))

    def test_health(self):
        status, ctype, body = self.get("/api/health")
        self.assertEqual(status, 200)
        self.assertIn("json", ctype)
        self.assertTrue(json.loads(body)["ok"])

    def test_list_endpoint(self):
        folder = os.path.join(self.tmp, "列表")
        os.makedirs(os.path.join(folder, "子目录"))
        with open(os.path.join(folder, "第04话.cbz"), "wb") as fh:
            fh.write(JPG)
        with open(os.path.join(folder, "ignore.txt"), "wb") as fh:
            fh.write(b"skip")

        res = self.post("/api/reader/list", {"path": folder})
        self.assertTrue(res["ok"], res)
        self.assertEqual([e["name"] for e in res["entries"]], ["子目录", "第04话.cbz"])
        self.assertEqual((res["dir_count"], res["file_count"]), (1, 1))

    def test_library_scan_and_cover(self):
        folder = os.path.join(self.tmp, "书架测试")
        src = os.path.join(folder, "src")
        os.makedirs(src)
        with open(os.path.join(src, "1.jpg"), "wb") as fh:
            fh.write(JPG)
        out = packer.pack(src, ["cbz"])["produced"][0]["path"]
        book = os.path.join(folder, "书.cbz")
        os.replace(out, book)
        shutil.rmtree(src)

        res = self.post("/api/library/scan", {"path": folder})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["kind"], "shelf")
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["items"][0]["name"], "书.cbz")

        status, ctype, data = self.get(
            "/api/library/cover?path=" + urllib.parse.quote(book))
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "image/jpeg")
        self.assertEqual(data, JPG)

    def test_recursive_scan_and_pack(self):
        root = os.path.join(self.tmp, "递归")
        for name in ("书A", "书B"):
            d = os.path.join(root, name)
            os.makedirs(d)
            for i in range(2):
                with open(os.path.join(d, f"{i + 1}.jpg"), "wb") as fh:
                    fh.write(JPG)

        scan = self.post("/api/scan", {"path": root, "recursive": True})
        self.assertTrue(scan["ok"], scan)
        self.assertEqual(scan["total"], 2)
        self.assertEqual(scan["images"], 4)

        job = self.post("/api/pack", {"path": root, "formats": ["cbz"], "recursive": True})
        snap = {}
        for _ in range(60):
            time.sleep(0.1)
            snap = json.loads(self.get(f"/api/job/{job['job']}")[2])
            if snap["job"]["status"] != "running":
                break

        self.assertEqual(snap["job"]["status"], "done", snap)
        res = snap["job"]["result"]
        self.assertTrue(res["recursive"])
        self.assertEqual(res["folders"], 2)
        self.assertEqual(len(res["produced"]), 2)
        self.assertTrue(os.path.isfile(os.path.join(root, "书A.cbz")))

    def test_emby_page_and_api(self):
        status, _ctype, body = self.get("/emby")
        self.assertEqual(status, 200)
        self.assertIn(b"emby.js", body)

        root = os.path.join(self.tmp, "剧集")
        chapter = os.path.join(root, "第1集")
        os.makedirs(chapter)
        with open(os.path.join(chapter, "剧名 - 01.mp4"), "wb") as fh:
            fh.write(b"video")

        scan = self.post("/api/emby/scan", {"path": root})
        self.assertTrue(scan["ok"], scan)
        self.assertEqual(scan["rows"][0]["episode"], 1)

        plan_payload = {"show": "测试剧", "year": "2023", "season": "1",
                        "delete_empty": True}
        build = self.post("/api/emby/build", {"path": root, "plan": plan_payload})
        self.assertTrue(build["ok"], build)
        self.assertEqual(build["rows"][0]["new_name"], "测试剧 S01E01.mp4")

        out = self.post("/api/emby/apply", {"path": root, "plan": plan_payload})
        self.assertTrue(out["ok"], out)
        self.assertTrue(os.path.isfile(os.path.join(
            root, "测试剧 (2023)", "Season 01", "测试剧 S01E01.mp4")))

    def test_organize_page_and_api(self):
        status, _ctype, body = self.get("/organize")
        self.assertEqual(status, 200)
        self.assertIn(b"organize.js", body)

        root = os.path.join(self.tmp, "整理")
        for rel in ("A", "B"):
            d = os.path.join(root, rel)
            os.makedirs(d)
            with open(os.path.join(d, "1.mp4"), "wb") as fh:
                fh.write(b"video")

        scan = self.post("/api/organize/scan", {"path": root})
        self.assertTrue(scan["ok"], scan)
        self.assertEqual(scan["totals"]["videos"], 2)

        plan_payload = {"folders": ["新"], "assignments": {}}
        plan = self.post("/api/organize/plan", {"path": root, "plan": plan_payload})
        self.assertTrue(plan["ok"], plan)
        self.assertTrue(plan["other"])

        out = self.post("/api/organize/apply", {"path": root, "plan": plan_payload})
        self.assertTrue(out["ok"], out)
        self.assertTrue(os.path.isdir(os.path.join(root, "其他")))
        self.assertFalse(os.path.exists(os.path.join(root, "A")))

    def test_rename_page_and_api(self):
        status, _ctype, body = self.get("/rename")
        self.assertEqual(status, 200)
        self.assertIn(b"rename.js", body)

        folder = os.path.join(self.tmp, "改名")
        os.makedirs(folder)
        with open(os.path.join(folder, "剧名 - 01 [1080p].mkv"), "wb") as fh:
            fh.write(b"video")

        res = self.post("/api/rename/scan", {"path": folder})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["items"][0]["new_name"], "剧名 S01E01.mkv")
        self.assertEqual(res["stats"]["ok"], 1)

        out = self.post("/api/rename/apply", {"path": folder})
        self.assertTrue(out["ok"], out)
        self.assertEqual([d["to"] for d in out["renamed"]], ["剧名 S01E01.mkv"])
        self.assertEqual(os.listdir(folder), ["剧名 S01E01.mkv"])

    def test_pages_served(self):
        status, ctype, body = self.get("/reader")
        self.assertEqual(status, 200)
        self.assertIn(b"ComicPack", body)
        self.assertIn(b"reader.js", body)

        info = self.post("/api/reader/open", {"path": self.cbz})
        self.assertTrue(info["ok"], info)
        self.assertEqual(info["page_count"], 3)

        status, ctype, data = self.get(f"/api/reader/page/{info['id']}/0")
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "image/jpeg")
        self.assertEqual(data, JPG)

        # 越界页码 -> 404 JSON
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get(f"/api/reader/page/{info['id']}/99")
        self.assertEqual(ctx.exception.code, 404)

        self.assertTrue(self.post(f"/api/reader/close/{info['id']}", {})["ok"])

    def test_pack_with_delete_source(self):
        src = os.path.join(self.tmp, "第09话")
        os.makedirs(src)
        for name in ("1.jpg", "2.jpg"):
            with open(os.path.join(src, name), "wb") as fh:
                fh.write(JPG)

        job = self.post("/api/pack", {"path": src, "formats": ["cbz"],
                                      "delete_source": True})
        snap = {}
        for _ in range(60):
            time.sleep(0.1)
            snap = json.loads(self.get(f"/api/job/{job['job']}")[2])
            if snap["job"]["status"] != "running":
                break

        self.assertEqual(snap["job"]["status"], "done", snap)
        self.assertEqual(snap["job"]["result"]["deleted"], 2)
        self.assertTrue(snap["job"]["result"]["removed_folder"])
        self.assertFalse(os.path.exists(src))
        self.assertTrue(os.path.isfile(snap["job"]["result"]["produced"][0]["path"]))

    def test_folder_chapters_over_http(self):
        lib = os.path.join(self.tmp, "书架")
        os.makedirs(lib)
        for name in ("第01话", "第02话"):
            src = os.path.join(lib, name)
            os.makedirs(src)
            with open(os.path.join(src, "1.jpg"), "wb") as fh:
                fh.write(JPG)
            packer.pack(src, ["cbz"])
            shutil.rmtree(src)

        # 选这个文件夹：列出所有包（不再自动打开第一本）
        res = self.post("/api/reader/open", {"path": lib})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["kind"], "library")
        self.assertEqual([i["name"] for i in res["items"]],
                         ["第01话.cbz", "第02话.cbz"])

        # 点开其中一本：返回同目录章节，序号用于“下一话”
        first = self.post("/api/reader/open", {"path": res["items"][0]["path"]})
        self.assertEqual(first["kind"], "zip")
        self.assertEqual(first["chapter_count"], 2)
        self.assertEqual(first["chapter_index"], 0)

        nxt = self.post("/api/reader/open", {"path": res["items"][1]["path"]})
        self.assertEqual(nxt["chapter_index"], 1)
        self.assertEqual(nxt["page_count"], 1)
        reader.close(nxt["id"])

    def test_subfolder_library_over_http(self):
        lib = os.path.join(self.tmp, "书库")
        os.makedirs(lib)
        os.makedirs(os.path.join(lib, "卷1"))
        res = self.post("/api/reader/open", {"path": lib})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["kind"], "library")
        self.assertEqual([i["name"] for i in res["items"]], ["卷1"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
