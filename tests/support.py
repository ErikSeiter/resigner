import io
import os
import plistlib
import shutil
import time
import unittest
import zipfile
from pathlib import Path

from . import DATA

ROOT = Path(__file__).resolve().parent.parent
UDID = "00008030-001234567890802E"
UDID2 = "00008101-000A11223344001E"
TV_UDID = "00008110-001A2B3C4D5E6F78"


def make_ipa(path, bundle="com.example.app", name="Example", extensions=0, platform="ios"):
    info = {"CFBundleIdentifier": bundle, "CFBundleName": name, "CFBundleDisplayName": name,
            "CFBundleExecutable": "x", "CFBundleShortVersionString": "1.0"}
    if platform == "tvos":
        info |= {"CFBundleSupportedPlatforms": ["AppleTVOS"], "UIDeviceFamily": [3], "DTPlatformName": "appletvos"}
    plist = plistlib.dumps(info)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Payload/Example.app/Info.plist", plist)
        z.writestr("Payload/Example.app/x", b"\0" * 64)
        for i in range(extensions):
            z.writestr(f"Payload/Example.app/PlugIns/E{i}.appex/Info.plist", plist)


class Fresh(unittest.TestCase):
    """Starts every test with an empty data folder."""

    def setUp(self):
        for child in Path(DATA).iterdir():
            shutil.rmtree(child) if child.is_dir() else child.unlink()
        from sideloop import config
        config.update({"RENEW_BEFORE_DAYS": "2", "AUTO_CHECK": "1"})

    def device(self, udid=UDID, name="iPhone"):
        from sideloop import store
        store.add_device(udid, name)
        return udid

    def tv(self, udid=TV_UDID, name="Living Room"):
        from sideloop import store
        store.add_device(udid, name, kind="appletv")
        return udid

    def app(self, bundle="com.example.app", name="Example", extensions=0, platform="ios"):
        from sideloop import store
        src = Path(DATA) / ".upload.ipa"
        make_ipa(src, bundle, name, extensions, platform)
        from sideloop import ipa
        return store.add_app(src, ipa.inspect(src), f"{name}.ipa")
