#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="${DATA_DIR:-/data}"
CONFIG="$DATA_DIR/config.env"
APPS_DIR="$DATA_DIR/apps"
DEVICES_DIR="$DATA_DIR/devices"
STATE_DIR="$DATA_DIR/state"
ALTSERVER_BIN="${ALTSERVER_BIN:-/usr/local/bin/AltServer}"

[[ -r "$CONFIG" ]] || { echo "missing config: $CONFIG" >&2; exit 78; }
set -a; . "$CONFIG"; set +a
# Apple IDs live in $DATA_DIR/accounts (several, in priority order); the pair in config.env is only a copy
# for older versions. Which one signs an app is decided per run by `python3 -m sideloop.accounts pick`.
unset APPLE_ID APPLE_PASSWORD
ACCOUNTS_DIR="$DATA_DIR/accounts"
: "${RENEW_BEFORE_DAYS:=2}"
: "${ANISETTE_SERVER:=https://new.ani.sidestore.io}"

FORCE=0; RECHECK=0; APP=""; DEVICE=""
while (( $# )); do
  case "$1" in
    --force) FORCE=1 ;;
    --recheck) RECHECK=1 ;;
    --app) APP="${2:?--app needs an id}"; shift ;;
    --device) DEVICE="${2:?--device needs a udid}"; shift ;;
  esac
  shift
done

