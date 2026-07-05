"""Pure-Python parser for osu! binary database files.

Supports ``osu!.db`` (the beatmap listing cache) and ``collection.db``
(user collections of beatmap MD5 hashes).

The binary layout is undocumented upstream; field order and primitive
encodings follow the reference implementation in the ``osu-db`` crate
(https://github.com/kovaxis/osu-db) and the osu! wiki.

Primitive encoding
------------------
* Integers: little-endian (u8/u16/u32/u64, and i32/i64 as signed).
* "opt_string": one marker byte (0x00 = absent, 0x0b = present) followed by a
  ULEB128 length and that many UTF-8 bytes.
* "datetime": u64 Windows ticks (100ns units since 0001-01-01).
* "star rating pair": 0x08 tag, u32 mod bits, then either (0x0d, f64) for
  versions < 20250107 or (0x0c, f32) for newer versions.
* Several difficulty values are stored as u8 in versions < 20140609 and f32
  afterwards; beatmap entries are length-prefixed in versions < 20191106.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# Breaking-change version boundaries in the osu!.db format.
_CHANGE_20140609 = 20140609
_CHANGE_20191106 = 20191106
_CHANGE_20250107 = 20250107

# Windows ticks epoch helpers. 1 tick = 100ns; ticks are counted from
# 0001-01-01T00:00:00Z, which is 62135596800 seconds before the Unix epoch.
_TICKS_PER_SECOND = 10_000_000
_UNIX_EPOCH_TICKS = 62135596800 * _TICKS_PER_SECOND


def _ticks_to_datetime(ticks: int) -> datetime:
    return datetime.fromtimestamp((ticks - _UNIX_EPOCH_TICKS) / _TICKS_PER_SECOND, tz=UTC)


class _Reader:
    """Tiny cursor over a bytes buffer with the primitives the format needs."""

    __slots__ = ("buf", "pos")

    def __init__(self, buf: bytes, pos: int = 0) -> None:
        self.buf = buf
        self.pos = pos

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.buf):
            raise EOFError(
                f"unexpected end of db: need {n} bytes at offset {self.pos}, "
                f"have {len(self.buf) - self.pos}"
            )
        chunk = self.buf[self.pos : self.pos + n]
        self.pos += n
        return chunk

    def u8(self) -> int:
        return self.take(1)[0]

    def bool_(self) -> bool:
        return self.u8() != 0

    def u16(self) -> int:
        return int.from_bytes(self.take(2), "little")

    def u32(self) -> int:
        return int.from_bytes(self.take(4), "little")

    def i32(self) -> int:
        return int.from_bytes(self.take(4), "little", signed=True)

    def u64(self) -> int:
        return int.from_bytes(self.take(8), "little")

    def f32(self) -> float:
        return _f32(self)

    def f64(self) -> float:
        return _f64(self)

    def datetime_(self) -> datetime:
        return _ticks_to_datetime(self.u64())

    def uleb128(self) -> int:
        result = 0
        shift = 0
        while True:
            byte = self.u8()
            result |= (byte & 0x7F) << shift
            if not (byte & 0x80):
                break
            shift += 7
        return result

    def opt_string(self) -> str | None:
        marker = self.u8()
        if marker == 0x00:
            return None
        if marker != 0x0B:
            raise ValueError(f"bad string marker 0x{marker:02x} at offset {self.pos - 1}")
        length = self.uleb128()
        return self.take(length).decode("utf-8")

    def remaining(self) -> int:
        return len(self.buf) - self.pos

    def expect(self, byte: int) -> None:
        got = self.u8()
        if got != byte:
            raise ValueError(
                f"expected tag byte 0x{byte:02x}, got 0x{got:02x} at offset {self.pos - 1}"
            )


def _f32(r: _Reader) -> float:
    import struct

    return struct.unpack("<f", r.take(4))[0]


def _f64(r: _Reader) -> float:
    import struct

    return struct.unpack("<d", r.take(8))[0]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StarRating:
    mods: int
    stars: float


@dataclass(slots=True)
class TimingPoint:
    bpm: float
    offset: float
    inherits: bool


@dataclass(slots=True)
class Beatmap:
    artist_ascii: str | None
    artist_unicode: str | None
    title_ascii: str | None
    title_unicode: str | None
    creator: str | None
    difficulty_name: str | None
    audio: str | None
    hash: str | None
    file_name: str | None
    status: int
    hitcircle_count: int
    slider_count: int
    spinner_count: int
    last_modified: datetime
    approach_rate: float
    circle_size: float
    hp_drain: float
    overall_difficulty: float
    slider_velocity: float
    std_ratings: list[StarRating] = field(default_factory=list)
    taiko_ratings: list[StarRating] = field(default_factory=list)
    ctb_ratings: list[StarRating] = field(default_factory=list)
    mania_ratings: list[StarRating] = field(default_factory=list)
    drain_time: int = 0
    total_time: int = 0
    preview_time: int = 0
    timing_points: list[TimingPoint] = field(default_factory=list)
    beatmap_id: int = 0
    beatmapset_id: int = 0
    thread_id: int = 0
    std_grade: int = 0
    taiko_grade: int = 0
    ctb_grade: int = 0
    mania_grade: int = 0
    local_beatmap_offset: int = 0
    stack_leniency: float = 0.0
    mode: int = 0
    song_source: str | None = None
    tags: str | None = None
    online_offset: int = 0
    title_font: str | None = None
    last_played: datetime | None = None
    is_osz2: bool = False
    folder_name: str | None = None
    last_online_check: datetime | None = None
    ignore_sounds: bool = False
    ignore_skin: bool = False
    disable_storyboard: bool = False
    disable_video: bool = False
    visual_override: bool = False
    mysterious_short: int | None = None
    mysterious_last_modified: int = 0
    mania_scroll_speed: int = 0

    @property
    def set_id(self) -> int:
        return self.beatmapset_id

    @property
    def display(self) -> str:
        artist = self.artist_ascii or self.artist_unicode or "?"
        title = self.title_ascii or self.title_unicode or "?"
        diff = self.difficulty_name or ""
        return f"{artist} - {title} [{diff}]"


@dataclass(slots=True)
class Listing:
    version: int
    folder_count: int
    unban_date: datetime | None
    player_name: str | None
    beatmaps: list[Beatmap]
    user_permissions: int

    def beatmaps_by_hash(self) -> dict[str, Beatmap]:
        out: dict[str, Beatmap] = {}
        for b in self.beatmaps:
            if b.hash:
                out[b.hash] = b
        return out

    def unique_set_ids(self) -> list[int]:
        seen: dict[int, None] = {}
        for b in self.beatmaps:
            sid = b.beatmapset_id
            if sid and sid > 0 and sid not in seen:
                seen[sid] = None
        return list(seen.keys())


@dataclass(slots=True)
class Collection:
    name: str | None
    beatmap_hashes: list[str | None]

    @property
    def present_hashes(self) -> Iterator[str]:
        return (h for h in self.beatmap_hashes if h)


@dataclass(slots=True)
class CollectionList:
    version: int
    collections: list[Collection]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _difficulty_value(r: _Reader, version: int) -> float:
    if version >= _CHANGE_20140609:
        return _f32(r)
    return float(r.u8())


def _star_ratings(r: _Reader, version: int) -> list[StarRating]:
    if version < _CHANGE_20140609:
        return []
    count = r.u32()
    out: list[StarRating] = []
    for _ in range(count):
        r.expect(0x08)
        mods = r.u32()
        if version < _CHANGE_20250107:
            r.expect(0x0D)
            stars = _f64(r)
        else:
            r.expect(0x0C)
            stars = _f32(r)
        out.append(StarRating(mods=mods, stars=stars))
    return out


def _timing_point(r: _Reader) -> TimingPoint:
    bpm = _f64(r)
    offset = _f64(r)
    inherits = r.bool_()
    return TimingPoint(bpm=bpm, offset=offset, inherits=inherits)


def _beatmap(r: _Reader, version: int) -> Beatmap:
    if version < _CHANGE_20191106:
        _ = r.u32()  # entry byte length prefix, unused when reading

    artist_ascii = r.opt_string()
    artist_unicode = r.opt_string()
    title_ascii = r.opt_string()
    title_unicode = r.opt_string()
    creator = r.opt_string()
    difficulty_name = r.opt_string()
    audio = r.opt_string()
    hash_ = r.opt_string()
    file_name = r.opt_string()
    status = r.u8()
    hitcircle_count = r.u16()
    slider_count = r.u16()
    spinner_count = r.u16()
    last_modified = r.datetime_()
    approach_rate = _difficulty_value(r, version)
    circle_size = _difficulty_value(r, version)
    hp_drain = _difficulty_value(r, version)
    overall_difficulty = _difficulty_value(r, version)
    slider_velocity = _f64(r)
    std_ratings = _star_ratings(r, version)
    taiko_ratings = _star_ratings(r, version)
    ctb_ratings = _star_ratings(r, version)
    mania_ratings = _star_ratings(r, version)
    drain_time = r.u32()
    total_time = r.u32()
    preview_time = r.u32()

    tp_count = r.u32()
    timing_points = [_timing_point(r) for _ in range(tp_count)]

    beatmap_id = r.i32()
    beatmapset_id = r.i32()
    thread_id = r.u32()
    std_grade = r.u8()
    taiko_grade = r.u8()
    ctb_grade = r.u8()
    mania_grade = r.u8()
    local_beatmap_offset = r.u16()
    stack_leniency = _f32(r)
    mode = r.u8()
    song_source = r.opt_string()
    tags = r.opt_string()
    online_offset = r.u16()
    title_font = r.opt_string()
    unplayed = r.bool_()
    last_played_raw = r.datetime_()
    is_osz2 = r.bool_()
    folder_name = r.opt_string()
    last_online_check = r.datetime_()
    ignore_sounds = r.bool_()
    ignore_skin = r.bool_()
    disable_storyboard = r.bool_()
    disable_video = r.bool_()
    visual_override = r.bool_()
    mysterious_short = r.u16() if version < _CHANGE_20140609 else None
    mysterious_last_modified = r.u32()
    mania_scroll_speed = r.u8()

    return Beatmap(
        artist_ascii=artist_ascii,
        artist_unicode=artist_unicode,
        title_ascii=title_ascii,
        title_unicode=title_unicode,
        creator=creator,
        difficulty_name=difficulty_name,
        audio=audio,
        hash=hash_,
        file_name=file_name,
        status=status,
        hitcircle_count=hitcircle_count,
        slider_count=slider_count,
        spinner_count=spinner_count,
        last_modified=last_modified,
        approach_rate=approach_rate,
        circle_size=circle_size,
        hp_drain=hp_drain,
        overall_difficulty=overall_difficulty,
        slider_velocity=slider_velocity,
        std_ratings=std_ratings,
        taiko_ratings=taiko_ratings,
        ctb_ratings=ctb_ratings,
        mania_ratings=mania_ratings,
        drain_time=drain_time,
        total_time=total_time,
        preview_time=preview_time,
        timing_points=timing_points,
        beatmap_id=beatmap_id,
        beatmapset_id=beatmapset_id,
        thread_id=thread_id,
        std_grade=std_grade,
        taiko_grade=taiko_grade,
        ctb_grade=ctb_grade,
        mania_grade=mania_grade,
        local_beatmap_offset=local_beatmap_offset,
        stack_leniency=stack_leniency,
        mode=mode,
        song_source=song_source,
        tags=tags,
        online_offset=online_offset,
        title_font=title_font,
        last_played=None if unplayed else last_played_raw,
        is_osz2=is_osz2,
        folder_name=folder_name,
        last_online_check=last_online_check,
        ignore_sounds=ignore_sounds,
        ignore_skin=ignore_skin,
        disable_storyboard=disable_storyboard,
        disable_video=disable_video,
        visual_override=visual_override,
        mysterious_short=mysterious_short,
        mysterious_last_modified=mysterious_last_modified,
        mania_scroll_speed=mania_scroll_speed,
    )


def parse_osu_db(source: bytes | Path | str) -> Listing:
    """Parse an ``osu!.db`` file from bytes or a path."""
    data = _read(source)
    r = _Reader(data)
    version = r.u32()
    folder_count = r.u32()
    account_unlocked = r.bool_()
    unlock_date = r.datetime_()
    player_name = r.opt_string()
    beatmap_count = r.u32()
    beatmaps = [_beatmap(r, version) for _ in range(beatmap_count)]
    user_permissions = r.u32()
    return Listing(
        version=version,
        folder_count=folder_count,
        unban_date=None if account_unlocked else unlock_date,
        player_name=player_name,
        beatmaps=beatmaps,
        user_permissions=user_permissions,
    )


def parse_collection_db(source: bytes | Path | str) -> CollectionList:
    """Parse a ``collection.db`` file from bytes or a path."""
    data = _read(source)
    r = _Reader(data)
    version = r.u32()
    count = r.u32()
    collections: list[Collection] = []
    for _ in range(count):
        name = r.opt_string()
        hash_count = r.u32()
        hashes = [r.opt_string() for _ in range(hash_count)]
        collections.append(Collection(name=name, beatmap_hashes=hashes))
    return CollectionList(version=version, collections=collections)


def _read(source: bytes | Path | str) -> bytes:
    if isinstance(source, (bytes, bytearray)):
        return bytes(source)
    return Path(source).read_bytes()
