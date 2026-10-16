#!/usr/bin/env bash
#
# Chrysa launcher — sets everything up on first run, then starts the app.
#
#   1. detects the platform: a Linux distribution (/etc/os-release) gets missing system
#      packages installed with apt, dnf, pacman or zypper (sudo only if something is
#      missing); Windows 10/11 (Git Bash/MSYS2/Cygwin, or run.bat) gets Python, VLC and
#      FFmpeg installed with winget
#   2. creates .venv and installs requirements.txt (re-checked on every launch)
#   3. Linux: writes the Chrysa icon and ~/.local/share/applications/chrysa.desktop
#      Windows: adds a Chrysa shortcut to the Start menu
#   4. starts Chrysa (Linux: on X11/XWayland, QT_QPA_PLATFORM=xcb)
#
# Usage:  ./run.sh [--setup-only] [--skip-system-deps] [--ask-shortcut] [chrysa options…]
#         ./run.sh --scan --list        # headless: rescan and print the library
#
set -Eeuo pipefail

APP_NAME="Chrysa"
IS_WINDOWS=0
case "$(uname -s 2>/dev/null)" in MINGW*|MSYS*|CYGWIN*) IS_WINDOWS=1 ;; esac

APP_DIR="$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")" && pwd -P)"
RUN_SH="$APP_DIR/run.sh"
VENV_DIR="$APP_DIR/.venv"
if (( IS_WINDOWS )); then VENV_BIN="$VENV_DIR/Scripts" VENV_PY="$VENV_DIR/Scripts/python.exe"
else VENV_BIN="$VENV_DIR/bin" VENV_PY="$VENV_DIR/bin/python"; fi
REQUIREMENTS="$APP_DIR/requirements.txt"
DATA_DIR="$HOME/.local/share/chrysa"
ICON_FILE="$HOME/.local/share/icons/hicolor/scalable/apps/chrysa.svg"
DESKTOP_FILE="$HOME/.local/share/applications/chrysa.desktop"

SETUP_ONLY=0
ASK_SHORTCUT=0
SKIP_SYSTEM_DEPS="${CHRYSA_SKIP_SYSTEM_DEPS:-0}"
APP_ARGS=()
for arg in "$@"; do
    case "$arg" in
        --setup-only) SETUP_ONLY=1 ;;
        --skip-system-deps) SKIP_SYSTEM_DEPS=1 ;;
        --ask-shortcut) ASK_SHORTCUT=1 ;;
        -h|--help)
            sed -n "2,16p" "$RUN_SH" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) APP_ARGS+=("$arg") ;;
    esac
done

INTERACTIVE=0
[[ -t 0 && -t 2 ]] && INTERACTIVE=1

if [[ -t 2 ]]; then RED=$'\033[1;31m' YELLOW=$'\033[1;33m' RESET=$'\033[0m'; else RED="" YELLOW="" RESET=""; fi
log()  { printf '%s[chrysa]%s %s\n' "$RED" "$RESET" "$*" >&2; }
warn() { printf '%s[chrysa]%s %s\n' "$YELLOW" "$RESET" "$*" >&2; }
notify() {  # a desktop notification when there is no terminal to read warnings in
    if (( ! INTERACTIVE )) && command -v notify-send >/dev/null 2>&1; then
        notify-send --app-name="$APP_NAME" --icon=dialog-warning "$1" "$2" || true
    fi
}
die() {
    printf '%s[chrysa] error:%s %s\n' "$RED" "$RESET" "$*" >&2
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
# Designed for a freshly installed distribution: everything Chrysa needs (Python,
# venv, pip, VLC, FFmpeg, Qt's X11 libraries) is detected and installed here.

OS_ID=""
if [[ -r /etc/os-release ]]; then
    OS_ID="$( . /etc/os-release && printf '%s' "${ID:-}" )"
fi

detect_package_manager() {
    local like=""
    if [[ -r /etc/os-release ]]; then
        like="$( . /etc/os-release && printf '%s' "${ID_LIKE:-}" )"
    fi
    case " ${OS_ID,,} ${like,,} " in
        *" ubuntu "*|*" debian "*|*" linuxmint "*|*" pop "*|*" raspbian "*|*" neon "*|*" elementary "*|*" zorin "*|*" kali "*|*" mx "*|*" deepin "*)
            echo apt ;;
        *" fedora "*|*" rhel "*|*" centos "*|*" rocky "*|*" almalinux "*|*" nobara "*|*" ol "*)
            echo dnf ;;
        *" arch "*|*" manjaro "*|*" endeavouros "*|*" garuda "*|*" cachyos "*|*" artix "*|*" arcolinux "*)
            echo pacman ;;
        *" suse "*|*" opensuse "*|*" opensuse-leap "*|*" opensuse-tumbleweed "*|*" sles "*)
            echo zypper ;;
        *)  # Unknown ID: go by whichever package manager is present.
            if   command -v apt-get >/dev/null 2>&1; then echo apt
            elif command -v dnf     >/dev/null 2>&1; then echo dnf
            elif command -v pacman  >/dev/null 2>&1; then echo pacman
            elif command -v zypper  >/dev/null 2>&1; then echo zypper
            else echo unknown
            fi ;;
    esac
}

