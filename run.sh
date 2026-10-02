#!/usr/bin/env bash
#
# Theia launcher — sets everything up on first run, then starts the app.
#
#   1. detects the distribution (/etc/os-release) and installs missing system
#      packages with apt, dnf or pacman (asks for sudo only if something is missing)
#   2. creates .venv and installs requirements.txt (re-checked on every launch)
#   3. writes the Theia icon and ~/.local/share/applications/theia.desktop
#   4. starts Theia on X11/XWayland (QT_QPA_PLATFORM=xcb)
#
# Usage:  ./run.sh [--setup-only] [--skip-system-deps] [theia options…]
#         ./run.sh --scan --list        # headless: rescan and print the library
#
set -Eeuo pipefail

APP_NAME="Theia"
APP_DIR="$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")" && pwd -P)"
RUN_SH="$APP_DIR/run.sh"
VENV_DIR="$APP_DIR/.venv"
REQUIREMENTS="$APP_DIR/requirements.txt"
DATA_DIR="$HOME/.local/share/theia"
ICON_FILE="$HOME/.local/share/icons/hicolor/scalable/apps/theia.svg"
DESKTOP_FILE="$HOME/.local/share/applications/theia.desktop"

SETUP_ONLY=0
SKIP_SYSTEM_DEPS="${THEIA_SKIP_SYSTEM_DEPS:-0}"
APP_ARGS=()
for arg in "$@"; do
    case "$arg" in
        --setup-only) SETUP_ONLY=1 ;;
        --skip-system-deps) SKIP_SYSTEM_DEPS=1 ;;
        -h|--help)
            sed -n '2,13p' "$RUN_SH" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) APP_ARGS+=("$arg") ;;
    esac
done

INTERACTIVE=0
[[ -t 0 && -t 2 ]] && INTERACTIVE=1

if [[ -t 2 ]]; then RED=$'\033[1;31m' YELLOW=$'\033[1;33m' RESET=$'\033[0m'; else RED="" YELLOW="" RESET=""; fi
log()  { printf '%s[theia]%s %s\n' "$RED" "$RESET" "$*" >&2; }
warn() { printf '%s[theia]%s %s\n' "$YELLOW" "$RESET" "$*" >&2; }
die() {
    printf '%s[theia] error:%s %s\n' "$RED" "$RESET" "$*" >&2
    # Started from the app launcher there is no terminal to read this in.
    if (( ! INTERACTIVE )) && command -v notify-send >/dev/null 2>&1; then
        notify-send --app-name="$APP_NAME" --icon=dialog-error "$APP_NAME could not start" "$*" || true
    fi
    exit 1
}
trap 'die "setup failed at line $LINENO (see the output above)"' ERR

# --------------------------------------------------------------------------
# 1. System dependencies
# --------------------------------------------------------------------------

detect_package_manager() {
    local id="" like=""
    if [[ -r /etc/os-release ]]; then
        id="$( . /etc/os-release && printf '%s' "${ID:-}" )"
        like="$( . /etc/os-release && printf '%s' "${ID_LIKE:-}" )"
    fi
    case " ${id,,} ${like,,} " in
        *" ubuntu "*|*" debian "*|*" linuxmint "*|*" pop "*|*" raspbian "*|*" neon "*|*" elementary "*|*" zorin "*|*" kali "*)
            echo apt ;;
        *" fedora "*|*" rhel "*|*" centos "*|*" rocky "*|*" almalinux "*|*" nobara "*)
            echo dnf ;;
        *" arch "*|*" manjaro "*|*" endeavouros "*|*" garuda "*|*" cachyos "*|*" artix "*)
            echo pacman ;;
        *)  # Unknown ID: go by whichever package manager is present.
            if   command -v apt-get >/dev/null 2>&1; then echo apt
            elif command -v dnf     >/dev/null 2>&1; then echo dnf
            elif command -v pacman  >/dev/null 2>&1; then echo pacman
            else echo unknown
            fi ;;
    esac
}

# Print the packages that still need installing, one per line.
missing_packages() {
    local pkg
    case "$1" in
        apt)
            # vlc + libvlc-dev: player and libVLC; ffmpeg: thumbnails and durations;
            # python3-venv/-pip: the virtualenv; libxcb-cursor0: required by Qt 6's X11 plugin.
            for pkg in vlc libvlc-dev ffmpeg python3-venv python3-pip libxcb-cursor0; do
                [[ "$(dpkg-query -W -f='${db:Status-Status}' "$pkg" 2>/dev/null || true)" == installed ]] || echo "$pkg"
            done ;;
        dnf)
            for pkg in vlc vlc-devel python3 python3-pip xcb-util-cursor; do
                rpm -q --quiet "$pkg" 2>/dev/null || echo "$pkg"
            done
            # Either Fedora's ffmpeg-free or RPM Fusion's ffmpeg provides the binaries.
            command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1 || echo ffmpeg-free ;;
        pacman)
            for pkg in vlc ffmpeg python python-pip xcb-util-cursor; do
                pacman -Qq "$pkg" >/dev/null 2>&1 || echo "$pkg"
            done
            # Arch ships VLC's codecs and video outputs as separate plugin packages.
            if pacman -Si vlc-plugins-all >/dev/null 2>&1 && ! pacman -Qq vlc-plugins-all >/dev/null 2>&1; then
                echo vlc-plugins-all
            fi ;;
    esac
}

