import os
import re
import shutil
import time

from . import ipa
from .config import APPS, DEVICES, GROUPS, read_env, write_env

APP_KEYS = ["BUNDLE_ID", "APP_NAME", "APP_VERSION", "IPA_SOURCE", "ADDED", "DEVICES", "APP_GROUPS", "APP_PLATFORM"]
DEVICE_KEYS = ["DEVICE_NAME", "ADDED", "DEVICE_KIND"]
GROUP_KEYS = ["GROUP_NAME", "DEVICES", "ADDED"]
UDID_RE = re.compile(r"^[0-9A-Fa-f-]{20,64}$")


def read_state(folder):
    def rd(name):
        try:
            return (folder / name).read_text().strip()
        except OSError:
            return ""
    lr = rd("last_run").split("\t")
    last_run = None
    if len(lr) >= 2 and lr[0].isdigit():
        last_run = {"t": int(lr[0]), "result": lr[1], "message": lr[2] if len(lr) > 2 else ""}
    return {"expiry": int(rd("expiry") or 0) or None, "last_run": last_run, "account": rd("account") or None}


def device_ids():
    return sorted(f.stem for f in DEVICES.glob("*.env")) if DEVICES.is_dir() else []


def resolve_device(udid):
    return {u.lower(): u for u in device_ids()}.get(str(udid).lower())


def device_name(udid):
    return read_env(DEVICES / f"{udid}.env", DEVICE_KEYS)["DEVICE_NAME"] or udid


def device_kind(udid):
    """"appletv" for an Apple TV (signed and installed by plumesign), "ios" for an iPhone or iPad (AltServer)."""
    return "appletv" if read_env(DEVICES / f"{udid}.env", DEVICE_KEYS)["DEVICE_KIND"] == "appletv" else "ios"


def app_platform(app_id, meta=None):
    return "tvos" if (meta or app_meta(app_id))["APP_PLATFORM"] == "tvos" else "ios"


def compatible(app_id, udid, meta=None):
    """An Apple TV only takes tvOS apps, and an iPhone or iPad only iOS apps."""
    return (device_kind(udid) == "appletv") == (app_platform(app_id, meta) == "tvos")


def add_device(udid, name, groups=None, kind="ios"):
    """Registers a device. It gets every app that doesn't follow groups and fits it, plus the apps of `groups`."""
    if not UDID_RE.match(udid):
        raise ValueError("that doesn't look like a device ID")
    new = udid not in device_ids()
    old = read_env(DEVICES / f"{udid}.env", DEVICE_KEYS)
    write_env(DEVICES / f"{udid}.env", {"DEVICE_NAME": name or udid, "ADDED": old["ADDED"] or int(time.time()),
                                         "DEVICE_KIND": "appletv" if kind == "appletv" else "ios"})
    if new:
        for app_id in app_ids():
            if app_groups(app_id) or not compatible(app_id, udid):
                continue
            devs = app_devices(app_id)
            if udid not in devs:
                set_app_devices(app_id, devs + [udid])
    for gid in groups or []:
        if gid in group_ids():
            members = group_devices(gid)
            if udid not in members:
                set_group_devices(gid, members + [udid])
    sync_apps()


def remove_device(udid):
    (DEVICES / f"{udid}.env").unlink(missing_ok=True)
    for gid in group_ids():
        meta = group_meta(gid)
        meta["DEVICES"] = " ".join(u for u in meta["DEVICES"].split() if u.lower() != udid.lower())
        write_env(GROUPS / f"{gid}.env", meta)
    for app_id in app_ids():
        set_app_devices(app_id, [u for u in app_meta(app_id)["DEVICES"].split() if u.lower() != udid.lower()])
        shutil.rmtree(APPS / app_id / "state" / udid, ignore_errors=True)


def group_ids():
    return sorted(f.stem for f in GROUPS.glob("*.env")) if GROUPS.is_dir() else []


def group_meta(gid):
    return read_env(GROUPS / f"{gid}.env", GROUP_KEYS)


def group_devices(gid):
    known = {u.lower(): u for u in device_ids()}
    return [known[u.lower()] for u in group_meta(gid)["DEVICES"].split() if u.lower() in known]


def group_name(gid):
    return group_meta(gid)["GROUP_NAME"] or gid


def set_group_devices(gid, udids):
    meta = group_meta(gid)
    meta["DEVICES"] = " ".join(udids)
    write_env(GROUPS / f"{gid}.env", meta)
    sync_apps()


def add_group(name):
    name = " ".join(str(name).split())[:40]
    if not name:
        raise ValueError("give the group a name")
    if any(group_name(g).lower() == name.lower() for g in group_ids()):
        raise ValueError("there is already a group with that name")
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or "group"
    gid, n = base, 1
    while gid in group_ids():
        n += 1
        gid = f"{base}-{n}"
    write_env(GROUPS / f"{gid}.env", {"GROUP_NAME": name, "DEVICES": "", "ADDED": int(time.time())})
    return gid


