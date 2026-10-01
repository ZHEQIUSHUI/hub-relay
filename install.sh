#!/usr/bin/env bash
# Install hub-relay into its own venv and link `relay` into ~/.local/bin.
#   ./install.sh                 # from this checkout
#   ./install.sh <pip-spec>      # e.g. git+https://github.com/ZHEQIUSHUI/hub-relay
#   RELAY_EXTRAS=ms ./install.sh # also install the ModelScope backend
set -euo pipefail
SRC="${1:-$(cd "$(dirname "$0")" && pwd)}"
VENV="${RELAY_VENV:-$HOME/.local/share/hub-relay/venv}"
PY="${PYTHON:-python3}"
if [ ! -x "$VENV/bin/python" ]; then
  mkdir -p "$(dirname "$VENV")"
  if ! "$PY" -m venv "$VENV" 2>/dev/null; then
    # Debian/Ubuntu without python3-venv: bootstrap with virtualenv from pip
    rm -rf "$VENV"
    "$PY" -m pip install --user -q virtualenv 2>/dev/null || "$PY" -m pip install --user --break-system-packages -q virtualenv
    "$PY" -m virtualenv -q "$VENV"
  fi
fi
SPEC="$SRC"
[ -n "${RELAY_EXTRAS:-}" ] && SPEC="$SRC[$RELAY_EXTRAS]"
"$VENV/bin/pip" install -q -U pip
"$VENV/bin/pip" install -q -U "$SPEC"
mkdir -p "$HOME/.local/bin"
ln -sf "$VENV/bin/relay" "$HOME/.local/bin/relay"
echo "installed: $HOME/.local/bin/relay  ($("$VENV/bin/relay" --help | head -1))"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "note: add ~/.local/bin to PATH";; esac
echo "next: relay init --name <this-machine>   &&   relay install-skill"
