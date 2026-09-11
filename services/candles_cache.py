"""A bounded, process-external store for downloaded historical candles.

Each backtest runs in its own worker process, which is what makes a run killable and
budgeted (ARCH-063) and what makes two runs structurally unable to corrupt each other
(CORR-060). The cost of that isolation is that `BacktestingDataProvider.candles_feeds` --
the per-instance dict that used to let a second backtest over the same market skip the
download -- dies with the process. An optimizer sweeping N configs over one market
therefore downloaded the same candle history N times.

This puts the cache back, outside the process, and stores the only thing that is safe to
share: the immutable *data*. Nothing here is an engine, a controller or a data provider,
and a reader gets its own unpickled copy of a frame -- so a run can do what it likes with
what it is handed, and no state is reachable from two runs at once. That is the property
CORR-060 established and this must not undo.

Three rules make it safe to serve:

- **Range-exact keys.** An entry is keyed by everything the download is a function of --
  connector, pair, interval, max_records and the run's window -- so a hit covers exactly
  the range that was asked for. A window the cache does not hold is a miss, never a
  narrower frame passed off as a wider one.
- **A freshness bound.** A window ending near "now" is downloaded with its last candle
  still forming, so an entry is only served for `ttl_seconds` after it was fetched. Past
  that it is a miss and the data is fetched again.
- **A size bound.** The number of entries is capped and the least recently used are
  dropped, so sweeping across many pairs and timeframes cannot grow the store without
  limit.

Single-flight: the entry file doubles as the lock, so N workers that all miss the same key
at once produce one download rather than N. A caller whose filesystem has no `flock` still
gets correct results -- just, at worst, a duplicated first download.

A cache is an optimization and never a failure mode: every read and write swallows its own
I/O errors and degrades to "download it again".
"""
import hashlib
import logging
import os
import pickle
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

try:
    import fcntl
except ImportError:  # non-POSIX: single-flight degrades to "everyone downloads once"
    fcntl = None

logger = logging.getLogger(__name__)

# How long a leftover half-written entry from a killed worker is left alone before it is
# swept. A write takes seconds, so anything older than this is debris, not a live write.
_TMP_GRACE = 600.0

_ENTRY_SUFFIX = ".pkl"
_TMP_SUFFIX = ".pkl.tmp"


def cache_key(*parts: Any) -> str:
    """A stable key over everything a download is a function of."""
    joined = "|".join(str(part) for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


class CandlesCache:
    """Downloaded candle frames on disk, keyed by exact range, bounded in count and age."""

    def __init__(self, path: str, max_entries: int, ttl_seconds: float):
        self._path = Path(path)
        self._max_entries = int(max_entries)
        self._ttl = float(ttl_seconds)
        if self.enabled:
            try:
                self._path.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                logger.warning(f"Candle cache at {self._path} is unusable, running without it: {e}")
                self._max_entries = 0

    @property
    def enabled(self) -> bool:
        """A cap of zero turns the cache off entirely -- the escape hatch for an operator."""
        return self._max_entries > 0

    def _entry_path(self, key: str) -> Path:
        return self._path / f"{key}{_ENTRY_SUFFIX}"

    def get(self, key: str) -> Optional[Any]:
        """The frame stored under this exact key, or None if it is absent, stale or unreadable.

        Never deletes: a caller that misses goes on to overwrite the entry anyway, and
        removing a file another worker is holding open as its single-flight lock would only
        cost a duplicate download.
        """
        if not self.enabled:
            return None
        path = self._entry_path(key)
        try:
            with open(path, "rb") as fh:
                envelope = pickle.load(fh)
        except FileNotFoundError:
            return None
        except Exception as e:  # truncated, half-written, or written by another pandas
            logger.debug(f"Candle cache entry {key} is unreadable, refetching: {e}")
            return None
        if not isinstance(envelope, dict) or "frame" not in envelope:
            return None
        if time.time() - float(envelope.get("created_at", 0)) > self._ttl:
            return None
        # Recency for eviction is the file's mtime, kept apart from the fetch time inside
        # the envelope so that reading a hot entry cannot extend its freshness.
        try:
            os.utime(path, None)
        except OSError:
            pass
        return envelope["frame"]

    def put(self, key: str, frame: Any) -> None:
        """Store a frame under an exact key, atomically, then honour the size bound."""
        if not self.enabled:
            return
        tmp = self._path / f"{key}.{os.getpid()}.{uuid.uuid4().hex}{_TMP_SUFFIX}"
        try:
            with open(tmp, "wb") as fh:
                pickle.dump({"created_at": time.time(), "frame": frame}, fh, protocol=pickle.HIGHEST_PROTOCOL)
            # Replace is atomic, so a concurrent reader sees either the old entry or the
            # new one, never a partial write.
            os.replace(tmp, self._entry_path(key))
        except Exception as e:
            logger.warning(f"Could not cache candles under {key}: {e}")
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return
        self._evict()

    @contextmanager
    def single_flight(self, key: str):
        """Serialize the workers that miss the same key, so only the first one downloads.

        The entry file is its own lock: taking it creates an empty placeholder, which reads
        as a miss until it is replaced by a real entry, and which the size bound sweeps like
        any other entry. Callers must re-check the cache inside this block -- the whole point
        is that whoever waited here finds the entry the first one just wrote.

        The lock is advisory and best-effort: without `flock` support the block is a no-op
        and the only consequence is that a first download happens more than once.
        """
        if not self.enabled or fcntl is None:
            yield
            return
        handle = None
        try:
            handle = open(self._entry_path(key), "a+b")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError as e:
            logger.debug(f"Candle cache could not lock {key}, downloading unguarded: {e}")
            if handle is not None:
                handle.close()
            handle = None
        try:
            yield
        finally:
            if handle is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
                handle.close()

    def _evict(self) -> None:
        """Drop the least recently used entries beyond the cap, and any abandoned writes."""
        try:
            entries = [(path.stat().st_mtime, path) for path in self._path.glob(f"*{_ENTRY_SUFFIX}")]
        except OSError as e:
            logger.debug(f"Could not scan the candle cache at {self._path}: {e}")
            return
        entries.sort()
        for _, path in entries[: max(0, len(entries) - self._max_entries)]:
            self._unlink(path)

        cutoff = time.time() - max(self._ttl, _TMP_GRACE)
        for path in self._path.glob(f"*{_TMP_SUFFIX}"):
            try:
                if path.stat().st_mtime < cutoff:
                    self._unlink(path)
            except OSError:
                continue

    @staticmethod
    def _unlink(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            logger.debug(f"Could not drop candle cache file {path}: {e}")
