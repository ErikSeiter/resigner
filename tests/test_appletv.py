"""Apple TV support outside the refresh script: telling tvOS apps from iOS apps, which device takes which app,
finding and pairing a TV, and what the web server and the job panel make of it."""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from resigner import accounts, appletv, devices, ipa, store
from resigner.jobs import Job, Jobs
from resigner.server import App

from .support import ROOT, TV_UDID, UDID, Fresh, make_ipa

STUBS = ROOT / "tests" / "stubs"


def svc(ip="192.168.1.50", port=49152, name="Living Room", ident="AA:BB", tag="t0k3n"):
    return {"name": name, "host": "Living-Room.local", "ip": ip, "port": port,
            "txt": {"identifier": ident, "authTag": tag}}


class WithStub(Fresh):
    """plumesign is the stand-in from tests/stubs, with its own scratch folder."""

    def setUp(self):
        super().setUp()
        self.stub = Path(tempfile.mkdtemp(prefix="stub-"))
        patch = mock.patch.dict(os.environ, {"PATH": f"{STUBS}:{os.environ['PATH']}", "STUB_DIR": str(self.stub)})
        patch.start()
        self.addCleanup(patch.stop)
        appletv._identified.clear()
        self.addCleanup(appletv._identified.clear)


class Platform(Fresh):
    def inspect(self, platform):
        path = Path(tempfile.mkdtemp()) / "a.ipa"
        make_ipa(path, platform=platform)
        return ipa.inspect(path)

    def test_a_tvos_ipa_is_recognised(self):
        self.assertEqual(self.inspect("tvos")["platform"], "tvos")

    def test_an_ordinary_ipa_is_ios(self):
        self.assertEqual(self.inspect("ios")["platform"], "ios")

    def test_the_other_ways_a_tvos_ipa_can_say_so(self):
        self.assertEqual(ipa.platform_of({"DTPlatformName": "appletvos"}), "tvos")
        self.assertEqual(ipa.platform_of({"UIDeviceFamily": [3]}), "tvos")
        self.assertEqual(ipa.platform_of({"CFBundleSupportedPlatforms": ["AppleTVSimulator"]}), "tvos")
        self.assertEqual(ipa.platform_of({"UIDeviceFamily": [1, 2]}), "ios")
        self.assertEqual(ipa.platform_of({}), "ios")


class FromTheBinary(Fresh):
    """Some tvOS builds claim iOS in their Info.plist (so that iOS-only sideloaders take them): the binary decides."""

    def inspect(self, **kw):
        path = Path(tempfile.mkdtemp()) / "a.ipa"
        make_ipa(path, **kw)
        return ipa.inspect(path)

    def test_a_tvos_binary_in_an_ios_looking_ipa_is_a_tvos_app(self):
        self.assertEqual(self.inspect(platform="ios", binary="tvos")["platform"], "tvos")

    def test_the_older_load_command_is_read_too(self):
        self.assertEqual(self.inspect(platform="ios", binary="tvos", via="min")["platform"], "tvos")
        self.assertEqual(self.inspect(platform="tvos", binary="ios", via="min")["platform"], "ios")

    def test_the_binary_wins_over_the_plist(self):
        self.assertEqual(self.inspect(platform="tvos", binary="ios")["platform"], "ios")

    def test_a_binary_that_says_nothing_leaves_it_to_the_plist(self):
        self.assertEqual(self.inspect(platform="tvos")["platform"], "tvos")
        self.assertEqual(self.inspect(platform="ios")["platform"], "ios")

    def test_encryption_is_still_found(self):
        self.assertEqual(self.inspect(binary="tvos")["encrypted"], "no")

    def test_an_old_cached_answer_is_looked_at_again(self):
        import json
        app_id = self.app("com.example.tv", "TV App", platform="ios", binary="tvos")
        folder = Path(store.APPS) / app_id
        info = ipa.cached(folder)
        self.assertEqual(info["platform"], "tvos")
        cache = folder / "info.json"
        stale = {**json.loads(cache.read_text()), "platform": "ios", "_v": 1}
        cache.write_text(json.dumps(stale))
        self.assertEqual(ipa.cached(folder)["platform"], "tvos")

    def test_an_app_filed_under_the_wrong_platform_is_moved_to_the_devices_that_fit(self):
        phone, tv = self.device(), self.tv()
        app_id = self.app("com.example.tv", "TV App", platform="ios", binary="tvos")
        self.assertEqual(store.app_devices(app_id), [tv])
        meta = store.app_meta(app_id)  # as it was filed before the binary was read
        meta["APP_PLATFORM"], meta["DEVICES"] = "ios", phone
        store.write_env(Path(store.APPS) / app_id / "meta.env", meta)
        self.assertEqual(store.app_devices(app_id), [phone])
        store.migrate_platforms()
        self.assertEqual(store.app_view(app_id)["platform"], "tvos")
        self.assertEqual(store.app_devices(app_id), [tv])

    def test_an_app_that_follows_a_group_is_synced_after_the_move(self):
        tv = self.tv()
        gid = store.add_group("AppleTV")
        store.set_group_devices(gid, [tv])
        app_id = self.app("com.example.tv", "TV App", platform="ios", binary="tvos")
        store.set_app_groups(app_id, [gid])
        meta = store.app_meta(app_id)
        meta["APP_PLATFORM"] = "ios"
        store.write_env(Path(store.APPS) / app_id / "meta.env", meta)
        store.migrate_platforms()
        self.assertEqual(store.app_devices(app_id), [tv])

    def test_a_right_platform_is_left_alone(self):
        phone = self.device()
        app_id = self.app("com.example.ios", "Phone App", platform="ios", binary="ios")
        store.migrate_platforms()
        self.assertEqual(store.app_devices(app_id), [phone])


