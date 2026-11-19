import hashlib
import http.server
import threading
import unittest
from pathlib import Path

from resigner import config, download
from resigner.jobs import Jobs
from resigner.server import App

from .support import Fresh, make_ipa
from .test_server import FakeHealth


class Site(http.server.BaseHTTPRequestHandler):
    files = {}

    def do_GET(self):
        if self.path == "/jump":
            self.send_response(302)
            self.send_header("Location", "file:///etc/passwd")
            self.end_headers()
        elif self.path == "/hop":
            self.send_response(302)
            self.send_header("Location", "/Cool%20App.ipa")
            self.end_headers()
        elif self.path.split("?")[0] in self.files:
            body = self.files[self.path.split("?")[0]]
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


class Base(Fresh):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        super().setUp()
        make_ipa(config.DATA / "good.ipa", bundle="com.example.tv", name="Tv App", platform="tvos")
        self.good = (config.DATA / "good.ipa").read_bytes()
        Site.files = {"/Cool%20App.ipa": self.good, "/plain": b"not a zip file", "/empty": b""}
        self.dest = config.DATA / "pending.ipa"



class Download(Base):
    def test_downloads_the_file_and_names_it_from_the_link(self):
        name, digest = download.fetch(f"{self.base}/Cool%20App.ipa?x=1", self.dest, 1 << 30)
        self.assertEqual(name, "Cool App.ipa")
        self.assertEqual(digest, hashlib.sha256(self.good).hexdigest())
        self.assertEqual(self.dest.read_bytes(), self.good)
        self.assertEqual(list(config.DATA.glob("*.download")), [])

    def test_follows_a_redirect(self):
        self.assertEqual(download.fetch(f"{self.base}/hop", self.dest, 1 << 30)[0], "app.ipa")
        self.assertEqual(self.dest.read_bytes(), self.good)

    def test_a_matching_checksum_is_accepted_in_any_case(self):
        want = hashlib.sha256(self.good).hexdigest().upper()
        download.fetch(f"{self.base}/Cool%20App.ipa", self.dest, 1 << 30, f" {want} ")
        self.assertTrue(self.dest.exists())

    def test_a_wrong_checksum_leaves_nothing_behind(self):
        with self.assertRaisesRegex(ValueError, "doesn't match"):
            download.fetch(f"{self.base}/Cool%20App.ipa", self.dest, 1 << 30, "0" * 64)
        self.assertFalse(self.dest.exists())
        self.assertEqual(list(config.DATA.glob("*.download")), [])

    def test_a_replaced_pending_file_survives_a_failed_download(self):
        self.dest.write_bytes(b"earlier upload")
        with self.assertRaises(ValueError):
            download.fetch(f"{self.base}/missing.ipa", self.dest, 1 << 30)
        self.assertEqual(self.dest.read_bytes(), b"earlier upload")

    def test_only_web_links_are_accepted(self):
        for bad in ("", "ftp://example.com/a.ipa", "file:///etc/passwd", "example.com/a.ipa", "https://"):
            with self.assertRaisesRegex(ValueError, "web link"):
                download.fetch(bad, self.dest, 1 << 30)

    def test_a_redirect_to_a_local_file_is_refused(self):
        with self.assertRaisesRegex(ValueError, "web address"):
            download.fetch(f"{self.base}/jump", self.dest, 1 << 30)
        self.assertFalse(self.dest.exists())

    def test_server_errors_and_empty_files_are_reported(self):
        with self.assertRaisesRegex(ValueError, "answered 404"):
            download.fetch(f"{self.base}/missing.ipa", self.dest, 1 << 30)
        with self.assertRaisesRegex(ValueError, "empty"):
            download.fetch(f"{self.base}/empty", self.dest, 1 << 30)

    def test_a_file_over_the_limit_is_refused(self):
        with self.assertRaisesRegex(ValueError, "too large"):
            download.fetch(f"{self.base}/Cool%20App.ipa", self.dest, 100)
        self.assertFalse(self.dest.exists())

    def test_an_unreachable_host_is_a_message_not_a_crash(self):
        with self.assertRaisesRegex(ValueError, "Couldn't reach"):
            download.fetch("http://127.0.0.1:1/a.ipa", self.dest, 1 << 30, timeout=2)

    def test_a_malformed_checksum_is_refused_before_downloading(self):
        with self.assertRaisesRegex(ValueError, "checksum"):
            download.fetch(f"{self.base}/Cool%20App.ipa", self.dest, 1 << 30, "abc")


class FetchRoute(Base):
    def setUp(self):
        super().setUp()
        self.routes = App(Jobs(on_done=lambda: None), FakeHealth(), None).routes()
        # the route keeps its pending upload under the data folder, like an upload does
        self.pending = config.PENDING_IPA

    def test_the_route_returns_what_an_upload_returns_and_the_app_can_then_be_added(self):
        r = self.routes["/api/ipa/fetch"]({"url": f"{self.base}/Cool%20App.ipa"})
        self.assertEqual(r["info"]["bundle_id"], "com.example.tv")
        self.assertEqual(r["info"]["platform"], "tvos")
        self.assertEqual(r["filename"], "Cool App.ipa")
        self.assertEqual(r["sha256"], hashlib.sha256(self.good).hexdigest())
        app_id = self.routes["/api/ipa/use"]({"filename": r["filename"]})["id"]
        self.assertEqual(app_id, "com.example.tv")

    def test_something_that_is_not_an_ipa_is_discarded(self):
        with self.assertRaisesRegex(ValueError, "not an IPA"):
            self.routes["/api/ipa/fetch"]({"url": f"{self.base}/plain"})
        self.assertFalse(self.pending.exists())

    def test_the_checksum_can_be_checked_by_the_route(self):
        with self.assertRaisesRegex(ValueError, "doesn't match"):
            self.routes["/api/ipa/fetch"]({"url": f"{self.base}/Cool%20App.ipa", "sha256": "1" * 64})


if __name__ == "__main__":
    unittest.main()
