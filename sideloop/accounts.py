"""Apple ID accounts.

Several free Apple IDs can be added in a priority order. A new app (or an app on a new device) is signed with
the first account that still has room; Sideloop remembers which account signed each app on each device and always
re-signs it with that same account, because a different Apple ID means a different team and App ID, and iOS would
treat the result as another app.

On disk, per account:  data/accounts/<id>/account.env   credentials, priority, certificate folder for AltServer
                       data/accounts/<id>/ledger        one line per newly registered app: time, bundle id, App IDs
                       data/accounts/<id>/verified      exists once the account has signed something here
                       data/accounts/<id>/exhausted     touched when Apple said this account is out of signings
and per installed app:  data/apps/<app>/state/<udid>/account   the account that signed it

The command line (python3 -m sideloop.accounts ...) is what scripts/refresh.sh uses.
"""
import os
import re
import shutil
import sys
import time

from . import config, ipa, store
from .config import ACCOUNTS, APPS, DATA, read_env, write_env

KEYS = ["APPLE_ID", "APPLE_PASSWORD", "PRIORITY", "ENABLED", "WORK_DIR", "HOME_DIR", "LEGACY", "ADDED"]
DEFAULTS = {"ENABLED": "1"}

# AltServer's anisette identity (the "Mac" Apple sees) belongs to this install, not to an Apple ID, exactly like one
# Mac signed in with several Apple IDs. So every account shares one HOME and only the certificate cache
# (AltServerData in the working folder) is per account. A separate HOME would only mean a new provisioning
# handshake per account, and that is the step the public anisette servers sometimes fail.
SHARED_HOME = str(DATA / ".altserver")

APPS_PER_DEVICE = 3
APP_IDS_PER_WEEK = 10
WEEK = 7 * 86400
EXHAUSTED_FOR = 24 * 3600

# Apple's wording for "this free account has used up its signings". It isn't documented, so this is deliberately
# generous; a false positive only means the next account is tried.
LIMIT_RE = re.compile(
    r"maximum (number of )?(app ?ids?|apps|applications|certificates?|identifiers?)"
    r"|(app ?ids?|identifiers?|certificates?)[^\n]{0,60}(limit|maximum|too many)"
    r"|(limit|maximum)[^\n]{0,60}(app ?ids?|identifiers?|certificates?)"
    r"|too many (app ?ids?|apps|certificates?)"
    r"|MaximumAppIDLimit|reached (its|the|your) (app )?limit|limit (has been |was )?reached",
    re.I,
)


def _dir(aid):
    return ACCOUNTS / aid


def _file(aid):
    return _dir(aid) / "account.env"


def slug(email):
    return re.sub(r"[^a-z0-9]+", "-", email.strip().lower()).strip("-")[:60] or "account"


def meta(aid):
    return read_env(_file(aid), KEYS, DEFAULTS)


def _write(aid, m):
    _dir(aid).mkdir(parents=True, exist_ok=True, mode=0o700)
    write_env(_file(aid), {k: m.get(k, "") for k in KEYS})


def ids():
    """Account ids, highest priority first."""
    if not ACCOUNTS.is_dir():
        return []
    found = [d.name for d in ACCOUNTS.iterdir() if (d / "account.env").exists()]
    def order(a):
        p = meta(a)["PRIORITY"]
        return (int(p) if p.isdigit() else 10**6, a)
    return sorted(found, key=order)


def _renumber(order):
    for n, aid in enumerate(order, 1):
        m = meta(aid)
        if m["PRIORITY"] != str(n):
            m["PRIORITY"] = str(n)
            _write(aid, m)


def configured():
    return any(meta(a)["APPLE_ID"] and meta(a)["APPLE_PASSWORD"] for a in ids())


