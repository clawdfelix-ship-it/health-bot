"""
db.py — Neon Postgres persistence for health-bot.

Design principle: the file (health_data.json / meds_log.json) stays the local
fallback and is ALWAYS written first. The DB is a best-effort mirror so a database
outage can NEVER block Felix from recording a blood-pressure reading. Every public
function here swallows its own errors, logs them, and returns a status — callers
do not wrap calls in try/except for normal flow.

If DATABASE_URL is unset (local dev / misconfig), this module degrades to no-ops
and the bot runs exactly as before (file-only).

Tables live in the `health` Postgres schema (isolated from other apps sharing the
same Neon database). We always schema-qualify (`health.readings`) because the Neon
pooler rejects a startup-time search_path option.
"""

import os
import threading
import json

_DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

# One lock for the module; Postgres connections are short-lived (per op) so a
# single shared connection is not required.
_db_lock = threading.Lock()
_enabled = bool(_DATABASE_URL)
_backfilled = False


def enabled() -> bool:
    return _enabled


def _connect():
    """Open a connection. Imports psycopg lazily so file-only mode needs no driver."""
    import psycopg  # type: ignore
    return psycopg.connect(_DATABASE_URL, autocommit=True, connect_timeout=12)


def init_db() -> None:
    """Verify the cloud DB is reachable and the health tables exist.

    The `health` schema and its tables are provisioned out-of-band (by an
    admin/owner role) — the runtime role `health_app` intentionally has only
    DML privileges (SELECT/INSERT/UPDATE/DELETE), NOT CREATE, so it cannot
    create schemas or tables. We therefore do NOT issue CREATE statements here;
    a failing CREATE would just log a scary permission error every boot.
    Instead we do a lightweight read to confirm readiness."""
    if not _enabled:
        print("[db] DATABASE_URL not set — running file-only mode")
        return
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM health.readings LIMIT 1")
            cur.execute("SELECT 1 FROM health.meds_taken LIMIT 1")
        print("[db] ready (Neon, schema health)")
    except Exception as e:
        print(f"[db][WARN] init_db health-check failed (file-only fallback): {e}")


def upsert_reading(date: str, rtype: str, rtime: str, value: str) -> bool:
    """Insert/overwrite one reading. Idempotent on (date, type) — mirrors the
    file logic where the same day+type is replaced by the newest value."""
    if not _enabled:
        return False
    try:
        with _db_lock, _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO health.readings (date, type, time, value, updated_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (date, type)
                DO UPDATE SET time = EXCLUDED.time,
                              value = EXCLUDED.value,
                              updated_at = now()
                """,
                (date, rtype, rtime, str(value)),
            )
        return True
    except Exception as e:
        print(f"[db][WARN] upsert_reading {date}/{rtype} failed: {e}")
        return False


def bulk_upsert_readings(rows) -> int:
    """rows: iterable of (date, type, time, value). Returns count written.
    Used by CSV import and startup backfill."""
    if not _enabled:
        return 0
    n = 0
    try:
        with _db_lock, _connect() as conn, conn.cursor() as cur:
            for date, rtype, rtime, value in rows:
                cur.execute(
                    """
                    INSERT INTO health.readings (date, type, time, value, updated_at)
                    VALUES (%s, %s, %s, %s, now())
                    ON CONFLICT (date, type)
                    DO UPDATE SET time = EXCLUDED.time,
                                  value = EXCLUDED.value,
                                  updated_at = now()
                    """,
                    (date, rtype, rtime, str(value)),
                )
                n += 1
    except Exception as e:
        print(f"[db][WARN] bulk_upsert_readings failed after {n}: {e}")
    return n


def readings_count() -> int:
    if not _enabled:
        return 0
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM health.readings")
            return cur.fetchone()[0]
    except Exception as e:
        print(f"[db][WARN] readings_count failed: {e}")
        return -1


def mark_med_taken(date: str, med_key: str) -> bool:
    if not _enabled:
        return False
    try:
        with _db_lock, _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO health.meds_taken (date, med_key, taken_at)
                VALUES (%s, %s, now())
                ON CONFLICT (date, med_key) DO NOTHING
                """,
                (date, med_key),
            )
        return True
    except Exception as e:
        print(f"[db][WARN] mark_med_taken {date}/{med_key} failed: {e}")
        return False


def clear_meds(date: str) -> bool:
    """Remove all med check-ins for a date (matches clear_meds_today undo)."""
    if not _enabled:
        return False
    try:
        with _db_lock, _connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM health.meds_taken WHERE date = %s", (date,))
        return True
    except Exception as e:
        print(f"[db][WARN] clear_meds {date} failed: {e}")
        return False


def backfill_from_file(load_data_fn) -> bool:
    """One-time startup backfill: if the readings table is empty and the JSON
    file has records, bulk-insert them. Guarded by a module flag + emptiness check
    so it never re-runs or duplicates.
    `load_data_fn` is the bot's existing file loader (injected to avoid a cycle).
    """
    global _backfilled
    if not _enabled or _backfilled:
        return False
    _backfilled = True
    try:
        existing = readings_count()
        if existing > 0:
            print(f"[db] backfill skipped — {existing} readings already in DB")
            return False
        data = load_data_fn() or {}
        rows = []
        for date, day in data.items():
            for r in day.get("records", []):
                rows.append((date, r.get("type"), r.get("time", ""), r.get("value", "")))
        if not rows:
            print("[db] backfill skipped — no file records")
            return False
        n = bulk_upsert_readings(rows)
        print(f"[db] startup backfill: {n} readings file → Neon")
        return n > 0
    except Exception as e:
        print(f"[db][WARN] backfill failed: {e}")
        return False
