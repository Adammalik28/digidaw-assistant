#!/usr/bin/env bash
# Installs Digidaw's git hooks (run once after cloning): the tests run before every commit.
cd "$(dirname "$(readlink -f "$0")")/.." || exit 1
ln -sf ../../scripts/pre-commit .git/hooks/pre-commit && echo "hook pre-commit terpasang"
