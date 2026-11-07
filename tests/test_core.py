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
