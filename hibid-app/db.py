"""
SQLite storage for the HiBid app.

Tables:
  profiles      - one row per person ("Jack", "Jill", ...), no auth
  search_terms  - each profile's saved search terms + target quantity
  lots          - raw lot data, shared across everyone (not per-profile)
  lot_matches   - which lots matched which profile+term, with a score
  feedback      - thumbs up/down history, used to learn word weights
  word_weights  - per profile+term, learned score nudge per word
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

DB_PATH = Path(__file__).parent / "data" / "hibid.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS search_terms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    term TEXT NOT NULL,
    target_quantity INTEGER,
    UNIQUE(profile_id, term)
);

CREATE TABLE IF NOT EXISTS lots (
    lot_id TEXT PRIMARY KEY,
    title TEXT,
    current_bid REAL,
    status TEXT,
    time_left TEXT,
    closing_date TEXT,
    distance_miles REAL,
    auction_name TEXT,
    auction_city TEXT,
    auction_state TEXT,
    url TEXT,
    image_url TEXT,
    last_updated TEXT
);

CREATE TABLE IF NOT EXISTS lot_matches (
    lot_id TEXT NOT NULL REFERENCES lots(lot_id) ON DELETE CASCADE,
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    term TEXT NOT NULL,
    quantity_found INTEGER,
    score REAL,
    notified INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT,
    PRIMARY KEY (lot_id, profile_id, term)
);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    term TEXT NOT NULL,
    lot_id TEXT NOT NULL,
    liked INTEGER NOT NULL,
    timestamp TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS word_weights (
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    term TEXT NOT NULL,
    word TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (profile_id, term, word)
);
"""


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # A default profile so the app isn't empty on first run.
        conn.execute(
            "INSERT OR IGNORE INTO profiles (name) VALUES ('Me')"
        )
        # Migration: databases created before image_url existed won't have
        # the column yet (CREATE TABLE IF NOT EXISTS doesn't add columns to
        # an already-existing table). Add it if it's missing.
        existing_columns = {row["name"] for row in conn.execute("PRAGMA table_info(lots)")}
        if "image_url" not in existing_columns:
            conn.execute("ALTER TABLE lots ADD COLUMN image_url TEXT")


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- profiles
def list_profiles() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM profiles ORDER BY id").fetchall()


def get_profile_by_name(name: str) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM profiles WHERE name = ?", (name,)
        ).fetchone()


def add_profile(name: str) -> None:
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO profiles (name) VALUES (?)", (name,))


# ------------------------------------------------------------ search terms
def list_terms_for_profile(profile_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM search_terms WHERE profile_id = ? ORDER BY term",
            (profile_id,),
        ).fetchall()


def list_all_terms() -> list[sqlite3.Row]:
    """All (profile_id, term, target_quantity) rows across everyone."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT search_terms.*, profiles.name AS profile_name "
            "FROM search_terms JOIN profiles ON profiles.id = search_terms.profile_id"
        ).fetchall()


def add_term(profile_id: int, term: str, target_quantity: Optional[int]) -> None:
    term = term.strip().lower()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO search_terms (profile_id, term, target_quantity)
            VALUES (?, ?, ?)
            ON CONFLICT(profile_id, term) DO UPDATE SET
                target_quantity = excluded.target_quantity
            """,
            (profile_id, term, target_quantity),
        )


