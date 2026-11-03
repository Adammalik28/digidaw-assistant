#!/usr/bin/env bash
# Every test suite of Digidaw, offline (no sound, no network, nothing opened). Exit code 0 = all passed.
#   tests/run_all.sh            all suites (about 40 s)
# Also run by the git pre-commit hook (scripts/pre-commit): a commit that breaks a test is refused.
cd "$(dirname "$(readlink -f "$0")")/.." || exit 1
PY="${DIGIDAW_VENV:-${XDG_DATA_HOME:-$HOME/.local/share}/digidaw/venv}/bin/python"
failed=()
for t in tests/test_*.py; do
    out=$(env -u DIGIDAW_NO_CONSOLE SDL_AUDIODRIVER=dummy PYTHONPATH=. timeout 300 "$PY" "$t" 2>&1)
    if [[ $? -eq 0 ]] && grep -q "semua lulus" <<<"$out"; then
        printf '  ✓ %s\n' "$t"
    else
        printf '  ✗ %s\n' "$t"
        grep -E "✗|Error|assert" <<<"$out" | head -8 | sed 's/^/      /'
        failed+=("$t")
    fi
done
if (( ${#failed[@]} )); then
    echo "GAGAL: ${failed[*]}"
    exit 1
fi
echo "semua tes lulus"
