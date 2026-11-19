"""Fetches an IPA from a web link, for apps that are published as a download (a GitHub release, a vendor's site).

Only a signed-in user can reach this. The answer to the caller is the checked IPA or a short error, never the
body of whatever the link served, so it can't be used to read other services on the network.
"""
import hashlib
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request


class _Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme not in ("http", "https"):
            raise urllib.error.URLError("the link sends you somewhere that isn't a web address")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def filename_of(url):
    name = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1])
    return name if name.lower().endswith(".ipa") else "app.ipa"


def fetch(url, dest, limit, sha256="", timeout=30):
    """Downloads `url` to `dest`, which only appears once the download is complete (and matches `sha256`, when one
    is given). Returns (file name from the link, SHA-256 of the file). Raises ValueError with a message to show."""
    url = url.strip()
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Enter a web link that starts with https://")
    want = sha256.strip().lower()
    if want and not re.fullmatch(r"[0-9a-f]{64}", want):
        raise ValueError("A checksum is 64 characters, 0-9 and a-f")
    part = dest.with_name(dest.name + ".download")
    digest, size = hashlib.sha256(), 0
    try:
        with urllib.request.build_opener(_Redirects).open(urllib.request.Request(url, headers={"User-Agent": "Resigner"}), timeout=timeout) as r:
            if int(r.headers.get("Content-Length") or 0) > limit:
                raise ValueError("That file is too large")
            fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                while chunk := r.read(1 << 20):
                    size += len(chunk)
                    if size > limit:
                        raise ValueError("That file is too large")
                    digest.update(chunk)
                    f.write(chunk)
        if not size:
            raise ValueError("The link gave an empty file")
        if want and digest.hexdigest() != want:
            raise ValueError(f"The download doesn't match the checksum you gave (it is {digest.hexdigest()})")
        os.replace(part, dest)
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400:
            raise ValueError("The link sends you somewhere that isn't a web address") from None
        raise ValueError(f"The server answered {e.code}") from None
    except urllib.error.URLError as e:
        raise ValueError(f"Couldn't reach the link: {e.reason}") from None
    except (socket.timeout, OSError) as e:
        raise ValueError(f"The download failed: {e}") from None
    finally:
        part.unlink(missing_ok=True)
    return filename_of(url), digest.hexdigest()
