"""Rate-limited, multi-threaded downloader for the catboy.best beatmap mirror.

Endpoint: ``GET https://catboy.best/d/{beatmapset_id}`` returns the ``.osz``
archive (a zip) via ``Content-Disposition``. The mirror advertises
``X-Ratelimit-Limit: 1200`` per ~51s window and sets ``Retry-After`` on 429.

Concurrency model
-----------------
A single :class:`RateLimiter` (token bucket, refilled continuously) is shared
across all worker threads, so the aggregate request rate stays under the
configured ceiling regardless of worker count. A :class:`ThreadPoolExecutor`
performs the actual downloads in parallel. Per-host connection limits are
enforced by ``httpx``'s connection pool.
"""

from __future__ import annotations

import email.utils
import os
import re
import threading
import time
from collections.abc import Iterable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import httpx

from .richio import console

MIRROR = "https://catboy.best"
DEFAULT_WORKERS = 4
DEFAULT_RATE = 800  # mirror allows 1200/window; stay safely under to avoid bans
DEFAULT_WINDOW = 51.0
_MAX_RETRIES = 4
_DL_TIMEOUT = 120.0

# osu! filenames may contain '/' (rare) or other FS-unsafe characters.
_BAD_CHARS = re.compile(r"[\\/:*?\"<>|]")


def _safe_name(name: str, fallback: str) -> str:
    name = _BAD_CHARS.sub("_", name).strip().rstrip(".")
    return name or fallback


def _parse_retry_after(value: str | None) -> float:
    """Parse a Retry-After header, which may be either delta-seconds or an
    HTTP-date (RFC 7231). Returns seconds to wait, clamped to [0, 60]."""
    if not value:
        return 2.0
    value = value.strip()
    try:
        return max(0.0, min(float(value), 60.0))
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(value)
        delta = (dt - email.utils.localtime()).total_seconds()
        return max(0.0, min(delta, 60.0))
    except (TypeError, ValueError, OverflowError):
        return 2.0


@dataclass(slots=True)
class DownloadResult:
    set_id: int
    ok: bool
    path: Path | None = None
    skipped: bool = False
    error: str | None = None
    bytes_written: int = 0


