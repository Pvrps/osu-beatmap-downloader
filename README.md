# osu-downloader

Parses osu! database files (`osu!.db` and `collection.db`) and downloads the
referenced beatmaps from the [catboy.best](https://catboy.best) mirror.

## Setup

```sh
nix-shell shell.nix        # provides uv + python3.11
uv sync                    # create venv + install deps
```

## Usage

```sh
# Inspect a db file (version, player, beatmap/set counts, sample entries)
osu-downloader parse ~/Downloads/database1.db

# List collections from a collection.db
osu-downloader collections ~/Downloads/collection.db

# Download every beatmapset referenced by an osu!.db (deduped by set id)
osu-downloader download-db ~/Downloads/database1.db -o ./Songs

# Download a specific collection by name (needs both an osu!.db for hash->set
# resolution and a collection.db for the hash list)
osu-downloader download-collection \
    --db ~/Downloads/database1.db \
    --collections ~/Downloads/collection.db \
    --name "My Collection" -o ./Songs

# Download a single beatmapset by id
osu-downloader download-set 12345 -o ./Songs
```

Options for `download-*`:

- `-o, --output DIR`   output directory (default `./Songs`)
- `-w, --workers N`    parallel download threads (default 4)
- `--limit N`          stop after N successful downloads (for testing)
- `--rate N`           max requests per window (default 800; mirror allows 1200)
- `--window SECONDS`   rate-limit window length (default 51)
- `--no-video`         request no-video variants (mirror default already excludes video)
- `--dry-run`          resolve targets without downloading
- `--skip-existing`    skip files already present in output dir (default on)

## Rate limiting & bans

The mirror advertises `X-Ratelimit-Limit: 1200` per ~51s window. **Exceeding
it triggers a persistent IP ban** (HTTP 403 with a "you have been banned"
body), not a soft 429. The downloader uses a token bucket capped at the worker
count (no large startup burst) and a conservative default rate of 800/window.
If a ban is detected mid-run, the run aborts immediately with a clear message
rather than hammering the mirror.

If you get banned: wait for it to lift (or change IP), then re-run —
`--skip-existing` resumes from where you stopped. To appeal:
https://catboy.best/discord
