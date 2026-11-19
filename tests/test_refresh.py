"""Runs scripts/refresh.sh for real, with stand-ins for AltServer and the libimobiledevice tools."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from sideloop import accounts, config, store

from .support import ROOT, UDID, Fresh

STUBS = ROOT / "tests" / "stubs"


class Refresh(Fresh):
    pki = None

    @classmethod
    def setUpClass(cls):
        cls._pki = tempfile.TemporaryDirectory()
        cls.pki = cls._pki.name
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=t",
                        "-keyout", f"{cls.pki}/key.pem", "-out", f"{cls.pki}/cert.pem"], check=True,
                       capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls._pki.cleanup()

    def setUp(self):
        super().setUp()
        self.stub = Path(tempfile.mkdtemp(prefix="stub-"))
        (self.stub / "behaviour").mkdir()
        self.phone = self.device(UDID, "iPhone")
        self.a = accounts.save("a@example.com", "pw-a")
        self.b = accounts.save("b@example.com", "pw-b")
        for aid in (self.a, self.b):
            (Path(config.ACCOUNTS) / aid / "verified").touch()
        self.log = Path(config.STATE) / "refresh.log"

    def behave(self, email, what):
        (self.stub / "behaviour" / email).write_text(what)

    def calls(self):
        try:
            return [c.split("|") for c in (self.stub / "calls").read_text().splitlines()]
        except FileNotFoundError:
            return []

    def run_refresh(self, app, *flags, tty=False):
        meta = store.app_meta(app)
        (self.stub / "installed").unlink(missing_ok=True)
        # what is installed on the (fake) device: whatever this app already was, signed by its account
        pin = accounts.pin_of(app, self.phone)
        if pin:
            (self.stub / "installed").write_text(accounts.meta(pin)["APPLE_ID"])
        env = {**os.environ, "PATH": f"{STUBS}:{os.environ['PATH']}", "PYTHONPATH": str(ROOT),
               "DATA_DIR": config.DATA.as_posix(), "ALTSERVER_BIN": str(STUBS / "AltServer"),
               "STUB_DIR": str(self.stub), "STUB_UDID": UDID, "STUB_BUNDLE": meta["BUNDLE_ID"],
               "STUB_PKI": self.pki}
        cmd = ["bash", str(ROOT / "scripts" / "refresh.sh"), "--app", app, "--device", UDID, *flags]
        if tty:
            cmd = ["script", "-qec", " ".join(cmd), "/dev/null"]
        return subprocess.run(cmd, env=env, capture_output=True, text=True, stdin=None if tty else subprocess.DEVNULL,
                              timeout=120)

    def lines(self):
        return self.log.read_text() if self.log.exists() else ""

    # ---------------------------------------------------------------------------------------------------------

    def test_new_app_is_signed_by_the_first_account_in_its_own_folders(self):
        app = self.app()
        r = self.run_refresh(app, "--force")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        (email, cwd, home), = self.calls()
        self.assertEqual(email, "a@example.com")
        self.assertEqual(cwd, str(Path(config.ACCOUNTS) / self.a / "work"))
        self.assertEqual(home, str(Path(config.DATA) / ".altserver"))  # shared: one anisette identity per install
        self.assertEqual(accounts.pin_of(app, self.phone), self.a)
        self.assertEqual(accounts.app_ids_this_week(self.a), 1)
        self.assertIn("signing with Apple ID a@example.com", self.lines())
        self.assertTrue(store.read_state(Path(config.APPS) / app / "state" / self.phone)["expiry"])

    def test_the_old_certificate_cache_folder_is_used_for_the_migrated_account(self):
        shutil_ids = accounts.ids()
        for aid in shutil_ids:
            accounts.remove(aid)
        config.update({"APPLE_ID": "old@example.com", "APPLE_PASSWORD": "oldpw"})
        (Path(config.DATA) / "AltServerData").mkdir()  # it has signed in here before
        accounts.migrate()
        (aid,) = accounts.ids()
        app = self.app()
        self.assertEqual(self.run_refresh(app, "--force").returncode, 0)
        (email, cwd, home), = self.calls()
        self.assertEqual((email, cwd, home), ("old@example.com", str(config.DATA), str(Path(config.DATA) / ".altserver")))

    def test_an_install_from_before_the_update_is_resigned_in_the_background_with_the_old_apple_id(self):
        """The upgrade path: one Apple ID in config.env, an app already installed, no accounts folder yet."""
        import shutil
        shutil.rmtree(config.ACCOUNTS)
        config.update({"APPLE_ID": "old@example.com", "APPLE_PASSWORD": "oldpw"})
        (Path(config.DATA) / "AltServerData").mkdir()
        app = self.app()
        state = Path(config.APPS) / app / "state" / self.phone
        state.mkdir(parents=True)
        import time
        (state / "expiry").write_text(f"{int(time.time()) + 3600}\n")  # an hour left: due
        accounts.migrate()
        self.assertEqual(self.run_refresh(app).returncode, 0)  # no --force, the way the watcher runs it
        (email, cwd, home), = self.calls()
        self.assertEqual((email, cwd), ("old@example.com", str(config.DATA)))
        self.assertGreater(int((state / "expiry").read_text()), time.time() + 6 * 86400)

    def test_when_the_first_account_is_out_of_signings_the_second_one_takes_the_app(self):
        app = self.app()
        self.behave("a@example.com", "limit")
        r = self.run_refresh(app, "--force")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([c[0] for c in self.calls()], ["a@example.com", "b@example.com"])
        self.assertEqual(accounts.pin_of(app, self.phone), self.b)
        self.assertTrue(accounts.is_exhausted(self.a))
        self.assertIn("trying the next Apple ID", self.lines())

    def test_an_exhausted_account_is_tried_last_next_time(self):
        self.behave("a@example.com", "limit")
        self.assertEqual(self.run_refresh(self.app("com.one", "One"), "--force").returncode, 0)
        (self.stub / "calls").unlink()
        self.assertEqual(self.run_refresh(self.app("com.two", "Two"), "--force").returncode, 0)
        self.assertEqual([c[0] for c in self.calls()], ["b@example.com"])

    def test_every_account_out_of_signings_fails_with_a_clear_message(self):
        app = self.app()
        self.behave("a@example.com", "limit")
        self.behave("b@example.com", "limit")
        r = self.run_refresh(app, "--force")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual([c[0] for c in self.calls()], ["a@example.com", "b@example.com"])
        self.assertIsNone(accounts.pin_of(app, self.phone))
        self.assertIn("every available Apple ID is out of signings", self.lines())

    def test_an_installed_app_is_always_resigned_with_its_own_account(self):
        app = self.app()
        accounts.record(app, self.phone, self.b)
        r = self.run_refresh(app, "--force")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([c[0] for c in self.calls()], ["b@example.com"])
        self.assertEqual(accounts.pin_of(app, self.phone), self.b)
        self.assertEqual(accounts.app_ids_this_week(self.b), 1)  # a re-sign registers nothing new

    def test_no_fallback_for_an_installed_app_even_when_its_account_hits_a_limit(self):
        app = self.app()
        accounts.record(app, self.phone, self.b)
        self.behave("b@example.com", "limit")
        r = self.run_refresh(app, "--force")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual([c[0] for c in self.calls()], ["b@example.com"])
        self.assertEqual(accounts.pin_of(app, self.phone), self.b)

    def test_a_2fa_request_does_not_burn_through_the_other_accounts(self):
        app = self.app()
        self.behave("a@example.com", "2fa")
        r = self.run_refresh(app, "--force")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual([c[0] for c in self.calls()], ["a@example.com"])
        self.assertIn("2FA", self.lines())

    def test_background_runs_never_use_an_account_that_has_not_signed_in_before(self):
        (Path(config.ACCOUNTS) / self.a / "verified").unlink()
        (Path(config.ACCOUNTS) / self.b / "verified").unlink()
        app = self.app()
        r = self.run_refresh(app, "--force")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.calls(), [])
        self.assertIn("no Apple ID can sign this app", self.lines())

    def test_a_run_from_the_web_page_may_sign_an_account_in_for_the_first_time(self):
        (Path(config.ACCOUNTS) / self.a / "verified").unlink()
        app = self.app()
        r = self.run_refresh(app, "--force", tty=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([c[0] for c in self.calls()], ["a@example.com"])
        self.assertTrue((Path(config.ACCOUNTS) / self.a / "verified").exists())

    def test_recheck_works_without_a_global_apple_id(self):
        app = self.app()
        self.assertEqual(self.run_refresh(app, "--force").returncode, 0)
        r = self.run_refresh(app, "--recheck")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("rechecked on the device", self.lines())

    def test_switching_account_is_called_out_in_the_log(self):
        app = self.app()
        self.assertEqual(self.run_refresh(app, "--force").returncode, 0)
        self.assertIn("NOTE signed with a@example.com", self.lines())


if __name__ == "__main__":
    unittest.main()