class Compatibility(Fresh):
    def test_a_tv_only_takes_tvos_apps_and_the_other_devices_only_ios_apps(self):
        phone, tv = self.device(), self.tv()
        ios, tvos = self.app("com.example.ios", "Phone App"), self.app("com.example.tv", "TV App", platform="tvos")
        self.assertTrue(store.compatible(ios, phone))
        self.assertFalse(store.compatible(ios, tv))
        self.assertTrue(store.compatible(tvos, tv))
        self.assertFalse(store.compatible(tvos, phone))

    def test_a_new_tv_gets_the_tvos_apps_only(self):
        phone = self.device()
        ios, tvos = self.app("com.example.ios", "Phone App"), self.app("com.example.tv", "TV App", platform="tvos")
        tv = self.tv()
        self.assertEqual(store.app_devices(ios), [phone])
        self.assertEqual(store.app_devices(tvos), [tv])

    def test_a_new_tvos_app_goes_to_the_tv_and_not_the_phone(self):
        phone, tv = self.device(), self.tv()
        tvos = self.app("com.example.tv", "TV App", platform="tvos")
        self.assertEqual(store.app_devices(tvos), [tv])
        self.assertNotIn(phone, store.app_devices(tvos))

    def test_a_device_that_does_not_fit_cannot_be_forced_on_an_app(self):
        phone, tv = self.device(), self.tv()
        tvos = self.app("com.example.tv", "TV App", platform="tvos")
        store.set_app_devices(tvos, [phone, tv])
        self.assertEqual(store.app_devices(tvos), [tv])

    def test_the_app_view_says_what_it_is_and_what_each_target_is(self):
        self.device(), self.tv()
        tvos = self.app("com.example.tv", "TV App", platform="tvos")
        view = store.app_view(tvos)
        self.assertEqual(view["platform"], "tvos")
        self.assertEqual([t["kind"] for t in view["targets"]], ["appletv"])

    def test_an_app_cached_before_tv_support_counts_as_ios(self):
        phone = self.device()
        app_id = self.app()
        self.assertEqual(store.app_view(app_id)["platform"], "ios")
        self.assertEqual(store.app_devices(app_id), [phone])

    def test_the_device_kind_survives_a_re_add(self):
        tv = self.tv()
        store.add_device(tv, "Renamed", kind="appletv")
        self.assertEqual(store.device_kind(tv), "appletv")
        self.assertEqual(store.device_name(tv), "Renamed")
        self.assertEqual(store.device_kind(self.device()), "ios")


class Accounts(Fresh):
    def test_a_sign_in_for_the_iphone_does_not_count_for_the_apple_tv(self):
        aid = accounts.save("a@example.com", "pw")
        accounts.verified_file(aid).touch()
        view = accounts.view(aid)
        self.assertTrue(view["verified"])
        self.assertFalse(view["tv_verified"])
        accounts.verified_file(aid, "appletv").touch()
        self.assertTrue(accounts.view(aid)["tv_verified"])

    def test_the_files_are_separate(self):
        aid = accounts.save("a@example.com", "pw")
        self.assertNotEqual(accounts.verified_file(aid), accounts.verified_file(aid, "appletv"))