def save(apple_id, password=None):
    apple_id = str(apple_id).strip()
    if "@" not in apple_id:
        raise ValueError("enter the Apple ID's email address")
    aid = slug(apple_id)
    known = ids()
    m = meta(aid) if aid in known else {
        "PRIORITY": str(len(known) + 1), "ENABLED": "1", "ADDED": str(int(time.time())),
        "WORK_DIR": str(_dir(aid) / "work"), "HOME_DIR": SHARED_HOME,
    }
    if password:
        m["APPLE_PASSWORD"] = str(password)
    elif not m.get("APPLE_PASSWORD"):
        raise ValueError("enter the password")
    m["APPLE_ID"] = apple_id
    _write(aid, m)
    if m.get("LEGACY") == "1":
        # an older Sideloop reads these from config.env; keep them right in case the image is rolled back
        config.update({"APPLE_ID": apple_id, "APPLE_PASSWORD": m["APPLE_PASSWORD"]})
    return aid


def remove(aid):
    """Returns how many installed apps were signed by this account; they'll be re-signed with another one."""
    if aid not in ids():
        raise ValueError("no such Apple ID")
    orphaned = len(pinned(aid))
    shutil.rmtree(_dir(aid), ignore_errors=True)
    _renumber(ids())
    return orphaned


def move(aid, direction):
    order = ids()
    if aid not in order:
        raise ValueError("no such Apple ID")
    i = order.index(aid)
    j = i - 1 if direction == "up" else i + 1
    if 0 <= j < len(order):
        order[i], order[j] = order[j], order[i]
    _renumber(order)


def set_enabled(aid, enabled):
    if aid not in ids():
        raise ValueError("no such Apple ID")
    m = meta(aid)
    m["ENABLED"] = "1" if enabled else "0"
    _write(aid, m)


def migrate():
    """First start after an update: the single Apple ID from config.env becomes the first account, and
    everything that is already installed is pinned to it (its signing certificate lives in the old folders)."""
    if not ids():
        cfg = config.read()
        if not (cfg["APPLE_ID"] and cfg["APPLE_PASSWORD"]):
            return
        aid = slug(cfg["APPLE_ID"])
        _write(aid, {"APPLE_ID": cfg["APPLE_ID"], "APPLE_PASSWORD": cfg["APPLE_PASSWORD"], "PRIORITY": "1",
                     "ENABLED": "1", "WORK_DIR": str(DATA), "HOME_DIR": SHARED_HOME,
                     "LEGACY": "1", "ADDED": str(int(time.time()))})
    legacy = next((a for a in ids() if meta(a)["LEGACY"] == "1"), None)
    if not legacy:
        return
    signed_before = (DATA / "AltServerData").exists()  # AltServer's certificate cache: it has signed in here before
    for state in APPS.glob("*/state/*"):
        if (state / "expiry").exists() and not (state / "account").exists():
            (state / "account").write_text(legacy + "\n")
            signed_before = True
    if signed_before:
        (_dir(legacy) / "verified").touch()


def pin_of(app_id, udid):
    try:
        return (APPS / app_id / "state" / udid / "account").read_text().strip() or None
    except OSError:
        return None


def pinned(aid):
    """(app id, device) pairs this account has signed."""
    return [(s.parent.parent.name, s.name) for s in sorted(APPS.glob("*/state/*"))
            if (s / "account").exists() and (s / "account").read_text().strip() == aid]


def _ledger(aid):
    out = []
    try:
        for line in (_dir(aid) / "ledger").read_text().splitlines():
            t, _, rest = line.partition("\t")
            bundle, _, n = rest.partition("\t")
            if t.isdigit():
                out.append((int(t), bundle, int(n or 1)))
    except OSError:
        pass
    return out


def app_ids_this_week(aid, now=None):
    now = now or time.time()
    seen = {}
    for t, bundle, n in _ledger(aid):
        if now - t < WEEK:
            seen[bundle] = n
    return sum(seen.values())