install_command() {  # $1 = package manager, rest = packages
    local manager="$1"; shift
    case "$manager" in
        apt)    printf 'apt-get update || true; DEBIAN_FRONTEND=noninteractive apt-get install -y %s' "$*" ;;
        dnf)    printf 'dnf install -y %s' "$*" ;;
        pacman) printf 'pacman -S --needed --noconfirm %s' "$*" ;;
    esac
}

ensure_system_dependencies() {
    local manager missing=() command declined="$DATA_DIR/.declined-system-packages"
    manager="$(detect_package_manager)"
    if [[ "$manager" == unknown ]]; then
        warn "Unsupported distribution: install VLC (with libVLC), FFmpeg and Python 3 venv/pip yourself."
        return 0
    fi
    mapfile -t missing < <(missing_packages "$manager")
    if (( ${#missing[@]} == 0 )); then
        rm -f "$declined"
        return 0
    fi

    command="$(install_command "$manager" "${missing[@]}")"
    log "Missing system packages: ${missing[*]}"
    if (( EUID == 0 )); then
        sh -c "$command" || warn "Package installation failed."
    elif (( INTERACTIVE )) && command -v sudo >/dev/null 2>&1; then
        log "Installing them with sudo (you may be asked for your password)…"
        sudo sh -c "$command" || warn "Package installation was cancelled or failed."
    elif [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]] && command -v pkexec >/dev/null 2>&1 \
            && [[ "$(cat "$declined" 2>/dev/null || true)" != "${missing[*]}" ]]; then
        # No terminal (started from the app launcher): ask once through a graphical prompt.
        if ! pkexec sh -c "$command"; then
            mkdir -p "$DATA_DIR"
            printf '%s' "${missing[*]}" > "$declined"
            warn "Package installation was cancelled or failed."
        fi
    else
        warn "Cannot ask for administrator rights here. Run this in a terminal:"
        warn "    sudo sh -c '$command'"
    fi

    mapfile -t missing < <(missing_packages "$manager")
    if (( ${#missing[@]} )); then
        warn "Still missing: ${missing[*]} — continuing; Theia will say so if it cannot work without them."
    else
        rm -f "$declined"
    fi
}

# --------------------------------------------------------------------------
# 2. Python virtual environment
# --------------------------------------------------------------------------

find_python() {
    # Prefer the distribution's Python: python3-venv belongs to it, and it shares the
    # system C++ runtime with libVLC (a conda Python on PATH can clash with VLC plugins).
    local candidate
    for candidate in /usr/bin/python3 "$(command -v python3 2>/dev/null || true)"; do
        if [[ -n "$candidate" && -x "$candidate" ]] \
                && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

requirements_fingerprint() {
    { "$VENV_DIR/bin/python" -c 'import sys; print(sys.version)'; cat "$REQUIREMENTS"; } | sha256sum | cut -d' ' -f1
}

modules_present() {
    "$VENV_DIR/bin/python" - <<'PY' 2>/dev/null
import importlib.util, sys
sys.exit(0 if all(importlib.util.find_spec(m) for m in ("PyQt6", "vlc", "guessit")) else 1)
PY
}

ensure_virtualenv() {
    local python stamp="$VENV_DIR/.requirements.sha256" created=0
    if [[ ! -d "$VENV_DIR" ]] || ! "$VENV_DIR/bin/python" -c 'import sys' >/dev/null 2>&1; then
        python="$(find_python)" || die "Python 3.10 or newer is required (python3 was not found)."
        log "Creating virtual environment in .venv ($("$python" --version 2>&1))…"
        "$python" -m venv --clear "$VENV_DIR" \
            || die "Could not create the virtual environment. On Debian/Ubuntu: sudo apt install python3-venv"
        created=1
    fi

    set +u  # activate scripts of older Pythons trip over unset variables
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
    set -u

    if (( created )); then
        log "Upgrading pip…"
        python -m pip install --quiet --disable-pip-version-check --upgrade pip || warn "Could not upgrade pip."
    fi
    if (( created )) || [[ "$(cat "$stamp" 2>/dev/null || true)" != "$(requirements_fingerprint)" ]] || ! modules_present; then
        log "Installing Python requirements…"
        if python -m pip install --quiet --disable-pip-version-check -r "$REQUIREMENTS"; then
            requirements_fingerprint > "$stamp"
        elif modules_present; then
            warn "pip could not refresh the requirements (offline?); using what is installed."
        else
            die "Installing the Python requirements failed. Check your internet connection and run ./run.sh again."
        fi
    fi
}

# --------------------------------------------------------------------------
# 3. Icon and desktop entry
# --------------------------------------------------------------------------

# Install stdin as file $1, leaving it untouched when the content is unchanged.
install_if_changed() {
    local target="$1" tmp
    mkdir -p "$(dirname "$target")"
    tmp="$(mktemp "${target}.XXXXXX")"
    cat > "$tmp"
    if [[ -f "$target" ]] && cmp -s "$tmp" "$target"; then
        rm -f "$tmp"
        return 1
    fi
    chmod 644 "$tmp"
    mv -f "$tmp" "$target"
}

write_icon() {
    cat <<'SVG'
<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256">
  <title>Theia</title>
  <defs>
    <linearGradient id="tile" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#262626"/>
      <stop offset="1" stop-color="#0a0a0a"/>
    </linearGradient>
    <linearGradient id="ring" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#ff2f3a"/>
      <stop offset="1" stop-color="#b20710"/>
    </linearGradient>
  </defs>
  <rect x="8" y="8" width="240" height="240" rx="54" fill="url(#tile)"/>
  <rect x="8.5" y="8.5" width="239" height="239" rx="53.5" fill="none" stroke="#ffffff" stroke-opacity="0.08"/>
  <!-- the eye of Theia, goddess of sight: a red iris around a play symbol -->
  <circle cx="128" cy="128" r="76" fill="none" stroke="url(#ring)" stroke-width="20"/>
  <path d="M110 87.5v81a6 6 0 0 0 9.1 5.1l66.5-40.5a6 6 0 0 0 0-10.2l-66.5-40.5a6 6 0 0 0-9.1 5.1z" fill="#ffffff"/>
</svg>
SVG
}

desktop_quote() {  # quote one Exec= argument per the Desktop Entry Specification
    local value="$1"
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    value="${value//\$/\\\$}"
    value="${value//\`/\\\`}"
    value="${value//%/%%}"
    printf '"%s"' "$value"
}

install_desktop_integration() {
    local changed=0
    write_icon | install_if_changed "$APP_DIR/assets/theia.svg" || true
    if write_icon | install_if_changed "$ICON_FILE"; then
        changed=1
        if command -v gtk-update-icon-cache >/dev/null 2>&1; then
            gtk-update-icon-cache -q -t -f "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true
        fi
    fi

    if install_if_changed "$DESKTOP_FILE" <<DESKTOP
[Desktop Entry]
Version=1.0
Type=Application
Name=$APP_NAME
GenericName=Media Center
Comment=Offline media center for your local movies and TV shows
Exec=$(desktop_quote "$RUN_SH")
Icon=$ICON_FILE
Terminal=false
Categories=AudioVideo;Video;Player;
Keywords=video;movies;tv;shows;media;player;library;
StartupNotify=true
StartupWMClass=$APP_NAME
DESKTOP
    then
        changed=1
        if command -v desktop-file-validate >/dev/null 2>&1; then
            desktop-file-validate "$DESKTOP_FILE" >&2 || warn "The desktop entry did not validate cleanly."
        fi
        if command -v update-desktop-database >/dev/null 2>&1; then
            update-desktop-database -q "$(dirname "$DESKTOP_FILE")" >/dev/null 2>&1 || true
        fi
    fi
    if (( changed )); then
        log "Registered $APP_NAME in the application launcher ($DESKTOP_FILE)."
    fi
}

# --------------------------------------------------------------------------
# 4. Go
# --------------------------------------------------------------------------

cd "$APP_DIR"
[[ -f "$REQUIREMENTS" ]] || die "requirements.txt is missing from $APP_DIR."

if [[ "$SKIP_SYSTEM_DEPS" != 1 ]]; then
    ensure_system_dependencies
fi
ensure_virtualenv
install_desktop_integration

# libVLC renders into an X11 window. Forcing Qt onto X11 (XWayland in a Wayland
# session) keeps the video embedded instead of popping out into its own window.
export QT_QPA_PLATFORM=xcb

if (( SETUP_ONLY )); then
    log "Setup complete. Start $APP_NAME with ./run.sh or from the application launcher."
    exit 0
fi

trap - ERR
if (( ! INTERACTIVE )) && (( ${#APP_ARGS[@]} == 0 )); then
    # Started from the launcher: keep the last run's output for troubleshooting.
    mkdir -p "$DATA_DIR"
    exec "$VENV_DIR/bin/python" -m theia >"$DATA_DIR/theia.log" 2>&1
fi
exec "$VENV_DIR/bin/python" -m theia "${APP_ARGS[@]}"
