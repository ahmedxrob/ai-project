from __future__ import annotations

import concurrent.futures
import hashlib
import json
import sqlite3
import re
import unicodedata
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




def _catalog_norm(value, title=False):
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    text = re.sub(r"\b(feat\.?|ft\.?|featuring)\b.*$", "", text)
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()

def _catalog_variants(*values):
    combined = " ".join(_catalog_norm(v) for v in values if v)
    tokens = set(re.findall(r"\b(?:deluxe|expanded|anniversary|edition|remaster|remastered|live|acoustic|instrumental|karaoke|radio|single|album|bonus|explicit|clean|demo|mix|version|edit)\b", combined))
    return ",".join(sorted(tokens))

def _catalog_identity_values(metadata, relative_path):
    metadata = metadata or {}
    title = metadata.get("title") or Path(relative_path).stem
    artist = metadata.get("artist") or "Unknown Artist"
    album = metadata.get("album") or ""
    version = metadata.get("album_version") or ""
    duration = 0.0
    try:
        duration = float(metadata.get("duration") or 0)
    except (TypeError, ValueError):
        pass
    bucket = int(round(duration / 5.0) * 5) if duration > 0 else 0
    title_key = _catalog_norm(title, title=True)
    artist_key = _catalog_norm(artist)
    album_key = _catalog_norm(album)
    variant_key = _catalog_variants(title, album, version)
    return title_key, artist_key, album_key, variant_key, bucket

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
                device INTEGER DEFAULT 0,
                inode INTEGER DEFAULT 0,
                health_checked_at REAL DEFAULT 0,
                missing INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_library_songs_path ON library_songs(relative_path)")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(library_songs)")}
            if "strong_hash" not in columns:
                conn.execute("ALTER TABLE library_songs ADD COLUMN strong_hash TEXT DEFAULT ''")
            if "device" not in columns:
                conn.execute("ALTER TABLE library_songs ADD COLUMN device INTEGER DEFAULT 0")
            if "inode" not in columns:
                conn.execute("ALTER TABLE library_songs ADD COLUMN inode INTEGER DEFAULT 0")
            if "health_checked_at" not in columns:
                conn.execute("ALTER TABLE library_songs ADD COLUMN health_checked_at REAL DEFAULT 0")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_library_songs_fingerprint ON library_songs(fingerprint)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_library_songs_strong_hash ON library_songs(strong_hash)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_library_songs_missing ON library_songs(missing)")
            conn.execute("""CREATE TABLE IF NOT EXISTS library_song_identity (
                song_id TEXT PRIMARY KEY,
                title_key TEXT NOT NULL DEFAULT '',
                artist_key TEXT NOT NULL DEFAULT '',
                album_key TEXT NOT NULL DEFAULT '',
                variant_key TEXT NOT NULL DEFAULT '',
                duration_bucket INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0,
                FOREIGN KEY(song_id) REFERENCES library_songs(id) ON DELETE CASCADE
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_library_identity_artist_title ON library_song_identity(artist_key,title_key)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_library_identity_title_artist ON library_song_identity(title_key,artist_key)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_library_identity_album ON library_song_identity(album_key)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_library_identity_duration ON library_song_identity(duration_bucket)")
            conn.execute("""CREATE TABLE IF NOT EXISTS library_song_aliases (
                legacy_id TEXT PRIMARY KEY,
                song_id TEXT NOT NULL
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_library_song_aliases_song ON library_song_aliases(song_id)")
            missing_identity_rows = conn.execute("""
                SELECT s.id,s.relative_path,s.metadata_json,s.updated_at
                FROM library_songs s
                LEFT JOIN library_song_identity i ON i.song_id=s.id
                WHERE i.song_id IS NULL
            """).fetchall()
            for row in missing_identity_rows:
                self._upsert_identity_row(conn, row[0], self._metadata_from_row(row), row[1], row[3] or time.time())
            conn.execute("DELETE FROM library_song_identity WHERE song_id NOT IN (SELECT id FROM library_songs)")
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
        for table, column in (("stars", "item_id"), ("playback_positions", "song_id"), ("play_history", "song_id"), ("song_review", "song_id"), ("song_editor_history", "song_id"), ("subsonic_scrobbles", "song_id")):
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
        """Reconcile current files while preserving IDs only for proven moves/metadata edits."""
        self.init_schema()
        existing = self.rows()

        current_paths = set()
        for path in files:
            try:
                current_paths.add(str(path.relative_to(self.library_dir)))
            except (OSError, RuntimeError, ValueError):
                continue

        by_path = {
            str(row["relative_path"]): row
            for row in existing
            if not str(row.get("relative_path") or "").startswith(".retired/")
            and not int(row.get("missing") or 0)
        }
        # A filesystem move may leave the old catalog row active until the
        # reconciliation transaction finishes. Match moved files by device/inode
        # first, then by fast fingerprint only when the old path is no longer
        # present. This preserves IDs for renames/moves without letting an exact
        # copied file steal the active source ID.
        by_location = defaultdict(list)
        by_fp = defaultdict(list)
        for row in existing:
            rel = str(row.get("relative_path") or "")
            if rel.startswith(".retired/") or rel in current_paths:
                continue
            device = int(row.get("device") or 0)
            inode = int(row.get("inode") or 0)
            if device and inode:
                by_location[(device, inode)].append(row)
            fp = str(row.get("fingerprint") or "")
            if fp and not fp.startswith("legacy:"):
                by_fp[fp].append(row)

        claimed_lock = threading.Lock()
        claimed = set()

        def inspect(path):
            stat = path.stat()
            rel = str(path.relative_to(self.library_dir))
            row = by_path.get(rel)
            device = int(getattr(stat, "st_dev", 0) or 0)
            inode = int(getattr(stat, "st_ino", 0) or 0)
            unchanged = bool(row and int(row.get("size") or -1) == int(stat.st_size) and int(row.get("mtime_ns") or -1) == int(stat.st_mtime_ns) and int(row.get("device") or 0) == device and int(row.get("inode") or 0) == inode)
            if unchanged:
                with claimed_lock: claimed.add(str(row["id"]))
                return {"id":str(row["id"]),"relative_path":rel,"fingerprint":str(row.get("fingerprint") or ""),"strong_hash":str(row.get("strong_hash") or ""),"device":device,"inode":inode,"size":stat.st_size,"mtime_ns":stat.st_mtime_ns,"metadata":self._metadata_from_row(row),"created_at":row.get("created_at") or time.time(),"replaced_id":""}

            fp = file_fingerprint(path)
            sid = None; created = None; strong_hash = ""; replaced_id = ""
            if row and str(row.get("fingerprint") or "") == fp:
                # Metadata/stat changed, but the content fingerprint is identical: same song.
                sid = str(row["id"]); created = row.get("created_at") or time.time(); strong_hash = str(row.get("strong_hash") or "")
            elif row is None:
                stat_identity = (int(getattr(stat, "st_dev", 0) or 0), int(getattr(stat, "st_ino", 0) or 0))
                location_candidates = [
                    c for c in by_location.get(stat_identity, [])
                    if str(c.get("id")) not in claimed
                ] if stat_identity[0] and stat_identity[1] else []
                if location_candidates:
                    current_hash = None
                    for candidate in location_candidates:
                        candidate_fp = str(candidate.get("fingerprint") or "")
                        if not candidate_fp or candidate_fp != fp:
                            continue
                        if current_hash is None:
                            current_hash = strong_file_hash(path)
                        candidate_hash = str(candidate.get("strong_hash") or "")
                        if candidate_hash and candidate_hash != current_hash:
                            continue
                        sid = str(candidate["id"]); created = candidate.get("created_at") or time.time(); strong_hash = current_hash
                        with claimed_lock: claimed.add(sid)
                        break
                else:
                    # Cross-filesystem moves may lose inode identity. Only reuse an
                    # ID when the previous catalog path is absent from this scan,
                    # and verify the full content hash before doing so.
                    candidates = [c for c in by_fp.get(fp, []) if str(c.get("id")) not in claimed and str(c.get("relative_path") or "") not in current_paths]
                    current_hash = strong_file_hash(path) if candidates else ""
                    for candidate in candidates:
                        candidate_hash = str(candidate.get("strong_hash") or "")
                        if candidate_hash and candidate_hash != current_hash:
                            continue
                        sid = str(candidate["id"]); created = candidate.get("created_at") or time.time(); strong_hash = current_hash
                        with claimed_lock: claimed.add(sid)
                        break
            else:
                # Same path with different content is a new logical item. The old ID is retired below.
                replaced_id = str(row["id"])

            if not sid:
                sid = persistent_song_id(); created = time.time()
            metadata = metadata_loader(path)
            with claimed_lock: claimed.add(sid)
            return {"id":sid,"relative_path":rel,"fingerprint":fp,"strong_hash":strong_hash,"device":device,"inode":inode,"size":stat.st_size,"mtime_ns":stat.st_mtime_ns,"metadata":metadata,"created_at":created,"replaced_id":replaced_id}

        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1,int(metadata_workers))) as pool:
            records = [future.result() for future in (pool.submit(inspect, path) for path in files)]

        seen={str(record["id"]) for record in records}; now=time.time()
        with self._connect() as conn:
            for record in records:
                replaced_id=str(record.get("replaced_id") or "")
                if replaced_id and replaced_id != str(record["id"]):
                    old=conn.execute("SELECT relative_path FROM library_songs WHERE id=?",(replaced_id,)).fetchone()
                    if old:
                        original_rel=str(old[0] or record["relative_path"])
                        retired_rel=f".retired/{replaced_id}/{Path(original_rel).name or 'track'}"
                        conn.execute("UPDATE library_songs SET relative_path=?,missing=1,updated_at=? WHERE id=?",(retired_rel,now,replaced_id))
                conn.execute(
                    """INSERT INTO library_songs(id,relative_path,fingerprint,strong_hash,device,inode,health_checked_at,size,mtime_ns,metadata_json,missing,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,0,?,?)
                    ON CONFLICT(id) DO UPDATE SET relative_path=excluded.relative_path,fingerprint=excluded.fingerprint,strong_hash=CASE WHEN excluded.strong_hash<>'' THEN excluded.strong_hash ELSE library_songs.strong_hash END,device=excluded.device,inode=excluded.inode,size=excluded.size,mtime_ns=excluded.mtime_ns,metadata_json=excluded.metadata_json,missing=0,updated_at=excluded.updated_at""",
                    (record["id"],record["relative_path"],record["fingerprint"],record.get("strong_hash") or "",record.get("device",0),record.get("inode",0),0.0,record["size"],record["mtime_ns"],json.dumps(record["metadata"],ensure_ascii=False,separators=(",",":")),record["created_at"] or now,now),
                )
                self._upsert_identity_row(conn, record["id"], record["metadata"], record["relative_path"], now)
            if seen:
                placeholders=",".join("?" for _ in seen)
                conn.execute(f"UPDATE library_songs SET missing=1,updated_at=? WHERE missing=0 AND id NOT IN ({placeholders})",(now,*sorted(seen)))
            else:
                conn.execute("UPDATE library_songs SET missing=1,updated_at=? WHERE missing=0",(now,))
            conn.commit()
        return records

    def upsert_file(self, path, metadata_loader):
        """Upsert one newly finalized file without stealing active identities."""
        self.init_schema(); path=Path(path).resolve(); base=self.library_dir.resolve(); rel=str(path.relative_to(base)); stat=path.stat(); fp=file_fingerprint(path); now=time.time(); metadata=metadata_loader(path)
        with self._connect() as conn:
            row=conn.execute("SELECT * FROM library_songs WHERE relative_path=? AND missing=0",(rel,)).fetchone()
            sid=None; created=now; strong_hash=""
            if row and str(row["fingerprint"] or "") == fp:
                sid=str(row["id"]); created=row["created_at"] or now; strong_hash=str(row["strong_hash"] or "")
            elif row:
                # Final path already exists with different content; retire old identity.
                replaced_id=str(row["id"]); retired_rel=f".retired/{replaced_id}/{Path(rel).name or 'track'}"
                conn.execute("UPDATE library_songs SET relative_path=?,missing=1,updated_at=? WHERE id=?",(retired_rel,now,replaced_id))
            if not sid:
                # Only missing catalog rows can donate identity to a new path. Full hash proves the move.
                new_hash=strong_file_hash(path)
                candidates=conn.execute("SELECT * FROM library_songs WHERE fingerprint=? AND missing=1 ORDER BY updated_at DESC",(fp,)).fetchall()
                for candidate in candidates:
                    candidate_hash=str(candidate["strong_hash"] or "")
                    if candidate_hash and candidate_hash != new_hash: continue
                    sid=str(candidate["id"]); created=candidate["created_at"] or now; strong_hash=new_hash; break
                if not sid: sid=persistent_song_id()
            conn.execute(
                """INSERT INTO library_songs(id,relative_path,fingerprint,strong_hash,device,inode,health_checked_at,size,mtime_ns,metadata_json,missing,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,0,?,?)
                ON CONFLICT(id) DO UPDATE SET relative_path=excluded.relative_path,fingerprint=excluded.fingerprint,strong_hash=CASE WHEN excluded.strong_hash<>'' THEN excluded.strong_hash ELSE library_songs.strong_hash END,device=excluded.device,inode=excluded.inode,size=excluded.size,mtime_ns=excluded.mtime_ns,metadata_json=excluded.metadata_json,missing=0,updated_at=excluded.updated_at""",
                (sid,rel,fp,strong_hash,int(getattr(stat,"st_dev",0) or 0),int(getattr(stat,"st_ino",0) or 0),0.0,int(stat.st_size),int(stat.st_mtime_ns),json.dumps(metadata,ensure_ascii=False,separators=(",",":")),created,now),
            )
            self._upsert_identity_row(conn, sid, metadata, rel, now)
            conn.commit()
        return {"id":sid,"relative_path":rel,"metadata":metadata,"size":int(stat.st_size),"mtime_ns":int(stat.st_mtime_ns)}

    def mark_missing(self, song_id):
        with self._connect() as conn:
            conn.execute("UPDATE library_songs SET missing=1,updated_at=? WHERE id=?", (time.time(), song_id))
            conn.commit()

    def retire_path(self, path):
        """Retire a current catalog row after a physical file is deleted."""
        rel=str(Path(path).resolve().relative_to(self.library_dir.resolve()))
        with self._connect() as conn:
            row=conn.execute("SELECT id FROM library_songs WHERE relative_path=? AND missing=0",(rel,)).fetchone()
            if not row: return None
            sid=str(row[0]); retired_rel=f".retired/{sid}/{Path(rel).name or 'track'}"
            conn.execute("UPDATE library_songs SET relative_path=?,missing=1,updated_at=? WHERE id=?",(retired_rel,time.time(),sid)); conn.commit(); return sid

    def _upsert_identity_row(self, conn, song_id, metadata, relative_path, updated_at=None):
        title_key, artist_key, album_key, variant_key, duration_bucket = _catalog_identity_values(metadata, relative_path)
        conn.execute(
            "INSERT INTO library_song_identity(song_id,title_key,artist_key,album_key,variant_key,duration_bucket,updated_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(song_id) DO UPDATE SET title_key=excluded.title_key,artist_key=excluded.artist_key,album_key=excluded.album_key,variant_key=excluded.variant_key,duration_bucket=excluded.duration_bucket,updated_at=excluded.updated_at",
            (str(song_id), title_key, artist_key, album_key, variant_key, duration_bucket, updated_at or time.time()),
        )

    def duplicate_candidates(self, title, artist, album="", duration=0, limit=120):
        title_key, artist_key, album_key, variant_key, duration_bucket = _catalog_identity_values(
            {"title": title, "artist": artist, "album": album, "duration": duration}, title or "track"
        )
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT s.*, i.title_key, i.artist_key, i.album_key, i.variant_key, i.duration_bucket
                   FROM library_song_identity i
                   JOIN library_songs s ON s.id=i.song_id
                  WHERE s.missing=0 AND (
                        (i.artist_key=? AND i.title_key=?) OR
                        (i.title_key=? AND i.album_key=?) OR
                        (i.artist_key=? AND i.album_key=?)
                  )
                  ORDER BY CASE WHEN i.artist_key=? AND i.title_key=? THEN 0 ELSE 1 END, i.updated_at DESC
                  LIMIT ?""",
                (artist_key, title_key, title_key, album_key, artist_key, album_key, artist_key, title_key, max(1, int(limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    def health_batch(self, limit):
        self.init_schema()
        with self._connect() as conn:
            rows=conn.execute("SELECT * FROM library_songs WHERE missing=0 ORDER BY COALESCE(health_checked_at,0) ASC,id ASC LIMIT ?",(max(1,int(limit)),)).fetchall()
        return [dict(row) for row in rows]

    def mark_health_checked(self, song_ids, checked_at=None):
        ids=[str(value) for value in (song_ids or []) if str(value)]
        if not ids: return
        checked_at=checked_at or time.time()
        with self._connect() as conn:
            placeholders=",".join("?" for _ in ids)
            conn.execute(f"UPDATE library_songs SET health_checked_at=? WHERE id IN ({placeholders})",(checked_at,*ids)); conn.commit()

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

    def resolve_song_ids(self, song_ids):
        ids=[str(value or "")[:512] for value in (song_ids or []) if str(value or "")]
        if not ids:
            return {}
        with self._connect() as conn:
            placeholders=",".join("?" for _ in ids)
            rows=conn.execute(f"SELECT legacy_id,song_id FROM library_song_aliases WHERE legacy_id IN ({placeholders})",ids).fetchall()
        return {str(row[0]):str(row[1]) for row in rows}
