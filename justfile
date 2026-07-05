# osu-downloader justfile
# Usage:
#   just download <database> <output-dir>
#   just download databases/database1.db ~/Downloads/songs

default:
    @just --list

# Update devenv inputs (nixpkgs, devenv module versions) and sync deps
update:
    devenv update
    uv sync

# Garbage collect old devenv generations
gc:
    devenv gc

# Clean devenv cache (forces full rebuild on next shell)
clean:
    rm -rf .devenv devenv.lock

# Enter the devenv shell
shell:
    devenv shell

# Install/sync dependencies
install:
    devenv shell -- uv sync

# Parse a db file (inspect version, player, beatmap counts, sample entries)
parse database:
    devenv shell -- uv run osu-downloader parse {{database}}

# Download all beatmaps from a db file through the Windscribe VPN.
# Args: database path, output directory
download database output:
    devenv shell -- uv run osu-downloader \
        --ovpn vpn/Windscribe-Copenhagen-LEGO.ovpn \
        --vpn-creds vpn/.env \
        download-db \
        {{database}} \
        -o {{output}} \
        -w 4 \
        --rate 600