# Python >= 3.10 with working venv + ensurepip (Debian splits these into python3-venv).
python_is_usable() {
    [[ -n "$1" && -x "$1" ]] \
        && "$1" -c 'import sys, venv, ensurepip; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1 \
        && { (( ! IS_WINDOWS )) || python_is_64bit "$1"; }
}

# PyQt6 only ships 64-bit wheels, and python-vlc needs a libVLC of the same width.
python_is_64bit() {
    "$1" -c 'import struct, sys; sys.exit(0 if struct.calcsize("P") == 8 else 1)' >/dev/null 2>&1
}

find_python() {
    if (( IS_WINDOWS )); then
        local exe
        for exe in "$(command -v python 2>/dev/null || true)" "$(command -v python3 2>/dev/null || true)" \
                "$(py -3 -c 'import sys; print(sys.executable)' 2>/dev/null | tr -d '\r' || true)" \
                "$LOCALAPPDATA"/Programs/Python/Python3{14,13,12,11,10}/python.exe \
                "$PROGRAMFILES"/Python3{14,13,12,11,10}/python.exe; do
            # Skip the Microsoft Store stub, which opens the Store instead of running Python.
            [[ "$exe" == *WindowsApps* ]] && continue
            if python_is_usable "$exe"; then echo "$exe"; return 0; fi
        done
        return 1
    fi
    # Prefer the distribution's Python: python3-venv belongs to it, and it shares the
    # system C++ runtime with libVLC (a conda Python on PATH can clash with VLC plugins).
    local candidate
    for candidate in /usr/bin/python3 /usr/bin/python3.{14,13,12,11,10} "$(command -v python3 2>/dev/null || true)"; do
        if python_is_usable "$candidate"; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

# Print the packages that still need installing, one per line.
missing_packages() {
    local pkg
    case "$1" in
        apt)
            # vlc: player + libVLC; ffmpeg: thumbnails and durations; python3-venv/-pip:
            # the virtualenv; the xcb/GL/font libraries are what Qt 6's X11 plugin loads.
            for pkg in vlc ffmpeg python3 python3-venv python3-pip \
                    libxcb-cursor0 libxkbcommon-x11-0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
                    libxcb-render-util0 libxcb-shape0 libxcb-xinerama0 libegl1 libgl1 \
                    libfontconfig1 libdbus-1-3 libnotify-bin; do
                # Multi-arch libraries print one line per architecture, hence grep.
                dpkg-query -W -f='${db:Status-Status}\n' "$pkg" 2>/dev/null | grep -qx installed || echo "$pkg"
            done ;;
        dnf)
            for pkg in vlc python3 python3-pip xcb-util-cursor xcb-util-image xcb-util-keysyms \
                    xcb-util-renderutil xcb-util-wm libxkbcommon-x11 mesa-libEGL mesa-libGL \
                    fontconfig dbus-libs libnotify; do
                rpm -q --quiet "$pkg" 2>/dev/null || echo "$pkg"
            done
            # Python older than 3.10 (RHEL 9 and clones): install a newer one beside it.
            if ! python_is_usable /usr/bin/python3 && ! find_python >/dev/null; then
                for pkg in python3.12 python3.12-pip; do
                    rpm -q --quiet "$pkg" 2>/dev/null || echo "$pkg"
                done
            fi
            # Either Fedora's ffmpeg-free or RPM Fusion's ffmpeg provides the binaries.
            command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1 || echo ffmpeg-free ;;
        pacman)
            for pkg in vlc ffmpeg python python-pip xcb-util-cursor xcb-util-image xcb-util-keysyms \
                    xcb-util-renderutil xcb-util-wm libxkbcommon-x11 libglvnd fontconfig dbus libnotify polkit; do
                pacman -Qq "$pkg" >/dev/null 2>&1 || echo "$pkg"
            done
            # Arch ships VLC's codecs and video outputs as separate plugin packages.
            if ! pacman -Qq vlc-plugins-all >/dev/null 2>&1 && pacman -Si vlc-plugins-all >/dev/null 2>&1; then
                echo vlc-plugins-all
            fi ;;
        zypper)
            for pkg in vlc ffmpeg python3 python3-pip libxcb-cursor0 libxcb-icccm4 libxcb-image0 \
                    libxcb-keysyms1 libxcb-render-util0 libxcb-shape0 libxkbcommon-x11-0 libEGL1 \
                    libGL1 fontconfig libnotify-tools; do
                rpm -q --quiet "$pkg" 2>/dev/null || echo "$pkg"
            done ;;
    esac
}

