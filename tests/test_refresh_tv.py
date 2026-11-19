"""Runs scripts/refresh.sh and scripts/refresh-tv.sh for real, with stand-ins for plumesign and AltServer."""
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from resigner import accounts, config, store

from .support import ROOT, TV_UDID, UDID, Fresh

STUBS = ROOT / "tests" / "stubs"
ADDRESS = "192.168.1.50 49152"


class RefreshTV(Fresh):
    def setUp(self):
        super().setUp()
        self.stub = Path(tempfile.mkdtemp(prefix="stub-"))
        (self.stub / "behaviour").mkdir()
        self.tv_udid = self.tv()
        self.a = accounts.save("a@example.com", "pw-a")
        self.b = accounts.save("b@example.com", "pw-b")
        for aid in (self.a, self.b):
            accounts.verified_file(aid, "appletv").touch()
        (self.stub / "sessions").write_text("a@example.com\nb@example.com\n")
        self.log = Path(config.STATE) / "refresh.log"

    def behave(self, email, what):
        (self.stub / "behaviour" / email).write_text(what)

    def signed_out(self):
        (self.stub / "sessions").write_text("")

    def calls(self):
        try:
            return [c.split("|") for c in (self.stub / "calls").read_text().splitlines()]
        except FileNotFoundError:
            return []

    def run_script(self, app, *flags, device=TV_UDID, script="refresh.sh", tty=False, stdin=None, servers=None,
                   locate=ADDRESS, tries=1):
        env = {**os.environ, "PATH": f"{STUBS}:{os.environ['PATH']}", "PYTHONPATH": str(ROOT),
               "DATA_DIR": config.DATA.as_posix(), "ALTSERVER_BIN": str(STUBS / "AltServer"),
               "STUB_DIR": str(self.stub), "STUB_UDID": UDID, "STUB_BUNDLE": "x", "STUB_PKI": "/nonexistent",
               "TV_LOCATE_TRIES": str(tries)}
        env.pop("XDG_CONFIG_HOME", None)
        if locate:
            env["RESIGNER_TV_LOCATE"] = locate
        else:
            env.pop("RESIGNER_TV_LOCATE", None)
        if servers:
            env["ANISETTE_SERVER"] = servers
        cmd = ["bash", str(ROOT / "scripts" / script), *(["--app", app] if app else []),
               *(["--device", device] if device else []), *flags]
        if tty:
            cmd = ["script", "-qec", " ".join(cmd), "/dev/null"]
        return subprocess.run(cmd, env=env, capture_output=True, text=True, input=stdin,
                              stdin=None if (tty or stdin is not None) else subprocess.DEVNULL, timeout=120)

    def lines(self):
        return self.log.read_text() if self.log.exists() else ""

    def state(self, app):
        return Path(config.APPS) / app / "state" / self.tv_udid

    # ---------------------------------------------------------------------------------------------------------

    def test_a_tvos_app_is_signed_and_installed_on_the_apple_tv(self):
        app = self.app("com.example.tv", "TVApp", platform="tvos")
        r = self.run_script(app, "--force")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        (kind, email, ip, port, udid, server, xdg), = self.calls()
        self.assertEqual((kind, email, ip, port, udid), ("sign", "a@example.com", "192.168.1.50", "49152", TV_UDID))
        self.assertEqual(xdg, str(Path(config.DATA) / "xdg"))
        self.assertEqual(accounts.pin_of(app, self.tv_udid), self.a)
        self.assertGreater(store.read_state(self.state(app))["expiry"], time.time() + 6 * 86400)
        self.assertTrue((self.state(app) / "certificate").exists())
        self.assertIn("signing with Apple ID a@example.com", self.lines())
        self.assertIn("OK refreshed", self.lines())
        self.assertEqual(accounts.app_ids_this_week(self.a), 1)

    def test_a_signature_with_plenty_of_time_left_is_left_alone(self):
        app = self.app("com.example.tv", "TVApp", platform="tvos")
        self.state(app).mkdir(parents=True)
        (self.state(app) / "expiry").write_text(f"{int(time.time()) + 5 * 86400}\n")
        self.assertEqual(self.run_script(app).returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_an_expiring_signature_is_renewed_in_the_background(self):
        app = self.app("com.example.tv", "TVApp", platform="tvos")
        self.state(app).mkdir(parents=True)
        (self.state(app) / "expiry").write_text(f"{int(time.time()) + 3600}\n")
        (self.state(app) / "account").write_text(self.a + "\n")
        self.assertEqual(self.run_script(app).returncode, 0)
        self.assertEqual([c[0] for c in self.calls()], ["sign"])
        self.assertGreater(store.read_state(self.state(app))["expiry"], time.time() + 6 * 86400)

    def test_an_apple_tv_that_isnt_on_the_network_is_retried_later(self):
        app = self.app("com.example.tv", "TVApp", platform="tvos")
        r = self.run_script(app, "--force", locate=None)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.calls(), [])
        self.assertIn("not reachable", self.lines())
        self.assertEqual(store.read_state(self.state(app))["last_run"]["result"], "offline")

    def test_the_first_sign_in_for_an_apple_tv_takes_the_2fa_code_on_the_page(self):
        accounts.verified_file(self.a, "appletv").unlink()
        self.behave("a@example.com", "2fa")
        (self.stub / "sessions").write_text("")
        app = self.app("com.example.tv", "TVApp", platform="tvos")
        r = self.run_script(app, "--force", tty=True, stdin="123456\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([c[0] for c in self.calls()], ["login", "sign"])
        self.assertTrue(accounts.verified_file(self.a, "appletv").exists())
        self.assertEqual(accounts.pin_of(app, self.tv_udid), self.a)

    def test_a_wrong_2fa_code_stops_the_run(self):
        accounts.verified_file(self.a, "appletv").unlink()
        self.behave("a@example.com", "2fa")
        (self.stub / "sessions").write_text("")
        app = self.app("com.example.tv", "TVApp", platform="tvos")
        r = self.run_script(app, "--force", tty=True, stdin="000000\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual([c[0] for c in self.calls()], ["login"])
        self.assertFalse(accounts.verified_file(self.a, "appletv").exists())

    def test_a_background_run_skips_an_apple_id_that_never_signed_in_for_an_apple_tv(self):
        accounts.verified_file(self.a, "appletv").unlink()
        app = self.app("com.example.tv", "TVApp", platform="tvos")
        self.assertEqual(self.run_script(app, "--force").returncode, 0)
        self.assertEqual([c[1] for c in self.calls()], ["b@example.com"])  # b has signed in, a hasn't

    def test_a_lost_sign_in_is_redone_in_the_background(self):
        self.signed_out()
        app = self.app("com.example.tv", "TVApp", platform="tvos")
        r = self.run_script(app, "--force")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([c[0] for c in self.calls()], ["sign", "login", "sign"])
        self.assertIn("signing in again", self.lines())

    def test_a_lost_sign_in_that_needs_a_code_is_reported(self):
        self.signed_out()
        self.behave("a@example.com", "2fa")
        app = self.app("com.example.tv", "TVApp", platform="tvos")
        r = self.run_script(app, "--force")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("2FA code", self.lines())
        self.assertFalse((self.state(app) / "expiry").exists())

    def test_when_the_first_account_is_out_of_signings_the_second_takes_the_app(self):
        self.behave("a@example.com", "limit")
        app = self.app("com.example.tv", "TVApp", platform="tvos")
        r = self.run_script(app, "--force")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([c[1] for c in self.calls()], ["a@example.com", "b@example.com"])
        self.assertEqual(accounts.pin_of(app, self.tv_udid), self.b)
        self.assertTrue(accounts.is_exhausted(self.a))
        self.assertIn("trying the next Apple ID", self.lines())

    def test_a_failing_anisette_server_is_set_aside_and_the_next_one_used(self):
        (self.stub / "badservers").write_text("https://bad.example\n")
        app = self.app("com.example.tv", "TVApp", platform="tvos")
        r = self.run_script(app, "--force", servers="https://bad.example,https://good.example")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([c[5] for c in self.calls()], ["https://bad.example", "https://good.example"])
        self.assertIn("Anisette server https://bad.example failed", self.lines())

    def test_when_every_anisette_server_fails_the_hint_says_so(self):
        (self.stub / "badservers").write_text("https://bad.example\nhttps://worse.example\n")
        app = self.app("com.example.tv", "TVApp", platform="tvos")
        r = self.run_script(app, "--force", servers="https://bad.example,https://worse.example")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("sign-in helper (Anisette server) failed on every server", self.lines())

    def test_a_plain_failure_counts_up_and_is_recorded(self):
        self.behave("a@example.com", "fail")
        app = self.app("com.example.tv", "TVApp", platform="tvos")
        self.assertNotEqual(self.run_script(app, "--force").returncode, 0)
        self.assertEqual((self.state(app) / "consecutive_failures").read_text().strip(), "1")
        self.assertEqual(store.read_state(self.state(app))["last_run"]["result"], "fail")

    def test_recheck_reports_what_was_recorded(self):
        app = self.app("com.example.tv", "TVApp", platform="tvos")
        self.assertEqual(self.run_script(app, "--recheck").returncode, 0)
        self.assertIn("nothing recorded", self.lines())
        self.assertEqual(self.run_script(app, "--force").returncode, 0)
        self.assertEqual(self.run_script(app, "--recheck").returncode, 0)
        self.assertIn("recorded: signature valid until", self.lines())

    def test_an_ios_app_is_never_sent_to_the_apple_tv(self):
        app = self.app("com.example.phone", "Phone")
        r = self.run_script(app, "--force")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("only takes tvOS apps", r.stdout)
        self.assertEqual(self.calls(), [])

    def test_a_tvos_app_is_never_sent_to_an_iphone(self):
        phone = self.device(UDID, "iPhone")
        app = self.app("com.example.tv", "TVApp", platform="tvos")
        r = self.run_script(app, "--force", device=phone)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("can't go on an iPhone", r.stdout)
        self.assertEqual(self.calls(), [])

    def test_refreshing_everything_sends_each_app_to_the_right_kind_of_device(self):
        self.device(UDID, "iPhone")
        ios_app = self.app("com.example.phone", "Phone")
        tv_app = self.app("com.example.tv", "TVApp", platform="tvos")
        self.assertEqual(store.app_devices(ios_app), [UDID])
        self.assertEqual(store.app_devices(tv_app), [TV_UDID])
        accounts.verified_file(self.a, "ios").touch()
        self.run_script(None, "--force", device=None)  # the iPhone side has no profile to read here, so no rc check
        tv_calls = [c for c in self.calls() if c[0] == "sign"]
        ios_calls = [c for c in self.calls() if c[0] != "sign" and c[0] != "login"]
        self.assertEqual([(c[1], c[4]) for c in tv_calls], [("a@example.com", TV_UDID)])
        self.assertEqual(len(ios_calls), 1)  # AltServer, for the phone
        self.assertTrue((Path(config.APPS) / tv_app / "state" / TV_UDID / "expiry").exists())


if __name__ == "__main__":
    unittest.main()
