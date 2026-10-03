#!/usr/bin/env bash
# DIGIDAW setup for Linux (Arch, Debian/Ubuntu, Fedora). Safe to run again at any time (updates everything).
#
#   ./setup_digidaw.sh                 install / update everything, asking before optional parts
#   ./setup_digidaw.sh --yes           don't ask: accept the recommended choices
#   ./setup_digidaw.sh --autostart     also start Digidaw quietly at login
#   ./setup_digidaw.sh --no-ollama     skip the local AI (Ollama); everything then goes through Gemini
#
# What it does: system packages -> Python 3.12 environment (uv) -> Gemini API key (.env) -> speech models ->
# optional local AI (Ollama + qwen3) -> app-menu entry -> setup check.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
DIR="$PWD"
VENV="${DIGIDAW_VENV:-${XDG_DATA_HOME:-$HOME/.local/share}/digidaw/venv}"
PY_VERSION=3.12
LOCAL_MODEL="qwen3:4b-instruct"

YES=0; AUTOSTART=0; OLLAMA=1
for arg in "$@"; do
    case "$arg" in
        --yes|-y) YES=1 ;;
        --autostart) AUTOSTART=1 ;;
        --no-ollama) OLLAMA=0 ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
        *) echo "Opsi tidak dikenal: $arg (lihat --help)"; exit 1 ;;
    esac
done

say()  { printf '\n\033[1;36m[DIGIDAW]\033[0m %s\n' "$*"; }
info() { printf '          %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[ERROR]\033[0m %s\n' "$*" >&2; }
ask()  {   # ask "question" default(y/n) -> 0 = yes
    local default="$2" reply
    if ((YES)) || [[ ! -t 0 ]]; then [[ "$default" == y ]]; return; fi
    read -r -p "          $1 [$( [[ $default == y ]] && echo Y/n || echo y/N )] " reply
    reply="${reply:-$default}"; [[ "${reply,,}" == y* ]]
}

# ───────────────────────── 1. system packages ─────────────────────────
# portaudio: microphone · pipewire/wireplumber/pactl: volume, beeps, ducking · wl-clipboard/grim: clipboard &
# screenshots (Wayland) · playerctl: media keys · brightnessctl · libnotify · xdg-utils · glib2 (gio): app menu ·
# espeak-ng: offline voice · zenity: folder picker · gcc + headers: building PyAudio
say "Memeriksa program sistem..."
if command -v pacman >/dev/null; then
    PM="pacman"
    PACKAGES=(portaudio pipewire wireplumber libpulse wl-clipboard grim playerctl brightnessctl libnotify xdg-utils
              glib2 espeak-ng zenity pacman-contrib base-devel curl)
    is_installed() { pacman -Qq "$1" &>/dev/null; }
    install_cmd() { sudo pacman -S --needed "$@"; }
elif command -v apt-get >/dev/null; then
    PM="apt"
    PACKAGES=(portaudio19-dev python3-dev build-essential pipewire-bin wireplumber pulseaudio-utils wl-clipboard grim
              playerctl brightnessctl libnotify-bin xdg-utils libglib2.0-bin espeak-ng zenity curl)
    is_installed() { dpkg -s "$1" &>/dev/null; }
    install_cmd() { sudo apt-get update && sudo apt-get install -y "$@"; }
elif command -v dnf >/dev/null; then
    PM="dnf"
    PACKAGES=(portaudio-devel python3-devel gcc pipewire-utils wireplumber pulseaudio-utils wl-clipboard grim playerctl
              brightnessctl libnotify xdg-utils glib2 espeak-ng zenity curl)
    is_installed() { rpm -q "$1" &>/dev/null; }
    install_cmd() { sudo dnf install -y "$@"; }
else
    PM=""
    warn "Package manager tidak dikenali. Install manual: portaudio (+headers), pipewire, wireplumber, pactl,"
    warn "wl-clipboard, grim, playerctl, brightnessctl, libnotify, xdg-utils, espeak-ng, zenity, gcc, curl."
