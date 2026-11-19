"""Anisette servers.

AltServer needs one to sign in, and the free public ones fail now and then (HTTP 401 while provisioning, for
instance). ANISETTE_SERVER may therefore list several, separated by commas or spaces. A run starts with the one
that worked last. When AltServer says the problem is the server itself, and not the Apple ID, that server is set
aside for a while and the next one gets a go. AltServer keeps one identity per server (a file per host), so
switching never disturbs the others.

On disk:  data/state/anisette/last_good   the server that worked last
          data/state/anisette/bad/<key>   exists (touched) while a server is set aside

The command line (python3 -m sideloop.anisette ...) is what scripts/refresh.sh uses:
  order        the servers to try, best first, one per line
  good URL     this one worked
  bad URL      this one failed
  check        reads AltServer's output on stdin, exits 0 if it shows a server problem
"""
import hashlib
import os
import re
import sys
import time

from . import config

DEFAULT = "https://new.ani.sidestore.io"
BAD_FOR = 15 * 60

# AltServer shows its own anisette errors as "Alert: Anisette v3: ..." (an HTTP answer it didn't expect, a failed
# provisioning, ...). -27677 and -80043 are Apple refusing the anisette identity itself. plumesign (Apple TV) says
# "Anisette error: ...", "Anisette not provisioned!", "Provisioning socket error ..." or fails a request to the server's /v3/ address.
# Anything about the Apple ID (password, 2FA, signing limits) is deliberately not here: another server would not
# help and every attempt is another sign-in for Apple to see.
FAIL_RE = re.compile(
    r"Alert: Anisette (v3|server)"
    r"|Anisette v3: [^\n]*(answered HTTP|provisioning failed|timed out|rejects the identity)"
    r"|Anisette server [^\n]*(unreachable|can'?t be reached|could not|couldn'?t|timed out|refused)"
    r"|\((-27677|-80043)\)|anisette data is invalid"
    r"|Anisette not provisioned|Anisette error:|Provisioning socket error|error sending request for url \([^)\n]*/v3/",
    re.I,
)


def servers(raw=None):
    """The configured servers, without duplicates, in the order they were written."""
    raw = os.environ.get("ANISETTE_SERVER", DEFAULT) if raw is None else raw
    out = []
    for s in re.split(r"[,\s]+", raw or ""):
        s = s.strip().rstrip("/")
        if s and s not in out:
            out.append(s)
    return out or [DEFAULT]


def _dir():
    return config.STATE / "anisette"


def _mark(url):
    return _dir() / "bad" / hashlib.sha1(url.encode()).hexdigest()[:16]


def is_bad(url, now=None):
    try:
        return (now or time.time()) - _mark(url).stat().st_mtime < BAD_FOR
    except OSError:
        return False


def last_good():
    try:
        return (_dir() / "last_good").read_text().strip()
    except OSError:
        return ""


def good(url):
    url = url.strip().rstrip("/")
    _dir().mkdir(parents=True, exist_ok=True)
    (_dir() / "last_good").write_text(url + "\n")
    _mark(url).unlink(missing_ok=True)


def bad(url):
    url = url.strip().rstrip("/")
    _mark(url).parent.mkdir(parents=True, exist_ok=True)
    _mark(url).touch()


def order(now=None, raw=None):
    """Servers to try: the last one that worked first, the others as configured, those set aside at the end."""
    names = servers(raw)
    pref = last_good()
    if pref in names:
        names = [pref] + [n for n in names if n != pref]
    return [n for n in names if not is_bad(n, now)] + [n for n in names if is_bad(n, now)]


def failed(text):
    """Whether AltServer's output shows a problem with the anisette server."""
    return bool(FAIL_RE.search(text or ""))


def first_problem(text):
    m = FAIL_RE.search(text or "")
    if not m:
        return ""
    start = text.rfind("\n", 0, m.start()) + 1
    end = text.find("\n", m.end())
    return text[start:end if end >= 0 else len(text)].strip()[:160]


def _main(argv):
    cmd, args = (argv[0] if argv else ""), argv[1:]
    if cmd == "order":
        print("\n".join(order()))
    elif cmd == "good" and args:
        good(args[0])
    elif cmd == "bad" and args:
        bad(args[0])
    elif cmd == "check":
        text = sys.stdin.read().replace("\r", "\n")
        problem = first_problem(text)
        if problem:
            print(problem)
        sys.exit(0 if problem else 1)
    else:
        sys.exit("usage: python3 -m sideloop.anisette order|good URL|bad URL|check")


if __name__ == "__main__":
    _main(sys.argv[1:])
