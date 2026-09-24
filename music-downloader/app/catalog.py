from __future__ import annotations

import concurrent.futures
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from collections import defaultdict
from pathlib import Path


class StorageUnavailable(RuntimeError):
    """Raised when the configured music storage cannot be read safely."""


AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".ogg", ".wav", ".opus", ".aac", ".alac"}


def legacy_song_id(download_dir: Path, relative_path: str) -> str:
    return "song-" + hashlib.sha1(str(relative_path).encode("utf-8")).hexdigest()[:20]


def persistent_song_id() -> str:
    return "song-" + uuid.uuid4().hex[:24]


def strong_file_hash(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Full SHA-256 content hash for collision-proof duplicate confirmation."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def file_fingerprint(path: Path) -> str:
    """Fast content fingerprint used only to match renames/moves."""
    stat = path.stat()
    size = int(stat.st_size)
    chunk = 64 * 1024
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        digest.update(handle.read(chunk))
        if size > chunk:
            handle.seek(max(0, size - chunk))
            digest.update(handle.read(chunk))
    return f"{size}:{digest.hexdigest()}"


class LibraryCatalog:
    """Authoritative SQLite catalog for media identity, paths and metadata."""

    def __init__(self, db_file: Path, library_dir: Path, legacy_index_file: Path):
        self.db_file = Path(db_file)
        self.library_dir = Path(library_dir)
        self.legacy_index_file = Path(legacy_index_file)

    def _connect(self):
        conn = sqlite3.connect(self.db_file, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")
        return conn

    def init_schema(self, conn=None):
        owned = conn is None
        conn = conn or self._connect()
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS library_songs (
                id TEXT PRIMARY KEY,
                relative_path TEXT UNIQUE NOT NULL,
                fingerprint TEXT DEFAULT '',
                size INTEGER DEFAULT 0,
                mtime_ns INTEGER DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                strong_hash TEXT DEFAULT '',
                missing INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_library_songs_path ON library_songs(relative_path)")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(library_songs)")}
            if "strong_hash" not in columns:
                conn.execute("ALTER TABLE library_songs ADD COLUMN strong_hash TEXT DEFAULT ''")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_library_songs_fingerprint ON library_songs(fingerprint)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_library_songs_strong_hash ON library_songs(strong_hash)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_library_songs_missing ON library_songs(missing)")
            conn.execute("""CREATE TABLE IF NOT EXISTS library_song_aliases (
                legacy_id TEXT PRIMARY KEY,
                song_id TEXT NOT NULL
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_library_song_aliases_song ON library_song_aliases(song_id)")
            if owned:
                conn.commit()
        finally:
            if owned:
                conn.close()

    @staticmethod
    def _metadata_from_row(row):
        try:
            data = json.loads(row["metadata_json"] or "{}")
            return data if isinstance(data, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}

    def rows(self):
        with self._connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM library_songs").fetchall()]

    def migrate_legacy_index(self, metadata_loader=None):
        """Migrate the old path-keyed index exactly once, including saved references."""
        self.init_schema()
        with self._connect() as conn:
            count = int(conn.execute("SELECT COUNT(*) FROM library_songs").fetchone()[0])
        if count or not self.legacy_index_file.exists():
            return 0
        try:
            payload = json.loads(self.legacy_index_file.read_text(encoding="utf-8"))
        except Exception:
            return 0
        entries = payload.get("entries", {}) if isinstance(payload, dict) else {}
        if not isinstance(entries, dict) or not entries:
            return 0
        records = []
        aliases = []
        now = time.time()
        base = self.library_dir.resolve()
        for rel, raw in entries.items():
            rel = str(rel)
            try:
                path = (self.library_dir / rel).resolve()
                path.relative_to(base)
            except (OSError, RuntimeError, ValueError):
                continue
            raw = dict(raw) if isinstance(raw, dict) else {}
            if path.is_file():
                try:
                    st = path.stat()
                    fp = file_fingerprint(path)
                except OSError:
                    st = None
                    fp = ""
            else:
                st = None
                fp = ""
            if not fp:
                fp = "legacy:" + hashlib.sha1(rel.encode("utf-8")).hexdigest()
            metadata = {
                "title": raw.get("title") or path.stem,
                "artist": raw.get("artist") or "Unknown Artist",
                "album_artist": raw.get("album_artist") or raw.get("artist") or "Unknown Artist",
                "album": raw.get("album") or path.stem,
                "genre": raw.get("genre") or "",
                "year": raw.get("year") or "",
                "track": raw.get("track") or 0,
                "disc": raw.get("disc") or 0,
                "duration": raw.get("duration") or 0,
                "bit_rate": raw.get("bit_rate") or 0,
                "bit_depth": raw.get("bit_depth") or 0,
                "sample_rate": raw.get("sample_rate") or 0,
                "channels": raw.get("channels") or 0,
                "replaygain_track_gain": raw.get("replaygain_track_gain"),
                "replaygain_album_gain": raw.get("replaygain_album_gain"),
                "replaygain_track_peak": raw.get("replaygain_track_peak"),
                "replaygain_album_peak": raw.get("replaygain_album_peak"),
                "has_artwork": bool(raw.get("has_artwork")),
            }
            sid = persistent_song_id()
            records.append((
                sid, rel, fp, "",
                int(st.st_size) if st else int(raw.get("size") or 0),
                int(st.st_mtime_ns) if st else int(raw.get("mtime_ns") or 0),
                json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                0 if st else 1, now, now,
            ))
            aliases.append((legacy_song_id(self.library_dir, rel), sid))
        if not records:
            return 0
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO library_songs(id,relative_path,fingerprint,strong_hash,size,mtime_ns,metadata_json,missing,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                records,
            )
            conn.executemany("INSERT OR IGNORE INTO library_song_aliases(legacy_id,song_id) VALUES(?,?)", aliases)
            self._remap_references(conn, dict(aliases))
            conn.commit()
        return len(records)

    @staticmethod
    def _remap_references(conn, alias_map):
        if not alias_map:
            return
        for table, column in (("stars", "item_id"), ("playback_positions", "song_id"), ("play_history", "song_id"), ("song_review", "song_id"), ("song_editor_history", "song_id")):
            try:
                rows = conn.execute(f"SELECT rowid,{column} FROM {table}").fetchall()
            except sqlite3.Error:
                continue
            for row in rows:
                value = str(row[1] or "")
                replacement = alias_map.get(value)
                if replacement and replacement != value:
                    conn.execute(f"UPDATE {table} SET {column}=? WHERE rowid=?", (replacement, row[0]))
        try:
            rows = conn.execute("SELECT id,song_ids FROM playlists").fetchall()
            for row in rows:
                try:
                    ids = json.loads(row[1] or "[]")
                    ids = ids if isinstance(ids, list) else []
                except Exception:
                    ids = []
                mapped = [alias_map.get(str(value), value) for value in ids]
                if mapped != ids:
                    conn.execute("UPDATE playlists SET song_ids=?,updated_at=? WHERE id=?", (json.dumps(mapped, separators=(",", ":")), time.time(), row[0]))
        except sqlite3.Error:
            pass
        try:
            rows = conn.execute("SELECT session_key,state_json FROM player_sessions").fetchall()
            for row in rows:
                try:
                    state = json.loads(row[1] or "{}")
                except Exception:
                    continue
                changed = False
                if isinstance(state, dict):
                    sid = str(state.get("songId") or "")
                    if sid in alias_map:
                        state["songId"] = alias_map[sid]
                        changed = True
                    for item in state.get("queue") if isinstance(state.get("queue"), list) else []:
                        if isinstance(item, dict) and str(item.get("id") or "") in alias_map:
                            item["id"] = alias_map[str(item["id"])]
                            changed = True
                    daily = state.get("dailyMix")
                    for item in daily.get("tracks") if isinstance(daily, dict) and isinstance(daily.get("tracks"), list) else []:
                        if isinstance(item, dict) and str(item.get("id") or "") in alias_map:
                            item["id"] = alias_map[str(item["id"])]
                            changed = True
                if changed:
                    conn.execute("UPDATE player_sessions SET state_json=?,updated_at=? WHERE session_key=?", (json.dumps(state, ensure_ascii=False, separators=(",", ":")), time.time(), row[0]))
        except sqlite3.Error:
            pass

    def reconcile(self, files, metadata_loader, metadata_workers=8):
        """Reconcile current files. Rename/move detection preserves IDs by fingerprint."""
        self.init_schema()
        existing = self.rows()
        by_path = {str(row["relative_path"]): row for row in existing}
        by_fp = defaultdict(list)
        for row in existing:
            fp = str(row.get("fingerprint") or "")
            if fp and not fp.startswith("legacy:"):
                by_fp[fp].append(row)

        current_paths = set()
        for path in files:
            try:
                current_paths.add(str(path.relative_to(self.library_dir)))
            except (OSError, RuntimeError, ValueError):
                continue

        claimed_lock = threading.Lock()

        def inspect(path):
            stat = path.stat()
            rel = str(path.relative_to(self.library_dir))
            row = by_path.get(rel)
            unchanged = bool(row and int(row.get("size") or -1) == int(stat.st_size) and int(row.get("mtime_ns") or -1) == int(stat.st_mtime_ns))
            if unchanged:
                with claimed_lock:
                    claimed.add(str(row["id"]))
                return {
                    "id": str(row["id"]), "relative_path": rel, "fingerprint": str(row.get("fingerprint") or ""),
                    "strong_hash": str(row.get("strong_hash") or ""), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "metadata": self._metadata_from_row(row), "created_at": row.get("created_at") or time.time(),
                }
            fp = file_fingerprint(path)
            sid = str(row["id"]) if row else None
            created = row.get("created_at") if row else None
            if not sid:
                with claimed_lock:
                    for candidate in by_fp.get(fp, []):
                        candidate_path = str(candidate.get("relative_path") or "")
                        if candidate_path not in current_paths and str(candidate["id"]) not in claimed:
                            sid = str(candidate["id"])
                            created = candidate.get("created_at") or time.time()
                            claimed.add(sid)
                            break
            if not sid:
                sid = persistent_song_id()
                created = time.time()
            metadata = metadata_loader(path)
            with claimed_lock:
                claimed.add(sid)
            return {"id": sid, "relative_path": rel, "fingerprint": fp, "strong_hash": str(row.get("strong_hash") or "") if row else "", "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "metadata": metadata, "created_at": created}

        claimed = set()
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, int(metadata_workers))) as pool:
            futures = [pool.submit(inspect, path) for path in files]
            records = [future.result() for future in futures]

        seen = {str(record["id"]) for record in records}
        now = time.time()
        with self._connect() as conn:
            for record in records:
                conn.execute(
                    """INSERT INTO library_songs(id,relative_path,fingerprint,strong_hash,size,mtime_ns,metadata_json,missing,created_at,updated_at) VALUES(?,?,?,?,?,?,?,0,?,?)
                    ON CONFLICT(id) DO UPDATE SET relative_path=excluded.relative_path,fingerprint=excluded.fingerprint,strong_hash=CASE WHEN excluded.strong_hash<>'' THEN excluded.strong_hash ELSE library_songs.strong_hash END,size=excluded.size,mtime_ns=excluded.mtime_ns,metadata_json=excluded.metadata_json,missing=0,updated_at=excluded.updated_at""",
                    (record["id"],record["relative_path"],record["fingerprint"],record.get("strong_hash") or "",record["size"],record["mtime_ns"],json.dumps(record["metadata"],ensure_ascii=False,separators=(",", ":")),record["created_at"] or now,now),
                )
            if seen:
                placeholders = ",".join("?" for _ in seen)
                conn.execute(f"UPDATE library_songs SET missing=1,updated_at=? WHERE missing=0 AND id NOT IN ({placeholders})", (now,*sorted(seen)))
            else:
                conn.execute("UPDATE library_songs SET missing=1,updated_at=? WHERE missing=0", (now,))
            conn.commit()
        return records

    def mark_missing(self, song_id):
        with self._connect() as conn:
            conn.execute("UPDATE library_songs SET missing=1,updated_at=? WHERE id=?", (time.time(), song_id))
            conn.commit()

    def song_for_path(self, path):
        rel = str(Path(path).resolve().relative_to(self.library_dir.resolve()))
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM library_songs WHERE relative_path=?", (rel,)).fetchone()
        return dict(row) if row else None

    def resolve_song_id(self, song_id):
        sid = str(song_id or "")[:512]
        if not sid:
            return ""
        with self._connect() as conn:
            row = conn.execute("SELECT song_id FROM library_song_aliases WHERE legacy_id=?", (sid,)).fetchone()
        return str(row[0]) if row else sid
