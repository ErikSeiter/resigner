import os
import unittest
from pathlib import Path
from unittest import mock

from sideloop import accounts, anisette, config, store
from sideloop.jobs import Job, Jobs
from sideloop.server import App, run_refresh

from .support import UDID, UDID2, Fresh


class FakeHealth:
    def snapshot(self):
        return {"devices": [], "scanning": False, "anisette": True, "muxer": True}

    def visible(self, udid):
        return {"udid": udid, "name": "Fake"}

    def scan(self):
        pass


class Api(Fresh):
    def setUp(self):
        super().setUp()
        self.api = App(Jobs(on_done=lambda: None), FakeHealth(), None)
        self.routes = self.api.routes()

    def post(self, path, **data):
        return self.routes[path](data)

    def test_state_lists_accounts_and_groups_without_passwords(self):
        self.device(UDID, "iPhone")
        self.post("/api/account/save", apple_id="a@example.com", password="secret-pw")
        self.post("/api/group/add", name="Kids", devices=[UDID])
        state = self.api.state()
        self.assertEqual([a["apple_id"] for a in state["accounts"]], ["a@example.com"])
        self.assertEqual(state["groups"], [{"id": "kids", "name": "Kids", "devices": [UDID]}])
        self.assertNotIn("secret-pw", repr(state))

    def test_old_endpoint_still_saves_an_apple_id(self):
        self.post("/api/appleid", apple_id="a@example.com", password="x")
        self.assertEqual(len(accounts.ids()), 1)

    def test_configured_needs_an_account_a_device_and_an_app(self):
        self.assertFalse(self.api.state()["configured"])
        self.device()
        self.app()
        self.assertFalse(self.api.state()["configured"])
        self.post("/api/account/save", apple_id="a@example.com", password="x")
        self.assertTrue(self.api.state()["configured"])

    def test_account_management(self):
        self.post("/api/account/save", apple_id="a@example.com", password="x")
        self.post("/api/account/save", apple_id="b@example.com", password="x")
        self.post("/api/account/move", id="b-example-com", direction="up")
        self.assertEqual([a["apple_id"] for a in self.api.state()["accounts"]], ["b@example.com", "a@example.com"])
        self.post("/api/account/enable", id="a-example-com", enabled=False)
        self.assertFalse(self.api.state()["accounts"][1]["enabled"])
        self.assertEqual(self.post("/api/account/remove", id="a-example-com"), {"orphaned": 0})
        with self.assertRaises(ValueError):
            self.post("/api/account/remove", id="a-example-com")
        with self.assertRaises(ValueError):
            self.post("/api/account/move", id="b-example-com", direction="sideways")

    def test_removing_an_account_reports_the_apps_it_signed(self):
        phone = self.device()
        app = self.app()
        aid = accounts.save("a@example.com", "x")
        accounts.record(app, phone, aid)
        self.assertEqual(self.post("/api/account/remove", id=aid), {"orphaned": 1})

    def test_apps_that_follow_groups_refuse_device_toggles(self):
        phone, ipad = self.device(UDID, "iPhone"), self.device(UDID2, "iPad")
        app = self.app()
        gid = self.post("/api/group/add", name="Kids", devices=[ipad])["id"]
        self.post("/api/app/groups", app=app, groups=[gid])
        self.assertEqual(store.app_devices(app), [ipad])
        with self.assertRaises(ValueError):
            self.post("/api/app/devices", app=app, devices=[phone])
        self.post("/api/app/groups", app=app, groups=[])
        self.post("/api/app/devices", app=app, devices=[phone])
        self.assertEqual(store.app_devices(app), [phone])

    def test_group_endpoints_validate(self):
        with self.assertRaises(ValueError):
            self.post("/api/group/remove", id="missing")
        with self.assertRaises(ValueError):
            self.post("/api/group/add", name="")
        gid = self.post("/api/group/add", name="Kids")["id"]
        self.post("/api/group/rename", id=gid, name="Family")
        self.assertEqual(self.api.state()["groups"][0]["name"], "Family")
        self.post("/api/group/remove", id=gid)
        self.assertEqual(self.api.state()["groups"], [])


