#!/bin/sh
# Stentor installer for macOS and Linux.  Usage: curl -fsSL <site>/install.sh | sh
# Running it again updates to the latest release.
set -eu

REPO="Can-0f-Tuna/stentor"
# Read the old installer variables for upgrades from before 0.10.
stentor_env() {
  eval 'printf "%s" "${STENTOR_'"$1"'-${HOLD_DICTATE_'"$1"'-}}"'
}
say() { printf '\n==> %s\n' "$*"; }

# 1. uv: installs Python and the tool without touching your system Python.
if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv (Python tool installer)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"; export PATH

# 2. Linux system packages (audio library, clipboard and paste helpers).
if [ "$(uname -s)" = "Linux" ] && [ -z "$(stentor_env SKIP_SYSTEM)" ]; then
  if [ -n "${WAYLAND_DISPLAY:-}" ]; then helpers="wl-clipboard wtype"; else helpers="xclip xdotool"; fi
  if command -v apt-get >/dev/null 2>&1; then
    mgr="apt-get install -y"; portaudio="libportaudio2"
  elif command -v dnf >/dev/null 2>&1; then
    mgr="dnf install -y"; portaudio="portaudio"
  elif command -v pacman >/dev/null 2>&1; then
    mgr="pacman -S --needed --noconfirm"; portaudio="portaudio"
  elif command -v zypper >/dev/null 2>&1; then
    mgr="zypper install -y"; portaudio="libportaudio2"
  else
    mgr=""
  fi
  if [ -n "$mgr" ]; then
    say "Installing system packages (needs sudo): $portaudio $helpers"
    for pkg in $portaudio $helpers; do
      sudo $mgr "$pkg" >/dev/null 2>&1 || echo "    could not install $pkg; run 'stentor doctor' later for help"
    done
  else
    echo "Install these with your package manager: PortAudio, $helpers"
  fi
  if ! id -nG | tr ' ' '\n' | grep -qx input; then
    say "Allowing keyboard access (adds you to the 'input' group)"
    sudo usermod -aG input "$USER" && echo "    Log out and back in once so the hotkey works."
  fi
fi

# Python 3.13 is the newest one the offline engine's libraries have wheels for everywhere (see updater.py).
python="$(stentor_env PYTHON)"
python="${python:-3.13}"
source="$(stentor_env SOURCE)"
tag=""
if [ -z "$source" ]; then
  # The newest stable Stentor release, or the main branch if there is none yet; an archive, so no git is needed.
  # Not GitHub's "latest": that stays on hold-dictate 0.9.4 for the copies installed before the rename.
  releases=$(curl -fsSL "https://api.github.com/repos/$REPO/releases?per_page=100" 2>/dev/null) || releases="[]"
  tag=$(printf '%s' "$releases" | uv run --no-project --python "$python" python -c '
import json, re, sys
data = json.load(sys.stdin)
tags = [r["tag_name"] for r in (data if isinstance(data, list) else []) if not (r["draft"] or r["prerelease"])
        and re.fullmatch(r"v\d+\.\d+\.\d+", r["tag_name"])]
versions = {tuple(map(int, tag[1:].split("."))): tag for tag in tags}
print(versions[max(versions)] if versions and max(versions) >= (0, 10, 0) else "")
' 2>/dev/null) || tag=""
  if [ -n "$tag" ]; then ref="tags/$tag"; else ref="heads/main"; fi
fi

tools="$(uv tool dir)"
extras=""
[ -n "$(stentor_env OFFLINE)" ] && extras="[local]"
# Running it again keeps the offline engine if this computer has it (STENTOR_OFFLINE=1 adds it).
# An install from when this was called hold-dictate counts too: its offline engine comes along.
old_tool="$tools/hold-dictate"
for installed_python in "$tools/stentor/bin/python" "$old_tool/bin/python"; do
  if [ -x "$installed_python" ] && "$installed_python" -c 'from importlib.util import find_spec; raise SystemExit(find_spec("faster_whisper") is None)'; then
    extras="[local]"
  fi
done

set --
if [ -z "$source" ]; then
  source="stentor$extras @ https://github.com/$REPO/archive/refs/$ref.tar.gz"
elif [ -n "$extras" ]; then
  set -- "$@" --with 'faster-whisper>=1.0'
fi
say "Installing Stentor $tag"
uv tool install --force --python "$python" "$@" "$source"

say "Starting Stentor"
stentor service install
# An install from when this was called hold-dictate. `service install` has just stopped it and taken its
# start-at-login entry away; its tool goes too, so the old command is not left behind. Its settings were copied.
# Only now, once Stentor runs: an install that fails half way leaves the old copy working.
if [ -d "$old_tool" ]; then
  uv tool uninstall hold-dictate >/dev/null 2>&1 || true
fi
if [ -z "$(stentor_env NO_SETUP)" ]; then
  say "Opening the settings in your browser. Add your API key there."
  stentor settings --wait 10 || echo "Open the settings any time with: stentor settings"
fi
