import os
import time
import unittest
from pathlib import Path

from resigner import accounts, config, store

from .support import UDID, UDID2, Fresh


class Accounts(Fresh):
    def test_save_orders_and_updates(self):
        a = accounts.save("one@example.com", "pw1")
        b = accounts.save("Two@Example.com", "pw2")
        self.assertEqual(accounts.ids(), [a, b])
        self.assertEqual(accounts.meta(b)["APPLE_ID"], "Two@Example.com")
        accounts.save("one@example.com")  # no password given: keeps the old one
        self.assertEqual(accounts.meta(a)["APPLE_PASSWORD"], "pw1")
        accounts.save("one@example.com", "new")
        self.assertEqual(accounts.meta(a)["APPLE_PASSWORD"], "new")
        with self.assertRaises(ValueError):
            accounts.save("three@example.com")
        with self.assertRaises(ValueError):
            accounts.save("nope", "x")

    def test_passwords_with_shell_characters_survive(self):
        pw = "p'a\"s $x`y`\\z!"
        a = accounts.save("one@example.com", pw)
        self.assertEqual(accounts.meta(a)["APPLE_PASSWORD"], pw)

    def test_move_and_remove_renumber(self):
        a, b, c = (accounts.save(f"{n}@example.com", "x") for n in "abc")
        accounts.move(c, "up")
        self.assertEqual(accounts.ids(), [a, c, b])
        accounts.move(a, "up")  # already first
        self.assertEqual(accounts.ids(), [a, c, b])
        accounts.remove(c)
        self.assertEqual(accounts.ids(), [a, b])
        self.assertEqual([accounts.meta(x)["PRIORITY"] for x in accounts.ids()], ["1", "2"])

    def test_migrate_turns_the_old_apple_id_into_the_first_account_and_pins_installed_apps(self):
        config.update({"APPLE_ID": "old@example.com", "APPLE_PASSWORD": "oldpw"})
        phone = self.device()
        app = self.app()
        state = Path(config.APPS) / app / "state" / phone
        state.mkdir(parents=True)
        (state / "expiry").write_text("9999999999\n")
        accounts.migrate()
        (aid,) = accounts.ids()
        m = accounts.meta(aid)
        self.assertEqual((m["APPLE_ID"], m["APPLE_PASSWORD"], m["LEGACY"]), ("old@example.com", "oldpw", "1"))
        self.assertEqual(m["WORK_DIR"], str(config.DATA))  # its certificate cache stays where it is
        self.assertEqual(accounts.pin_of(app, phone), aid)
        accounts.migrate()  # idempotent
        self.assertEqual(accounts.ids(), [aid])

    def test_legacy_password_is_mirrored_for_rollbacks(self):
        config.update({"APPLE_ID": "old@example.com", "APPLE_PASSWORD": "oldpw"})
        accounts.migrate()
        accounts.save("old@example.com", "changed")
        self.assertEqual(config.read()["APPLE_PASSWORD"], "changed")
        other = accounts.save("other@example.com", "o")
        self.assertEqual(config.read()["APPLE_ID"], "old@example.com")
        self.assertIn(other, accounts.ids())


    def test_the_busiest_device_is_what_the_three_apps_limit_counts(self):
        a = accounts.save("a@example.com", "pw")
        self.device(UDID)
        self.device(UDID2, "iPad")
        one, two = self.app("com.one", "One"), self.app("com.two", "Two")
        self.assertEqual(accounts.busiest_device(a), 0)
        accounts.record(one, UDID, a)
        accounts.record(one, UDID2, a)  # the same app on a second device is not a second app on either
        self.assertEqual((accounts.busiest_device(a), accounts.view(a)["per_device"]), (1, 1))
        accounts.record(two, UDID, a)
        self.assertEqual(accounts.view(a)["per_device"], 2)


