"""
SQLite storage for bird sighting archive.

Schema: a sightings table plus an FTS5 virtual table for full-text search.
Triggers keep FTS in sync on insert/delete.
"""

import os
import re
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

SG_TZ = ZoneInfo("Asia/Singapore")
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_PATH = os.path.join(PROJECT_DIR, "sightings.db")


SCHEMA = """
CREATE TABLE IF NOT EXISTS sightings (
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL,
  species TEXT NOT NULL,
  location TEXT,
  observer TEXT,
  notes TEXT,
  source_msg_id INTEGER,
  created_at TEXT NOT NULL,
  UNIQUE(date, species, location, source_msg_id)
);

CREATE INDEX IF NOT EXISTS idx_species_date ON sightings(species, date DESC);
CREATE INDEX IF NOT EXISTS idx_date ON sightings(date);

CREATE VIRTUAL TABLE IF NOT EXISTS sightings_fts USING fts5(
  species, location, observer, notes,
  content='sightings', content_rowid='id',
  tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS sightings_ai AFTER INSERT ON sightings BEGIN
  INSERT INTO sightings_fts(rowid, species, location, observer, notes)
  VALUES (new.id, new.species, new.location, new.observer, new.notes);
END;

CREATE TRIGGER IF NOT EXISTS sightings_ad AFTER DELETE ON sightings BEGIN
  INSERT INTO sightings_fts(sightings_fts, rowid, species, location, observer, notes)
  VALUES('delete', old.id, old.species, old.location, old.observer, old.notes);
END;
"""


def connect(db_path=DEFAULT_DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path=DEFAULT_DB_PATH):
    """Create tables, indexes, and triggers if missing."""
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def insert_sightings(records, db_path=DEFAULT_DB_PATH):
    """
    Bulk insert sightings. Idempotent on (date, species, location, source_msg_id).

    Each record is a dict with keys: date, species, location, observer, notes, source_msg_id.
    Required: date, species. All others optional.

    Returns the number of new rows actually inserted.
    """
    if not records:
        return 0

    init_db(db_path)
    conn = connect(db_path)
    now = datetime.now(SG_TZ).isoformat()
    inserted = 0
    try:
        for r in records:
            if not r.get("date") or not r.get("species"):
                continue
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO sightings
                  (date, species, location, observer, notes, source_msg_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    r["date"],
                    r["species"].strip(),
                    (r.get("location") or "").strip() or None,
                    (r.get("observer") or "").strip() or None,
                    (r.get("notes") or "").strip() or None,
                    r.get("source_msg_id"),
                    now,
                ),
            )
            inserted += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return inserted


def _build_fts_query(query, acronym_map=None):
    """
    Convert a user query into an FTS5 expression.

    Each token becomes a prefix-match clause. If a token matches a known acronym
    (case-insensitive), it expands to (acronym OR (expansion tokens)) so that
    rows containing either the acronym OR its full form are matched.
    """
    tokens = [t for t in query.strip().split() if t]
    if not tokens:
        return None

    clauses = []
    for tok in tokens:
        key = tok.upper()
        if acronym_map and key in acronym_map:
            expansion = acronym_map[key]
            exp_tokens = [t for t in re.findall(r"\w+", expansion) if t]
            if exp_tokens:
                exp_clause = " ".join(f'"{t}"*' for t in exp_tokens)
                clauses.append(f'("{tok}"* OR ({exp_clause}))')
                continue
        clauses.append(f'"{tok}"*')
    return " ".join(clauses)


def search(query, limit=10, db_path=DEFAULT_DB_PATH, acronym_map=None):
    """
    Full-text search across species/location/observer/notes.
    Returns most recent sightings first. Each row is a dict.

    If acronym_map is provided, query tokens that match known acronyms are
    expanded so e.g. searching "SBG" also matches "Singapore Botanic Gardens".
    """
    if not query or not query.strip():
        return []

    init_db(db_path)

    fts_query = _build_fts_query(query, acronym_map)
    if not fts_query:
        return []

    conn = connect(db_path)
    try:
        cur = conn.execute(
            """
            SELECT s.id, s.date, s.species, s.location, s.observer, s.notes, s.source_msg_id
            FROM sightings_fts f
            JOIN sightings s ON s.id = f.rowid
            WHERE sightings_fts MATCH ?
            ORDER BY s.date DESC
            LIMIT ?
            """,
            (fts_query, limit),
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def prune_older_than(days=90, db_path=DEFAULT_DB_PATH):
    """Delete sightings older than `days` days. Returns the number of rows deleted."""
    init_db(db_path)
    conn = connect(db_path)
    try:
        cutoff = (datetime.now(SG_TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
        cur = conn.execute("DELETE FROM sightings WHERE date < ?", (cutoff,))
        deleted = cur.rowcount
        conn.commit()
        # Optimize FTS index after bulk deletes
        if deleted:
            conn.execute("INSERT INTO sightings_fts(sightings_fts) VALUES('optimize')")
            conn.commit()
        return deleted
    finally:
        conn.close()


def count(db_path=DEFAULT_DB_PATH):
    """Return total row count. Useful for sanity checks."""
    init_db(db_path)
    conn = connect(db_path)
    try:
        cur = conn.execute("SELECT COUNT(*) FROM sightings")
        return cur.fetchone()[0]
    finally:
        conn.close()