# The shell command (run as root) that installs the packages. It copes with a stale or
# missing package database, repositories that still need enabling, and a single
# unavailable package (it falls back to installing the rest one by one).
install_command() {  # $1 = package manager, rest = packages
    local manager="$1"; shift
    local pkgs="$*"
    case "$manager" in
        apt)
            printf 'export DEBIAN_FRONTEND=noninteractive; '
            printf 'apt-get update || true; '
            # Ubuntu keeps VLC in "universe"; make sure it is enabled.
            printf 'if [ -z "$(apt-cache policy vlc 2>/dev/null | sed -n "s/.*Candidate: //p" | grep -v none)" ] && command -v add-apt-repository >/dev/null 2>&1; then add-apt-repository -y universe && apt-get update || true; fi; '
            printf 'apt-get install -y %s || { for p in %s; do apt-get install -y "$p" || true; done; }' "$pkgs" "$pkgs" ;;
        dnf)
            # VLC and the full FFmpeg live in RPM Fusion, which a fresh Fedora does not enable.
            printf 'if ! dnf -q list --available vlc >/dev/null 2>&1 && ! rpm -q --quiet vlc; then '
            printf 'if [ -f /etc/fedora-release ]; then dnf install -y "https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %%fedora).noarch.rpm" || true; '
            printf 'else dnf install -y epel-release || true; dnf install -y --nogpgcheck "https://mirrors.rpmfusion.org/free/el/rpmfusion-free-release-$(rpm -E %%rhel).noarch.rpm" || true; fi; fi; '
            printf 'dnf install -y --skip-unavailable %s' "$pkgs" ;;
        pacman)
            printf 'pacman -S --needed --noconfirm %s || pacman -Syu --needed --noconfirm %s || { for p in %s; do pacman -S --needed --noconfirm "$p" || true; done; }' "$pkgs" "$pkgs" "$pkgs" ;;
        zypper)
            printf 'zypper --non-interactive refresh || true; zypper --non-interactive install --no-recommends %s || { for p in %s; do zypper --non-interactive install --no-recommends "$p" || true; done; }' "$pkgs" "$pkgs" ;;
    esac
}