if [[ -z "$APP" || -z "$DEVICE" ]]; then
  rc=0; n=0
  for d in "$APPS_DIR"/*/; do
    [[ -r "$d/meta.env" && -r "$d/app.ipa" ]] || continue
    id="$(basename "$d")"
    [[ -z "$APP" || "$APP" == "$id" ]] || continue
    for u in $(set -a; . "$d/meta.env"; printf '%s' "${DEVICES:-}"); do
      [[ -z "$DEVICE" || "${DEVICE,,}" == "${u,,}" ]] || continue
      [[ -r "$DEVICES_DIR/$u.env" ]] || continue
      n=$((n+1))
      args=(--app "$id" --device "$u")
      (( FORCE )) && args+=(--force)
      (( RECHECK )) && args+=(--recheck)
      "${BASH_SOURCE[0]}" "${args[@]}" || rc=$?
    done
  done
  (( n )) || echo "nothing to do: no app is assigned to ${DEVICE:-any device}"
  exit "$rc"
fi

APP_DIR="$APPS_DIR/$APP"
[[ -r "$APP_DIR/meta.env" ]] || { echo "unknown app: $APP" >&2; exit 78; }
set -a; . "$APP_DIR/meta.env"; set +a
DEVICE_UDID="$DEVICE"
DEVICE_NAME="$DEVICE"
[[ -r "$DEVICES_DIR/$DEVICE.env" ]] && { set -a; . "$DEVICES_DIR/$DEVICE.env"; set +a; }
IPA_PATH="$APP_DIR/app.ipa"
APP_STATE="$APP_DIR/state/$DEVICE"
TAG="[${APP_NAME:-$APP} on $DEVICE_NAME] "
EXPIRY="$APP_STATE/expiry"
CERT="$APP_STATE/certificate"
FAILS="$APP_STATE/consecutive_failures"
LOG_FILE="$STATE_DIR/refresh.log"
NOISE='^(Signing|Installation) Progress:|^Writing File: |^Signing: |^(Data|Value) ?: |^X-(Apple|Mme|MMe)-|^(MachineID|One-Time Password|Local User ID|Device UDID|Device Description|Date|Sanitized client info) ?:|^Byte:|^(HMAC_OUT|NP):|anisette|^Received (auth )?response status code'

# Anisette servers to try (ANISETTE_SERVER may list several): the one that worked last comes first.
mapfile -t ANI_SERVERS < <(ANISETTE_SERVER="$ANISETTE_SERVER" python3 -m sideloop.anisette order 2>/dev/null)
(( ${#ANI_SERVERS[@]} )) || ANI_SERVERS=("${ANISETTE_SERVER%%[, ]*}")
# Stock AltServer removes every free provisioning profile on the device before installing. Once the
# last profile of a developer is gone iOS forgets that the user trusted it, so every refresh brought
# back "Untrusted Developer". The patched AltServer leaves them in place, and prune_profiles below
# removes the outdated ones afterwards, never the newest.
export ALTSERVER_KEEP_PROFILES=1
export HOME="$DATA_DIR/.altserver"
mkdir -p "$STATE_DIR" "$APP_STATE" "$HOME"
# AltServer caches its signing certificate in ./AltServerData. Without that cache it revokes the
# certificate and makes a new one, and iOS then asks to trust the developer again. Every Apple ID gets
# its own folder for that (HOME, and so the anisette identity, is shared); see run_altserver.
cd "$DATA_DIR"

now() { date +%s; }
fmt_epoch() { date -d "@$1" '+%a %d %b %H:%M'; }
log() {
  local line; line="$(date '+%Y-%m-%d %H:%M:%S') $TAG$*"
  printf '%s\n' "$line" | tee -a "$LOG_FILE"
}
record() { printf '%s\t%s\t%s\n' "$(now)" "$1" "$2" > "$APP_STATE/last_run"; }

exec 9>"$STATE_DIR/.lock"
if ! flock -n 9; then
  echo "${TAG}waiting for $(cat "$STATE_DIR/.running" 2>/dev/null || echo "another run") to finish…"
  flock -w 1800 9 || { echo "${TAG}gave up waiting after 30 minutes"; exit 1; }
fi
printf '%s' "${TAG% }" > "$STATE_DIR/.running"
trap 'rm -f "$STATE_DIR/.running"' EXIT
RUN_START="$(now)"

conn_flag() {
  if timeout 10 idevice_id -l 2>/dev/null | grep -qi "^$DEVICE_UDID"; then echo ""
  elif timeout 10 idevice_id -n 2>/dev/null | grep -qi "^$DEVICE_UDID"; then printf '%s\n' -n
  else return 1; fi
}

device_online() {
  local f; f="$(conn_flag)" || return 1
  timeout 15 ideviceinfo ${f:+"$f"} -u "$DEVICE_UDID" -k DeviceName >/dev/null 2>&1
}

# Copies the device's provisioning profiles into a new temporary folder and prints its path.
copy_profiles() {
  local f tmp
  f="$(conn_flag)" || return 1
  tmp="$(mktemp -d)"
  if ! timeout 90 ideviceprovision ${f:+"$f"} -u "$DEVICE_UDID" copy "$tmp" >/dev/null 2>&1; then
    rm -rf "$tmp"; return 1
  fi
  echo "$tmp"
}

# Prints "<expiry> <certificate sha1>" of the newest profile in a folder for the main app of a
# bundle. Exits 1 when there is none, 2 when it is older than min_created. With a certificate
# sha1, profiles signed by another certificate (e.g. SideStore with another Apple ID) are skipped.
read_profile() {
  python3 - "$1" "$2" "${3:-0}" "${4:-}" <<'PY'
import datetime, glob, hashlib, plistlib, re, subprocess, sys
folder, bundle, min_created, cert = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
utc = lambda d: d.replace(tzinfo=datetime.timezone.utc).timestamp()
main_app = re.compile(r"^(\w+)\." + re.escape(bundle) + r"\.\1$")
best = None
for p in glob.glob(folder + "/*.mobileprovision"):
    xml = subprocess.run(["openssl", "smime", "-verify", "-noverify", "-inform", "DER", "-in", p],
                         capture_output=True).stdout
    try:
        d = plistlib.loads(xml)
    except Exception:
        continue
    if bundle and not main_app.match(d.get("Entitlements", {}).get("application-identifier", "")):
        continue
    c, e = d.get("CreationDate"), d.get("ExpirationDate")
    certs = [hashlib.sha1(x).hexdigest() for x in d.get("DeveloperCertificates") or [b""]]
    if cert and cert not in certs:
        continue
    if c and e and (best is None or c > best[0]):
        best = (c, int(utc(e)), certs[0])
if best is None:
    sys.exit(1)
if utc(best[0]) < min_created:
    sys.exit(2)
print(best[1], best[2])
PY
}

probe_expiry() {
  local tmp rc=0
  tmp="$(copy_profiles)" || return 1
  read_profile "$tmp" "$BUNDLE_ID" "${1:-0}" || rc=$?
  rm -rf "$tmp"
  return $rc
}

# AltServer removes the provisioning profiles of every sideloaded app on the device before it
# installs, and puts the others back only when the install succeeds. A run that fails or times
# out halfway leaves those apps without a profile, so iOS refuses to open them with "Untrusted
# Developer" even though their signature looks valid here. Check the other apps on the device
# after every run, and forget the signature of any that lost its profile so it gets reinstalled.
heal_siblings() {
  local tmp d s p name bundle cert
  tmp="$(copy_profiles)" || return 0
  for d in "$APPS_DIR"/*/; do
    s="$d/state/$DEVICE"
    [[ "$(basename "$d")" != "$APP" && -r "$s/expiry" && -r "$d/meta.env" ]] || continue
    IFS=$'\t' read -r bundle name < <(set -a; . "$d/meta.env"; printf '%s\t%s\n' "${BUNDLE_ID:-}" "${APP_NAME:-}")
    [[ -n "$bundle" ]] || continue
    cert=""; [[ -r "$s/certificate" ]] && cert="$(cat "$s/certificate")"
    if p="$(read_profile "$tmp" "$bundle" 0 "$cert")"; then
      echo "${p%% *}" > "$s/expiry"
    else
      rm -f "$s/expiry"
      log "WARNING ${name:-$bundle} lost its provisioning profile during this run and won't open;" \
          "it will be reinstalled on the next run"
    fi
  done
  rm -rf "$tmp"
}

