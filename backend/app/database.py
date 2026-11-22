from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "novatech_security.db"


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialize_database() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS rooms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS cameras (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                host TEXT NOT NULL,
                port INTEGER NOT NULL DEFAULT 554,
                stream_path TEXT NOT NULL DEFAULT '/stream1',
                username TEXT NOT NULL DEFAULT '',
                password_encrypted TEXT NOT NULL DEFAULT '',
                vendor TEXT NOT NULL DEFAULT 'generic',
                control_port INTEGER NOT NULL DEFAULT 9000,
                room_id INTEGER,
                enabled INTEGER NOT NULL DEFAULT 1,
                ai_enabled INTEGER NOT NULL DEFAULT 1,
                connection_status TEXT NOT NULL DEFAULT 'registered',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(room_id) REFERENCES rooms(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS storage_targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                host TEXT NOT NULL,
                protocol TEXT NOT NULL CHECK(protocol IN ('smb', 'nfs')),
                share_path TEXT NOT NULL,
                mount_path TEXT NOT NULL,
                username TEXT NOT NULL DEFAULT '',
                password_encrypted TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'configured-not-mounted',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                camera_id INTEGER,
                camera_name TEXT NOT NULL DEFAULT 'Unknown camera',
                event_type TEXT NOT NULL,
                summary TEXT NOT NULL,
                confidence REAL,
                occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(camera_id) REFERENCES cameras(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS person_tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                public_id TEXT NOT NULL UNIQUE,
                camera_id INTEGER,
                camera_name TEXT NOT NULL DEFAULT 'Unknown camera',
                tracker_key TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                ended_at TEXT,
                confidence REAL,
                appearance_description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'complete')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(camera_id) REFERENCES cameras(id) ON DELETE SET NULL
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_person_tracks_active_key
                ON person_tracks(camera_id, tracker_key) WHERE ended_at IS NULL;
            CREATE INDEX IF NOT EXISTS idx_person_tracks_last_seen
                ON person_tracks(last_seen DESC);

            CREATE TABLE IF NOT EXISTS person_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id INTEGER NOT NULL,
                relative_path TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                media_type TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                FOREIGN KEY(track_id) REFERENCES person_tracks(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_person_snapshots_track
                ON person_snapshots(track_id, captured_at DESC);

            CREATE TABLE IF NOT EXISTS audio_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                camera_id INTEGER,
                action TEXT NOT NULL,
                content_preview TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(camera_id) REFERENCES cameras(id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_audio_actions_created
                ON audio_actions(created_at DESC);
            """
        )

        # Safe additive migration for databases created before v0.4.0.
        camera_columns = {row[1] for row in conn.execute("PRAGMA table_info(cameras)").fetchall()}
        if "vendor" not in camera_columns:
            conn.execute("ALTER TABLE cameras ADD COLUMN vendor TEXT NOT NULL DEFAULT 'generic'")
        if "control_port" not in camera_columns:
            conn.execute("ALTER TABLE cameras ADD COLUMN control_port INTEGER NOT NULL DEFAULT 9000")
        if "ai_enabled" not in camera_columns:
            conn.execute("ALTER TABLE cameras ADD COLUMN ai_enabled INTEGER NOT NULL DEFAULT 1")


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]
