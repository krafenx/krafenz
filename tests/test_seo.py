"""Проверки SEO-маршрутов и метаданных без доступа к рабочей базе.

Запуск: python -m unittest discover -s tests -p 'test_seo.py'
Требуются зависимости сервера и httpx для FastAPI TestClient.
"""
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

# Импорт сервера инициализирует БД: всегда используем временный каталог.
_data_dir = tempfile.TemporaryDirectory()
os.environ["DATA_DIR"] = _data_dir.name
os.environ["ENV_SYNC"] = "0"
os.environ["AUTO_UPDATE"] = "0"

from fastapi.testclient import TestClient
import server


class Metadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.canonicals = []
        self.meta = {}
        self.titles = []
        self.h1_count = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "link" and "canonical" in attrs.get("rel", "").lower().split():
            self.canonicals.append(attrs.get("href"))
        if tag == "meta":
            key = attrs.get("name") or attrs.get("property")
            if key:
                self.meta[key] = attrs.get("content", "")
        if tag == "title":
            self._in_title = True
        if tag == "h1":
            self.h1_count += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.titles.append(data)


class SeoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(server.app, base_url="https://krafen.me", follow_redirects=False)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def test_public_page_redirects_preserve_query_for_get_and_head(self):
        redirects = {"/index.html": "/"}
        for page in ("watchlist", "gamelist", "setup", "wishlist", "danonsha"):
            redirects[f"/{page}/"] = f"/{page}"
            redirects[f"/{page}.html"] = f"/{page}"
        for original, target in redirects.items():
            for method in ("GET", "HEAD"):
                with self.subTest(original=original, method=method):
                    response = self.client.request(method, original + "?a=1&b=x%2Fy")
                    self.assertEqual(response.status_code, 301)
                    self.assertEqual(response.headers["location"], target + "?a=1&b=x%2Fy")
                    self.assertEqual(self.client.get(target).status_code, 200)

    def test_sitemap_pages_have_consistent_metadata(self):
        sitemap = self.client.get("/sitemap.xml")
        self.assertEqual(sitemap.status_code, 200)
        root = ET.fromstring(sitemap.content)
        urls = [element.text for element in root.findall("{*}url/{*}loc")]
        self.assertEqual(len(urls), 6)
        titles = set()
        descriptions = set()
        for url in urls:
            with self.subTest(url=url):
                parsed = urlparse(url)
                self.assertEqual((parsed.scheme, parsed.netloc), ("https", "krafen.me"))
                response = self.client.get(parsed.path)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("noindex", response.headers.get("x-robots-tag", "").lower())
                meta = Metadata()
                meta.feed(response.text)
                self.assertEqual(meta.canonicals, [url])
                self.assertEqual(meta.meta.get("og:url"), url)
                self.assertTrue(meta.meta.get("og:title"))
                self.assertTrue(meta.meta.get("og:description"))
                self.assertEqual(meta.meta.get("og:image"), "https://krafen.me/favicon.png")
                self.assertEqual(meta.h1_count, 1)
                title = "".join(meta.titles)
                description = meta.meta.get("description", "")
                self.assertTrue(30 <= len(title) <= 60)
                self.assertTrue(70 <= len(description) <= 160)
                titles.add(title)
                descriptions.add(description)
        self.assertEqual(len(titles), len(urls))
        self.assertEqual(len(descriptions), len(urls))

    def test_noindex_pages_and_public_data(self):
        robots = self.client.get("/robots.txt").text
        self.assertIn("Disallow: /api/", robots)
        self.assertNotIn("Disallow: /tasks", robots)
        for name in ("watchlist", "gamelist", "setup", "wishlist"):
            with self.subTest(name=name):
                self.assertIn(f"Allow: /api/{name}$", robots)
                response = self.client.get(f"/api/{name}")
                self.assertEqual(response.status_code, 200)
                self.assertIn("noindex", response.headers.get("x-robots-tag", ""))
        for path in ("/tasks", "/admin", "/danonsha/watchlist"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn("noindex", response.headers.get("x-robots-tag", ""))
        self.assertEqual(self.client.get("/this-page-does-not-exist").status_code, 404)
        self.assertEqual(self.client.get("/style.css").status_code, 200)
        self.assertEqual(self.client.get("/app.js").status_code, 200)
        self.assertEqual(self.client.get("/favicon.png").status_code, 200)


if __name__ == "__main__":
    unittest.main()