def rename_group(gid, name):
    name = " ".join(str(name).split())[:40]
    if not name:
        raise ValueError("give the group a name")
    if any(g != gid and group_name(g).lower() == name.lower() for g in group_ids()):
        raise ValueError("there is already a group with that name")
    meta = group_meta(gid)
    meta["GROUP_NAME"] = name
    write_env(GROUPS / f"{gid}.env", meta)


def remove_group(gid):
    """Deleting a group leaves its apps on the devices they reach now; apps that only followed this group
    stop following groups and keep their current device list."""
    (GROUPS / f"{gid}.env").unlink(missing_ok=True)
    for app_id in app_ids():
        meta = app_meta(app_id)
        if gid in meta["APP_GROUPS"].split():
            meta["APP_GROUPS"] = " ".join(g for g in meta["APP_GROUPS"].split() if g != gid)
            write_env(APPS / app_id / "meta.env", meta)
    sync_apps()


def app_groups(app_id, meta=None):
    known = set(group_ids())
    return [g for g in (meta or app_meta(app_id))["APP_GROUPS"].split() if g in known]


def set_app_groups(app_id, gids):
    known = group_ids()
    meta = app_meta(app_id)
    meta["APP_GROUPS"] = " ".join(g for g in known if g in set(gids))
    write_env(APPS / app_id / "meta.env", meta)
    sync_app(app_id)


def sync_app(app_id):
    """An app that follows groups reaches exactly the devices of those groups."""
    groups = app_groups(app_id)
    if not groups:
        return
    members = {u.lower() for g in groups for u in group_devices(g)}
    set_app_devices(app_id, [u for u in device_ids() if u.lower() in members])  # set_app_devices drops the unfit ones


def sync_apps():
    for app_id in app_ids():
        sync_app(app_id)


def migrate_platforms():
    """An app added before its platform was read from the binary (or before Apple TV support) may be filed under
    the wrong one. Looks at every IPA again and moves an app to the devices that fit it."""
    for app_id in app_ids():
        info = ipa.cached(APPS / app_id)
        if not info or "error" in info:
            continue
        meta = app_meta(app_id)
        platform = "tvos" if info.get("platform") == "tvos" else "ios"
        if meta["APP_PLATFORM"] == platform:
            continue
        meta["APP_PLATFORM"] = platform
        write_env(APPS / app_id / "meta.env", meta)
        if app_groups(app_id):
            sync_app(app_id)
        else:  # not tied to a group: it follows every device that fits
            set_app_devices(app_id, device_ids())


def app_ids():
    if not APPS.is_dir():
        return []
    return sorted(d.name for d in APPS.iterdir() if (d / "meta.env").exists() and (d / "app.ipa").exists())


def app_meta(app_id):
    return read_env(APPS / app_id / "meta.env", APP_KEYS)


def app_devices(app_id, meta=None):
    meta = meta or app_meta(app_id)
    known = {u.lower(): u for u in device_ids()}
    return [known[u.lower()] for u in meta["DEVICES"].split()
            if u.lower() in known and compatible(app_id, known[u.lower()], meta)]


def set_app_devices(app_id, udids):
    meta = app_meta(app_id)
    meta["DEVICES"] = " ".join(u for u in udids if compatible(app_id, u, meta))
    write_env(APPS / app_id / "meta.env", meta)


def app_view(app_id):
    d, meta = APPS / app_id, app_meta(app_id)
    return {
        "id": app_id,
        "bundle_id": meta["BUNDLE_ID"],
        "name": meta["APP_NAME"] or app_id,
        "version": meta["APP_VERSION"],
        "platform": app_platform(app_id, meta),
        "info": ipa.cached(d),
        "groups": app_groups(app_id, meta),
        "targets": [{"udid": u, "name": device_name(u), "kind": device_kind(u), "signature": read_state(d / "state" / u)}
                    for u in app_devices(app_id, meta)],
    }


def add_app(src, info, filename, original=None):
    app_id = re.sub(r"[^A-Za-z0-9._-]", "_", info["bundle_id"])[:120] or "app"
    d = APPS / app_id
    (d / "state").mkdir(parents=True, exist_ok=True)
    os.replace(src, d / "app.ipa")
    if original:
        os.replace(original, d / "app.orig.ipa")
    else:
        (d / "app.orig.ipa").unlink(missing_ok=True)
    old = app_meta(app_id)
    platform = "tvos" if info.get("platform") == "tvos" else "ios"
    meta = {
        "BUNDLE_ID": info["bundle_id"],
        "APP_NAME": info.get("name", ""),
        "APP_VERSION": info.get("version", ""),
        "IPA_SOURCE": filename[:200],
        "ADDED": old["ADDED"] or int(time.time()),
        "DEVICES": old["DEVICES"] if old["ADDED"] else " ".join(device_ids()),
        "APP_GROUPS": old["APP_GROUPS"],
        "APP_PLATFORM": platform,
    }
    meta["DEVICES"] = " ".join(u for u in meta["DEVICES"].split() if compatible(app_id, u, meta))
    write_env(d / "meta.env", meta)
    sync_app(app_id)
    (d / "info.json").unlink(missing_ok=True)
    return app_id


def remove_app(app_id):
    shutil.rmtree(APPS / app_id)