class Finding(WithStub):
    def test_a_registered_tv_is_found_when_it_announces_itself(self):
        tv = self.tv()
        with mock.patch.dict(os.environ, {"STUB_TV_UDID": tv}), mock.patch("resigner.mdns.browse", return_value=[svc()]):
            found = appletv.scan_live()
        self.assertEqual([(d["udid"], d["ip"], d["port"], d["kind"]) for d in found],
                         [(tv, "192.168.1.50", 49152, "appletv")])
        self.assertTrue(found[0]["reachable"])

    def test_an_iphone_announcing_the_same_service_is_not_a_tv(self):
        self.tv()
        with mock.patch("resigner.mdns.browse", return_value=[svc()]):
            self.assertEqual(appletv.scan_live(), [])  # the stub finds no pairing file for it

    def test_a_tv_that_is_not_registered_is_ignored(self):
        with mock.patch.dict(os.environ, {"STUB_TV_UDID": TV_UDID}), mock.patch("resigner.mdns.browse", return_value=[svc()]) as m:
            self.assertEqual(appletv.scan_live(), [])
        m.assert_not_called()  # nothing to look for, so no network traffic either

    def test_the_answer_for_an_announcement_is_remembered(self):
        tv = self.tv()
        with mock.patch.dict(os.environ, {"STUB_TV_UDID": tv}), mock.patch("resigner.mdns.browse", return_value=[svc()]):
            appletv.scan_live()
            (self.stub / "marker").write_text("x")
            with mock.patch("resigner.appletv._run", side_effect=AssertionError("asked twice")):
                self.assertEqual(len(appletv.scan_live()), 1)

    def test_locate_gives_the_address(self):
        tv = self.tv()
        with mock.patch.dict(os.environ, {"STUB_TV_UDID": tv}), mock.patch("resigner.mdns.browse", return_value=[svc(ip="10.0.0.7", port=5000)]):
            self.assertEqual(appletv.locate(tv), ("10.0.0.7", 5000))

    def test_locate_says_so_when_the_tv_is_away(self):
        tv = self.tv()
        with mock.patch("resigner.mdns.browse", return_value=[]):
            self.assertIsNone(appletv.locate(tv))

    def test_the_tvs_waiting_for_a_pin_are_listed_once(self):
        found = [svc(), svc(), svc(ip="192.168.1.51", name="Bedroom")]
        with mock.patch("resigner.mdns.browse", return_value=found) as m:
            tvs = appletv.pairable()
        self.assertEqual([(t["name"], t["ip"], t["port"]) for t in tvs],
                         [("Living Room", "192.168.1.50", 49152), ("Bedroom", "192.168.1.51", 49152)])
        self.assertEqual(m.call_args[0][0], [appletv.SERVICE_PAIRING])

    def test_profile_info_reads_the_expiry_and_certificate(self):
        import datetime
        import plistlib
        path = Path(tempfile.mkdtemp()) / "p.mobileprovision"
        when = datetime.datetime(2030, 1, 2, 3, 4, 5)
        path.write_bytes(plistlib.dumps({"ExpirationDate": when, "DeveloperCertificates": [b"cert"]}))
        expiry, sha = appletv.profile_info(path)
        self.assertEqual(expiry, int(when.replace(tzinfo=datetime.timezone.utc).timestamp()))
        self.assertEqual(len(sha), 40)
        self.assertIsNone(appletv.profile_info(path.with_name("missing")))


