#!/usr/bin/env bash
# Starts Digidaw without a terminal (the HUD is the interface); output goes to logs/digidaw.log.
# Extra arguments are passed on, e.g.  ./start_digidaw.sh --background
DIR="$(dirname "$(readlink -f "$0")")"
VENV="${DIGIDAW_VENV:-${XDG_DATA_HOME:-$HOME/.local/share}/digidaw/venv}"
cd "$DIR" || exit 1

if [[ ! -x "$VENV/bin/python" ]]; then
    notify-send -u critical "Digidaw" "Belum disetup. Jalankan ./setup_digidaw.sh di folder $DIR" 2>/dev/null
    exit 1
fi

# started from autostart / the app menu the session variables may be incomplete: find Hyprland and Wayland
RUNTIME="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if [[ -z "${HYPRLAND_INSTANCE_SIGNATURE:-}" && -d "$RUNTIME/hypr" ]]; then
    HYPRLAND_INSTANCE_SIGNATURE="$(ls -t "$RUNTIME/hypr" 2>/dev/null | head -n1)"
    export HYPRLAND_INSTANCE_SIGNATURE
fi
if [[ -z "${WAYLAND_DISPLAY:-}" ]]; then
    sock="$(ls "$RUNTIME" 2>/dev/null | grep -E '^wayland-[0-9]+$' | head -n1)"
    [[ -n "$sock" ]] && export WAYLAND_DISPLAY="$sock"
fi
export PATH="$HOME/.local/bin:$PATH"          # claude / agy (Project Builder)
export PYTHONIOENCODING=utf-8 DIGIDAW_NO_CONSOLE=1

mkdir -p logs
exec "$VENV/bin/python" main.py "$@" </dev/null >>logs/digidaw.log 2>&1
