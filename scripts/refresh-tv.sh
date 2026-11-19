#!/usr/bin/env bash
# Signs and installs one app on one Apple TV. scripts/refresh.sh hands over to this for devices of kind "appletv"
# (it is called with the same arguments: --app ID --device UDID [--force] [--recheck]).
#
# An Apple TV isn't reached through the muxer like an iPhone. It is paired once (a PIN shown on the TV) and
# found on the network by name; plumesign then signs the app with the Apple ID and installs it over a tunnel.
# The signature of a free Apple ID lasts 7 days, so this runs again before that, exactly like for an iPhone.
set -euo pipefail

DATA_DIR="${DATA_DIR:-/data}"
CONFIG="$DATA_DIR/config.env"
APPS_DIR="$DATA_DIR/apps"
DEVICES_DIR="$DATA_DIR/devices"
STATE_DIR="$DATA_DIR/state"
PLUMESIGN="${PLUMESIGN_BIN:-plumesign}"

[[ -r "$CONFIG" ]] || { echo "missing config: $CONFIG" >&2; exit 78; }
set -a; . "$CONFIG"; set +a
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
[[ -n "$APP" && -n "$DEVICE" ]] || { echo "refresh-tv.sh needs --app and --device" >&2; exit 64; }

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
NOISE='^(Signing|Installation) Progress:|^Writing File: |^Signing: |^(Data|Value) ?: |^X-(Apple|Mme|MMe)-|^(MachineID|One-Time Password|Local User ID|Device UDID|Device Description|Date|Sanitized client info) ?:|^Byte:|^(HMAC_OUT|NP):|anisette|adsid|gs_token|^Received (auth )?response status code'

# plumesign keeps its Apple ID sessions, anisette identities and pairing files under $XDG_CONFIG_HOME/PlumeImpactor.
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$DATA_DIR/xdg}"
mkdir -p "$STATE_DIR" "$APP_STATE" "$XDG_CONFIG_HOME"
cd "$DATA_DIR"