class Picking(Fresh):
    def setUp(self):
        super().setUp()
        self.phone = self.device(UDID, "iPhone")
        self.a = accounts.save("a@example.com", "x")
        self.b = accounts.save("b@example.com", "x")
        for aid in (self.a, self.b):
            (Path(config.ACCOUNTS) / aid / "verified").touch()

    def test_priority_order_for_a_new_app(self):
        app = self.app()
        self.assertEqual(accounts.pick(app, self.phone), [self.a, self.b])
        accounts.move(self.b, "up")
        self.assertEqual(accounts.pick(app, self.phone), [self.b, self.a])

    def test_an_installed_app_only_ever_uses_its_account(self):
        app = self.app()
        accounts.record(app, self.phone, self.b)
        self.assertEqual(accounts.pick(app, self.phone), [self.b])
        accounts.set_enabled(self.b, False)  # "don't use for new apps" doesn't strand what it already signed
        self.assertEqual(accounts.pick(app, self.phone), [self.b])

    def test_pin_of_a_removed_account_is_forgotten(self):
        app = self.app()
        accounts.record(app, self.phone, self.b)
        accounts.remove(self.b)
        self.assertEqual(accounts.pick(app, self.phone), [self.a])

    def test_disabled_accounts_are_not_used_for_new_apps(self):
        app = self.app()
        accounts.set_enabled(self.a, False)
        self.assertEqual(accounts.pick(app, self.phone), [self.b])

    def test_three_apps_per_device_then_the_next_account(self):
        for i in range(3):
            accounts.record(self.app(f"com.app{i}", f"App{i}"), self.phone, self.a)
            (Path(config.APPS) / f"com.app{i}" / "state" / self.phone / "expiry").write_text("9999999999\n")
        fourth = self.app("com.app3", "App3")
        self.assertEqual(accounts.pick(fourth, self.phone), [self.b, self.a])  # a is last resort, not dropped
        other = self.device(UDID2, "iPad")  # the limit is per device
        self.assertEqual(accounts.pick(fourth, other), [self.a, self.b])

    def test_ten_app_ids_a_week(self):
        for i in range(3):  # 3 apps with 2 extensions each = 9 App IDs
            accounts.record(self.app(f"com.big{i}", f"Big{i}", extensions=2), self.phone, self.a)
        # these aren't "installed" (no expiry file) so only the weekly ledger counts
        self.assertEqual(accounts.app_ids_this_week(self.a), 9)
        small = self.app("com.small", "Small")
        self.assertEqual(accounts.pick(small, self.phone), [self.a, self.b])  # 9 + 1 fits
        two = self.app("com.two", "Two", extensions=1)
        self.assertEqual(accounts.pick(two, self.phone), [self.b, self.a])  # 9 + 2 doesn't

    def test_ledger_entries_expire_after_a_week(self):
        app = self.app()
        accounts.record(app, self.phone, self.a)
        old = time.time() - 8 * 86400
        (Path(config.ACCOUNTS) / self.a / "ledger").write_text(f"{int(old)}\tcom.example.app\t9\n")
        self.assertEqual(accounts.app_ids_this_week(self.a), 0)

    def test_exhausted_account_goes_last_for_a_day_and_recovers_on_success(self):
        app = self.app()
        accounts.mark_exhausted(self.a)
        self.assertEqual(accounts.pick(app, self.phone), [self.b, self.a])
        day = time.time() + 25 * 3600
        self.assertEqual(accounts.pick(app, self.phone, now=day), [self.a, self.b])
        accounts.record(self.app("com.other", "Other"), self.phone, self.a)
        self.assertFalse(accounts.is_exhausted(self.a))

    def test_background_runs_skip_accounts_that_never_signed_in(self):
        app = self.app()
        (Path(config.ACCOUNTS) / self.a / "verified").unlink()
        self.assertEqual(accounts.pick(app, self.phone), [self.b])
        self.assertEqual(accounts.pick(app, self.phone, interactive=True), [self.a, self.b])
        (Path(config.ACCOUNTS) / self.b / "verified").unlink()
        self.assertEqual(accounts.pick(app, self.phone), [])

    def test_limit_detection(self):
        for text in ("Your maximum App ID limit has been reached. You may create up to 10 App IDs every 7 days.",
                     "Error: ALTAppleAPIErrorMaximumAppIDLimitReached",
                     "You have reached the maximum number of certificates",
                     "too many App IDs"):
            self.assertTrue(accounts.LIMIT_RE.search(text), text)
        for text in ("Apple is asking for a two-factor code", "Request canceled by user",
                     "Installation Progress: 0.5", "Failed to set app extension placeholders"):
            self.assertFalse(accounts.LIMIT_RE.search(text), text)


if __name__ == "__main__":
    unittest.main()
