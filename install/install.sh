#!/bin/sh
# pdatum installer for macOS, Linux and WSL.
#
#   curl -fsSL https://pdatum.pearachute.com/install.sh | sh
#
# Installs the pdatum CLI as an isolated tool -- with uv if you have it, else
# pipx, else it installs uv first (uv brings its own Python, so none is needed
# beforehand) -- and puts it on your PATH for new terminals.
#
# Set PDATUM_NO_MODIFY_PATH=1 to leave shell profiles alone.
#
# Everything lives inside main(), called on the last line, so a download cut
# off halfway runs nothing rather than half an install.

set -eu

# What PATH was before this script touched it: whether `pdatum` will be found
# in the user's next terminal depends on this, not on our own adjustments.
ORIGINAL_PATH="$PATH"

say() { printf '%s\n' "$*"; }
die() { printf 'pdatum install: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
on_original_path() {
    case ":$ORIGINAL_PATH:" in *":$1:"*) return 0 ;; esac
    return 1
}

install_uv() {
    say "Installing uv (a Python tool installer, from astral.sh)..."
    if [ -n "${PDATUM_NO_MODIFY_PATH:-}" ]; then
        UV_NO_MODIFY_PATH=1
        export UV_NO_MODIFY_PATH
    fi
    if have curl; then
        curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1
    elif have wget; then
        wget -qO- https://astral.sh/uv/install.sh | sh >/dev/null 2>&1
    else
        die "need curl or wget to download uv"
    fi
    # uv's installer edits shell profiles, which this shell has already read.
    PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    have uv || die "uv installed but is not on PATH; open a new terminal and rerun"
}

# Ask the installer where it put the binary rather than assume ~/.local/bin:
# UV_TOOL_BIN_DIR, PIPX_BIN_DIR or XDG_BIN_HOME can each move it.
find_pdatum() {
    for dir in \
        "$(uv tool dir --bin 2>/dev/null || true)" \
        "$(pipx environment --value PIPX_BIN_DIR 2>/dev/null || true)" \
        "$HOME/.local/bin"; do
        [ -n "$dir" ] || continue
        for exe in "$dir/pdatum" "$dir/pdatum.exe"; do
            if [ -x "$exe" ]; then printf '%s\n' "$exe"; return; fi
        done
    done
    command -v pdatum 2>/dev/null || true
}

# Have the tool that installed pdatum add its bin directory to the shell
# profiles; each knows its own directory and which profiles to touch.
add_to_path() {
    [ -z "${PDATUM_NO_MODIFY_PATH:-}" ] || return 1
    case "$1" in
        uv) uv tool update-shell >/dev/null 2>&1 ;;
        pipx) pipx ensurepath >/dev/null 2>&1 ;;
        *) return 1 ;;
    esac
}

main() {
    if have uv; then
        via=uv
    elif have pipx; then
        via=pipx
    else
        install_uv
        via=uv
    fi
    say "Installing pdatum with $via..."
    if [ "$via" = uv ]; then
        uv tool install --quiet --upgrade pdatum
    else
        pipx install --quiet --force pdatum >/dev/null
    fi

    bin="$(find_pdatum)"
    [ -n "$bin" ] || die "pdatum installed but cannot be found; open a new terminal and run 'pdatum --version'"
    dir="$(dirname "$bin")"

    say ""
    say "Installed $("$bin" --version 2>/dev/null | head -n 1)"
    if ! on_original_path "$dir"; then
        say ""
        if add_to_path "$via"; then
            say "Added $dir to your PATH. Open a new terminal to use 'pdatum',"
            say "or use it in this one now with:"
        else
            say "$dir is not on your PATH. Add it, or for this terminal run:"
        fi
        say "  export PATH=\"$dir:\$PATH\""
    fi
    say ""
    say "Next, save your API key (it is checked with the server first):"
    say "  pdatum key save pdatum_..."
    say ""
    say "Setting this up for an AI agent? 'pdatum skill' prints instructions for it to save."
}

main "$@"