# Run a shell command as root: directly, via sudo, via su, or via a graphical prompt.
run_privileged() {
    local command="$1" declined="$DATA_DIR/.declined-system-packages"
    if (( EUID == 0 )); then
        sh -c "$command"
    elif (( INTERACTIVE )) && command -v sudo >/dev/null 2>&1; then
        log "Installing with sudo (you may be asked for your password)…"
        sudo sh -c "$command"
    elif (( INTERACTIVE )) && command -v su >/dev/null 2>&1; then
        log "sudo is not installed; installing with su (enter the root password)…"
        su -c "$command"
    elif [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]] && command -v pkexec >/dev/null 2>&1 \
            && [[ "$(cat "$declined" 2>/dev/null || true)" != "$command" ]]; then
        # No terminal (started from the app launcher): ask through a graphical prompt.
        pkexec sh -c "$command" || { mkdir -p "$DATA_DIR"; printf '%s' "$command" > "$declined"; return 1; }
    else
        return 1
    fi
}

# True when the Windows PE file $1 (exe/dll) is a 64-bit x86 binary.
pe_is_x64() {
    local offset machine
    [[ -f "$1" ]] || return 1
    offset="$(od -An -t u4 -j 60 -N 4 "$1" 2>/dev/null | tr -d ' ')" || return 1
    [[ -n "$offset" ]] || return 1
    machine="$(od -An -t x2 -j $((offset + 4)) -N 2 "$1" 2>/dev/null | tr -d ' ')"
    [[ "$machine" == 8664 ]]
}

# Directory of a 64-bit VLC install (a 32-bit one in "Program Files (x86)" is ignored).
find_vlc64() {
    local base="${PROGRAMW6432:-${PROGRAMFILES% (x86)}}" dir
    for dir in "$base/VideoLAN/VLC" "$base (x86)/VideoLAN/VLC" "$LOCALAPPDATA/Programs/VideoLAN/VLC"; do
        if pe_is_x64 "$dir/libvlc.dll"; then echo "$dir"; return 0; fi
    done
    return 1
}

# Put the usual install locations of FFmpeg and the 64-bit VLC on PATH (a shell started
# before winget finished does not see them yet) and point python-vlc at the 64-bit libVLC.
windows_refresh_path() {
    local dir vlc
    for dir in "$LOCALAPPDATA/Microsoft/WinGet/Links" "${PROGRAMW6432:-$PROGRAMFILES}/ffmpeg/bin" \
            "$LOCALAPPDATA"/Microsoft/WinGet/Packages/Gyan.FFmpeg*/ffmpeg-*/bin; do
        [[ -d "$dir" ]] && PATH="$PATH:$dir"
    done
    if vlc="$(find_vlc64)"; then
        export PYTHON_VLC_LIB_PATH="$vlc/libvlc.dll" PYTHON_VLC_MODULE_PATH="$vlc/plugins"
    fi
    export PATH
    hash -r
}

windows_missing_packages() {  # prints winget package ids
    find_python >/dev/null || echo Python.Python.3.12
    find_vlc64 >/dev/null || echo VideoLAN.VLC
    command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1 || echo Gyan.FFmpeg
}