def delete_term(term_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM search_terms WHERE id = ?", (term_id,))


# ------------------------------------------------------------------- lots
def upsert_lot(lot: dict[str, Any], now_iso: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO lots (lot_id, title, current_bid, status, time_left,
                               closing_date, distance_miles, auction_name,
                               auction_city, auction_state, url, image_url, last_updated)
            VALUES (:lot_id, :title, :current_bid, :status, :time_left,
                    :closing_date, :distance_miles, :auction_name,
                    :auction_city, :auction_state, :url, :image_url, :last_updated)
            ON CONFLICT(lot_id) DO UPDATE SET
                title=excluded.title, current_bid=excluded.current_bid,
                status=excluded.status, time_left=excluded.time_left,
                closing_date=excluded.closing_date,
                distance_miles=excluded.distance_miles,
                auction_name=excluded.auction_name,
                auction_city=excluded.auction_city,
                auction_state=excluded.auction_state,
                url=excluded.url, image_url=excluded.image_url,
                last_updated=excluded.last_updated
            """,
            {**lot, "last_updated": now_iso},
        )


def upsert_match(
    lot_id: str,
    profile_id: int,
    term: str,
    quantity_found: Optional[int],
    score: float,
    now_iso: str,
) -> bool:
    """Returns True if this is a brand-new match (worth notifying about)."""
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT 1 FROM lot_matches WHERE lot_id=? AND profile_id=? AND term=?",
            (lot_id, profile_id, term),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO lot_matches (lot_id, profile_id, term, quantity_found, score, notified, first_seen)
            VALUES (?, ?, ?, ?, ?, 0, ?)
            ON CONFLICT(lot_id, profile_id, term) DO UPDATE SET
                quantity_found=excluded.quantity_found, score=excluded.score
            """,
            (lot_id, profile_id, term, quantity_found, score, now_iso),
        )
        return existing is None


def unnotified_matches() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT lot_matches.*, lots.title, lots.url, lots.closing_date,
                   lots.auction_name, profiles.name AS profile_name
            FROM lot_matches
            JOIN lots ON lots.lot_id = lot_matches.lot_id
            JOIN profiles ON profiles.id = lot_matches.profile_id
            WHERE lot_matches.notified = 0
            """
        ).fetchall()


def mark_notified(lot_id: str, profile_id: int, term: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE lot_matches SET notified = 1 WHERE lot_id=? AND profile_id=? AND term=?",
            (lot_id, profile_id, term),
        )


def home_feed(status: str = "OPEN", limit: int = 300) -> list[sqlite3.Row]:
    """All currently-matched lots across everyone, soonest-closing first."""
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT lots.*, GROUP_CONCAT(DISTINCT profiles.name) AS profile_names,
                   MAX(lot_matches.score) AS best_score
            FROM lots
            JOIN lot_matches ON lot_matches.lot_id = lots.lot_id
            JOIN profiles ON profiles.id = lot_matches.profile_id
            WHERE lots.status = ?
            GROUP BY lots.lot_id
            ORDER BY (lots.closing_date IS NULL OR lots.closing_date = ''), lots.closing_date ASC
            LIMIT ?
            """,
            (status, limit),
        ).fetchall()


def profile_feed(profile_id: int, status: str = "OPEN", limit: int = 300) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT lots.*, lot_matches.term, lot_matches.quantity_found, lot_matches.score
            FROM lots
            JOIN lot_matches ON lot_matches.lot_id = lots.lot_id
            WHERE lot_matches.profile_id = ? AND lots.status = ?
            ORDER BY lot_matches.score DESC, lots.closing_date ASC
            LIMIT ?
            """,
            (profile_id, status, limit),
        ).fetchall()


# --------------------------------------------------------------- feedback
def record_feedback(profile_id: int, term: str, lot_id: str, liked: bool) -> None:
    from datetime import datetime, timezone

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO feedback (profile_id, term, lot_id, liked, timestamp) VALUES (?, ?, ?, ?, ?)",
            (profile_id, term, lot_id, int(liked), datetime.now(timezone.utc).isoformat()),
        )


def get_word_weights(profile_id: int, term: str) -> dict[str, float]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT word, weight FROM word_weights WHERE profile_id=? AND term=?",
            (profile_id, term),
        ).fetchall()
        return {row["word"]: row["weight"] for row in rows}


def bump_word_weight(profile_id: int, term: str, word: str, delta: float, clamp: float = 3.0) -> None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT weight FROM word_weights WHERE profile_id=? AND term=? AND word=?",
            (profile_id, term, word),
        ).fetchone()
        new_weight = (row["weight"] if row else 0.0) + delta
        new_weight = max(-clamp, min(clamp, new_weight))
        conn.execute(
            """
            INSERT INTO word_weights (profile_id, term, word, weight) VALUES (?, ?, ?, ?)
            ON CONFLICT(profile_id, term, word) DO UPDATE SET weight=excluded.weight
            """,
            (profile_id, term, word, new_weight),
        )
