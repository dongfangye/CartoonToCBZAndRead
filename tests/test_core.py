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


if __name__ == "__main__":
    unittest.main(verbosity=2)
