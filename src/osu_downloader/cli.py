"""Command-line interface for osu-downloader."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from .db import (
    Beatmap,
    CollectionList,
    Listing,
    parse_collection_db,
    parse_osu_db,
)
from .downloader import download_sets
from .richio import console
from .vpn import inside_vpn, print_vpn_banner, re_exec_under_vpn

_STATUS_NAMES = {
    0: "Unknown",
    1: "Unsubmitted",
    2: "Pending/WIP/Graveyard",
    4: "Ranked",
    5: "Approved",
    6: "Qualified",
    7: "Loved",
}
_MODE_NAMES = {0: "osu!", 1: "Taiko", 2: "CTB", 3: "Mania"}


def _load_db(path: Path) -> Listing:
    console.print(f"[cyan]Parsing[/] {path} ...")
    listing = parse_osu_db(path)
    console.print(
        f"  version={listing.version} player={listing.player_name!r} "
        f"beatmaps={len(listing.beatmaps)} "
        f"unique_sets={len(listing.unique_set_ids())}"
    )
    return listing


def _load_collections(path: Path) -> CollectionList:
    console.print(f"[cyan]Parsing[/] {path} ...")
    cl = parse_collection_db(path)
    console.print(f"  version={cl.version} collections={len(cl.collections)}")
    return cl


@click.group()
@click.option(
    "--ovpn",
    "ovpn",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="OpenVPN config file. Re-execs the command inside an isolated VPN "
    "network namespace so only this process's traffic uses the tunnel.",
)
@click.option(
    "--vpn-creds",
    "vpn_creds",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="VPN credentials file (username=.../password=... .env, or plain "
    "user\\npass). Required with --ovpn.",
)
def main(ovpn: Path | None, vpn_creds: Path | None) -> None:
    """Parse osu! .db files and download beatmaps from the catboy.best mirror."""
    if ovpn is not None:
        if vpn_creds is None:
            raise click.UsageError("--ovpn requires --vpn-creds")
        if inside_vpn():
            print_vpn_banner()
            return
        re_exec_under_vpn(ovpn, vpn_creds, list(sys.argv))


@main.command("parse")
@click.argument("db_path", type=click.Path(exists=True, path_type=Path))
@click.option("-n", "--show", default=5, help="Number of sample beatmaps to print.")
def cmd_parse(db_path: Path, show: int) -> None:
    """Inspect an osu!.db file."""
    listing = _load_db(db_path)
    for b in listing.beatmaps[:show]:
        _print_beatmap(b)
    console.print(f"... ({len(listing.beatmaps)} total)")


@main.command("collections")
@click.argument("db_path", type=click.Path(exists=True, path_type=Path))
def cmd_collections(db_path: Path) -> None:
    """List collections from a collection.db."""
    cl = _load_collections(db_path)
    for i, c in enumerate(cl.collections):
        present = sum(1 for h in c.beatmap_hashes if h)
        console.print(
            f"  [{i}] {c.name!r}  beatmaps={len(c.beatmap_hashes)} (hashes={present})"
        )


@main.command("download-db")
@click.argument("db_path", type=click.Path(exists=True, path_type=Path))
@click.option("-o", "--output", default=Path("Songs"), type=click.Path(path_type=Path))
@click.option("-w", "--workers", default=4, type=int)
@click.option("--rate", default=800.0, type=float)
@click.option("--window", default=51.0, type=float)
@click.option("--limit", default=None, type=int)
@click.option("--no-video", is_flag=True, default=False)
@click.option("--skip-existing/--no-skip-existing", default=True)
@click.option("--dry-run", is_flag=True, default=False)
def cmd_download_db(
    db_path: Path,
    output: Path,
    workers: int,
    rate: float,
    window: float,
    limit: int | None,
    no_video: bool,
    skip_existing: bool,
    dry_run: bool,
) -> None:
    """Download every unique beatmapset referenced by an osu!.db."""
    listing = _load_db(db_path)
    set_ids = listing.unique_set_ids()
    console.print(f"[cyan]Resolved[/] {len(set_ids)} unique beatmapsets.")
    if limit:
        set_ids = set_ids[:limit]
        console.print(f"  (limited to {len(set_ids)})")
    if dry_run:
        for sid in set_ids[:50]:
            console.print(f"  would download #{sid}")
        console.print(f"... ({len(set_ids)} total, dry-run)")
        return
    download_sets(
        set_ids,
        output_dir=output,
        workers=workers,
        rate=rate,
        window=window,
        skip_existing=skip_existing,
        no_video=no_video,
    )


@main.command("download-collection")
@click.option(
    "--db", "db_path", required=True, type=click.Path(exists=True, path_type=Path)
)
@click.option(
    "--collections",
    "collections_path",
    required=True,
    type=click.Path(exists=True, path_type=Path),
)
@click.option("--name", default=None, help="Collection name to download (default: all).")
@click.option("-o", "--output", default=Path("Songs"), type=click.Path(path_type=Path))
@click.option("-w", "--workers", default=4, type=int)
@click.option("--rate", default=800.0, type=float)
@click.option("--window", default=51.0, type=float)
@click.option("--limit", default=None, type=int)
@click.option("--no-video", is_flag=True, default=False)
@click.option("--skip-existing/--no-skip-existing", default=True)
@click.option("--dry-run", is_flag=True, default=False)
def cmd_download_collection(
    db_path: Path,
    collections_path: Path,
    name: str | None,
    output: Path,
    workers: int,
    rate: float,
    window: float,
    limit: int | None,
    no_video: bool,
    skip_existing: bool,
    dry_run: bool,
) -> None:
    """Download beatmapsets belonging to one or more collections.

    Collection.db stores MD5 hashes, so an osu!.db is needed to resolve those
    hashes to beatmapset ids.
    """
    listing = _load_db(db_path)
    cl = _load_collections(collections_path)
    by_hash = listing.beatmaps_by_hash()

    chosen = cl.collections
    if name is not None:
        chosen = [c for c in cl.collections if c.name == name]
        if not chosen:
            console.print(f"[red]No collection named[/] {name!r}")
            sys.exit(1)

    set_ids: list[int] = []
    missing = 0
    for c in chosen:
        for h in c.present_hashes:
            b = by_hash.get(h)
            if b and b.beatmapset_id > 0:
                set_ids.append(b.beatmapset_id)
            else:
                missing += 1
    set_ids = list(dict.fromkeys(set_ids))
    console.print(
        f"[cyan]Resolved[/] {len(set_ids)} unique sets from "
        f"{sum(len(c.beatmap_hashes) for c in chosen)} hashes "
        f"({missing} unresolvable)."
    )
    if limit:
        set_ids = set_ids[:limit]
        console.print(f"  (limited to {len(set_ids)})")
    if dry_run:
        for sid in set_ids[:50]:
            console.print(f"  would download #{sid}")
        console.print(f"... ({len(set_ids)} total, dry-run)")
        return
    download_sets(
        set_ids,
        output_dir=output,
        workers=workers,
        rate=rate,
        window=window,
        skip_existing=skip_existing,
        no_video=no_video,
    )


@main.command("download-set")
@click.argument("set_id", type=int)
@click.option("-o", "--output", default=Path("Songs"), type=click.Path(path_type=Path))
@click.option("-w", "--workers", default=4, type=int)
@click.option("--rate", default=800.0, type=float)
@click.option("--window", default=51.0, type=float)
@click.option("--no-video", is_flag=True, default=False)
@click.option("--skip-existing/--no-skip-existing", default=True)
def cmd_download_set(
    set_id: int,
    output: Path,
    workers: int,
    rate: float,
    window: float,
    no_video: bool,
    skip_existing: bool,
) -> None:
    """Download a single beatmapset by id."""
    download_sets(
        [set_id],
        output_dir=output,
        workers=1,
        rate=rate,
        window=window,
        skip_existing=skip_existing,
        no_video=no_video,
    )


def _print_beatmap(b: Beatmap) -> None:
    status = _STATUS_NAMES.get(b.status, str(b.status))
    mode = _MODE_NAMES.get(b.mode, str(b.mode))
    console.print(
        f"  #{b.beatmap_id} set={b.beatmapset_id} [{mode}/{status}] {b.display}"
    )


if __name__ == "__main__":
    main()