class RateLimiter:
    """Continuous-refill token bucket shared across threads.

    Tokens are worth one request each. The bucket holds at most ``capacity``
    tokens and refills at ``rate / window`` tokens per second, so the long-term
    average rate is bounded while short bursts up to ``capacity`` are allowed.
    """

    __slots__ = ("_rate", "_capacity", "_tokens", "_last", "_lock", "_cond")

    def __init__(self, rate: float, window: float, capacity: float | None = None) -> None:
        self._rate = rate / window
        # Cap capacity at the worker count (set by caller) so the bucket can
        # never burst beyond a handful of simultaneous requests. A capacity
        # equal to `rate` would allow `rate` requests to fire instantly and
        # then refill, easily 2x'ing the mirror's per-window ceiling -> ban.
        self._capacity = capacity if capacity is not None else 1.0
        self._tokens = self._capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)

    def acquire(self, stop: threading.Event | None = None) -> bool:
        """Block until a token is available (or ``stop`` is set)."""
        with self._cond:
            while True:
                self._refill_locked()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
                if stop is not None and stop.is_set():
                    return False
                # Sleep until ~1 token is available, then re-check.
                deficit = 1.0 - self._tokens
                wait = max(deficit / self._rate, 0.01)
                self._cond.wait(timeout=wait)

    def _refill_locked(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._last = now

    def shutdown(self) -> None:
        with self._cond:
            self._cond.notify_all()


def _filename_from_response(resp: httpx.Response, set_id: int) -> str:
    cd = resp.headers.get("content-disposition", "")
    match = re.search(r'filename="([^"]+)"', cd)
    if match:
        return _safe_name(match.group(1), f"{set_id}.osz")
    return f"{set_id}.osz"


def _download_one(
    client: httpx.Client,
    limiter: RateLimiter,
    set_id: int,
    output_dir: Path,
    skip_existing: bool,
    no_video: bool,
    stop: threading.Event,
) -> DownloadResult:
    if stop.is_set():
        return DownloadResult(set_id=set_id, ok=False, error="cancelled")

    target_dir = output_dir
    # Probe for any existing file named like this set to avoid re-downloading.
    if skip_existing:
        existing = list(target_dir.glob(f"{set_id} *.osz")) + list(
            target_dir.glob(f"{set_id}.osz")
        )
        if existing:
            return DownloadResult(set_id=set_id, ok=True, skipped=True, path=existing[0])

    url = f"{MIRROR}/d/{set_id}"
    if no_video:
        url += "?novideo=1"

    last_err: str | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        if stop.is_set():
            return DownloadResult(set_id=set_id, ok=False, error="cancelled")
        if not limiter.acquire(stop):
            return DownloadResult(set_id=set_id, ok=False, error="cancelled")
        try:
            with client.stream("GET", url, timeout=_DL_TIMEOUT) as resp:
                if resp.status_code == 404:
                    return DownloadResult(
                        set_id=set_id, ok=False, error="not found on mirror"
                    )
                if resp.status_code == 403:
                    # The mirror returns 403 (not 429) when it rate-limit-bans
                    # an IP, with a JSON body saying so. This is a hard,
                    # persistent ban: every subsequent request will 403 too, so
                    # stop the entire run immediately rather than hammering and
                    # making the ban worse. Distinguish from a per-set 403 by
                    # reading the (small) body.
                    body = resp.read().decode("utf-8", "replace").lower()
                    if "banned" in body:
                        stop.set()
                        limiter.shutdown()
                        return DownloadResult(
                            set_id=set_id,
                            ok=False,
                            error=(
                                "IP banned by mirror (over rate limit). "
                                "Stop the run; wait or change IP. "
                                "See https://catboy.best/discord"
                            ),
                        )
                    return DownloadResult(
                        set_id=set_id, ok=False, error=f"HTTP 403: {body[:120]}"
                    )
                if resp.status_code == 429 or resp.status_code >= 500:
                    retry_after = _parse_retry_after(resp.headers.get("retry-after"))
                    last_err = f"HTTP {resp.status_code}"
                    time.sleep(retry_after)
                    continue
                if resp.status_code >= 400:
                    return DownloadResult(
                        set_id=set_id,
                        ok=False,
                        error=f"HTTP {resp.status_code}",
                    )

                fname = _filename_from_response(resp, set_id)
                out_path = target_dir / fname
                tmp_path = out_path.with_suffix(out_path.suffix + ".part")
                written = 0
                with open(tmp_path, "wb") as fh:
                    for chunk in resp.iter_bytes(chunk_size=1 << 16):
                        if stop.is_set():
                            fh.close()
                            tmp_path.unlink(missing_ok=True)
                            return DownloadResult(
                                set_id=set_id, ok=False, error="cancelled"
                            )
                        fh.write(chunk)
                        written += len(chunk)
                os.replace(tmp_path, out_path)
                return DownloadResult(
                    set_id=set_id, ok=True, path=out_path, bytes_written=written
                )
        except (httpx.TransportError, httpx.HTTPError) as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            backoff = min(2**attempt, 30.0)
            time.sleep(backoff)
            continue

    return DownloadResult(set_id=set_id, ok=False, error=last_err)


def download_sets(
    set_ids: Iterable[int],
    output_dir: Path,
    workers: int = DEFAULT_WORKERS,
    rate: float = DEFAULT_RATE,
    window: float = DEFAULT_WINDOW,
    skip_existing: bool = True,
    no_video: bool = False,
    limit: int | None = None,
    stop: threading.Event | None = None,
) -> list[DownloadResult]:
    """Download many beatmapsets in parallel, respecting rate limits."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stop = stop or threading.Event()

    ids = list(dict.fromkeys(int(i) for i in set_ids if int(i) > 0))
    if limit is not None:
        ids = ids[:limit]
    if not ids:
        return []

    # Capacity = workers lets each worker grab one token immediately (needed for
    # parallelism) but prevents a large startup burst that would exceed the
    # mirror's per-window ceiling and trigger an IP ban.
    limiter = RateLimiter(rate=rate, window=window, capacity=float(workers))
    results: list[DownloadResult] = []
    total = len(ids)

    limits = httpx.Limits(max_connections=workers, max_keepalive_connections=workers)
    # The mirror 403s requests with custom/non-browser User-Agents, so use a
    # mainstream browser UA.
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        )
    }

    console.print(
        f"[cyan]Downloading[/] {total} beatmapset(s) -> "
        f"[bold]{output_dir}[/] with {workers} workers "
        f"(rate {rate:.0f}/{window:.0f}s)"
    )

    with (
        ThreadPoolExecutor(max_workers=workers) as pool,
        httpx.Client(headers=headers, limits=limits, follow_redirects=True) as client,
    ):
        futures: dict[Future[DownloadResult], int] = {
            pool.submit(
                _download_one,
                client,
                limiter,
                sid,
                output_dir,
                skip_existing,
                no_video,
                stop,
            ): sid
            for sid in ids
        }
        done = 0
        banned = False
        for fut in as_completed(futures):
            sid = futures[fut]
            try:
                res = fut.result()
            except Exception as exc:  # noqa: BLE001 - never let one worker kill the run
                res = DownloadResult(
                    set_id=sid, ok=False, error=f"{type(exc).__name__}: {exc}"
                )
            results.append(res)
            done += 1
            if res.error and "banned" in res.error.lower():
                banned = True
                console.print(
                    f"  [{done}/{total}] [red bold]BANNED[/] #{res.set_id}: {res.error}"
                )
                # Cancel remaining futures; stop event is already set.
                for f in futures:
                    f.cancel()
                break
            if res.skipped:
                console.print(
                    f"  [{done}/{total}] [yellow]skip[/] #{res.set_id} (exists)"
                )
            elif res.ok:
                console.print(
                    f"  [{done}/{total}] [green]ok[/]   #{res.set_id} "
                    f"({res.bytes_written / 1e6:.1f} MB) {res.path.name if res.path else ''}"
                )
            else:
                console.print(
                    f"  [{done}/{total}] [red]fail[/] #{res.set_id}: {res.error}"
                )

    limiter.shutdown()
    ok = sum(1 for r in results if r.ok)
    skipped = sum(1 for r in results if r.skipped)
    failed = total - ok
    if banned:
        console.print(
            "[red bold]ABORTED: IP banned by mirror.[/] All further requests "
            "would fail. Wait for the ban to lift (or change IP) before "
            "re-running; --skip-existing will resume. "
            "Appeal: https://catboy.best/discord"
        )
    console.print(f"[bold]Done:[/] {ok} ok ({skipped} skipped), {failed} failed.")
    return results