def is_exhausted(aid, now=None):
    try:
        return (now or time.time()) - (_dir(aid) / "exhausted").stat().st_mtime < EXHAUSTED_FOR
    except OSError:
        return False


def _app_ids_needed(app_id):
    info = ipa.cached(APPS / app_id) or {}
    return 1 + int(info.get("extension_count") or 0)


def room(aid, app_id, udid, now=None):
    """Whether Sideloop's own records say this account can take one more app on this device. Only a hint:
    Apple has the final say, and the account may be used by other tools too."""
    if is_exhausted(aid, now):
        return False
    here = sum(1 for a in store.app_ids() if a != app_id and pin_of(a, udid) == aid
               and (APPS / a / "state" / udid / "expiry").exists())
    if here >= APPS_PER_DEVICE:
        return False
    bundle = store.app_meta(app_id)["BUNDLE_ID"]
    already = any(b == bundle and (now or time.time()) - t < WEEK for t, b, _ in _ledger(aid))
    return already or app_ids_this_week(aid, now) + _app_ids_needed(app_id) <= APP_IDS_PER_WEEK


def pick(app_id, udid, interactive=False, now=None):
    """Accounts to try for this app on this device, best first.

    An installed app keeps the account that signed it. A new install tries the enabled accounts in priority order,
    those with room first. A background run skips an account that was never used here, because its first sign-in
    needs a 2FA code that only the web page can take."""
    pin = pin_of(app_id, udid)
    if pin and pin in ids():
        return [pin]
    usable = [a for a in ids() if meta(a)["ENABLED"] == "1" and meta(a)["APPLE_ID"] and meta(a)["APPLE_PASSWORD"]]
    if not interactive:
        usable = [a for a in usable if (_dir(a) / "verified").exists()]
    return [a for a in usable if room(a, app_id, udid, now)] + [a for a in usable if not room(a, app_id, udid, now)]


def record(app_id, udid, aid):
    """Called after a successful install: pin the app to the account and log the App IDs it used."""
    first = pin_of(app_id, udid) != aid
    state = APPS / app_id / "state" / udid
    state.mkdir(parents=True, exist_ok=True)
    (state / "account").write_text(aid + "\n")
    if first:
        bundle = store.app_meta(app_id)["BUNDLE_ID"]
        with open(_dir(aid) / "ledger", "a") as f:
            f.write(f"{int(time.time())}\t{bundle}\t{_app_ids_needed(app_id)}\n")
    (_dir(aid) / "verified").touch()
    (_dir(aid) / "exhausted").unlink(missing_ok=True)


def mark_exhausted(aid):
    if aid in ids():
        (_dir(aid) / "exhausted").touch()


def view(aid, now=None):
    m = meta(aid)
    return {
        "id": aid, "apple_id": m["APPLE_ID"], "priority": int(m["PRIORITY"] or 0), "enabled": m["ENABLED"] == "1",
        "verified": (_dir(aid) / "verified").exists(), "exhausted": is_exhausted(aid, now),
        "apps": len(pinned(aid)), "app_ids_week": app_ids_this_week(aid, now),
    }


def _main(argv):
    cmd, args = (argv[0] if argv else ""), argv[1:]
    opt = {a[2:]: args[i + 1] for i, a in enumerate(args) if a.startswith("--") and i + 1 < len(args)}
    flags = {a[2:] for a in args if a.startswith("--")}
    if cmd == "pick":
        print("\n".join(pick(opt["app"], opt["device"], "interactive" in flags)))
    elif cmd == "record":
        record(opt["app"], opt["device"], opt["account"])
    elif cmd == "exhausted":
        mark_exhausted(opt["account"])
    elif cmd == "limit":
        sys.exit(0 if LIMIT_RE.search(sys.stdin.read()) else 1)
    else:
        sys.exit("usage: python3 -m sideloop.accounts pick|record|exhausted|limit")


if __name__ == "__main__":
    _main(sys.argv[1:])