ensure_windows_dependencies() {
    local missing=() id
    case "${PROCESSOR_ARCHITEW6432:-${PROCESSOR_ARCHITECTURE:-AMD64}}" in
        x86|X86) warn "This is a 32-bit Windows: PyQt6 and a matching libVLC are only available for 64-bit Windows, so Chrysa cannot run here." ;;
    esac
    windows_refresh_path
    mapfile -t missing < <(windows_missing_packages)
    (( ${#missing[@]} )) || return 0
    command -v winget >/dev/null 2>&1 \
        || { warn "winget was not found: install ${missing[*]} yourself (64-bit Python 3.10+, 64-bit VLC, FFmpeg), then run run.sh again."; return 0; }
    log "Missing or 32-bit: ${missing[*]} — installing the 64-bit versions with winget (Windows may ask for permission)…"
    for id in "${missing[@]}"; do
        # --force: a 32-bit copy of the same package counts as "already installed" otherwise;
        # the 64-bit build is installed beside it and the old one is left alone.
        winget install --id "$id" -e --silent --architecture x64 --force \
            --accept-package-agreements --accept-source-agreements \
            || warn "winget could not install $id."
    done
    windows_refresh_path
    mapfile -t missing < <(windows_missing_packages)
    if (( ${#missing[@]} )); then
        warn "Still missing: ${missing[*]} — if they were just installed, close this window and run run.sh again."
    fi
}

ensure_system_dependencies() {
    if (( IS_WINDOWS )); then ensure_windows_dependencies; return 0; fi
    local manager missing=() command
    manager="$(detect_package_manager)"
    if [[ "$manager" == unknown ]]; then
        warn "Unsupported distribution: install VLC (with libVLC), FFmpeg, Python 3.10+ (venv, pip) and Qt's xcb libraries yourself."
        return 0
    fi
    mapfile -t missing < <(missing_packages "$manager")
    if (( ${#missing[@]} == 0 )); then
        return 0
    fi

    command="$(install_command "$manager" "${missing[@]}")"
    log "Missing system packages: ${missing[*]}"
    if ! run_privileged "$command"; then
        warn "Could not install the system packages automatically."
    fi
    hash -r

    mapfile -t missing < <(missing_packages "$manager")
    if (( ${#missing[@]} )); then
        warn "Still missing: ${missing[*]} — continuing; Chrysa will say so if it cannot work without them."
        warn "To install them yourself, run in a terminal:  sudo sh -c '$command'"
        notify "Chrysa is missing system packages" "Run ./run.sh in a terminal to install: ${missing[*]}"
    fi
}

# --------------------------------------------------------------------------
# 2. Python virtual environment
# --------------------------------------------------------------------------

requirements_fingerprint() {
    { "$VENV_PY" -c 'import sys; print(sys.version)'; cat "$REQUIREMENTS"; } | sha256sum | cut -d' ' -f1
}

modules_present() {
    "$VENV_PY" - <<'PY' 2>/dev/null
import importlib.util, sys
sys.exit(0 if all(importlib.util.find_spec(m) for m in ("PyQt6", "vlc", "guessit", "subliminal")) else 1)
PY
}

ensure_virtualenv() {
    local python stamp="$VENV_DIR/.requirements.sha256" created=0
    if (( IS_WINDOWS )) && [[ -f "$VENV_PY" ]] && ! python_is_64bit "$VENV_PY"; then
        log "The existing .venv is 32-bit; rebuilding it with 64-bit Python."
        rm -rf "$VENV_DIR"
    fi
    if [[ ! -d "$VENV_DIR" ]] || ! "$VENV_PY" -c 'import sys' >/dev/null 2>&1; then
        python="$(find_python)" || die "Python 3.10 or newer with venv and pip is required, and none was found or installable. Install it (Debian/Ubuntu: python3 python3-venv python3-pip) and run ./run.sh again."
        log "Creating virtual environment in .venv ($("$python" --version 2>&1))…"
        "$python" -m venv --clear "$VENV_DIR" \
            || die "Could not create the virtual environment. On Debian/Ubuntu: sudo apt install python3-venv"
        "$VENV_PY" -m pip --version >/dev/null 2>&1 \
            || die "The virtual environment has no pip. On Debian/Ubuntu: sudo apt install python3-venv python3-pip"
        created=1
    fi

    set +u  # activate scripts of older Pythons trip over unset variables
    # shellcheck disable=SC1091
    source "$VENV_BIN/activate"
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

check_libvlc() {
    # python-vlc raises on import when libvlc.so cannot be found, and Instance() fails
    # when VLC's plugins are missing; either way no video would play.
    local output
    if ! output="$("$VENV_PY" -c 'import vlc, sys; sys.exit(0 if vlc.Instance("--quiet") else 1)' 2>&1)"; then
        warn "libVLC is not usable, so videos will not play: ${output##*$'\n'}"
        if (( IS_WINDOWS )); then
            warn "Install 64-bit VLC with:  winget install --id VideoLAN.VLC -e --architecture x64 --force"
        fi
        case "$(( IS_WINDOWS )) $(detect_package_manager)" in
            1*) ;;
            *apt)    warn "Install it with:  sudo apt install vlc libvlc-dev" ;;
            *dnf)    warn "Install it with:  sudo dnf install vlc  (VLC is in RPM Fusion on Fedora)" ;;
            *pacman) warn "Install it with:  sudo pacman -S vlc vlc-plugins-all  (run  sudo pacman -Syu  first if it cannot find the package)" ;;
            *)      warn "Install VLC (the package that provides libvlc.so) with your package manager." ;;
        esac
        notify "Chrysa cannot play videos" "libVLC is missing. Run ./run.sh in a terminal for the install command."
    fi
}

check_ffmpeg() {
    local tool
    for tool in ffmpeg ffprobe; do
        command -v "$tool" >/dev/null 2>&1 \
            || warn "$tool was not found: thumbnails and durations will be missing. Install FFmpeg with your package manager."
    done
}

check_qt() {
    # Qt's X11 plugin fails at startup when one of its shared libraries is absent;
    # name the missing ones instead of leaving a bare "could not load xcb" error.
    local plugin missing
    (( IS_WINDOWS )) && return 0
    plugin="$("$VENV_PY" -c 'import os, PyQt6; print(os.path.join(os.path.dirname(PyQt6.__file__), "Qt6", "plugins", "platforms", "libqxcb.so"))' 2>/dev/null || true)"
    [[ -f "$plugin" ]] && command -v ldd >/dev/null 2>&1 || return 0
    missing="$(ldd "$plugin" 2>/dev/null | sed -n 's/^[[:space:]]*\(.*\) => not found.*/\1/p' | tr '\n' ' ')"
    if [[ -n "${missing// /}" ]]; then
        warn "Qt cannot start: these libraries are missing: $missing"
        warn "Install the packages that provide them (e.g. libxcb-*, libxkbcommon-x11, libEGL/libGL) and run ./run.sh again."
        notify "Chrysa cannot start" "Missing system libraries: $missing"
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
  <title>Chrysa</title>
  <defs>
    <linearGradient id="tile" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#0b2a3a"/>
      <stop offset="1" stop-color="#04090f"/>
    </linearGradient>
    <radialGradient id="bloom" cx="0.5" cy="0.95" r="0.75">
      <stop offset="0" stop-color="#22e5ff" stop-opacity="0.5"/>
      <stop offset="0.6" stop-color="#0aa6d6" stop-opacity="0.16"/>
      <stop offset="1" stop-color="#0aa6d6" stop-opacity="0"/>
    </radialGradient>
    <linearGradient id="glyph" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#e8fdff"/>
      <stop offset="1" stop-color="#27d9f5"/>
    </linearGradient>
  </defs>
  <rect x="8" y="8" width="240" height="240" rx="58" fill="url(#tile)"/>
  <rect x="8" y="8" width="240" height="240" rx="58" fill="url(#bloom)"/>
  <rect x="8.5" y="8.5" width="239" height="239" rx="57.5" fill="none" stroke="#ffffff" stroke-opacity="0.12"/>
  <!-- a capital "C" -->
  <path d="M182 84A66 66 0 1 0 182 172" fill="none" stroke="url(#glyph)" stroke-width="30" stroke-linecap="round"/>
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

make_windows_shortcut() {  # $1 = .lnk path; starts Chrysa without a console window
    local shortcut="$1" pythonw
    pythonw="$(cygpath -w "$VENV_BIN/pythonw.exe")"
    powershell.exe -NoProfile -Command "\$s = (New-Object -ComObject WScript.Shell).CreateShortcut('$(cygpath -w "$shortcut")'); \$s.TargetPath = '$pythonw'; \$s.Arguments = '-m chrysa'; \$s.WorkingDirectory = '$(cygpath -w "$APP_DIR")'; \$s.Description = 'Offline media center'; \$s.Save()" \
        >/dev/null 2>&1
}

install_windows_shortcut() {  # Start menu entry
    local shortcut="$APPDATA/Microsoft/Windows/Start Menu/Programs/$APP_NAME.lnk"
    [[ -f "$shortcut" ]] && return 0
    make_windows_shortcut "$shortcut" && log "Added $APP_NAME to the Start menu." || true
}

desktop_dir() {
    local dir=""
    case "$(uname -s 2>/dev/null)" in
        MINGW*|MSYS*|CYGWIN*) dir="$(powershell.exe -NoProfile -Command '[Environment]::GetFolderPath("Desktop")' 2>/dev/null | tr -d '\r')"
                              [[ -n "$dir" ]] && dir="$(cygpath -u "$dir")" ;;
        *) command -v xdg-user-dir >/dev/null 2>&1 && dir="$(xdg-user-dir DESKTOP 2>/dev/null || true)" ;;
    esac
    [[ -n "$dir" && -d "$dir" ]] || dir="$HOME/Desktop"
    echo "$dir"
}

# Create (or, with "no", remove) the desktop shortcut on any supported OS.
set_desktop_shortcut() {  # $1 = yes|no
    local dir target
    dir="$(desktop_dir)"
    if (( IS_WINDOWS )); then
        target="$dir/$APP_NAME.lnk"
        if [[ "$1" == yes ]]; then make_windows_shortcut "$target" && log "Desktop shortcut created: $target"
        else rm -f "$target"; fi
    elif [[ "$(uname -s)" == Darwin ]]; then
        target="$dir/$APP_NAME.command"
        if [[ "$1" == yes ]]; then
            printf '#!/bin/bash\nexec "%s"\n' "$RUN_SH" > "$target" && chmod +x "$target" && log "Desktop shortcut created: $target"
        else rm -f "$target"; fi
    else
        target="$dir/$APP_NAME.desktop"
        if [[ "$1" == yes ]]; then
            mkdir -p "$dir"
            cp -f "$DESKTOP_FILE" "$target" && chmod +x "$target"
            # GNOME and Cinnamon only run a desktop file marked as trusted.
            command -v gio >/dev/null 2>&1 && gio set "$target" metadata::trusted true >/dev/null 2>&1 || true
            log "Desktop shortcut created: $target"
        else rm -f "$target"; fi
    fi
}

# Ask once (the answer is remembered; --ask-shortcut asks again). Needs a terminal.
ask_desktop_shortcut() {
    local answer="" file="$DATA_DIR/.desktop-shortcut"
    (( INTERACTIVE )) || return 0
    if (( ! ASK_SHORTCUT )) && [[ -f "$file" ]]; then return 0; fi
    while [[ "${answer,,}" != y* && "${answer,,}" != n* ]]; do
        read -r -p "[chrysa] Create a desktop shortcut for $APP_NAME? [y/n] " answer || return 0
    done
    mkdir -p "$DATA_DIR"
    if [[ "${answer,,}" == y* ]]; then set_desktop_shortcut yes; echo yes > "$file"
    else set_desktop_shortcut no; echo no > "$file"; log "No desktop shortcut."; fi
}

install_desktop_integration() {
    if (( IS_WINDOWS )); then install_windows_shortcut; return 0; fi
    local changed=0
    write_icon | install_if_changed "$APP_DIR/assets/chrysa.svg" || true
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
check_libvlc
check_ffmpeg
check_qt
install_desktop_integration
ask_desktop_shortcut

# libVLC renders into an X11 window. Forcing Qt onto X11 (XWayland in a Wayland
# session) keeps the video embedded instead of popping out into its own window.
if (( ! IS_WINDOWS )); then export QT_QPA_PLATFORM=xcb; fi

if (( SETUP_ONLY )); then
    log "Setup complete. Start $APP_NAME with ./run.sh or from the application launcher."
    exit 0
fi

trap - ERR
if (( ! INTERACTIVE )) && (( ${#APP_ARGS[@]} == 0 )); then
    # Started from the launcher: keep the last run's output for troubleshooting.
    mkdir -p "$DATA_DIR"
    exec "$VENV_PY" -m chrysa >"$DATA_DIR/chrysa.log" 2>&1
fi
exec "$VENV_PY" -m chrysa "${APP_ARGS[@]}"
