import time
import unittest
from pathlib import Path

from sideloop import anisette, config

from .support import Fresh

A, B, C = "https://a.example", "https://b.example", "https://c.example"

# What AltServer really printed (tests/ keeps the wording that matters; see the refresh log of 17 Nov 2026)
PROVISIONING_401 = """Installing app...
Anisette server https://new.ani.sidestore.io: using v3 (auto).
Anisette v3: provisioning device 5F241D87 with https://new.ani.sidestore.io...
Anisette v3: /grandslam/MidService/finishMachineProvisioning answered HTTP 401.
Alert: Anisette v3: /grandslam/MidService/finishMachineProvisioning answered HTTP 401.
Press any key to continue...
Error: com.rileytestut.AltServer (13)."""
SUCCESS = """Anisette v3: headers for device FD464BB5.
Device ID: FD464BB5...
GrandSlam status: au=(none) hsc=200
Got token for com.apple.gs.xcode.auth.
Notify: Installation Succeeded"""
WRONG_2FA = """Anisette v3: headers for device FD464BB5.
Requires two factor...
Enter two factor code
Received 2FA response status code: 401
Alert: Could not install app.ipa to unknown.
    Server returned invalid response.
Error: com.rileytestut.ALTAppleAPI (17)."""
LIMIT = "Alert: Could not install app. Your maximum App ID limit has been reached."
BAD_PASSWORD = "Alert: Could not install app.ipa to unknown.\n    Your Apple ID or password is incorrect. (-22406)"


class Servers(Fresh):
    def test_the_list_may_use_commas_spaces_and_trailing_slashes(self):
        self.assertEqual(anisette.servers(f"{A}/, {B}  {A}\n{C}"), [A, B, C])

    def test_an_empty_setting_falls_back_to_the_default(self):
        self.assertEqual(anisette.servers(""), [anisette.DEFAULT])

    def test_configured_order_when_nothing_has_happened(self):
        self.assertEqual(anisette.order(raw=f"{A},{B}"), [A, B])

    def test_the_last_one_that_worked_goes_first(self):
        anisette.good(B)
        self.assertEqual(anisette.order(raw=f"{A},{B},{C}"), [B, A, C])

    def test_a_server_that_is_no_longer_configured_is_not_resurrected(self):
        anisette.good(C)
        self.assertEqual(anisette.order(raw=f"{A},{B}"), [A, B])

    def test_a_failed_server_goes_last_until_it_has_been_set_aside_long_enough(self):
        anisette.bad(A)
        self.assertEqual(anisette.order(raw=f"{A},{B}"), [B, A])
        later = time.time() + anisette.BAD_FOR + 5
        self.assertEqual(anisette.order(now=later, raw=f"{A},{B}"), [A, B])

    def test_a_server_that_works_again_is_cleared(self):
        anisette.bad(A)
        anisette.good(A)
        self.assertFalse(anisette.is_bad(A))
        self.assertEqual(anisette.order(raw=f"{B},{A}"), [A, B])


class Detection(unittest.TestCase):
    def test_a_rejected_provisioning_is_a_server_problem(self):
        self.assertTrue(anisette.failed(PROVISIONING_401))
        self.assertIn("finishMachineProvisioning", anisette.first_problem(PROVISIONING_401))

    def test_apple_refusing_the_identity_is_a_server_problem(self):
        self.assertTrue(anisette.failed("Alert: Could not install app.ipa\n    The provided anisette data is invalid. (-27677)"))
        self.assertTrue(anisette.failed("Alert: Could not install\n    This action cannot be completed (-80043)"))

    def test_a_carriage_return_terminal_log_still_matches(self):
        self.assertTrue(anisette.failed(PROVISIONING_401.replace("\n", "\r\n")))

    def test_things_that_are_not_the_servers_fault_do_not_match(self):
        for text in (SUCCESS, WRONG_2FA, LIMIT, BAD_PASSWORD, "", "Something broke"):
            self.assertFalse(anisette.failed(text), text)
            self.assertEqual(anisette.first_problem(text), "")


if __name__ == "__main__":
    unittest.main()