fi
if [[ -n "$PM" ]]; then
    missing=()
    for p in "${PACKAGES[@]}"; do is_installed "$p" || missing+=("$p"); done
    if ((${#missing[@]})); then
        info "Belum terpasang: ${missing[*]}"
        if [[ -t 0 ]]; then
            install_cmd "${missing[@]}" || warn "Sebagian paket gagal dipasang; fitur terkait mungkin tidak jalan."
        else
            err "Jalankan dulu: ${PM} install ${missing[*]}"; exit 1
        fi
    else
        info "Semua program sistem sudah ada."
    fi
fi
if [[ -z "${HYPRLAND_INSTANCE_SIGNATURE:-}" ]]; then
    warn "Hyprland tidak terdeteksi. Digidaw tetap jalan, tetapi kontrol jendela/workspace & hotkey butuh Hyprland."
fi

# ───────────────────────── 2. Python environment ─────────────────────────
if ! command -v uv >/dev/null && [[ ! -x "$HOME/.local/bin/uv" ]]; then
    say "Memasang uv (pengelola Python, dari astral.sh)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"
command -v uv >/dev/null || { err "uv tidak bisa dipasang. Lihat https://docs.astral.sh/uv/"; exit 1; }

say "Menyiapkan Python $PY_VERSION di $VENV ..."
uv python install "$PY_VERSION"
if [[ ! -x "$VENV/bin/python" ]] || ! "$VENV/bin/python" -c "import sys; assert sys.version_info[:2] == (3, 12)" 2>/dev/null; then
    rm -rf "$VENV"
    mkdir -p "$(dirname "$VENV")"
    uv venv --python "$PY_VERSION" "$VENV"
fi
say "Menginstall / memperbarui modul Python (pertama kali bisa beberapa menit)..."
if ! uv pip install --python "$VENV/bin/python" -r requirements.txt; then
    err "Sebagian modul gagal diinstall. Periksa koneksi internet lalu jalankan lagi."
    exit 1
fi
PY="$VENV/bin/python"

# ───────────────────────── 3. Gemini API key ─────────────────────────
[[ -f .env ]] || cp .env.example .env
if ! grep -qE '^GEMINI_API_KEY=.+' .env; then
    say "Gemini API key (untuk pertanyaan online, berita, analisis layar, dll.)"
    info "Gratis: buka https://aistudio.google.com/apikey → 'Create API key', lalu salin ke sini."
    info "Boleh dikosongkan sekarang dan diisi nanti di file .env atau Control Panel → AI."
    if [[ -t 0 ]] && ((!YES)); then
        read -r -s -p "          Tempel API key (Enter = lewati): " KEY; echo
        if [[ -n "${KEY// }" ]]; then
            sed -i "s|^GEMINI_API_KEY=.*|GEMINI_API_KEY=${KEY// }|" .env
            info "Tersimpan di .env (file ini tidak ikut ter-upload ke GitHub)."
        fi
    fi
fi
chmod 600 .env

# ───────────────────────── 4. speech recognition models ─────────────────────────
say "Mengunduh model pengenal suara lokal (Whisper tiny + base, ±220 MB, sekali saja)..."
"$PY" -c "import local_stt, sys; sys.exit(0 if local_stt.ensure_models() else 1)" \
    || warn "Unduhan model gagal; Digidaw akan mencoba lagi saat pertama jalan."

# ───────────────────────── 5. local AI (Ollama) ─────────────────────────
if ((OLLAMA)); then
    say "Otak lokal (Ollama + $LOCAL_MODEL, ±2,5 GB): pertanyaan ringan & perintah jadi cepat, gratis, bisa offline."
    if ! command -v ollama >/dev/null; then
        if ask "Pasang Ollama sekarang?" y; then
            if [[ "$PM" == "pacman" ]]; then
                if command -v nvidia-smi >/dev/null; then sudo pacman -S --needed ollama-cuda
                elif lspci 2>/dev/null | grep -qiE "vga.*(amd|radeon)"; then sudo pacman -S --needed ollama-rocm
                else sudo pacman -S --needed ollama; fi
                sudo systemctl enable --now ollama
            else
                curl -fsSL https://ollama.com/install.sh | sh
            fi
        else
            info "Dilewati. Tanpa Ollama semua pertanyaan lewat Gemini."
        fi
    fi
    if command -v ollama >/dev/null; then
        if ! curl -s --max-time 2 http://127.0.0.1:11434/api/version >/dev/null; then
            (sudo systemctl enable --now ollama 2>/dev/null || (nohup ollama serve >/dev/null 2>&1 &)) || true
            sleep 3
        fi
        if ollama list 2>/dev/null | grep -q "^${LOCAL_MODEL}"; then
            info "$LOCAL_MODEL sudah ada."
        elif ask "Unduh model $LOCAL_MODEL sekarang?" y; then
            ollama pull "$LOCAL_MODEL" || warn "Unduhan model gagal; jalankan nanti: ollama pull $LOCAL_MODEL"
        fi
    fi
fi

# ───────────────────────── 6. app menu entry / autostart ─────────────────────────
chmod +x start_digidaw.sh start_digidaw_console.sh start_digidaw_text.sh 2>/dev/null || true
desktop_entry() {
    cat <<EOF
[Desktop Entry]
Type=Application
Name=Digidaw
Comment=Digidaw - Personal AI Assistant
Exec="$DIR/start_digidaw.sh"${1:+ $1}
Path=$DIR
Icon=$DIR/digidaw.ico
Terminal=false
Categories=Utility;
X-GNOME-Autostart-enabled=true
EOF
}
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$APPS"
desktop_entry > "$APPS/digidaw.desktop"
say "Shortcut 'Digidaw' ditambahkan ke menu aplikasi."
if ((AUTOSTART)) || ask "Jalankan Digidaw otomatis setiap login?" n; then
    AUTOSTART_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
    mkdir -p "$AUTOSTART_DIR"
    desktop_entry --background > "$AUTOSTART_DIR/digidaw.desktop"
    info "Digidaw akan berjalan otomatis saat login (bisa diubah di Control Panel)."
fi

# ───────────────────────── 7. check ─────────────────────────
say "Memeriksa hasil setup..."
"$PY" check_setup.py || true

echo
say "Selesai! Jalankan Digidaw:"
info "• dari menu aplikasi (cari 'Digidaw'), atau"
info "• ./start_digidaw.sh            (tanpa terminal, log di logs/digidaw.log)"
info "• ./start_digidaw_console.sh    (dengan log langsung di terminal)"
info "Lalu ucapkan: \"Digidaw, jam berapa sekarang?\""
