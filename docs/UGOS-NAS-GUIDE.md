# Sideloop on a UGREEN NAS (UGOS Pro)

Install and automatically re-sign iPhone apps (IPAs) from your NAS, with a free Apple ID, and without a Mac or PC. Written and tested on a **UGREEN NASync DXP4800** running **UGOS Pro** with the Docker app. The same steps work on other x86 NAS systems that have a Docker GUI; only the menu names differ.

**Time:** about 30 minutes, most of it waiting for the first build.

## What you need

| | |
| --- | --- |
| NAS | UGOS Pro with the **Docker** app installed (App Center). Intel/AMD CPU. For an ARM NAS see the note in [`nas/docker-compose.ugos.yml`](../nas/docker-compose.ugos.yml) |
| Apple ID | A **free, throwaway** Apple ID with two-factor authentication on. Don't use your main one: its password is stored on the NAS (see [Security](#security)) |
| iPhone / iPad | Plus a USB cable for the one-time pairing |
| IPA | An **unencrypted (decrypted)** `.ipa`. Apps straight from the App Store are FairPlay-encrypted and won't launch; Sideloop warns you about that when you add the file |

## How it works (60 seconds)

A free Apple ID can only sign apps for **7 days**. Sideloop runs on the NAS, signs the IPA with your Apple ID and installs it on the iPhone over your home Wi-Fi. A background check repeats this before the signature runs out, so the app keeps working and **keeps its data**.

Signing needs an "anisette" helper (it makes your NAS look like a Mac to Apple). Since Apple's October 2026 changes the NAS provisions its own identity through a public anisette server; only that handshake goes through it, never your Apple ID or password. Three servers are listed in `ANISETTE_SERVER` and Sideloop moves on to the next one by itself when one of them fails.

## 1. Create the data folder

1. Open **Docker** once so UGOS creates the shared folder `docker`.
2. In **Files**, open the shared folder `docker` and create a folder named `sideloop-app`.

Everything Sideloop stores (settings, Apple ID, your IPAs, the anisette identity) ends up in `sideloop-app/data`. If your storage pool is not `volume1`, remember that for step 2.

## 2. Create the Docker project

1. Open [`nas/docker-compose.ugos.yml`](../nas/docker-compose.ugos.yml) and copy the whole file.
2. In **Docker → Project → Create**:
   - **Project name:** `sideloop`
   - **Path:** the `docker/sideloop-app` folder from step 1
   - **Source:** *Create docker-compose.yml* and paste the file
3. Before you confirm, change `TZ` to your time zone. If your pool isn't `volume1`, fix the three `/volume1/...` paths.
4. Confirm. The first start **builds** AltServer from source, which takes **5 to 15 minutes**. The project shows `1/1 running` when it's done.

> **Port 8080 busy?** The web UI listens on 8080 on the NAS itself. If something else uses it, add `UI_PORT: "8090"` (or any free port) under `environment:` and use that port below.

## 3. Open the web UI

Go to `http://<nas-ip>:8080` in a browser on your home network and **set a password** (it protects the page, and the page controls your Apple ID).

## 4. Pair the iPhone

1. Plug the iPhone into a **USB port on the NAS** and unlock it.
2. In Sideloop press **Pair New Device…**. When the iPhone asks *Trust This Computer?* tap **Trust** and enter your passcode.
3. The device shows up; add it. You can unplug the cable afterwards. From now on the NAS reaches the phone over Wi-Fi.

## 5. Enter your Apple ID

In the web UI add the Apple ID and its **regular password**. App-specific passwords don't work.

You can add **more than one** (Settings → Apple IDs). The first one that has room signs a new app; when it runs out, the next one takes over. An installed app always stays with the Apple ID that signed it, because another Apple ID would be another developer and iOS would treat the result as a different app. Each Apple ID has to be **trusted once** on the device (Settings → General → VPN & Device Management), and its first install needs a 2FA code that you enter in the web page.

## 5b. Groups (optional)

Settings → Groups lets you make sets of devices, for example *Family iPads*. Tie an app to a group (the chips under the app) and it only goes to that group's devices. Apps that aren't tied to a group go to every device, including ones you add later.

## 6. Add an IPA

Press **Add App…** and choose the `.ipa`. Sideloop shows what it found (encryption, tweaks, extensions).