# Removes provisioning profiles of this Apple ID's team that a newer profile for the same app or
# extension replaced, on every app of the team. The newest one always stays, so the developer remains trusted.
prune_profiles() {
  local f tmp u
  f="$(conn_flag)" || return 0
  tmp="$(copy_profiles)" || return 0
  python3 - "$tmp" "$BUNDLE_ID" <<'PY' | while read -r u; do
import glob, plistlib, re, subprocess, sys
folder, bundle = sys.argv[1], sys.argv[2]
profiles = []
for p in glob.glob(folder + "/*.mobileprovision"):
    xml = subprocess.run(["openssl", "smime", "-verify", "-noverify", "-inform", "DER", "-in", p],
                         capture_output=True).stdout
    try:
        d = plistlib.loads(xml)
    except Exception:
        continue
    profiles.append((d.get("Entitlements", {}).get("application-identifier", ""), d["CreationDate"], d["UUID"]))
main = re.compile(r"^(\w+)\." + re.escape(bundle) + r"\.\1$")
# The app was just installed, so its newest profile belongs to this Apple ID's team. Profiles of
# other teams (SideStore, Xcode) are left alone.
ours = max(((c, a) for a, c, _ in profiles if main.match(a)), default=None)
if ours is None:
    sys.exit()
team = ours[1].split(".", 1)[0]
newest = {}
for a, c, u in profiles:
    if a.split(".", 1)[0] == team and (a not in newest or c > newest[a][0]):
        newest[a] = (c, u)
for a, c, u in profiles:
    if a in newest and newest[a][1] != u:
        print(u)
PY
    timeout 30 ideviceprovision ${f:+"$f"} -u "$DEVICE_UDID" remove "$u" </dev/null >/dev/null 2>&1 || true
  done
  rm -rf "$tmp"
}

save_probe() {
  local e c old=""
  read -r e c <<< "$1"
  echo "$e" > "$EXPIRY"
  [[ -r "$CERT" ]] && old="$(cat "$CERT")"
  echo "$c" > "$CERT"
  if [[ -n "$old" && "$old" != "$c" ]]; then
    log "WARNING the signing certificate changed, so iOS will ask you to trust the developer again" \
        "(Settings > General > VPN & Device Management). It changes when ${APPLE_ID:-the Apple ID} is also used by" \
        "Xcode, AltStore, SideStore or Sideloadly, or when $DATA_DIR/AltServerData is lost."
  fi
}