class Pairing(WithStub):
    def run_pair(self, pin, ip="192.168.1.50", port=49152):
        job = Job("pairtv", "Pairing the Apple TV")
        result = {}

        def go():
            try:
                appletv.pair(job, ip, port)
            except Exception as e:  # the same thing Jobs._run does
                result["error"] = str(e)

        t = threading.Thread(target=go)
        t.start()
        deadline = time.time() + 20
        while not job.needs_input and t.is_alive() and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(job.needs_input, "the PIN prompt never showed up")
        self.assertTrue(job.view()["accepts_input"])
        job.send(pin)
        t.join(30)
        self.assertFalse(t.is_alive())
        return job, result

    def test_a_right_pin_pairs_and_registers_the_tv(self):
        job, result = self.run_pair("1234")
        self.assertNotIn("error", result)
        self.assertEqual(store.device_ids(), [TV_UDID])
        self.assertEqual(store.device_kind(TV_UDID), "appletv")
        self.assertEqual(store.device_name(TV_UDID), "Living Room")
        self.assertEqual((self.stub / "pair_calls").read_text().strip(), "192.168.1.50:49152")
        self.assertIn("Added Living Room", "\n".join(job.lines))

    def test_the_pin_is_not_left_in_the_log(self):
        job, _ = self.run_pair("1234")
        self.assertNotIn("1234", "\n".join(job.lines))

    def test_a_wrong_pin_registers_nothing(self):
        _, result = self.run_pair("9999")
        self.assertIn("pairing didn't work", result["error"])
        self.assertEqual(store.device_ids(), [])

    def test_pairing_a_tv_hands_it_the_tvos_apps_already_added(self):
        tvos = self.app("com.example.tv", "TV App", platform="tvos")
        ios = self.app("com.example.ios", "Phone App")
        self.run_pair("1234")
        self.assertEqual(store.app_devices(tvos), [TV_UDID])
        self.assertEqual(store.app_devices(ios), [])

    def test_a_new_tv_joins_an_empty_group_that_only_holds_tvos_apps(self):
        gid = store.add_group("AppleTV")
        tvos = self.app("com.example.tv", "TV App", platform="tvos")
        store.set_app_groups(tvos, [gid])
        self.assertEqual(store.app_devices(tvos), [])
        job, _ = self.run_pair("1234")
        self.assertEqual(store.group_devices(gid), [TV_UDID])
        self.assertEqual(store.app_devices(tvos), [TV_UDID])
        self.assertIn("in the group AppleTV", "\n".join(job.lines))

    def test_a_new_tv_stays_out_of_groups_that_are_not_for_it(self):
        mixed = store.add_group("Everything")
        used = store.add_group("Phones")
        store.set_group_devices(used, [self.device()])
        for gid, bundle, platform in ((mixed, "com.example.tv", "tvos"), (mixed, "com.example.ios", "ios"), (used, "com.example.p", "ios")):
            store.set_app_groups(self.app(bundle, bundle, platform=platform), [gid])
        self.run_pair("1234")
        self.assertEqual(store.group_devices(mixed), [])
        self.assertEqual(store.group_devices(used), [UDID])

    def test_pairing_an_already_known_tv_again_leaves_the_groups_alone(self):
        gid = store.add_group("AppleTV")
        store.set_app_groups(self.app("com.example.tv", "TV App", platform="tvos"), [gid])
        self.tv()
        self.run_pair("1234")
        self.assertEqual(store.group_devices(gid), [])

    def test_a_bad_address_is_refused_before_anything_runs(self):
        job = Job("pairtv", "x")
        with self.assertRaises(ValueError):
            appletv.pair(job, "192.168.1.5; rm -rf /", 49152)
        self.assertFalse((self.stub / "pair_calls").exists())

    def test_it_says_so_when_the_helper_is_missing(self):
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}), mock.patch.object(appletv, "BIN", "no-such-plumesign"):
            with self.assertRaisesRegex(RuntimeError, "isn't installed"):
                appletv.pair(Job("pairtv", "x"), "192.168.1.50", 49152)


class FakeHealth:
    def snapshot(self):
        return {"devices": [], "scanning": False, "anisette": True, "muxer": True}

    def visible(self, udid):
        return None

    def scan(self):
        pass


