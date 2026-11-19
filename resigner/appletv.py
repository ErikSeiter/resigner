"""Apple TV support.

An iPhone or iPad is reached through the usb/network muxer and signed by AltServer. An Apple TV isn't: tvOS has no
lockdown service for that. It is paired once with a PIN shown on the TV, after which it is reached directly over
the network with the pairing file that step wrote. `plumesign` (the signing tool of PlumeImpactor, MIT licensed)
does the pairing, the signing and the install. This module is the glue: it finds the TV on the network, runs
plumesign for the pairing, and tells scripts/refresh-tv.sh where the TV is and when its signature runs out.

plumesign keeps everything in $XDG_CONFIG_HOME/PlumeImpactor: accounts.json (the Apple ID sessions),
anisette/<account> (one identity per Apple ID) and pairing_files/<udid>.plist (+ .json with the TV's name).

The command line (python3 -m resigner.appletv ...) is what scripts/refresh-tv.sh uses:
  locate UDID    prints "<ip> <port>" of that Apple TV, exit 1 when it isn't on the network
  profile FILE   prints "<expiry epoch> <certificate sha1>" of a provisioning profile
"""
import datetime
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import mdns, store
from .config import DATA
from .jobs import run_pty

BIN = os.environ.get("PLUMESIGN_BIN", "plumesign")
XDG = Path(os.environ.get("XDG_CONFIG_HOME") or DATA / "xdg")
PAIRING_DIR = XDG / "PlumeImpactor" / "pairing_files"
SERVICE = "_remotepairing._tcp"
SERVICE_PAIRING = "_remotepairing-manual-pairing._tcp"
SCAN_TIME = 3.0
UNKNOWN_FOR = 60  # an announcement that matched no pairing file is looked at again after this long
UDID_OUT = re.compile(r"UDID: `([^`]+)`")

_identified = {}  # (identifier, auth tag) -> (udid or "", when)


def available():
    return shutil.which(BIN) is not None


def env():
    return {**os.environ, "XDG_CONFIG_HOME": str(XDG)}