class Retry(Fresh):
    """The terminal run: Apple says the picked account is out of signings, so the next try excludes it."""

    def go(self, script, extra=("--app", "x", "--device", "y")):
        job = Job("refresh", "test")
        run_refresh(job, ["bash", "-c", script, "_", *extra])
        return job

    def setUp(self):
        super().setUp()
        accounts.save("b@example.com", "Zq9#pw")  # a retry only makes sense when there is another account

    def test_a_limit_error_marks_the_account_and_runs_again(self):
        aid = accounts.save("a@example.com", "Zq9#pw")
        marker = Path(config.DATA) / "ran"
        script = f'''
if [[ -e "{marker}" ]]; then echo "signing with Apple ID b@example.com [other]"; exit 0; fi
touch "{marker}"; echo "signing with Apple ID a@example.com [{aid}]"
echo "Your maximum App ID limit has been reached."; exit 1'''
        job = self.go(script)
        self.assertEqual(job.rc, 0)
        self.assertTrue(accounts.is_exhausted(aid))
        self.assertTrue(any("Trying the next one" in line for line in job.lines))

    def test_it_does_not_loop_on_the_same_account(self):
        aid = accounts.save("a@example.com", "Zq9#pw")
        counter = Path(config.DATA) / "count"
        script = f'''echo x >> "{counter}"; echo "signing with Apple ID a@example.com [{aid}]"
echo "maximum App ID limit"; exit 1'''
        job = self.go(script)
        self.assertNotEqual(job.rc, 0)
        self.assertEqual(len(counter.read_text().split()), 2)  # once, then once more, then it gives up

    def test_untargeted_runs_are_not_repeated(self):
        aid = accounts.save("a@example.com", "Zq9#pw")
        counter = Path(config.DATA) / "count"
        script = f'''echo x >> "{counter}"; echo "signing with Apple ID a@example.com [{aid}]"
echo "maximum App ID limit"; exit 1'''
        self.go(script, extra=())
        self.assertEqual(len(counter.read_text().split()), 1)

    def test_other_failures_are_not_retried(self):
        aid = accounts.save("a@example.com", "Zq9#pw")
        counter = Path(config.DATA) / "count"
        script = f'''echo x >> "{counter}"; echo "signing with Apple ID a@example.com [{aid}]"
echo "Something broke"; exit 1'''
        self.go(script)
        self.assertEqual(len(counter.read_text().split()), 1)

    # --- anisette servers: the web page retries a failed helper with the next server -------------------------

    TWO = {"ANISETTE_SERVER": "https://a.example,https://b.example"}
    FAILS = ('echo "signing and installing x, via Anisette server {url}"\n'
             'echo "Alert: Anisette v3: /grandslam/MidService/finishMachineProvisioning answered HTTP 401."; exit 1')

    def test_a_failing_anisette_server_is_set_aside_and_the_run_repeats(self):
        marker = Path(config.DATA) / "ran"
        script = f'''
if [[ -e "{marker}" ]]; then echo "signing and installing x, via Anisette server https://b.example"; exit 0; fi
touch "{marker}"
{self.FAILS.format(url="https://a.example")}'''
        with mock.patch.dict(os.environ, self.TWO):
            job = self.go(script)
        self.assertEqual(job.rc, 0)
        self.assertTrue(anisette.is_bad("https://a.example"))
        self.assertFalse(anisette.is_bad("https://b.example"))
        self.assertTrue(any("Trying the next one" in line for line in job.lines))

    def test_it_stops_when_every_anisette_server_has_failed(self):
        counter = Path(config.DATA) / "count"
        script = f'''echo x >> "{counter}"
if [[ "$(wc -l < "{counter}")" == 1 ]]; then {self.FAILS.format(url="https://a.example")}; fi
{self.FAILS.format(url="https://b.example")}'''
        with mock.patch.dict(os.environ, self.TWO):
            job = self.go(script)
        self.assertNotEqual(job.rc, 0)
        self.assertEqual(len(counter.read_text().split()), 2)
        self.assertTrue(anisette.is_bad("https://a.example") and anisette.is_bad("https://b.example"))

    def test_a_single_anisette_server_is_not_repeated(self):
        counter = Path(config.DATA) / "count"
        script = f'''echo x >> "{counter}"\n{self.FAILS.format(url="https://a.example")}'''
        with mock.patch.dict(os.environ, {"ANISETTE_SERVER": "https://a.example"}):
            self.go(script)
        self.assertEqual(len(counter.read_text().split()), 1)

    def test_untargeted_runs_do_not_repeat_for_a_failing_anisette_server(self):
        counter = Path(config.DATA) / "count"
        script = f'''echo x >> "{counter}"\n{self.FAILS.format(url="https://a.example")}'''
        with mock.patch.dict(os.environ, self.TWO):
            self.go(script, extra=())
        self.assertEqual(len(counter.read_text().split()), 1)


if __name__ == "__main__":
    unittest.main()
