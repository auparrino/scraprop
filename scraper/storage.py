"""SQLite-backed listing store with republish detection."""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Iterable, List, Optional

from .common import Listing


SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    listing_id      TEXT PRIMARY KEY,        -- "<source>:<external_id>"
    source          TEXT NOT NULL,
    external_id     TEXT NOT NULL,
    url             TEXT NOT NULL,
    title           TEXT,
    address         TEXT,
    barrio          TEXT,
    price_usd       INTEGER,
    expensas_ars    INTEGER,
    m2              INTEGER,
    ambientes       INTEGER,
    dormitorios     INTEGER,
    antiguedad      TEXT,
    orientacion     TEXT,
    description     TEXT,
    fingerprint     TEXT NOT NULL,
    first_seen      TEXT NOT NULL,           -- ISO date
    last_seen       TEXT NOT NULL,
    times_seen      INTEGER NOT NULL DEFAULT 1,
    is_republish    INTEGER NOT NULL DEFAULT 0,
    republish_of    TEXT,                    -- listing_id of the original, when known
    raw_json        TEXT
);

CREATE INDEX IF NOT EXISTS idx_listings_fingerprint ON listings(fingerprint);
CREATE INDEX IF NOT EXISTS idx_listings_first_seen ON listings(first_seen);

CREATE TABLE IF NOT EXISTS runs (
    run_date        TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    total_seen      INTEGER NOT NULL DEFAULT 0,
    new_listings    INTEGER NOT NULL DEFAULT 0,
    republishes     INTEGER NOT NULL DEFAULT 0,
    notes           TEXT
);
"""


class Store:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        # Forward-compatible migrations for older DBs created before
        # these columns existed.
        self._add_column_if_missing("listings", "antiguedad", "TEXT")
        self._add_column_if_missing("listings", "orientacion", "TEXT")
        self.db.commit()

    def _add_column_if_missing(self, table: str, column: str, decl: str) -> None:
        existing = {row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------------ #
    # Upserts
    # ------------------------------------------------------------------ #
    def upsert(self, listing: Listing, today: str) -> dict:
        """Insert or refresh a listing. Returns {status, republish_of}.

        status ∈ {"new", "new_republish", "seen"}.
          - "new"            : first time we ever see this listing AND no fingerprint match
          - "new_republish"  : first time we see this listing_id, but fingerprint already seen
          - "seen"           : we already have this listing_id; just bump last_seen
        """
        fp = listing.fingerprint
        row = self.db.execute(
            "SELECT listing_id, times_seen FROM listings WHERE listing_id = ?",
            (listing.listing_id,),
        ).fetchone()

        if row:
            self.db.execute(
                """UPDATE listings
                      SET last_seen = ?, times_seen = times_seen + 1,
                          price_usd = COALESCE(?, price_usd),
                          expensas_ars = COALESCE(?, expensas_ars),
                          antiguedad = COALESCE(antiguedad, ?),
                          orientacion = COALESCE(orientacion, ?),
                          url = ?, raw_json = ?
                    WHERE listing_id = ?""",
                (
                    today,
                    listing.price_usd,
                    listing.expensas_ars,
                    listing.antiguedad,
                    listing.orientacion,
                    listing.url,
                    json.dumps(listing.to_dict(), ensure_ascii=False),
                    listing.listing_id,
                ),
            )
            self.db.commit()
            return {"status": "seen", "republish_of": None}

        # New listing_id — check fingerprint to detect republish
        republish_of: Optional[str] = None
        match = self.db.execute(
            """SELECT listing_id FROM listings
                WHERE fingerprint = ? AND listing_id <> ?
             ORDER BY first_seen ASC LIMIT 1""",
            (fp, listing.listing_id),
        ).fetchone()
        if match:
            republish_of = match["listing_id"]

        self.db.execute(
            """INSERT INTO listings (
                    listing_id, source, external_id, url, title, address, barrio,
                    price_usd, expensas_ars, m2, ambientes, dormitorios,
                    antiguedad, orientacion, description,
                    fingerprint, first_seen, last_seen, times_seen,
                    is_republish, republish_of, raw_json
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                listing.listing_id,
                listing.source,
                listing.external_id,
                listing.url,
                listing.title,
                listing.address,
                listing.barrio,
                listing.price_usd,
                listing.expensas_ars,
                listing.m2,
                listing.ambientes,
                listing.dormitorios,
                listing.antiguedad,
                listing.orientacion,
                listing.description,
                fp,
                today,
                today,
                1,
                1 if republish_of else 0,
                republish_of,
                json.dumps(listing.to_dict(), ensure_ascii=False),
            ),
        )
        self.db.commit()
        return {
            "status": "new_republish" if republish_of else "new",
            "republish_of": republish_of,
        }

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #
    def listings_first_seen_on(self, day: str, *, exclude_republish: bool = True) -> List[sqlite3.Row]:
        sql = "SELECT * FROM listings WHERE first_seen = ?"
        if exclude_republish:
            sql += " AND is_republish = 0"
        sql += " ORDER BY source, price_usd"
        return list(self.db.execute(sql, (day,)).fetchall())

    def republishes_first_seen_on(self, day: str) -> List[sqlite3.Row]:
        sql = ("SELECT * FROM listings WHERE first_seen = ? AND is_republish = 1 "
               "ORDER BY source, price_usd")
        return list(self.db.execute(sql, (day,)).fetchall())

    def all_active_listings(self) -> List[sqlite3.Row]:
        """All non-republish listings, used by the viewer/JSON export."""
        return list(self.db.execute(
            "SELECT * FROM listings WHERE is_republish = 0 "
            "ORDER BY first_seen DESC, price_usd ASC"
        ).fetchall())

    # ------------------------------------------------------------------ #
    # Run tracking
    # ------------------------------------------------------------------ #
    def start_run(self, run_date: str, started_at: str) -> None:
        self.db.execute(
            """INSERT INTO runs (run_date, started_at) VALUES (?, ?)
               ON CONFLICT(run_date) DO UPDATE SET started_at = excluded.started_at""",
            (run_date, started_at),
        )
        self.db.commit()

    def finish_run(self, run_date: str, finished_at: str,
                   total_seen: int, new_listings: int, republishes: int,
                   notes: str = "") -> None:
        self.db.execute(
            """UPDATE runs SET finished_at = ?, total_seen = ?,
                                new_listings = ?, republishes = ?, notes = ?
                WHERE run_date = ?""",
            (finished_at, total_seen, new_listings, republishes, notes, run_date),
        )
        self.db.commit()
