#!/usr/bin/env bash
#
# Kenzy installer — https://kenzy.ai
#
#   curl -fsSL https://kenzy.ai/install.sh | bash
#
# Creates a per-user virtualenv, installs the Kenzy services you choose from
# PyPI, downloads the inference models, and scaffolds your config home. The
# kenzy-* commands are linked into ~/.local/bin so they're on your PATH.
#
# Flags (or the matching environment variable):
#   --profile node|server|all   KENZY_PROFILE   what to install (skips the prompt)
#   --no-apt                     KENZY_NO_APT=1  don't install system packages (apt)
#   --package PATH               KENZY_PACKAGE   install a local wheel/sdist/source
#                                                dir instead of PyPI (pre-release testing)
#   --version X.Y.Z              KENZY_VERSION   pin a specific PyPI version
#   --node-id ID                 KENZY_NODE_ID   stable node_id for a node install
#                                                (default: a generated uuid)
#   --token TOKEN                 KENZY_TOKEN     join token: for a node, paste the
#                                                server's token (Settings/CLI output);
#                                                for server/all, share a token across hosts
#                                                (default: one is generated)
#   --constraints FILE           KENZY_CONSTRAINTS  pip constraints file of dependency
#                                                pins to honor on install + every upgrade
#                                                (seeds the config home's constraints.txt)
#   --tls                        KENZY_TLS=1     server/all: enable TLS (the DEFAULT) with a
#                                                generated self-signed cert — node audio (wss),
#                                                the dashboard (https), AND the backend services
#                                                (they receive the pair via config-pull and
#                                                bring their own listeners up as https)
#   --no-tls                     KENZY_TLS=0     opt out — plaintext install (skips the question)
#   --tls-cert PATH              KENZY_TLS_CERT  use your own certificate instead of
#   --tls-key PATH               KENZY_TLS_KEY   generating one (both required; implies --tls)
#   --llm CHOICE                 KENZY_LLM       server/all: the language model ("brain") —
#                                                openai (default) | claude | ollama | skip.
#                                                Interactive installs ask; --yes/no-TTY = openai
#   --llm-model NAME             KENZY_LLM_MODEL override the model string (LiteLLM format,
#                                                e.g. "anthropic/claude-sonnet-5", "ollama/qwen3:8b")
#   --llm-url URL                KENZY_LLM_URL   the model server's URL (ollama;
#                                                default http://127.0.0.1:11434)
#   --local-voice                KENZY_LOCAL_VOICE=1  use the local Kokoro voice (installs the
#                                                kokoro extra + espeak-ng; tts stays off the cloud)
#   --no-media-keys              KENZY_MEDIA_KEYS=0  node/all: skip speakerphone volume-button
#                                                support. ON by default because the recommended
#                                                node IS a USB speakerphone and those have keys.
#                                                Defaulted-on installs the mediakeys extra (evdev
#                                                builds from source — needs gcc + python3-dev) and
#                                                add this user to the 'input' group (takes effect
#                                                at next login/reboot). Opt out if you don't want
#                                                a compiler or /dev/input access on this host.
#                                                The feature itself stays OFF until enabled
#                                                per node (the audio setup wizard offers it)
#   --cuda-torch                 KENZY_CPU_TORCH=0  keep torch's standard CUDA wheels on a
#                                                Linux/x86 host. The default is auto: a host with
#                                                no NVIDIA card gets the CPU-only build instead,
#                                                skipping ~2.7 GB of CUDA runtime it cannot use.
#                                                --cpu-torch forces the CPU build on a GPU host
#   --no-service                 KENZY_NO_SERVICE=1  don't create/start systemd
#                                                --user units (just install + config)
#   --yes                        KENZY_YES=1     assume defaults, no prompts (CI)
#   --home DIR                   KENZY_HOME      config home (default ~/.config/kenzy)
#   --venv DIR                   KENZY_VENV      virtualenv location
#                                                (default ~/.local/share/kenzy/venv)
#   --uninstall                  KENZY_UNINSTALL=1  remove Kenzy: stop/disable the
#                                                systemd --user units, delete the venv
#                                                and the kenzy-* commands, then exit
#   --purge                      KENZY_PURGE=1   with --uninstall, ALSO delete the
#                                                config home ($KENZY_HOME)
#
set -eo pipefail

PROFILE="${KENZY_PROFILE:-}"
ASSUME_YES="${KENZY_YES:-0}"
NO_APT="${KENZY_NO_APT:-0}"
PACKAGE="${KENZY_PACKAGE:-}"
VERSION="${KENZY_VERSION:-}"
NODE_ID="${KENZY_NODE_ID:-}"
TOKEN="${KENZY_TOKEN:-}"
CONSTRAINTS="${KENZY_CONSTRAINTS:-}"
TLS="${KENZY_TLS:-}"                   # ""=ask (server/all, interactive), 1=on, 0=off
LLM="${KENZY_LLM:-}"                   # ""=ask (server/all, interactive); openai|claude|ollama|skip
LLM_MODEL="${KENZY_LLM_MODEL:-}"
LLM_URL="${KENZY_LLM_URL:-}"
LLM_KEY=""; LLM_KEY_NAME=""
USE_KOKORO="${KENZY_LOCAL_VOICE:-0}"
USE_MEDIAKEYS="${KENZY_MEDIA_KEYS:-1}"   # node/all default: the recommended node has volume keys
CPU_TORCH="${KENZY_CPU_TORCH:-auto}"     # auto|1|0 — see the torch step below
TLS_CERT="${KENZY_TLS_CERT:-}"
TLS_KEY="${KENZY_TLS_KEY:-}"
NO_SERVICE="${KENZY_NO_SERVICE:-0}"
UNINSTALL="${KENZY_UNINSTALL:-0}"
PURGE="${KENZY_PURGE:-0}"
KENZY_HOME="${KENZY_HOME:-$HOME/.config/kenzy}"
VENV="${KENZY_VENV:-$HOME/.local/share/kenzy/venv}"
BINDIR="$HOME/.local/bin"

