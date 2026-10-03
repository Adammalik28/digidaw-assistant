#!/usr/bin/env bash
# Digidaw with a terminal, to watch the log live when something goes wrong.
DIR="$(dirname "$(readlink -f "$0")")"
VENV="${DIGIDAW_VENV:-${XDG_DATA_HOME:-$HOME/.local/share}/digidaw/venv}"
cd "$DIR" || exit 1
[[ -x "$VENV/bin/python" ]] || { echo "[DIGIDAW] Belum disetup. Jalankan ./setup_digidaw.sh dulu."; exit 1; }

printf '\033]0;DIGIDAW Console\007'           # window title: Digidaw ignores its own windows
export PATH="$HOME/.local/bin:$PATH" PYTHONIOENCODING=utf-8
"$VENV/bin/python" main.py "$@"
status=$?
if ((status != 0)); then
    echo
    echo "[DIGIDAW Error] Program terhenti dengan kode error $status."
    read -rp "Tekan Enter untuk menutup..." _
fi
exit $status