# Anisette servers to try (ANISETTE_SERVER may list several): the one that worked last comes first.
mapfile -t ANI_SERVERS < <(ANISETTE_SERVER="$ANISETTE_SERVER" python3 -m resigner.anisette order 2>/dev/null)
(( ${#ANI_SERVERS[@]} )) || ANI_SERVERS=("${ANISETTE_SERVER%%[, ]*}")

now() { date +%s; }
fmt_epoch() { date -d "@$1" '+%a %d %b %H:%M'; }
log() {
  local line; line="$(date '+%Y-%m-%d %H:%M:%S') $TAG$*"
  printf '%s\n' "$line" | tee -a "$LOG_FILE"
}
record() { printf '%s\t%s\t%s\n' "$(now)" "$1" "$2" > "$APP_STATE/last_run"; }

# One run at a time, shared with the iPhone and iPad runs.
exec 9>"$STATE_DIR/.lock"
if ! flock -n 9; then
  echo "${TAG}waiting for $(cat "$STATE_DIR/.running" 2>/dev/null || echo "another run") to finish…"
  flock -w 1800 9 || { echo "${TAG}gave up waiting after 30 minutes"; exit 1; }
fi
printf '%s' "${TAG% }" > "$STATE_DIR/.running"
out="$(mktemp)"; prov="$(mktemp)"
trap 'rm -f "$STATE_DIR/.running" "$out" "$prov"' EXIT

days_left() { [[ -r "$EXPIRY" ]] && echo $(( ( $(cat "$EXPIRY") - $(now) ) / 86400 )) || echo -999; }
read_fails() { local n=0; [[ -r "$FAILS" ]] && n="$(tr -cd '0-9' < "$FAILS")"; echo "${n:-0}"; }

if (( RECHECK )); then
  # The Apple TV can't be asked what is installed on it; all there is to report is what was recorded.
  if [[ -r "$EXPIRY" ]]; then
    log "recorded: signature valid until $(fmt_epoch "$(cat "$EXPIRY")") (an Apple TV can't be checked from here)"
  else
    log "nothing recorded: not installed on this Apple TV yet"
  fi
  exit 0
fi

LEFT="$(days_left)"
(( FORCE == 0 && LEFT > RENEW_BEFORE_DAYS )) && exit 0

if [[ -r "$EXPIRY" ]]; then
  log "signature has ${LEFT}d left (threshold ${RENEW_BEFORE_DAYS}d)$( (( FORCE )) && echo ", forced run" )"
else
  log "no signature on record yet, doing the first install"
fi

[[ -r "$IPA_PATH" ]] || { log "ERROR IPA missing: $IPA_PATH"; record fail "IPA missing"; exit 1; }

# Where is the Apple TV right now? It announces itself on the network; asleep or off it doesn't.
TV_IP=""; TV_PORT=""
for (( _try = 0; _try < ${TV_LOCATE_TRIES:-3}; _try++ )); do
  if found="$(python3 -m resigner.appletv locate "$DEVICE_UDID" 2>/dev/null)"; then
    read -r TV_IP TV_PORT <<< "$found"
    break
  fi
  sleep 2
done
if [[ -z "$TV_IP" ]]; then
  log "${DEVICE_NAME} not reachable (asleep, off, or not on this network); will retry next run"
  record offline "device not reachable"
  exit 0
fi

INTERACTIVE=0
[[ -t 0 && -t 1 ]] && INTERACTIVE=1

PREVIOUS=""
[[ -r "$APP_STATE/account" ]] && PREVIOUS="$(tr -d '[:space:]' < "$APP_STATE/account")"
pick_args=(--app "$APP" --device "$DEVICE_UDID")
(( INTERACTIVE )) && pick_args+=(--interactive)
if ! CANDIDATES="$(python3 -m resigner.accounts pick "${pick_args[@]}")"; then
  log "ERROR could not choose an Apple ID"; record fail "could not choose an Apple ID"; exit 1
fi
if [[ -z "$CANDIDATES" ]]; then
  hint="no Apple ID can sign this app for an Apple TV. Add one in Settings. A background run only uses an Apple ID that has signed in for an Apple TV before, so install once from the web page to sign a new one in"
  log "FAIL $hint"; record fail "$hint"; exit 1
fi

# plumesign signs in once and remembers the session. Needs a 2FA code the first time, so only the web page does it.
login_account() {  # $1 = Apple ID, $2 = password; the 2FA code (if asked) is typed by the person on the web page
  local r
  set +e
  if (( INTERACTIVE )); then
    timeout --foreground 600 "$PLUMESIGN" account login -u "$1" -p "$2" 2>&1 | tee -a "$out"
    r=${PIPESTATUS[0]}
  else
    timeout 120 "$PLUMESIGN" account login -u "$1" -p "$2" </dev/null 2>&1 | tee -a "$out"
    r=${PIPESTATUS[0]}
  fi
  set -e
  return "$r"
}

sign_attempt() {  # $1 = Apple ID. Appends plumesign's output to $out and returns its exit status.
  local r
  : > "$prov"
  set +e
  if (( INTERACTIVE )); then
    timeout --foreground 2400 "$PLUMESIGN" sign-rsd --apple-id -u "$1" --register-and-install \
      --output-provision "$prov" --ip "$TV_IP" --port "$TV_PORT" --udid "$DEVICE_UDID" -p "$IPA_PATH" 2>&1 | tee -a "$out"
    r=${PIPESTATUS[0]}
  else
    timeout 2400 "$PLUMESIGN" sign-rsd --apple-id -u "$1" --register-and-install \
      --output-provision "$prov" --ip "$TV_IP" --port "$TV_PORT" --udid "$DEVICE_UDID" -p "$IPA_PATH" </dev/null 2>&1 | tee -a "$out"
    r=${PIPESTATUS[0]}
  fi
  set -e
  return "$r"
}

installed_ok() { grep -qE 'Installation (complete|Succeeded)' "$out"; }

save_probe() {
  local e c old=""
  read -r e c <<< "$1"
  echo "$e" > "$EXPIRY"
  [[ -r "$CERT" ]] && old="$(cat "$CERT")"
  echo "$c" > "$CERT"
  if [[ -n "$old" && "$old" != "$c" ]]; then
    log "WARNING the signing certificate changed; the Apple TV may ask you to trust the developer again" \
        "(it changes when ${APPLE_ID:-the Apple ID} is also used by Xcode, AltStore, SideStore or Sideloadly)"
  fi
}

n_left="$(printf '%s\n' $CANDIDATES | wc -l)"
LIMIT_FOUND=""
rc=1
for ACCOUNT in $CANDIDATES; do
  n_left=$(( n_left - 1 ))
  LIMIT_FOUND=""
  { IFS= read -r -d '' APPLE_ID; IFS= read -r -d '' APPLE_PASSWORD; } \
    < <(set -a; . "$ACCOUNTS_DIR/$ACCOUNT/account.env"; printf '%s\0%s\0' "$APPLE_ID" "$APPLE_PASSWORD")

  log "signing with Apple ID $APPLE_ID [$ACCOUNT]"
  : > "$out"
  logged_in=0
  # First time for this Apple ID on an Apple TV, from the web page: sign in now, the 2FA code is asked here.
  if (( INTERACTIVE )) && [[ ! -e "$ACCOUNTS_DIR/$ACCOUNT/verified-tv" ]]; then
    if ! login_account "$APPLE_ID" "$APPLE_PASSWORD"; then
      log "FAIL could not sign in $APPLE_ID for an Apple TV"
      record fail "could not sign in $APPLE_ID"
      exit 1
    fi
    logged_in=1
  fi

  n_ani=${#ANI_SERVERS[@]}
  (( INTERACTIVE )) && n_ani=1   # on the web page a failed helper is retried by the page itself, with the next server
  for (( si = 0; si < n_ani; si++ )); do
    ANI="${ANI_SERVERS[si]}"
    export PLUME_ANISETTE_URL="$ANI"
    : > "$out"
    log "signing and installing $(basename "$IPA_PATH") on $DEVICE_UDID, via Anisette server $ANI"
    sign_attempt "$APPLE_ID" && rc=0 || rc=$?
    # A session that was lost (or never made): sign in again with the stored password, once. Works without a code
    # when Apple still trusts this install; otherwise the person has to do it from the web page.
    if (( rc != 0 && ! logged_in && ! INTERACTIVE )) && grep -qiE "Account '[^']*' not found|Request to developer session failed|Authentication extra step required|session[^\n]{0,40}(expired|invalid)|invalid[^\n]{0,20}(session|token)" "$out" \
        && ! python3 -m resigner.anisette check < "$out" > /dev/null; then
      log "the sign-in for $APPLE_ID is gone; signing in again"
      logged_in=1
      if login_account "$APPLE_ID" "$APPLE_PASSWORD"; then
        : > "$out"
        sign_attempt "$APPLE_ID" && rc=0 || rc=$?
      fi
    fi
    tr '\r' '\n' < "$out" | grep -vE "$NOISE" | cut -c1-400 >> "$LOG_FILE" || true
    if (( rc != 0 && ! INTERACTIVE )) && problem="$(python3 -m resigner.anisette check < "$out")"; then
      python3 -m resigner.anisette bad "$ANI" || true
      if (( si + 1 < n_ani )); then
        log "Anisette server $ANI failed ($problem); trying ${ANI_SERVERS[si + 1]}"
        continue
      fi
    fi
    break
  done

  if (( rc == 0 )) && installed_ok; then
    if info="$(python3 -m resigner.appletv profile "$prov")"; then
      save_probe "$info"
    else
      echo $(( $(now) + 7 * 86400 - 600 )) > "$EXPIRY"   # the profile wasn't kept; a free one lasts 7 days
    fi
    echo 0 > "$FAILS"
    python3 -m resigner.accounts record --app "$APP" --device "$DEVICE_UDID" --account "$ACCOUNT" || true
    python3 -m resigner.anisette good "$ANI" || true
    if [[ "$PREVIOUS" != "$ACCOUNT" ]]; then
      log "NOTE signed with $APPLE_ID. If the Apple TV says the developer isn't trusted, trust it in Settings > General > Device Management"
    fi
    msg="refreshed, valid until $(fmt_epoch "$(cat "$EXPIRY")")"
    log "OK $msg"; record ok "$msg"
    exit 0
  fi

  # Apple refusing because this account has used up its signings: another account can take a new app. An app that
  # is already installed has to stay with the account that signed it.
  if (( rc != 0 )) && [[ -z "$PREVIOUS" || "$PREVIOUS" != "$ACCOUNT" ]] && python3 -m resigner.accounts limit < "$out"; then
    LIMIT_FOUND="$ACCOUNT"
    python3 -m resigner.accounts exhausted --account "$ACCOUNT" || true
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
  hint="plumesign finished, but didn't report a finished install"
elif [[ -n "$LIMIT_FOUND" ]]; then
  hint="Apple says every available Apple ID is out of signings; add another one or wait (a free Apple ID allows 3 apps per device and 10 App IDs a week)"
elif (( ! INTERACTIVE )) && python3 -m resigner.anisette check < "$out" > /dev/null; then
  hint="the sign-in helper (Anisette server) failed${ANI_SERVERS[1]:+ on every server}; try again in a few minutes, or list more servers in ANISETTE_SERVER"
elif grep -qiE 'Enter 2FA code|code sent' "$out"; then
  hint="Apple is asking for a 2FA code; sign it in from the web UI once"
elif grep -qiE "pairing|Failed to validate|RemotePairing" "$out"; then
  hint="the Apple TV didn't accept the pairing; pair it again (a tvOS update can drop it)"
elif (( rc == 124 )); then
  hint="plumesign timed out"
fi
n=$(( $(read_fails) + 1 )); echo "$n" > "$FAILS"
log "FAIL plumesign exited $rc (consecutive failures: $n)${hint:+ - $hint}"
record fail "plumesign exited $rc${hint:+: $hint}"
exit "$rc"