# --- arg parsing -------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2 ;;
    --profile=*) PROFILE="${1#*=}"; shift ;;
    --no-apt) NO_APT=1; shift ;;
    --package) PACKAGE="$2"; shift 2 ;;
    --package=*) PACKAGE="${1#*=}"; shift ;;
    --version) VERSION="$2"; shift 2 ;;
    --version=*) VERSION="${1#*=}"; shift ;;
    --node-id) NODE_ID="$2"; shift 2 ;;
    --node-id=*) NODE_ID="${1#*=}"; shift ;;
    --token) TOKEN="$2"; shift 2 ;;
    --token=*) TOKEN="${1#*=}"; shift ;;
    --constraints) CONSTRAINTS="$2"; shift 2 ;;
    --constraints=*) CONSTRAINTS="${1#*=}"; shift ;;
    --tls) TLS=1; shift ;;
    --no-tls) TLS=0; shift ;;
    --tls-cert) TLS_CERT="$2"; TLS=1; shift 2 ;;
    --tls-cert=*) TLS_CERT="${1#*=}"; TLS=1; shift ;;
    --tls-key) TLS_KEY="$2"; TLS=1; shift 2 ;;
    --tls-key=*) TLS_KEY="${1#*=}"; TLS=1; shift ;;
    --llm) LLM="$2"; shift 2 ;;
    --llm=*) LLM="${1#*=}"; shift ;;
    --llm-model) LLM_MODEL="$2"; shift 2 ;;
    --llm-model=*) LLM_MODEL="${1#*=}"; shift ;;
    --llm-url) LLM_URL="$2"; shift 2 ;;
    --llm-url=*) LLM_URL="${1#*=}"; shift ;;
    --local-voice) USE_KOKORO=1; shift ;;
    --cuda-torch) CPU_TORCH=0; shift ;;
    --cpu-torch) CPU_TORCH=1; shift ;;
    --media-keys) USE_MEDIAKEYS=1; shift ;;      # now the default; kept for compatibility
    --no-media-keys) USE_MEDIAKEYS=0; shift ;;
    --no-service) NO_SERVICE=1; shift ;;
    --home) KENZY_HOME="$2"; shift 2 ;;
    --home=*) KENZY_HOME="${1#*=}"; shift ;;
    --venv) VENV="$2"; shift 2 ;;
    --venv=*) VENV="${1#*=}"; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    --purge) PURGE=1; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    -h|--help) awk 'NR==1{next} /^set /{exit} {sub(/^#? ?/,"");print}' "$0"; exit 0 ;;
    *) printf "Unknown option: %s (try --help)\n" "$1" >&2; exit 2 ;;
  esac
done

# --- output helpers ----------------------------------------------------------
if [ -t 1 ]; then
  B=$(tput bold 2>/dev/null || true); R=$(tput sgr0 2>/dev/null || true)
  BLUE=$(tput setaf 4 2>/dev/null || true); YEL=$(tput setaf 3 2>/dev/null || true)
  REDC=$(tput setaf 1 2>/dev/null || true); GRN=$(tput setaf 2 2>/dev/null || true)
else
  B=""; R=""; BLUE=""; YEL=""; REDC=""; GRN=""
fi
step() { printf "\n${BLUE}${B}==>${R} ${B}%s${R}\n" "$*"; }
info() { printf "    %s\n" "$*"; }
warn() { printf "${YEL}!!  %s${R}\n" "$*"; }
die()  { printf "${REDC}${B}xx  %s${R}\n" "$*" >&2; exit 1; }

trap 'die "Install failed on line $LINENO. Re-run, or follow the manual setup at https://docs.kenzy.ai"' ERR

# --- a controlling terminal, even under `curl | bash` ------------------------
if [ -e /dev/tty ] && [ "$ASSUME_YES" != "1" ]; then TTY=/dev/tty; else TTY=""; fi

confirm() { # confirm "question"  -> 0 (yes) / 1 (no); defaults to yes
  [ -z "$TTY" ] && return 0
  printf "${B}%s${R} [Y/n] " "$1" > "$TTY"
  local r; read -r r < "$TTY" || r=""
  case "$r" in [nN]*) return 1 ;; *) return 0 ;; esac
}

confirm_no() { # confirm_no "question"  -> 0 (yes) / 1 (no); defaults to NO
  [ -z "$TTY" ] && return 1
  printf "${B}%s${R} [y/N] " "$1" > "$TTY"
  local r; read -r r < "$TTY" || r=""
  case "$r" in [yY]*) return 0 ;; *) return 1 ;; esac
}

SUDO=""; [ "$(id -u)" != "0" ] && SUDO="sudo"

# --- uninstall ---------------------------------------------------------------
safe_rmrf() { # safe_rmrf DIR LABEL  -> refuses dangerously shallow/critical paths
  case "$1" in
    ""|"/"|"$HOME"|"$HOME/") warn "refusing to remove unsafe path '$1' ($2)"; return 1 ;;
  esac
  rm -rf "$1"
}

