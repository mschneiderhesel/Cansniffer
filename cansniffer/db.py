"""SQLite-Speicher für Frames, Marker, Sessions und Dekoder."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    started REAL NOT NULL,
    note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS frames (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    can_id INTEGER NOT NULL,
    ext INTEGER NOT NULL DEFAULT 0,
    dlc INTEGER NOT NULL,
    data BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_frames_session_ts ON frames(session_id, ts);
CREATE INDEX IF NOT EXISTS ix_frames_session_id ON frames(session_id, can_id);
CREATE TABLE IF NOT EXISTS markers (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    label TEXT NOT NULL,
    head INTEGER,
    key INTEGER,
    note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_markers_session ON markers(session_id, ts);
CREATE TABLE IF NOT EXISTS decoders (
    can_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    layout TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._buffer: list[tuple] = []
        self._last_flush = time.time()

    # --- Sessions -------------------------------------------------------
    def new_session(self, name: str, note: str = "") -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO sessions(name, started, note) VALUES (?,?,?)",
                (name, time.time(), note),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def sessions(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT s.id, s.name, s.started, s.note,
                          (SELECT COUNT(*) FROM frames f WHERE f.session_id=s.id) AS frames,
                          (SELECT COUNT(*) FROM markers m WHERE m.session_id=s.id) AS markers
                   FROM sessions s ORDER BY s.id DESC"""
            ).fetchall()
        return [dict(zip(("id", "name", "started", "note", "frames", "markers"), r)) for r in rows]

    # --- Frames ---------------------------------------------------------
    def add_frame(self, session_id: int, ts: float, can_id: int, ext: bool, data: bytes):
        self._buffer.append((session_id, ts, can_id, int(ext), len(data), data))
        if len(self._buffer) >= 200 or time.time() - self._last_flush > 0.5:
            self.flush()

    def flush(self):
        if not self._buffer:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT INTO frames(session_id, ts, can_id, ext, dlc, data) VALUES (?,?,?,?,?,?)",
                self._buffer,
            )
            self._conn.commit()
        self._buffer.clear()
        self._last_flush = time.time()

    def frames(self, session_id: int, t0: float | None = None, t1: float | None = None,
               can_id: int | None = None, limit: int = 100000) -> list[dict]:
        self.flush()
        q = "SELECT ts, can_id, ext, dlc, data FROM frames WHERE session_id=?"
        args: list = [session_id]
        if t0 is not None:
            q += " AND ts>=?"; args.append(t0)
        if t1 is not None:
            q += " AND ts<=?"; args.append(t1)
        if can_id is not None:
            q += " AND can_id=?"; args.append(can_id)
        q += " ORDER BY ts LIMIT ?"; args.append(limit)
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [{"ts": r[0], "id": r[1], "ext": bool(r[2]), "dlc": r[3], "data": bytes(r[4])} for r in rows]

    # --- Markers --------------------------------------------------------
    def add_marker(self, session_id: int, label: str, head: int | None = None,
                   key: int | None = None, note: str = "", ts: float | None = None) -> dict:
        ts = ts or time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO markers(session_id, ts, label, head, key, note) VALUES (?,?,?,?,?,?)",
                (session_id, ts, label, head, key, note),
            )
            self._conn.commit()
        return {"id": cur.lastrowid, "session_id": session_id, "ts": ts, "label": label,
                "head": head, "key": key, "note": note}

    def markers(self, session_id: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, ts, label, head, key, note FROM markers WHERE session_id=? ORDER BY ts",
                (session_id,),
            ).fetchall()
        return [dict(zip(("id", "ts", "label", "head", "key", "note"), r)) for r in rows]

    def delete_marker(self, marker_id: int):
        with self._lock:
            self._conn.execute("DELETE FROM markers WHERE id=?", (marker_id,))
            self._conn.commit()

    # --- Decoders -------------------------------------------------------
    def decoders(self) -> dict[int, dict]:
        with self._lock:
            rows = self._conn.execute("SELECT can_id, name, layout FROM decoders").fetchall()
        return {r[0]: {"name": r[1], "layout": json.loads(r[2])} for r in rows}

    def set_decoder(self, can_id: int, name: str, layout: list[dict]):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO decoders(can_id, name, layout) VALUES (?,?,?)",
                (can_id, name, json.dumps(layout)),
            )
            self._conn.commit()

    def delete_decoder(self, can_id: int):
        with self._lock:
            self._conn.execute("DELETE FROM decoders WHERE can_id=?", (can_id,))
            self._conn.commit()
