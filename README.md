# osu-beatmap-downloader

Reads your osu! database files and downloads every beatmap they reference from
the [catboy.best](https://catboy.best) mirror. Handy for rebuilding a Songs
folder on a new install.

## What it does

- **Parses `osu!.db` and `collection.db`** to get your beatmaps and collections.
- **Downloads beatmapsets in parallel**, deduplicated by set ID, skipping files
  you already have so interrupted runs can resume.
- **Downloads a single collection** or a single beatmapset by ID.
- **Stays under the mirror's rate limit.** Going over it gets your IP banned
  (see [Rate limiting & bans](#rate-limiting--bans)).
- **Can route the downloads through a VPN** (Linux only). Only the downloader's own traffic
  goes through the tunnel (via an isolated network namespace); the rest of your
  system is untouched.

Sample databases are included in `databases/`.

## Requirements

- Python 3.11+ and [uv](https://docs.astral.sh/uv/), on any OS
- Optional: [devenv](https://devenv.sh) and [just](https://just.systems) (Linux
  or macOS), which provide Python, uv, OpenVPN, iproute2 and iptables for you
- Optional, for [VPN mode](#vpn-mode): Linux, `sudo`, an OpenVPN `.ovpn` config,
  and a credentials file

## Usage

With devenv and just:

```sh
just install                                      # set up the environment and dependencies
just parse databases/database1.db                 # inspect a db (version, player, counts, samples)
just download databases/database1.db ./Songs      # download everything
just download-vpn databases/database1.db ./Songs  # same, through the VPN (Linux only)
```

Without devenv (e.g. on Windows), install with `uv sync` and run the CLI
through uv:

```sh
uv sync
uv run osu-downloader download-db databases/database1.db -o ./Songs
```

More commands (prefix with `uv run` if you aren't in the devenv shell):

```sh
# Download every beatmapset referenced by an osu!.db
osu-downloader download-db databases/database1.db -o ./Songs

# List collections in a collection.db
osu-downloader collections collection.db

# Download one collection (needs the osu!.db to map hashes to sets)
osu-downloader download-collection \
    --db databases/database1.db \
    --collections collection.db \
    --name "My Collection" -o ./Songs

# Download a single beatmapset by ID
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

### VPN mode

Linux only. Pass `--ovpn <config.ovpn> --vpn-creds <file>` before the
subcommand. The tool re-runs itself under `vpn-run.sh` with `sudo`, which sets
up the tunnel in its own network namespace (using `ip`, `iptables` and a tun
device) and then drops back to your user for the download.

The credentials file is either `username=...` / `password=...` lines or plain
`user` / `pass` on two lines. `just download-vpn` looks for these files in
`vpn/`, which is gitignored so credentials never get committed:

```
vpn/.env
vpn/Windscribe-Copenhagen-LEGO.ovpn
```

## Rate limiting & bans

The mirror advertises `X-Ratelimit-Limit: 1200` per ~51s window. **Exceeding
it triggers a persistent IP ban** (HTTP 403 with a "you have been banned"
body), not a soft 429. The downloader uses a token bucket capped at the worker
count (no large startup burst) and a conservative default rate of 800/window.
If a ban is detected mid-run, the run aborts immediately with a clear message
rather than hammering the mirror.

If you get banned: wait for it to lift (or change IP, e.g. with VPN mode), then
re-run. `--skip-existing` resumes from where you stopped. To appeal:
https://catboy.best/discord
