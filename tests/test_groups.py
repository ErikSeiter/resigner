import unittest

from resigner import store

from .support import UDID, UDID2, Fresh


class Groups(Fresh):
    def setUp(self):
        super().setUp()
        self.phone, self.ipad = self.device(UDID, "iPhone"), self.device(UDID2, "iPad")

    def test_apps_without_groups_reach_every_device_even_new_ones(self):
        a = self.app()
        self.assertEqual(sorted(store.app_devices(a)), sorted([self.phone, self.ipad]))
        third = "00008020-00AABBCCDDEEFF01"
        store.add_device(third, "Louise")
        self.assertIn(third, store.app_devices(a))

    def test_group_limits_an_app_to_its_devices(self):
        kids = store.add_group("Kids")
        store.set_group_devices(kids, [self.ipad])
        a = self.app()
        store.set_app_groups(a, [kids])
        self.assertEqual(store.app_devices(a), [self.ipad])
        self.assertEqual(store.app_view(a)["groups"], [kids])

    def test_new_device_only_gets_ungrouped_apps_and_those_of_its_group(self):
        kids = store.add_group("Kids")
        grouped, free = self.app("com.a", "A"), self.app("com.b", "B")
        store.set_group_devices(kids, [self.ipad])
        store.set_app_groups(grouped, [kids])
        new = "00008020-00AABBCCDDEEFF01"
        store.add_device(new, "Louise")
        self.assertNotIn(new, store.app_devices(grouped))
        self.assertIn(new, store.app_devices(free))
        newer = "00008020-00AABBCCDDEEFF02"
        store.add_device(newer, "Kid's iPad", groups=[kids])
        self.assertIn(newer, store.app_devices(grouped))

    def test_changing_group_members_updates_the_apps(self):
        kids = store.add_group("Kids")
        a = self.app()
        store.set_app_groups(a, [kids])
        self.assertEqual(store.app_devices(a), [])
        store.set_group_devices(kids, [self.phone, self.ipad])
        self.assertEqual(sorted(store.app_devices(a)), sorted([self.phone, self.ipad]))
        store.set_group_devices(kids, [self.phone])
        self.assertEqual(store.app_devices(a), [self.phone])

    def test_app_in_two_groups_gets_the_union(self):
        g1, g2 = store.add_group("One"), store.add_group("Two")
        store.set_group_devices(g1, [self.phone])
        store.set_group_devices(g2, [self.ipad])
        a = self.app()
        store.set_app_groups(a, [g1, g2])
        self.assertEqual(sorted(store.app_devices(a)), sorted([self.phone, self.ipad]))

    def test_removing_a_group_keeps_what_the_apps_reach(self):
        g = store.add_group("Kids")
        store.set_group_devices(g, [self.ipad])
        a = self.app()
        store.set_app_groups(a, [g])
        store.remove_group(g)
        self.assertEqual(store.app_groups(a), [])
        self.assertEqual(store.app_devices(a), [self.ipad])
        store.add_device("00008020-00AABBCCDDEEFF01", "Louise")
        self.assertEqual(len(store.app_devices(a)), 2)  # an ungrouped app follows new devices again

    def test_removing_a_device_removes_it_from_groups(self):
        g = store.add_group("Kids")
        store.set_group_devices(g, [self.phone, self.ipad])
        store.remove_device(self.ipad)
        self.assertEqual(store.group_devices(g), [self.phone])

    def test_replacing_an_ipa_keeps_its_groups(self):
        g = store.add_group("Kids")
        store.set_group_devices(g, [self.ipad])
        a = self.app()
        store.set_app_groups(a, [g])
        self.assertEqual(self.app(), a)
        self.assertEqual(store.app_groups(a), [g])
        self.assertEqual(store.app_devices(a), [self.ipad])

    def test_group_names_are_unique_and_required(self):
        store.add_group("Kids")
        with self.assertRaises(ValueError):
            store.add_group(" kids ")
        with self.assertRaises(ValueError):
            store.add_group("  ")
        g = store.add_group("Kids!")
        self.assertNotEqual(g, store.add_group("Kids?"))

    def test_rename(self):
        g = store.add_group("Kids")
        store.rename_group(g, "Family iPads")
        self.assertEqual(store.group_name(g), "Family iPads")


if __name__ == "__main__":
    unittest.main()