stamp_success() {
  local p
  p="$(probe_expiry $(( RUN_START - 300 )))" || return 1
  save_probe "$p"
  echo 0 > "$FAILS"
}

days_left() { [[ -r "$EXPIRY" ]] && echo $(( ( $(cat "$EXPIRY") - $(now) ) / 86400 )) || echo -999; }
read_fails() { local n=0; [[ -r "$FAILS" ]] && n="$(tr -cd '0-9' < "$FAILS")"; echo "${n:-0}"; }

if (( RECHECK )); then
  device_online || { echo "${TAG}${DEVICE_NAME} not reachable; unlock it and try again"; exit 1; }
  p="$(probe_expiry)" || { rm -f "$EXPIRY"; log "not installed on this device"; exit 0; }
  save_probe "$p"
  log "rechecked on the device: signature valid until $(fmt_epoch "$(cat "$EXPIRY")")"
  exit 0
fi

LEFT="$(days_left)"
(( FORCE == 0 && LEFT > RENEW_BEFORE_DAYS )) && exit 0

if [[ -r "$EXPIRY" ]]; then
  log "signature has ${LEFT}d left (threshold ${RENEW_BEFORE_DAYS}d)$( (( FORCE )) && echo ", forced run" )"
else
  log "no signature on record yet, doing the first install"
fi

if ! device_online; then
  log "${DEVICE_NAME} not reachable (asleep, locked, or off this Wi-Fi); will retry next run"
  record offline "device not reachable"
  exit 0
fi

[[ -r "$IPA_PATH" ]] || { log "ERROR IPA missing: $IPA_PATH"; record fail "IPA missing"; exit 1; }

INTERACTIVE=0
[[ -t 0 && -t 1 ]] && INTERACTIVE=1

# Runs AltServer in an Apple ID's certificate folder (AltServerData lives in the working directory). HOME is the
# same for every account, so the anisette identity is shared.
run_altserver() { ( cd "$ACC_WORK" && export HOME="$ACC_HOME" && exec "$@" ); }

# The Apple IDs to try, best first. An installed app keeps the one that signed it, because a different Apple ID
# is a different team: iOS would see another app (and lose the data of the old one).
PREVIOUS=""
[[ -r "$APP_STATE/account" ]] && PREVIOUS="$(tr -d '[:space:]' < "$APP_STATE/account")"
pick_args=(--app "$APP" --device "$DEVICE_UDID")
(( INTERACTIVE )) && pick_args+=(--interactive)
if ! CANDIDATES="$(python3 -m sideloop.accounts pick "${pick_args[@]}")"; then
  log "ERROR could not choose an Apple ID"; record fail "could not choose an Apple ID"; exit 1
fi
if [[ -z "$CANDIDATES" ]]; then
  hint="no Apple ID can sign this app. Add one in Settings. A background run only uses an Apple ID that has signed something before, so install once from the web page to sign a new one in"
  log "FAIL $hint"; record fail "$hint"; exit 1
fi