do_uninstall() {
  trap - ERR  # this is teardown, not install — don't print the install-failed message
  local UNIT_DIR="$HOME/.config/systemd/user"
  step "Uninstalling Kenzy"
  info "Venv:        $VENV"
  info "Commands:    $BINDIR/kenzy-*"
  info "User units:  $UNIT_DIR/kenzy-*.service"
  if [ "$PURGE" = "1" ]; then
    warn "PURGE: also deleting config home $KENZY_HOME (configs, .env, node_id, speaker profiles)"
  else
    info "Config home: $KENZY_HOME  (kept — re-run with --purge to remove)"
  fi
  if ! confirm "Remove the above?"; then
    warn "Aborted — nothing removed."
    exit 0
  fi

  # 1. Stop + disable + remove systemd --user units (all kenzy-* regardless of profile).
  if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    local found=0
    for unit in "$UNIT_DIR"/kenzy-*.service; do
      [ -e "$unit" ] || continue
      found=1
      local name; name="$(basename "$unit")"
      systemctl --user disable --now "$name" >/dev/null 2>&1 || true
      rm -f "$unit"
      info "removed $name"
    done
    if [ "$found" = "1" ]; then systemctl --user daemon-reload || true; else info "no kenzy units found"; fi
  else
    info "systemd --user not available — skipping unit removal"
  fi

  # 2. Remove the kenzy-* command symlinks.
  if [ -d "$BINDIR" ]; then
    local n=0
    for cmd in "$BINDIR"/kenzy-*; do
      { [ -e "$cmd" ] || [ -L "$cmd" ]; } || continue
      rm -f "$cmd"; n=1
    done
    [ "$n" = "1" ] && info "removed kenzy-* from $BINDIR"
  fi

  # 3. Remove the virtualenv (and its kenzy-owned parent dir if now empty).
  if [ -d "$VENV" ] && safe_rmrf "$VENV" "venv"; then
    info "removed venv $VENV"
    local vparent; vparent="$(dirname "$VENV")"
    [ "$(basename "$vparent")" = "kenzy" ] && rmdir "$vparent" 2>/dev/null || true
  fi

  # 4. Optionally purge the config home.
  if [ "$PURGE" = "1" ] && [ -d "$KENZY_HOME" ]; then
    safe_rmrf "$KENZY_HOME" "config home" && info "purged config home $KENZY_HOME"
  fi

  printf "\n${GRN}${B}  Kenzy removed.${R}\n"
  [ "$PURGE" != "1" ] && info "Config kept at $KENZY_HOME (delete manually or re-run with --purge)."
  info "Left as-is: 'loginctl enable-linger $USER' (affects all your user services) and"
  info "shared model caches (e.g. ~/.cache/huggingface)."
  exit 0
}

if [ "$UNINSTALL" = "1" ]; then do_uninstall; fi

# --- banner ------------------------------------------------------------------
printf "\n${B}  KENZY${R}  ·  self-hosted home voice assistant\n"
printf "  ${BLUE}https://kenzy.ai${R}\n"

# --- profile selection -------------------------------------------------------
if [ -z "$PROFILE" ]; then
  if [ -n "$TTY" ]; then
    {
      printf "\n${B}What would you like to install?${R}\n"
      printf "  ${B}1)${R} Room node      — wake word + audio capture (a Pi in a room)\n"
      printf "  ${B}2)${R} Server stack   — server, stt, tts, llm, speaker\n"
      printf "  ${B}3)${R} Everything     — node + full server stack\n"
      printf "Choose [1-3] (default 1): "
    } > "$TTY"
    read -r choice < "$TTY" || choice=""
    case "$choice" in 2) PROFILE="server" ;; 3) PROFILE="all" ;; *) PROFILE="node" ;; esac
  else
    PROFILE="node"
  fi
fi

# EXTRAS = pip extras to install; SVC_LIST = systemd units to create, in start
# order (backends → server → node).
case "$PROFILE" in
  node)   EXTRAS="node"                        ; SVC_LIST="node" ;;
  server) EXTRAS="server,stt,tts,llm,speaker"  ; SVC_LIST="stt tts llm speaker s2s server" ;;
  all)    EXTRAS="node,server,stt,tts,llm,speaker" ; SVC_LIST="stt tts llm speaker s2s server node" ;;
  *) die "Unknown profile '$PROFILE' (use: node | server | all)" ;;
esac

# Speakerphone volume buttons (5.0.4): ON by default for node/all, because the
# recommended node IS a USB speakerphone with AEC and those carry +/- keys.
# evdev is deliberately NOT folded into $EXTRAS: it ships source-only on PyPI
# (no wheels, any platform), so it needs a compiler — and a default-on feature
# must never be able to abort the install, or fail an UPGRADE, on a host whose
# toolchain went missing. It gets its own tolerated pip step below instead.
case "$PROFILE" in
  node|all) : ;;
  *) USE_MEDIAKEYS=0 ;;   # server-only host: no audio device, nothing to listen to
esac
info "Profile: ${B}$PROFILE${R}  ·  Venv: ${B}$VENV${R}  ·  Config: ${B}$KENZY_HOME${R}"

# --- node identity (node installs only) --------------------------------------
# A node's identity is its node_id; its room name is server-owned and set later
# from the dashboard (pulled on every boot), so there is no room prompt. When
# --node-id is omitted, kenzy-init generates and prints one — use it to pre-seed
# configs/nodes/<id>.yaml on the server.
if { [ "$PROFILE" = "node" ] || [ "$PROFILE" = "all" ]; } && [ -n "$NODE_ID" ]; then
  info "Node id: ${B}$NODE_ID${R}"
fi

# --- TLS decision (server/all only) ------------------------------------------
# TLS is ON BY DEFAULT (3.11+): it encrypts everything Kenzy sends on the LAN —
# node audio, the dashboard, and the backend service mesh — and a self-signed
# cert is all that's needed (nodes and services connect encrypted-but-unverified,
# mDNS advertises the flag so they switch to wss:// on their own; only browsers
# show a one-time warning). Opt out with --no-tls for a plaintext install.
if [ "$PROFILE" = "node" ]; then
  [ "$TLS" = "1" ] && warn "--tls is server-side (the node speaks wss automatically) — ignoring."
  TLS=0
elif [ -n "$TLS_CERT" ] || [ -n "$TLS_KEY" ]; then
  [ -n "$TLS_CERT" ] && [ -n "$TLS_KEY" ] || die "--tls-cert and --tls-key must be given together."
  [ -s "$TLS_CERT" ] || die "--tls-cert not found: $TLS_CERT"
  [ -s "$TLS_KEY" ]  || die "--tls-key not found: $TLS_KEY"