def _run(argv, timeout):
    try:
        r = subprocess.run(argv, env=env(), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
        return r.stdout + r.stderr
    except (OSError, subprocess.TimeoutExpired):
        return ""


def paired():
    """{udid: {"name", "model"}} for every pairing file plumesign has stored."""
    out = {}
    for f in sorted(PAIRING_DIR.glob("*.plist")) if PAIRING_DIR.is_dir() else []:
        info = {}
        try:
            info = json.loads(f.with_suffix(".json").read_text())
        except (OSError, ValueError):
            pass
        out[f.stem] = {"name": str(info.get("name") or ""), "model": str(info.get("model") or "")}
    return out


def identify(svc, now=None):
    """The UDID of the paired Apple TV behind a _remotepairing announcement, or "" when it is no TV of ours
    (an iPhone announces the same service). plumesign checks the announcement's tag against the pairing files."""
    txt = svc.get("txt") or {}
    key = (txt.get("identifier", ""), txt.get("authTag", ""))
    if not all(key):
        return ""
    now = now or time.time()
    hit = _identified.get(key)
    if hit and (hit[0] or now - hit[1] < UNKNOWN_FOR):
        return hit[0]
    m = UDID_OUT.search(_run([BIN, "check", "find-pairing", "--identifier", key[0], "--auth-tag", key[1]], 20))
    _identified[key] = (m.group(1) if m else "", now)
    return _identified[key][0]


def forget_unknown():
    for key in [k for k, v in _identified.items() if not v[0]]:
        del _identified[key]


def scan_live(udids=None, timeout=SCAN_TIME):
    """The registered Apple TVs that answer on the network right now."""
    tvs = {u.lower(): u for u in (store.device_ids() if udids is None else udids) if store.device_kind(u) == "appletv"}
    if not tvs or not available():
        return []
    info, out = paired(), []
    for svc in mdns.browse([SERVICE], timeout):
        udid = tvs.get(identify(svc).lower())
        if udid and all(d["udid"] != udid for d in out):
            out.append({"udid": udid, "conn": "wifi", "reachable": True, "kind": "appletv",
                        "name": store.device_name(udid), "model": info.get(udid, {}).get("model") or "-",
                        "ios": "-", "ip": svc["ip"], "port": svc["port"]})
    return out


def pairable(timeout=SCAN_TIME):
    """Apple TVs waiting for a PIN (Settings > Remotes and Devices > Remote App and Devices)."""
    seen, out = set(), []
    for svc in mdns.browse([SERVICE_PAIRING], timeout):
        if (svc["ip"], svc["port"]) in seen:
            continue
        seen.add((svc["ip"], svc["port"]))
        out.append({"name": svc["name"] or svc["host"], "ip": svc["ip"], "port": svc["port"]})
    return out


def locate(udid, timeout=SCAN_TIME):
    fixed = os.environ.get("RESIGNER_TV_LOCATE", "").split()  # "<ip> <port>": for tests and debugging
    if len(fixed) == 2:
        return fixed[0], int(fixed[1])
    for d in scan_live([udid], timeout):
        return d["ip"], d["port"]
    return None


def groups_for_new_tv():
    """The groups a new Apple TV belongs in without being asked: empty ones that only hold tvOS apps, which is
    what a group made for Apple TVs looks like."""
    out = []
    for gid in store.group_ids():
        apps = [a for a in store.app_ids() if gid in store.app_groups(a)]
        if not store.group_devices(gid) and apps and all(store.app_platform(a) == "tvos" for a in apps):
            out.append(gid)
    return out


def _stamp():
    return {f.name: f.stat().st_mtime_ns for f in PAIRING_DIR.glob("*.json")} if PAIRING_DIR.is_dir() else {}


def pair(job, ip, port):
    """Pairs with the Apple TV at ip:port, which has to show a PIN. The PIN is typed on the web page."""
    if not available():
        raise RuntimeError("the Apple TV helper (plumesign) isn't installed in this image")
    ip, port = str(ip), str(int(port))
    if not re.fullmatch(r"[0-9A-Fa-f:.]{3,45}", ip):
        raise ValueError("that doesn't look like an address")
    before = _stamp()
    job.say(f"Pairing with the Apple TV at {ip}. Type the PIN it shows on the TV when asked.")
    run_pty(job, ["env", f"XDG_CONFIG_HOME={XDG}", BIN, "pair", "--ip", ip, "--port", port])
    if job.cancelled:
        raise RuntimeError("cancelled")
    if job.rc != 0:
        raise RuntimeError("pairing didn't work: check the PIN, that the Apple TV still shows it, and that it is "
                           "on the same network as this machine")
    after = _stamp()
    new = sorted(n for n, t in after.items() if before.get(n) != t)
    if not new:
        raise RuntimeError("plumesign finished, but no pairing was saved")
    udid = new[-1][:-len(".json")]
    info = paired().get(udid, {})
    name = info.get("name") or "Apple TV"
    forget_unknown()
    groups = groups_for_new_tv() if udid not in store.device_ids() else []
    store.add_device(udid, name, groups, kind="appletv")
    job.say(f"✓ Added {name}. Your tvOS apps will be installed on it.")
    if groups:
        job.say("It is in the group " + ", ".join(store.group_name(g) for g in groups) + " (change that under Settings > Groups).")


def profile_info(path):
    """(expiry as epoch seconds, sha1 of the signing certificate) of a provisioning profile file, or None."""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    try:
        data = plistlib.loads(raw)
    except Exception:
        r = subprocess.run(["openssl", "smime", "-verify", "-noverify", "-inform", "DER"], input=raw,
                           capture_output=True)
        try:
            data = plistlib.loads(r.stdout)
        except Exception:
            return None
    expiry = data.get("ExpirationDate")
    if not isinstance(expiry, datetime.datetime):
        return None
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=datetime.timezone.utc)
    certs = data.get("DeveloperCertificates") or [b""]
    return int(expiry.timestamp()), hashlib.sha1(certs[0]).hexdigest()


def _main(argv):
    cmd, args = (argv[0] if argv else ""), argv[1:]
    if cmd == "locate" and args:
        found = locate(args[0])
        if not found:
            sys.exit(1)
        print(*found)
    elif cmd == "profile" and args:
        info = profile_info(args[0])
        if not info:
            sys.exit(1)
        print(*info)
    else:
        sys.exit("usage: python3 -m resigner.appletv locate UDID|profile FILE")


if __name__ == "__main__":
    _main(sys.argv[1:])
