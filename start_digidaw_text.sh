#!/usr/bin/env bash
# Digidaw in text mode (microphone off).
exec "$(dirname "$(readlink -f "$0")")/start_digidaw_console.sh" --text "$@"