elif [ -z "$TLS" ]; then
  # Default on. The interactive prompt now defaults to YES; --yes / no TTY = on.
  if [ -n "$TTY" ] && ! confirm "Enable TLS? (recommended — encrypts audio, dashboard, and services; self-signed cert, browsers show a one-time warning)"; then
    TLS=0
  else
    TLS=1
  fi
fi

# --- language model decision (server/all only) --------------------------------
# The "brain" is the user's call — cloud (quickest) or their own hardware.
# OpenAI stays the default purely for time-to-first-conversation; the packaged
# config already points at it, so choosing 1 (or --yes) writes nothing.
if [ "$PROFILE" != "node" ]; then
  if [ -z "$LLM" ] && [ -n "$TTY" ]; then
    {
      printf "\n${B}Where should Kenzy's thinking happen (the language model)?${R}\n"
      printf "  ${B}1)${R} OpenAI (cloud)          — quickest start; one API key, no downloads\n"
      printf "  ${B}2)${R} Anthropic Claude (cloud) — same idea, different provider\n"
      printf "  ${B}3)${R} Ollama (your hardware)   — fully local; needs a running Ollama server\n"
      printf "  ${B}4)${R} Decide later             — configure from the dashboard when ready\n"
      printf "Choose [1-4] (default 1): "
    } > "$TTY"
    read -r llm_choice < "$TTY" || llm_choice=""
    case "$llm_choice" in 2) LLM="claude" ;; 3) LLM="ollama" ;; 4) LLM="skip" ;; *) LLM="openai" ;; esac
  fi
  LLM="${LLM:-openai}"

  case "$LLM" in
    openai)
      LLM_KEY_NAME="OPENAI_API_KEY"
      ;;
    claude)
      LLM_KEY_NAME="ANTHROPIC_API_KEY"
      [ -n "$LLM_MODEL" ] || LLM_MODEL="anthropic/claude-sonnet-5"
      ;;
    ollama)
      if [ -z "$LLM_URL" ] && [ -n "$TTY" ]; then
        printf "${B}Ollama URL${R} [http://127.0.0.1:11434]: " > "$TTY"
        read -r LLM_URL < "$TTY" || LLM_URL=""
      fi
      LLM_URL="${LLM_URL:-http://127.0.0.1:11434}"
      if [ -z "$LLM_MODEL" ] && [ -n "$TTY" ]; then
        printf "${B}Model name${R} (must be pulled in Ollama, e.g. 'ollama pull qwen3:8b') [qwen3:8b]: " > "$TTY"
        read -r LLM_MODEL < "$TTY" || LLM_MODEL=""
      fi
      LLM_MODEL="${LLM_MODEL:-qwen3:8b}"
      case "$LLM_MODEL" in ollama/*) : ;; *) LLM_MODEL="ollama/$LLM_MODEL" ;; esac
      info "A Pi can't run a capable model — point the URL at a machine that can."
      ;;
    skip) : ;;
    *) die "Unknown --llm choice '$LLM' (use: openai | claude | ollama | skip)" ;;
  esac

  # Cloud brains: offer to take the API key now (hidden input; Enter = later).
  if [ -n "$LLM_KEY_NAME" ] && [ -n "$TTY" ]; then
    printf "${B}%s${R} (paste to set now; Enter to add later in the dashboard): " "$LLM_KEY_NAME" > "$TTY"
    read -rs LLM_KEY < "$TTY" || LLM_KEY=""
    printf "\n" > "$TTY"
  fi

  # The voice defaults to OpenAI TTS — a non-OpenAI brain would think fine but
  # couldn't SPEAK without an OpenAI key. Offer the fully local voice instead.
  if { [ "$LLM" = "claude" ] || [ "$LLM" = "ollama" ]; } && [ "$USE_KOKORO" != "1" ]; then
    if [ -n "$TTY" ]; then
      if confirm "Kenzy's voice defaults to OpenAI's TTS. Use the LOCAL voice instead (Kokoro — bigger install, nothing leaves your network)?"; then
        USE_KOKORO=1
      else
        warn "Keeping the OpenAI voice — she'll need an OPENAI_API_KEY to speak"
        warn "(dashboard: Settings -> API keys), or switch tts to kokoro later."
      fi
    else
      warn "Non-OpenAI brain selected: the voice still defaults to OpenAI TTS."
      warn "Set OPENAI_API_KEY too, or switch the tts service to kokoro."
    fi
  fi
  [ "$USE_KOKORO" = "1" ] && EXTRAS="$EXTRAS,kokoro"
# Kokoro has no build for Python 3.13+. The extra carries a marker so this no
# longer breaks the install, but the user asked for a local voice and must be
# told they are not getting one — a silent downgrade to the cloud is exactly
# the wrong outcome for someone who chose local on purpose.
if [ "$USE_KOKORO" = "1" ] \
   && ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info[:2] < (3,13) else 1)'; then
  warn "The local Kokoro voice has no build for Python $(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])') yet."
  warn "Installing anyway: the tts service will use its CLOUD provider, not a local voice."
  warn "For a fully local voice today, use a host with Python 3.12 or older."
fi
fi

# --- system dependencies -----------------------------------------------------
# node + speaker capture audio via PortAudio; server-only still pulls the
# speaker extra (sounddevice), so PortAudio is needed for every profile.
# openssl is only exercised for --tls cert generation but is listed so a
# minimal image doesn't silently fall back to plaintext (usually a no-op).
APT_PKGS="python3 python3-venv python3-dev libportaudio2 portaudio19-dev openssl"
[ "$USE_KOKORO" = "1" ] && APT_PKGS="$APT_PKGS espeak-ng"   # the local voice's phonemizer
[ "$USE_MEDIAKEYS" = "1" ] && APT_PKGS="$APT_PKGS gcc"       # evdev builds from source
step "System dependencies"
if [ "$NO_APT" = "1" ]; then
  warn "Skipping system packages (--no-apt). Ensure these are present: $APT_PKGS"
elif command -v apt-get >/dev/null 2>&1; then
  if confirm "Install with: $SUDO apt-get install $APT_PKGS ?"; then
    $SUDO apt-get update
    # shellcheck disable=SC2086
    $SUDO apt-get install -y $APT_PKGS
  else
    warn "Skipped. Ensure these are installed yourself: $APT_PKGS"
  fi
else
  warn "No apt-get found — this installer only auto-installs system packages on"
  warn "Debian/Ubuntu. Install these with your package manager, then re-run with --no-apt:"
  warn "  $APT_PKGS"
  warn "  (Fedora: portaudio portaudio-devel · Arch: portaudio)"
fi

# Media keys need read access to /dev/input/* — that's the 'input' group. The
# group lands in the user's NEXT session, so the node service (systemd --user)
# only sees it after a reboot/re-login; the feature stays a labeled no-op
# until then, and until volume_buttons is enabled on the node's config.
command -v python3 >/dev/null 2>&1 || die "python3 is required but not installed."
python3 -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3,11) else 1)' \
  || die "Python 3.11+ required (found $(python3 -V 2>&1))."

# Wake-word engine strategy. openwakeword hard-requires tflite-runtime on Linux,
# and that package ships no wheel past cp311 and no sdist — so on 3.12+ the
# normal extra cannot install at all. Its CODE imports tflite lazily and only
# fails if handed a .tflite model, so we install it without dependencies and let
# Kenzy fall back to the bundled ONNX wake-word model (chosen automatically).
WAKEWORD_NODEPS=0
# Kept in step with openwakeword's real requirements minus tflite-runtime; a
# test in the repo fails if upstream's list changes (tests/test_setup_skips.py).
WAKEWORD_DEPS="onnxruntime tqdm scipy scikit-learn requests"
if [ "$PROFILE" = "node" ] || [ "$PROFILE" = "all" ]; then
  if [ "$(uname -s)" = "Linux" ] && \
     ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info[:2] <= (3,11) else 1)'; then
    WAKEWORD_NODEPS=1
    info "Python $(python3 -V 2>&1 | cut -d' ' -f2): installing the wake-word engine without"
    info "its tflite dependency (no wheel past 3.11) — Kenzy will use the ONNX model."
  else
    EXTRAS="$EXTRAS,wakeword"
  fi
fi

# --- resolve the package spec ------------------------------------------------
# Local package (file or dir) for pre-release testing, else PyPI.
if [ -n "$PACKAGE" ]; then
  [ -e "$PACKAGE" ] || die "--package path not found: $PACKAGE"
  PKG_ABS="$(cd "$(dirname "$PACKAGE")" && pwd)/$(basename "$PACKAGE")"
  SPEC="${PKG_ABS}[${EXTRAS}]"           # local wheel/sdist file or source dir
  MK_SPEC="${PKG_ABS}[mediakeys]"
  if [ -d "$PKG_ABS" ]; then
    SRC_DESC="local source dir $PKG_ABS"
  else
    SRC_DESC="local package $PKG_ABS"
  fi
elif [ -n "$VERSION" ]; then
  SPEC="kenzy[${EXTRAS}]==${VERSION}"
  MK_SPEC="kenzy[mediakeys]==${VERSION}"
  SRC_DESC="PyPI (pinned $VERSION)"
else
  # Floor at 3.0 so we never resolve the legacy 2.x monolith still on PyPI.
  SPEC="kenzy[${EXTRAS}]>=3.0.0"
  MK_SPEC="kenzy[mediakeys]>=3.0.0"
  SRC_DESC="PyPI"
fi

# --- virtualenv + install ----------------------------------------------------
step "Creating virtualenv ($VENV)"
mkdir -p "$(dirname "$VENV")"
python3 -m venv "$VENV"
# shellcheck disable=SC1091
. "$VENV/bin/activate"
python -m pip install --upgrade pip >/dev/null

CONSTRAINT_ARGS=()
[ -n "$CONSTRAINTS" ] && CONSTRAINT_ARGS=(-c "$CONSTRAINTS")

# torch's default Linux/x86 wheels bundle the CUDA runtime — roughly 2.7 GB of
# nvidia-* packages that a machine with no NVIDIA card can never use. Both the
# speaker extra (SpeechBrain) and the local Kokoro voice pull torch, so every
# server install was paying for a GPU it might not have. Installing the CPU
# build FIRST satisfies `torch>=2.0.0`, and the resolver then leaves it alone.
#
# Auto-detected because being wrong is expensive in both directions: a GPU host
# silently losing acceleration is as bad as a CPU host wasting the disk. Only
# Linux/x86 has CUDA wheels at all — macOS and arm64 are already CPU-only, so
# they skip this entirely.
case "$EXTRAS" in
  *speaker*|*kokoro*) TORCH_WANTED=1 ;;
  *) TORCH_WANTED=0 ;;
esac
if [ "$TORCH_WANTED" = "1" ] && [ "$(uname -s)" = "Linux" ] && [ "$(uname -m)" = "x86_64" ]; then
  if [ "$CPU_TORCH" = "auto" ]; then
    if { command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; } \
       || [ -e /proc/driver/nvidia/version ]; then
      CPU_TORCH=0
      info "NVIDIA GPU detected — using torch's standard CUDA build."
    else
      CPU_TORCH=1
    fi
  fi
  if [ "$CPU_TORCH" = "1" ]; then
    step "Installing CPU-only torch (skips ~2.7 GB of CUDA runtime this host cannot use)"
    # Tolerated failure, like the media-keys step: if the CPU index is
    # unreachable the normal resolution below still runs. You lose the disk
    # saving, never the install.
    if ! pip install "${CONSTRAINT_ARGS[@]}" \
         --index-url https://download.pytorch.org/whl/cpu torch torchaudio; then
      warn "CPU-only torch failed — falling back to the standard build."
      warn "The install continues; it will just be larger. Use --cuda-torch to skip this step."
    fi
  fi
fi

step "Installing Kenzy [$EXTRAS] from $SRC_DESC (this can take a few minutes)"
pip install "${CONSTRAINT_ARGS[@]}" "$SPEC"

if [ -n "$PACKAGE" ]; then
  # A local --package install is authoritative by definition: the operator
  # handed us the artifact. pip's version-equality check defeats that — a
  # rebuilt wheel carrying the SAME version as the installed one reports
  # "already satisfied" and keeps the OLD code (lived 2026-08-29: a lab
  # upgrade silently no-opped and the new kenzy-s2s entry point never
  # installed, 203/EXEC crash loop). Force the kenzy dist itself; deps stay
  # untouched — the resolve above already satisfied any new ones. Normal
  # (PyPI / version-pinned) installs never take this path.
  step "Forcing the local package over any same-version install"
  pip install "${CONSTRAINT_ARGS[@]}" --no-deps --force-reinstall "$PKG_ABS"
fi

if [ "$WAKEWORD_NODEPS" = "1" ]; then
  step "Installing the wake-word engine (without its tflite dependency)"
  if pip install "${CONSTRAINT_ARGS[@]}" --no-deps openwakeword \
     && pip install "${CONSTRAINT_ARGS[@]}" $WAKEWORD_DEPS; then
    info "openwakeword installed — the ONNX wake-word model will be used."
  else
    warn "Could not install the wake-word engine. The node will start but cannot"
    warn "detect its wake word. See https://docs.kenzy.ai/install-reference/"
  fi
fi

# Volume buttons, in their own step ON PURPOSE: evdev compiles from source, so
# this is the one dependency that can fail on a host without a toolchain. Kenzy
# is already installed at this point, and a node with no evdev reports volume
# buttons as an inactive no-op — so a failure here costs the feature, never the
# install. Same reasoning guards the upgrade path (the node only carries the
# mediakeys extra into pip when evdev already imports).
if [ "$USE_MEDIAKEYS" = "1" ]; then
  step "Installing volume-button support (evdev builds from source)"
  if ! pip install "${CONSTRAINT_ARGS[@]}" "$MK_SPEC"; then
    warn "evdev did not build — speakerphone volume buttons will stay unavailable."
    warn "Everything else installed fine. To add it later:"
    warn "  $SUDO apt-get install gcc python3-dev && $VENV/bin/pip install evdev"
    USE_MEDIAKEYS=0
  fi
fi

# Reading /dev/input/* needs the 'input' group — deliberately AFTER the build
# above, so a host that couldn't compile evdev doesn't get a standing privilege
# grant it can't use. The group lands in the user's NEXT session, so the node
# service (systemd --user) only sees it after a reboot/re-login; until then the
# feature is a labeled no-op, as it is until volume_buttons is enabled per node.
if [ "$USE_MEDIAKEYS" = "1" ]; then
  step "Volume buttons: adding $USER to the 'input' group (for /dev/input access)"
  info "(this grants read access to every input device — skip it with --no-media-keys)"
  if $SUDO usermod -aG input "$USER"; then
    warn "Group membership applies at next login — REBOOT (or log out/in) before"
    warn "enabling volume_buttons on this node, or the watcher sees no devices."
  else
    warn "Could not add $USER to 'input' — run: sudo usermod -aG input $USER"
  fi
fi

# --- models ------------------------------------------------------------------
step "Downloading inference models (kenzy-setup)"
kenzy-setup || warn "kenzy-setup hit a snag — re-run it later (it's on your PATH)."

# --- config scaffold ---------------------------------------------------------
# A node scaffolds only node.yaml (it pulls tuning from the server); server/all
# get the full config home with the dashboard turned on for the handoff below.
step "Scaffolding config home ($KENZY_HOME)"
INIT_ARGS=(--profile "$PROFILE")
[ -n "$NODE_ID" ] && INIT_ARGS+=(--node-id "$NODE_ID")
[ -n "$TOKEN" ] && INIT_ARGS+=(--token "$TOKEN")
[ -n "$CONSTRAINTS" ] && INIT_ARGS+=(--constraints "$CONSTRAINTS")
[ "$PROFILE" != "node" ] && INIT_ARGS+=(--enable-dashboard)
KENZY_HOME="$KENZY_HOME" kenzy-init "$KENZY_HOME" "${INIT_ARGS[@]}"

# --- apply the language-model choice (server/all only) ------------------------
# Choices land in configs/services/llm.yaml — the server-owned override the
# backend services actually pull (the flat configs/llm.yaml is only a local-
# mode fallback) — so the dashboard's Services editor sees and owns them.
set_env_key() { # set_env_key NAME VALUE — upsert into the config home's .env
  local envf="$KENZY_HOME/.env"
  touch "$envf"; chmod 600 "$envf" 2>/dev/null || true
  if grep -q "^${1}=" "$envf" 2>/dev/null; then
    sed -i "s|^${1}=.*|${1}=\"${2}\"|" "$envf"
  else
    printf '%s="%s"\n' "$1" "$2" >> "$envf"
  fi
}
if [ "$PROFILE" != "node" ] && [ "$LLM" != "skip" ] && [ -n "$LLM" ]; then
  SVC_DIR="$KENZY_HOME/configs/services"; mkdir -p "$SVC_DIR"
  if [ -n "$LLM_MODEL" ] || [ -n "$LLM_URL" ]; then
    {
      printf '# Written by install.sh — the language-model choice made at install time.\n'
      printf '# Edit here or from the dashboard (Services -> llm).\n'
      [ -n "$LLM_MODEL" ] && printf 'model: "%s"\n' "$LLM_MODEL"
      [ -n "$LLM_URL" ] && printf 'base_url: "%s"\n' "$LLM_URL"
    } > "$SVC_DIR/llm.yaml"
    info "wrote $SVC_DIR/llm.yaml (model ${LLM_MODEL:-packaged default}${LLM_URL:+ @ $LLM_URL})"
  fi
  if [ -n "$LLM_KEY" ]; then
    set_env_key "$LLM_KEY_NAME" "$LLM_KEY"
    info "stored $LLM_KEY_NAME in $KENZY_HOME/.env (never displayed again)"
  elif [ -n "$LLM_KEY_NAME" ]; then
    warn "$LLM_KEY_NAME not set — add it in the dashboard (Settings -> API keys) before talking."
  fi
fi
if [ "$PROFILE" != "node" ] && [ "$USE_KOKORO" = "1" ]; then
  mkdir -p "$KENZY_HOME/configs/services"
  printf '# Written by install.sh — the fully local voice chosen at install time.\nprovider: "kokoro"\n' > "$KENZY_HOME/configs/services/tts.yaml"
  info "voice: local Kokoro (configs/services/tts.yaml)"
fi

# --- TLS setup (server/all only) ----------------------------------------------
# Generates a self-signed pair into the config home (unless --tls-cert/--tls-key
# supplied one) and appends the tls: block to server.yaml. One pair covers both
# listeners: the node WebSocket port (wss) and the dashboard (https).
DASH_SCHEME="http"
if [ "$TLS" = "1" ]; then
  step "Enabling TLS"
  SERVER_YAML="$KENZY_HOME/configs/server.yaml"
  if [ -z "$TLS_CERT" ]; then
    CERT_DIR="$KENZY_HOME/certs"
    TLS_CERT="$CERT_DIR/kenzy.crt"; TLS_KEY="$CERT_DIR/kenzy.key"
    if [ -s "$TLS_CERT" ] && [ -s "$TLS_KEY" ]; then
      info "reusing existing certificate $TLS_CERT"
    elif command -v openssl >/dev/null 2>&1; then
      mkdir -p "$CERT_DIR"
      openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
        -keyout "$TLS_KEY" -out "$TLS_CERT" -subj "/CN=$(hostname)" 2>/dev/null
      chmod 600 "$TLS_KEY"
      info "generated a self-signed certificate (CN=$(hostname), valid 10 years) in $CERT_DIR"
    else
      warn "openssl not found — staying plaintext. Enable TLS later: generate a cert and"
      warn "add tls: {cert, key} to $SERVER_YAML (see docs.kenzy.ai)."
      TLS=0
    fi
  fi
  if [ "$TLS" = "1" ]; then
    if grep -q '^tls:' "$SERVER_YAML" 2>/dev/null; then
      info "server.yaml already has a tls: block — left as-is"
    else
      printf '\n# TLS (added by install.sh): wss on the node port + https on the dashboard;\n# co-located backend services pick the pair up via config-pull and serve https too.\ntls:\n  cert: %s\n  key: %s\n' \
        "$TLS_CERT" "$TLS_KEY" >> "$SERVER_YAML"
      info "wrote tls: {cert, key} into $SERVER_YAML"
    fi
    DASH_SCHEME="https"
  fi
fi

# --- link entry points onto PATH ---------------------------------------------
step "Linking commands into $BINDIR"
mkdir -p "$BINDIR"
for cmd in "$VENV"/bin/kenzy-*; do
  [ -e "$cmd" ] || continue
  ln -sf "$cmd" "$BINDIR/$(basename "$cmd")"
done
case ":$PATH:" in
  *":$BINDIR:"*) ON_PATH=1 ;;
  *) ON_PATH=0 ;;
esac

# --- run hints (used if we don't set up systemd) -----------------------------
case "$PROFILE" in
  node)   RUN_HINT="kenzy-node" ;;
  server) RUN_HINT="kenzy-server   # then: kenzy-stt / kenzy-tts / kenzy-llm / kenzy-speaker / kenzy-s2s" ;;
  all)    RUN_HINT="kenzy-server   # plus stt/tts/llm/speaker, and kenzy-node on each room device" ;;
esac
DASH_URL="$DASH_SCHEME://127.0.0.1:8770"   # dashboard default bind/port (localhost)

# --- systemd --user services -------------------------------------------------
# Per-user units: no root, no <1024 ports, started at boot via lingering. We skip
# cleanly (with manual run hints) when --no-service is set or there's no user bus.
UNIT_DIR="$HOME/.config/systemd/user"
SERVICE_SETUP=0
if [ "$NO_SERVICE" = "1" ]; then
  warn "Skipping systemd setup (--no-service). Start manually: $RUN_HINT"
elif ! command -v systemctl >/dev/null 2>&1 || ! systemctl --user show-environment >/dev/null 2>&1; then
  warn "systemd --user is not available here — skipping service setup."
  warn "Start Kenzy manually (see 'Next steps' below)."
else
  step "Installing systemd --user services"
  mkdir -p "$UNIT_DIR"
  for svc in $SVC_LIST; do
    # Backend services pull their config from the server at boot, so order them
    # after it when co-installed (ignored by systemd if kenzy-server isn't local).
    after_server=""
    case "$svc" in
      node)    desc="Kenzy Node (wake word + audio capture)" ;;
      server)  desc="Kenzy Server (WebSocket hub + pipeline)" ;;
      stt)     desc="Kenzy STT (speech-to-text)"; after_server=1 ;;
      tts)     desc="Kenzy TTS (text-to-speech)"; after_server=1 ;;
      llm)     desc="Kenzy LLM (language model)"; after_server=1 ;;
      speaker) desc="Kenzy Speaker (voice identification)"; after_server=1 ;;
      *)       desc="Kenzy $svc" ;;
    esac
    # A node joins with a signed, timestamp-fresh hello, so it cannot register
    # while its clock is wrong. Boards without a reliable RTC (most SBCs) come up
    # with whatever the hardware clock drifted to and are refused until NTP steps
    # them — ordering after time-sync.target waits for that instead. Harmless on
    # a host with a good clock; see also chrony-wait below.
    after_time=""
    [ "$svc" = "node" ] && after_time=1
    cat > "$UNIT_DIR/kenzy-$svc.service" <<EOF
[Unit]
Description=$desc
After=network-online.target
Wants=network-online.target
${after_time:+After=time-sync.target}
${after_time:+Wants=time-sync.target}
${after_server:+After=kenzy-server.service}

[Service]
Type=simple
WorkingDirectory=$KENZY_HOME
Environment=KENZY_HOME=$KENZY_HOME
EnvironmentFile=-$KENZY_HOME/.env
ExecStart=$VENV/bin/kenzy-$svc
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF
    info "wrote kenzy-$svc.service"
  done
  systemctl --user daemon-reload
  for svc in $SVC_LIST; do
    if systemctl --user enable --now "kenzy-$svc.service" 2>/dev/null; then
      info "started kenzy-$svc"
    else
      warn "could not start kenzy-$svc (check: systemctl --user status kenzy-$svc)"
    fi
  done
  SERVICE_SETUP=1
  # Linger lets user services run at boot without an active login (one sudo call).
  if confirm "Start Kenzy at boot? (one sudo call: loginctl enable-linger $USER)"; then
    if $SUDO loginctl enable-linger "$USER"; then
      info "lingering enabled — services start at boot"
    else
      warn "couldn't enable linger; services start on your next login instead"
    fi
  fi

  # Two small host settings that only matter when something goes wrong — which is
  # exactly when you cannot go back and enable them.
  #
  #  * Persistent journals. The default on many images is volatile, so a node's
  #    logs die with the reboot you performed to fix it, taking the evidence with
  #    them. A room that was mysteriously offline for two days is unexplainable
  #    without this.
  #  * chrony-wait. Ordering the unit after time-sync.target does nothing under
  #    chrony unless this is enabled — the target is reached without anything
  #    having actually synced.
  if confirm "Keep logs across reboots and wait for clock sync at boot? (recommended)"; then
    if $SUDO mkdir -p /etc/systemd/journald.conf.d 2>/dev/null && \
       printf '[Journal]\nStorage=persistent\n' | \
         $SUDO tee /etc/systemd/journald.conf.d/kenzy.conf >/dev/null 2>&1; then
      $SUDO systemctl restart systemd-journald >/dev/null 2>&1 || true
      info "journals are now persistent (journalctl --user -u kenzy-* survives reboots)"
    else
      warn "could not enable persistent journals — logs will be lost on reboot"
    fi
    if systemctl list-unit-files chrony-wait.service >/dev/null 2>&1 && \
       systemctl list-unit-files chrony-wait.service 2>/dev/null | grep -q chrony-wait; then
      $SUDO systemctl enable chrony-wait.service >/dev/null 2>&1 \
        && info "chrony-wait enabled — boot waits for a synced clock" \
        || warn "could not enable chrony-wait.service"
    fi
  fi
fi

printf "\n${GRN}${B}  Kenzy is installed.${R}\n\n"
info "Profile:     $PROFILE"
info "Virtualenv:  $VENV"
info "Config home: $KENZY_HOME"
info "Commands:    $BINDIR/kenzy-*"

printf "\n${B}Next steps${R}\n"
if [ "$ON_PATH" = "0" ]; then
  info "0. Add ~/.local/bin to your PATH (then restart your shell):"
  info "     echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.profile"
fi
if [ "$PROFILE" != "node" ]; then
  if [ "$LLM" = "ollama" ]; then
    info "1. She thinks on ${LLM_URL} (${LLM_MODEL#ollama/}) — make sure that"
    info "   model is pulled there:  ollama pull ${LLM_MODEL#ollama/}"
  elif [ -n "$LLM_KEY" ]; then
    info "1. Your $LLM_KEY_NAME is stored — she's ready to think."
  elif [ -n "$LLM_KEY_NAME" ]; then
    info "1. Add your $LLM_KEY_NAME in the dashboard (Settings -> API keys), then"
    info "   restart the llm + tts services from the Services tab."
  else
    info "1. Pick her language model in the dashboard (Services -> llm), or:"
    info "     \$EDITOR $KENZY_HOME/configs/services/llm.yaml"
  fi
fi
if [ "$SERVICE_SETUP" = "1" ]; then
  info "2. Manage services:"
  info "     systemctl --user status  kenzy-*"
  info "     systemctl --user restart kenzy-server     # after editing config"
  [ "$PROFILE" != "node" ] && info "Dashboard:       ${B}$DASH_URL${R}  (server is running)"
else
  info "2. Run it:"
  info "     $RUN_HINT"
  [ "$PROFILE" != "node" ] && info "Dashboard:       $DASH_URL  (once kenzy-server is running)"
fi
printf "\n"
[ "$TLS" = "1" ] && info "TLS is on:       your browser shows a one-time warning for the self-signed cert"
[ "$PROFILE" != "server" ] && info "Find your mic:   kenzy-devices"
[ "$PROFILE" != "server" ] && info "Calibrate audio: dashboard → this node → 'Set up / calibrate audio…' (one"
[ "$PROFILE" != "server" ] && info "                 automated pass: measures the room + your voice, applies itself)"
[ "$PROFILE" != "server" ] && info "                 headless fallback: systemctl --user stop kenzy-node && kenzy-node --calibrate"
[ "$PROFILE" != "node" ]   && info "Enroll a voice:  kenzy-enroll"
info "Docs:            https://docs.kenzy.ai"
printf "\n"
