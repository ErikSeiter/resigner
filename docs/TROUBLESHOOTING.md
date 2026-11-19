# Troubleshooting

These are the problems we ran into while getting Resigner to sign in after Apple's October 2026 changes, in the order you are likely to meet them. Where we know the cause we say so; where we only have a good guess we say that too.

Where to look first: the **Activity** tab in the web UI, or `data/state/refresh.log`.

## Quick table

| You see | What it means | What to do |
| --- | --- | --- |
| `-22411`, `-80045`, `-80046` when signing in | Your AltServer is too old for Apple's October 2026 sign-in | Build `ALTSERVER_TAG` **`ng-2026-11-05` or newer** (this fork's default) and redeploy. Older tags (for example `ng-2026-09-13`) fail |
| `-80043` | The shared "Mac identity" the anisette server hands out was blocked by Apple. We saw it with the old shared-identity mode on three public servers | Use the v3 mode this fork builds: AltServer provisions **its own** identity. Delete `data/xdg/AltServer/anisette-v3/` to force a fresh one |
| `HTTP 401` with `-27677` ("The provided anisette data is invalid") while provisioning | The public anisette server's provisioning step was rejected by Apple. It was **intermittent for us**: the same setup failed several times, then worked on later tries | Resigner tries the next server in `ANISETTE_SERVER` by itself and remembers which one worked last, so you normally only see this in the Activity log (`Anisette server … failed; trying …`). If every server failed it says so: wait a few minutes. To add a server, put it in `ANISETTE_SERVER` separated by a comma (`https://new.ani.sidestore.io,https://mac-ani.jkcoxson.com,https://ani3.stikstore.app`). Each server gets its own identity file in `data/xdg/AltServer/anisette-v3/`; delete a file only to force a fresh identity for that server |
| `Error while SRP initial authentication : Unexpected response from IDMS Server (-1)` | Apple's sign-in server gave an answer AltServer couldn't use. We saw it **once**, from the automatic re-signer after about ten failed attempts in a row. We haven't confirmed the cause (Apple throttling repeated attempts is our guess) | Don't keep retrying. Switch **Automatic** off, wait a while, then try once from the web UI |
| `Apple is asking for a 2FA code; sign it from the web UI once` | The background re-signer can't type a code | Open the web UI, press **Install** (or **Re-sign**) on the app and enter the code there |
| `no Apple ID can sign this app` | Every Apple ID is switched off for new apps, or the only ones left have never signed anything here. A background run only uses an Apple ID that has signed something before, because the first sign-in needs a 2FA code | Switch an Apple ID on, or press **Install** on the app in the web UI so you can enter the code |
| `Apple says every available Apple ID is out of signings` | Apple refused with a limit message for each Apple ID that was tried (3 apps per device, 10 App IDs per week) | Add another Apple ID in Settings, remove an app you don't use, or wait: Resigner tries a limited Apple ID again after a day. An app that is already installed is never moved to another Apple ID |
| The same app shows up twice, or iOS asks to trust a second developer | A second Apple ID signed it or another app (each Apple ID is its own developer) | Trust the extra developer under VPN & Device Management. If you removed an Apple ID that had signed an app, that app is installed again with another one at the next run |
| An *Apple ID sign-in requested* prompt on your iPhone that you didn't cause | A sign-in attempt that reached Apple. It's usually the **automatic re-signer** retrying | Switch **Automatic** off while troubleshooting; switch it back on once an install works |
| Install runs to the end, then `Failed to set app extension placeholders` (`IXErrorDomain Code=2`) | The app has extensions (share sheet, widgets, ...) and a free Apple ID can't sign those for iOS | Remove the app, add the IPA again and keep **Remove App Extensions** on. Or strip it by hand, see [below](#strip-extensions-by-hand) |
| `Request canceled by user` at *Fetching certificate...* | Probably Apple's certificate limit for free accounts (the prompt gets cancelled because nobody can answer it). It was **intermittent for us**: it succeeded on a retry | Retry. If it keeps happening, wait a while and retry, or use another throwaway Apple ID (a free Apple ID can only hold a few signing certificates at a time) |
| `Failed to install DeveloperDiskImage.dmg` | Shown as an error line by the AltServer version we build, but it did **not** stop an install from completing | Ignore it unless the install itself fails right after |
| `timeout: the monitored command dumped core` after `InstallComplete` | AltServer crashes while exiting, **after** the install finished | Harmless. Check the app on the phone |
| *Untrusted Developer* when opening the app | You haven't trusted the profile yet | Settings → General → VPN & Device Management → your Apple ID → Trust |
| The app does not open at all / "Developer Mode required" | Developer Mode is off | Settings → Privacy & Security → Developer Mode → on, restart |
| *"The Apple sign-in helper isn't running"* in the UI | The anisette server can't be reached | Check internet access from the NAS and `ANISETTE_SERVER`. This fork treats any HTTP answer (even 404) as "up" |
| `no signature on record yet, doing the first install` right after you installed by hand | Resigner doesn't know about an install you made from a shell | Press **Check Everything** in the web UI (or run `refresh.sh --recheck`) while the iPhone is **unlocked and on the home Wi-Fi**. It reads the profile from the iPhone and records the expiry, without contacting Apple. Until then the automatic re-signer keeps trying a "first install", so switch **Automatic** off first |

## Apple TV

Apple TV signing is a separate path from the iPhone one (plumesign instead of AltServer), so an Apple TV problem never stops an iPhone or iPad refresh.