out="$(mktemp)"
trap 'rm -f "$STATE_DIR/.running" "$out"' EXIT
LIMIT_FOUND=""
n_left="$(printf '%s\n' $CANDIDATES | wc -l)"
for ACCOUNT in $CANDIDATES; do
  n_left=$(( n_left - 1 ))
  LIMIT_FOUND=""
  { IFS= read -r -d '' APPLE_ID; IFS= read -r -d '' APPLE_PASSWORD; IFS= read -r -d '' ACC_WORK; IFS= read -r -d '' ACC_HOME; } \
    < <(set -a; . "$ACCOUNTS_DIR/$ACCOUNT/account.env"; printf '%s\0%s\0%s\0%s\0' "$APPLE_ID" "$APPLE_PASSWORD" "$WORK_DIR" "$HOME_DIR")
  mkdir -p "$ACC_WORK" "$ACC_HOME"

  log "signing with Apple ID $APPLE_ID [$ACCOUNT]"
  n_ani=${#ANI_SERVERS[@]}
  (( INTERACTIVE )) && n_ani=1   # on the web page a failed helper is retried by the page itself, with the next server
  for (( si = 0; si < n_ani; si++ )); do
    ANI="${ANI_SERVERS[si]}"
    export ALTSERVER_ANISETTE_SERVER="$ANI"
    : > "$out"
    log "signing and installing $(basename "$IPA_PATH") on $DEVICE_UDID, via Anisette server $ANI"
    set +e
    if (( INTERACTIVE )); then
      run_altserver timeout --foreground 2400 "$ALTSERVER_BIN" -u "$DEVICE_UDID" -a "$APPLE_ID" -p "$APPLE_PASSWORD" "$IPA_PATH"
      rc=$?
    else
      run_altserver timeout 2400 "$ALTSERVER_BIN" -u "$DEVICE_UDID" -a "$APPLE_ID" -p "$APPLE_PASSWORD" "$IPA_PATH" </dev/null 2>&1 \
        | tee "$out"
      rc=${PIPESTATUS[0]}
      tr '\r' '\n' < "$out" | grep -vE "$NOISE" | cut -c1-400 >> "$LOG_FILE"
    fi
    set -e
    # A problem with the anisette server itself (not with the Apple ID): set it aside and try the next one.
    if (( rc != 0 && ! INTERACTIVE )) && problem="$(python3 -m sideloop.anisette check < "$out")"; then
      python3 -m sideloop.anisette bad "$ANI" || true
      if (( si + 1 < n_ani )); then
        log "Anisette server $ANI failed ($problem); trying ${ANI_SERVERS[si + 1]}"
        continue
      fi
    fi
    break
  done

  if (( rc == 0 )) && stamp_success; then
    python3 -m sideloop.accounts record --app "$APP" --device "$DEVICE_UDID" --account "$ACCOUNT" || true
    python3 -m sideloop.anisette good "$ANI" || true
    msg="refreshed, valid until $(fmt_epoch "$(cat "$EXPIRY")")"
    if [[ "$PREVIOUS" != "$ACCOUNT" ]]; then
      log "NOTE signed with $APPLE_ID. If iOS says Untrusted Developer, trust it in Settings > General > VPN & Device Management"
    fi
    log "OK $msg"; record ok "$msg"
    prune_profiles
    heal_siblings
    exit 0
  fi

  # Apple refusing because this account has used up its signings: another account can take a new app. An app
  # that is already installed has to stay with the account that signed it.
  if (( rc != 0 )) && [[ -z "$PREVIOUS" || "$PREVIOUS" != "$ACCOUNT" ]] && python3 -m sideloop.accounts limit < "$out"; then
    LIMIT_FOUND="$ACCOUNT"
    python3 -m sideloop.accounts exhausted --account "$ACCOUNT" || true
    if (( n_left > 0 )); then
      log "$APPLE_ID has no signings left ($(tr '\r' '\n' < "$out" | grep -iE 'maximum|limit|too many' | head -n1 | cut -c1-160)); trying the next Apple ID"
      continue
    fi
  fi
  break
done

hint=""
if (( rc == 0 )); then
  rc=1
  hint="AltServer finished, but the app isn't on the device (did it lock or leave Wi-Fi mid-copy?)"
elif [[ -n "$LIMIT_FOUND" ]]; then
  hint="Apple says every available Apple ID is out of signings; add another one or wait (a free Apple ID allows 3 apps per device and 10 App IDs a week)"
elif (( ! INTERACTIVE )) && python3 -m sideloop.anisette check < "$out" > /dev/null; then
  hint="the sign-in helper (Anisette server) failed${ANI_SERVERS[1]:+ on every server}; try again in a few minutes, or list more servers in ANISETTE_SERVER"
elif grep -qiE 'two.?factor code|verification code' "$out"; then
  hint="Apple is asking for a 2FA code; sign it from the web UI once"
elif (( rc == 124 )); then
  hint="AltServer timed out"
fi
n=$(( $(read_fails) + 1 )); echo "$n" > "$FAILS"
log "FAIL AltServer exited $rc (consecutive failures: $n)${hint:+ - $hint}"
record fail "AltServer exited $rc${hint:+: $hint}"
heal_siblings
exit "$rc"
