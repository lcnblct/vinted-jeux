#!/usr/bin/env python3
"""Fail when the monitor has not completed a recent successful scan."""

from __future__ import annotations

import argparse
from contextlib import closing
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / "seen.db"
DEFAULT_MAX_AGE_MINUTES = 45.0
DEFAULT_FUTURE_TOLERANCE_MINUTES = 5.0


def read_last_successful_scan(db_path: Path | str) -> datetime:
    """Read and normalize the successful scan timestamp as UTC."""
    try:
        with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)) as con:
            row = con.execute("SELECT value FROM meta WHERE key='last_successful_scan_at'").fetchone()
    except (OSError, sqlite3.Error) as exc:
        raise RuntimeError(f"cannot read health metadata: {exc}") from exc
    if not row or not row[0]:
        raise RuntimeError("meta.last_successful_scan_at is missing")
    value = str(row[0]).strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("meta.last_successful_scan_at is invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def check_health(db_path: Path | str, max_age_minutes: float = DEFAULT_MAX_AGE_MINUTES,
                 now: datetime | None = None,
                 future_tolerance_minutes: float = DEFAULT_FUTURE_TOLERANCE_MINUTES) -> None:
    """Raise ``RuntimeError`` when scan health is missing, stale, or future."""
    if max_age_minutes < 0 or future_tolerance_minutes < 0:
        raise RuntimeError("health thresholds must be non-negative")
    last = read_last_successful_scan(db_path)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if last > current + timedelta(minutes=future_tolerance_minutes):
        raise RuntimeError(f"last successful scan is in the future: {last.isoformat()}")
    age_seconds = max(0.0, (current - last).total_seconds())
    if age_seconds > max_age_minutes * 60:
        raise RuntimeError(f"last successful scan is stale: {last.isoformat()} ({age_seconds / 60:.1f} minutes ago; limit {max_age_minutes:g})")
    print(f"healthy: last successful scan {last.isoformat()} ({age_seconds / 60:.1f} minutes ago)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--max-age-minutes", type=float, default=DEFAULT_MAX_AGE_MINUTES)
    parser.add_argument("--future-tolerance-minutes", type=float, default=DEFAULT_FUTURE_TOLERANCE_MINUTES)
    args = parser.parse_args(argv)
    try:
        check_health(args.db, args.max_age_minutes, future_tolerance_minutes=args.future_tolerance_minutes)
    except RuntimeError as exc:
        print(f"health check failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