**Keep "Remove App Extensions" on** when it appears. Apps that contain extensions (share sheet, widgets, notification service, ...) are rejected by iOS when they are signed with a free Apple ID: the install runs to the end and then fails with `Failed to set app extension placeholders`. AltStore and Sideloadly strip extensions for the same reason. The cost is that those extra features are gone; the app itself works. The untouched file is kept as `app.orig.ipa` next to the signed one.

Then press **Add App**.

## 7. First install and the 2FA code

1. Press **Install** next to the app.
2. Apple sends a **verification code** to your trusted devices. Enter it on the *Verification Code* screen in the web UI. The background re-signer can't type a code, so the **first sign-in always has to be done from the web UI**.
3. Wait for *Done*. It can take a few minutes. **If it fails, press it again**: the public anisette servers are sometimes flaky for a moment (see [Troubleshooting](TROUBLESHOOTING.md)).

## 8. On the iPhone

1. **Settings → Privacy & Security → Developer Mode** → turn on, then restart when asked. (If the switch isn't there yet, restart once and look again.)
2. **Settings → General → VPN & Device Management** → tap your Apple ID under *Developer App* → **Trust**.
3. Open the app.

## 9. Let it renew itself

Leave **Automatic** on. Sideloop re-signs when less than the *Re-sign Early* time (default 2 days) is left. For that the iPhone must be **unlocked and on your home Wi-Fi at some point**. Free Apple ID limits: **3 apps per device** and **10 App IDs per week** (every app extension needs its own App ID, one more reason to remove them). With several Apple IDs, a new app moves on to the next one when the first has no room left.

While you are troubleshooting, **switch Automatic off**: every automatic attempt that reaches Apple can make your iPhone show an *Apple ID sign-in requested* prompt.

## 10. Apple TV (optional)

An Apple TV with **tvOS 17 or newer** can be kept signed the same way. It is paired differently from an iPhone (no cable, a PIN instead) and only takes **tvOS apps**; your iPhone and iPad apps are not touched by any of this.

1. The Apple TV and the NAS must be on the **same network and subnet**. The compose file already uses `network_mode: host`, which is what lets Sideloop find the TV.
2. On the Apple TV: **Settings → Remotes and Devices → Remote App and Devices**. Leave it open; it shows a PIN when something asks to pair.
3. In Sideloop: **Settings → Pair Apple TV…**, pick the TV in the list, then type the **PIN** from the TV screen when the page asks for it.
4. **Add App…** with a tvOS `.ipa`. The page says *Apple TV app* and offers it only to Apple TVs.
5. Press **Install**. The first sign-in with an Apple ID on the Apple TV needs a 2FA code in the web page, even if that Apple ID already works for your iPhone. After that the Apple TV is re-signed by itself while it is on and awake on the network.

**Good to know:** the free limits (3 apps per device, 10 App IDs a week) apply to the Apple TV too. A tvOS update can drop the pairing, so pair again if the TV stops showing up as *Connected*. Turning off *Automatically Update tvOS* (Settings → System → Software Updates) helps. If the page says the Apple TV helper isn't installed, the image was built without it (the download or its checksum check failed); redeploy the project to try again.

## Security

- `data/accounts/*/account.env` (and a copy of the first one in `data/config.env`) hold the Apple ID **passwords in plain text** (file mode 600). Use throwaway Apple IDs, and keep the `docker` shared folder admin-only.
- `data/xdg` holds the anisette identity, which acts like a password for this "device". Don't copy it around or publish it. For an Apple TV it also holds the pairing and the signed-in Apple ID sessions (`data/xdg/PlumeImpactor`): same care.
- The web UI has its own password but no HTTPS. Keep it on your LAN and don't forward port 8080 on your router.

## Everyday tasks

| Task | How |
| --- | --- |
| See what happened | *Activity* tab in the web UI, or `data/state/refresh.log` |
| Run a shell in the container | Docker → Container → `sideloop` → Terminal → `/bin/bash`. Keep what you paste short; the web terminal drops long pastes |
| Update AltServer | Change `ng-2026-11-05` to a newer tag from [AltServer-Linux tags](https://github.com/jaakkopalvaila/AltServer-Linux/tags), then Project → `sideloop` → Compose configuration → **Redeploy** |
| Back up | Copy `docker/sideloop-app/data` |
| Remove | Delete the project, then the `sideloop-app` folder. Remove the app and its profile on the iPhone in Settings |

## Something went wrong?

See [Troubleshooting](TROUBLESHOOTING.md). It lists every Apple and AltServer error we ran into, with the fix.