class Server(WithStub):
    def setUp(self):
        super().setUp()
        self.jobs = Jobs(on_done=lambda: None)
        self.api = App(self.jobs, FakeHealth(), None)
        self.routes = self.api.routes()

    def test_the_state_marks_the_kind_of_every_device_and_whether_tvs_are_possible(self):
        self.device(), self.tv()
        state = self.api.state()
        self.assertEqual({d["udid"]: d["kind"] for d in state["registered"]}, {UDID: "ios", TV_UDID: "appletv"})
        self.assertEqual(state["appletv"], {"available": True})

    def test_the_state_says_when_the_helper_is_missing(self):
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}), mock.patch.object(appletv, "BIN", "no-such-plumesign"):
            self.assertEqual(self.api.state()["appletv"], {"available": False})

    def test_discover_lists_the_tvs_showing_a_pin(self):
        with mock.patch("resigner.mdns.browse", return_value=[svc()]):
            self.assertEqual(self.routes["/api/appletv/discover"]({}),
                             {"tvs": [{"name": "Living Room", "ip": "192.168.1.50", "port": 49152}]})

    def test_discover_refuses_without_the_helper(self):
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}), mock.patch.object(appletv, "BIN", "no-such-plumesign"):
            with self.assertRaisesRegex(ValueError, "isn't installed"):
                self.routes["/api/appletv/discover"]({})

    def test_pair_validates_the_address(self):
        pair = self.routes["/api/appletv/pair"]
        for bad in ({}, {"ip": "192.168.1.50"}, {"ip": "192.168.1.50", "port": "x"}, {"ip": "192.168.1.50", "port": 0},
                    {"ip": "192.168.1.50", "port": 70000}, {"ip": "$(reboot)", "port": 1234}, {"ip": "", "port": 1234}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                pair(bad)
        self.assertIsNone(self.jobs.current)

    def test_pair_starts_a_job_that_takes_the_pin(self):
        out = self.routes["/api/appletv/pair"]({"ip": "192.168.1.50", "port": 49152})
        job = self.jobs.current
        self.assertEqual(out, {"job": job.id})
        self.assertEqual(job.kind, "pairtv")
        deadline = time.time() + 20
        while not job.needs_input and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(job.needs_input)
        self.routes["/api/job/input"]({"text": "1234"})
        deadline = time.time() + 30
        while job.running and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(job.rc, 0)
        self.assertEqual(store.device_kind(TV_UDID), "appletv")


class JobPanel(Fresh):
    def feed(self, *lines):
        job = Job("refresh", "x")
        for line in lines:
            job.say(line)
        return job

    def test_the_plumesign_steps_move_the_progress(self):
        job = self.feed("[Game] signing and installing Game.ipa on 0000, via Anisette server x")
        self.assertEqual((job.stage, job.app), ("signin", "Game"))
        job.say("Signing bundle: /data/apps/game/app.ipa")
        self.assertEqual(job.stage, "sign")
        job.say("Installing to device: 0000")
        self.assertEqual(job.stage, "send")
        job.say("Installation complete!")
        self.assertEqual((job.stage, job.fraction), ("install", 1.0))

    def test_a_pin_prompt_asks_for_input(self):
        job = Job("pairtv", "x")
        job.add("Enter PIN:")
        self.assertTrue(job.needs_input)

    def test_signing_clears_the_wait_for_a_code(self):
        job = Job("refresh", "x")
        job.say("Enter 2FA code:")
        self.assertTrue(job.needs_input)
        job.say("Signing bundle: app.ipa")
        self.assertFalse(job.needs_input)


class Health(Fresh):
    def scan(self, probe=(), live=(), mux=True):
        h = devices.Health()
        arrivals = []
        h.on_arrival = arrivals.append
        with mock.patch.object(devices, "_mux_reachable", return_value=mux), \
                mock.patch.object(devices, "_anisette_up", return_value=True), \
                mock.patch.object(devices, "_probe", return_value=list(probe)), \
                mock.patch.object(devices.appletv, "scan_live", return_value=list(live)):
            h.scan()
        return h, arrivals

    def tv_live(self):
        return {"udid": TV_UDID, "conn": "wifi", "reachable": True, "kind": "appletv", "name": "Living Room",
                "model": "AppleTV14,1", "ios": "-", "ip": "192.168.1.50", "port": 49152}

    def test_a_tv_on_the_network_is_a_device_that_has_arrived(self):
        self.tv()
        h, arrivals = self.scan(live=[self.tv_live()])
        self.assertEqual(arrivals, [TV_UDID])
        self.assertEqual(h.visible(TV_UDID)["kind"], "appletv")

    def test_a_tv_is_found_even_when_the_phone_muxer_is_down(self):
        self.tv()
        h, arrivals = self.scan(live=[self.tv_live()], mux=False)
        self.assertEqual(arrivals, [TV_UDID])
        self.assertFalse(h.snapshot()["muxer"])

    def test_a_tv_that_keeps_answering_arrives_only_once(self):
        self.tv()
        h, arrivals = self.scan(live=[self.tv_live()])
        with mock.patch.object(devices, "_mux_reachable", return_value=True), \
                mock.patch.object(devices, "_anisette_up", return_value=True), \
                mock.patch.object(devices, "_probe", return_value=[]), \
                mock.patch.object(devices.appletv, "scan_live", return_value=[self.tv_live()]):
            h.scan()
        self.assertEqual(arrivals, [TV_UDID])

    def test_a_failing_tv_scan_does_not_hide_the_phones(self):
        self.device()
        phone = {"udid": UDID, "conn": "wifi", "reachable": True, "kind": "ios", "name": "iPhone", "model": "iPhone", "ios": "18.0"}
        h = devices.Health()
        with mock.patch.object(devices, "_mux_reachable", return_value=True), \
                mock.patch.object(devices, "_anisette_up", return_value=True), \
                mock.patch.object(devices, "_probe", return_value=[phone]), \
                mock.patch.object(devices.appletv, "scan_live", side_effect=OSError("no network")):
            h.scan()
        self.assertEqual([d["udid"] for d in h.snapshot()["devices"]], [UDID])


if __name__ == "__main__":
    unittest.main()
