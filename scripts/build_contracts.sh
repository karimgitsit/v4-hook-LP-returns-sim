#!/usr/bin/env bash
# Build the v4-core foundry artifacts that the pyrevm harness loads.
#
# Why this script: forge by default uses the `default` profile in
# contracts/v4-core/foundry.toml, which sets `via_ir = true` and
# `optimizer_runs = 44_444_444` — a multi-minute compile. For the
# simulator we only need executable bytecode, so we force the `debug`
# profile and a small optimizer-runs value. This produces functionally
# equivalent contracts in seconds.
#
# Requires `forge` on PATH and solc 0.8.26.
# If solc is not auto-detected, pre-place it at $HOME/.svm/0.8.26/solc-0.8.26
# (see README "Bootstrapping" section).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT/contracts/v4-core"

if [[ ! -d lib/solmate ]]; then
    echo "v4-core sub-submodules not initialised. Run:"
    echo "  git submodule update --init --recursive"
    exit 1
fi

SOLC_PATH="${SOLC_PATH:-$HOME/.svm/0.8.26/solc-0.8.26}"
EXTRA_ARGS=()
if [[ -x "$SOLC_PATH" ]]; then
    EXTRA_ARGS+=(--use "$SOLC_PATH" --no-auto-detect)
fi

FOUNDRY_PROFILE=debug FOUNDRY_DISABLE_NIGHTLY_WARNING=1 \
    forge build --skip test --skip script "${EXTRA_ARGS[@]}"

echo "OK — artifacts in contracts/v4-core/out/"

# Build the vendored third-party example hooks (e.g. AntiSandwichHook) AFTER
# v4-core, so they compile against the exact v4-core source the harness deploys.
# Their artifacts are loaded via the runner's --hook path (out/<Hook>.sol/<Hook>.json).
if [[ -d "$REPO_ROOT/contracts/hooks" ]]; then
    cd "$REPO_ROOT/contracts/hooks"
    FOUNDRY_DISABLE_NIGHTLY_WARNING=1 \
        forge build "${EXTRA_ARGS[@]}"
    echo "OK — hook artifacts in contracts/hooks/out/"
fi