| You see | What it means | What to do |
| --- | --- | --- |
| *The Apple TV helper isn't installed in this image* (no **Pair Apple TV…** button) | The Docker build couldn't download plumesign or its SHA-256 didn't match, so it built the image without it on purpose | Redeploy the project (check the NAS can reach github.com). The build log says `plumesign installed` or `WARNING: plumesign could not be installed` |
| **Pair Apple TV…** finds nothing | The TV isn't showing a PIN, or it isn't on the same network | Open Settings → Remotes and Devices → Remote App and Devices on the TV and look again. Same subnet, no guest network or client isolation; Resigner needs `network_mode: host` |
| Pairing fails after the PIN | Wrong PIN, or the PIN timed out | Press **Pair Apple TV…** again and use the PIN that is on the screen now |
| The Apple TV shows *Away* | It is off, asleep, or Resigner can't see it on the network (mDNS) | Wake it. Pairing can also be gone after a tvOS update: pair again. A re-sign waits until the TV is back |
| `Account '…' not found`, `Authentication extra step required` in the log | The Apple ID's session for the Apple TV is missing or expired | Press **Install** on the app in the web page and enter the 2FA code; background runs can't |
| *The first install on an Apple TV needs a verification code* | A sign-in for the iPhone doesn't count for the Apple TV | Same: start the install from the web page once |
| `skipped: an Apple TV only takes tvOS apps` / `a tvOS app can't go on an iPhone or iPad` | An app of the other kind was pointed at the device | Harmless. Resigner only offers each app to devices that fit it |
| The Apple TV asks to trust the developer, or the app disappears after a week | The signature ran out, or another Apple ID signed it | Check that **Automatic** is on and that the TV was on and awake on the network during the last days; press **Re-sign** to do it now |

## Add from a link

Pasting a link under **Add App…** makes the NAS download the file itself. The link has to be a direct download of the `.ipa` (a GitHub *release* page is not; its *asset* link is). Only `http(s)` links work.

| You see | What it means | What to do |
| --- | --- | --- |
| `The server answered 404` (or 403) | The link is wrong, has expired or needs a login | Copy the download link again from the site |
| `Couldn't reach the link: …` | The NAS can't reach that site (DNS, a firewall, no internet) | Check internet access from the NAS; open the link from another device to see if it works at all |
| `not an IPA (an IPA is a zip file)` | The link gave something else, often a web page | Use the link of the `.ipa` file itself |
| `That file is too large` | Over the 4 GB limit | Use a smaller build |

## Run AltServer by hand and see the real error

The web UI hides most of AltServer's output. To see everything, open **Docker → Container → `resigner` → Terminal → `/bin/bash`** and run (the UDID comes from `idevice_id -l`; replace `<bundle-id>`):

```bash
(set -a; . /data/config.env; set +a
 HOME=/data/.altserver ALTSERVER_ANISETTE_SERVER="$ANISETTE_SERVER" ALTSERVER_KEEP_PROFILES=1 \
 nohup timeout 300 /usr/local/bin/AltServer -d -u <UDID> -a "$APPLE_ID" -p "$APPLE_PASSWORD" \
   /data/apps/<bundle-id>/app.ipa </dev/null >/tmp/run.txt 2>&1 &)
```

With several Apple IDs, use the one in question (`ls /data/accounts`; the first one is also in `config.env`) and its certificate folder: `cd /data/accounts/<id>/work` (the first one keeps using `/data`). All accounts share `HOME=/data/.altserver` and the anisette identity. The refresh log now says which Apple ID signed each run.

Read it while it runs, with your Apple ID hidden so you can share the log:

```bash
(. /data/config.env; sed "s/$APPLE_ID/<id>/g" /tmp/run.txt \
  | grep -avE '^[0-9a-f]{4}: |^ *$|afc\.c|service\.c|idevice\.c' | tail -n 30 | cut -c1-170)
```

Delete `/tmp/run.txt` afterwards.

Tips for the web terminal: it drops long pastes, so keep commands short; run long jobs with `nohup ... &` and read the file; if the terminal disconnects, open a new session and wait for the green dot.

A run started like this does a real sign-in, so it can trigger a 2FA prompt, and only the web UI can take the code. Sign in from the web UI once first, then use the shell for diagnosing.

## Strip extensions by hand

Same thing the **Remove App Extensions** switch does. In the container shell:

```bash
python3 - <<'EOF'
import sys; sys.path.insert(0, "/opt")
from resigner import ipa
d = "/data/apps/<bundle-id>/"
import shutil; shutil.copy(d + "app.ipa", d + "app.orig.ipa")
print(ipa.strip_extensions(d + "app.orig.ipa", d + "app.ipa"))
EOF
```

To go back: copy `app.orig.ipa` over `app.ipa`.

## When Apple changes things again

This setup depends on three third-party pieces that Apple's changes can break: AltServer-Linux, the anisette server, and Apple's own sign-in. When sign-in suddenly fails with a new error code:

1. Look for a newer tag at [AltServer-Linux](https://github.com/jaakkopalvaila/AltServer-Linux/tags), set `ALTSERVER_TAG` and redeploy. Check that the two patches in `altserver/` still apply: `git apply --check altserver/*.patch` inside a checkout of that tag with submodules.
2. Try another anisette server.
3. Open an issue on [AltServer-Linux](https://github.com/jaakkopalvaila/AltServer-Linux/issues), including the (masked) output of the manual run above.

If none of that works, a one-time install from a Mac or Windows PC with AltStore Classic or Sideloadly is the fallback; the 7-day signature then has to be renewed the same way.
