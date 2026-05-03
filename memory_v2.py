"""Character Memory v2 — structured per-character DB with embeddings + Sonnet librarian.

This module is the implementation of the design in MEMORY_DESIGN.md.

It is intentionally isolated from claude_bridge.py: the bridge calls into a
small public surface (`prepare_turn`, `record_turn`, `is_enabled`, etc.) and
this module owns everything underneath — schema, connection pooling, embedding
model lifecycle, Sonnet invocation, JSON op application.

Layout per character (mirrors MEMORY_DESIGN.md):

    character_memory/<char_key>/
        memory.db           SQLite — main char's DB
        needs.json          current need values (Stage 6)
        card_seed.json      bootstrap snapshot (Stage 3)
        sonnet_errors.log   maintenance failures (Stage 5)
        npcs/
            <npc_key>/
                memory.db   NPC DB (lighter schema, Stage 7)
                npc_card.json

Stages 1-2 in this file: DB plumbing + embeddings module.
Later stages append below.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterable, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Anchor everything to the bridge directory so paths are stable regardless
# of where the bridge process was spawned from.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
MEMORY_ROOT = os.path.join(_THIS_DIR, "character_memory")

# Bumped whenever the schema changes. Drives migration logic in
# `ensure_schema`. PRAGMA user_version stores the applied version per DB.
SCHEMA_VERSION = 1

# All recognized memory `type` values. Used both for validation of inserts
# and as the inject-priority order for `format_injection` later.
MEMORY_TYPES = (
    "desire",
    "event",
    "fact",
    "rule",
    "relationship",
    "trait",
    "place",
    "possession",
    "body",
    "secret",
)

# Status values an entry can hold. `active` is the default; everything else
# is set by Sonnet maintenance ops or manual GUI edits.
MEMORY_STATUSES = ("active", "resolved", "dormant", "contradicted", "mutated")


# ---------------------------------------------------------------------------
# Logging shim
# ---------------------------------------------------------------------------
# memory_v2 is imported by claude_bridge.py, which has its own log(). We do
# late binding via set_logger() so this module stays importable in isolation
# (e.g., for unit tests) without dragging the whole bridge module along.

_log_fn = None


def set_logger(fn):
    """Inject the bridge's log() so memory_v2 messages land in the same console."""
    global _log_fn
    _log_fn = fn


def log(msg: str, level: str = "INFO"):
    if _log_fn is not None:
        try:
            _log_fn(f"[mem] {msg}", level)
            return
        except Exception:
            pass
    # Stdout fallback for standalone use / before set_logger fires.
    print(f"[mem:{level}] {msg}")


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_-]")


def safe_dirname(key: Optional[str]) -> Optional[str]:
    """Sanitize a char_key (or npc_key) into a filesystem-safe directory name.

    Keeps the same algorithm as v1 (claude_bridge._safe_char_dirname) so dirs
    line up across the v1 / v2 transition for the same character.
    """
    if not key:
        return None
    safe = _SAFE_NAME_RE.sub("_", str(key))[:64]
    return safe or None


def char_dir(char_key: str, create: bool = False) -> Optional[str]:
    """Absolute path to a character's memory directory."""
    name = safe_dirname(char_key)
    if not name:
        return None
    path = os.path.join(MEMORY_ROOT, name)
    if create:
        try:
            os.makedirs(path, exist_ok=True)
            os.makedirs(os.path.join(path, "npcs"), exist_ok=True)
        except OSError as e:
            log(f"Failed to create char dir {path}: {e}", "ERROR")
            return None
    return path


def npc_dir(char_key: str, npc_key: str, create: bool = False) -> Optional[str]:
    """Absolute path to a specific NPC's memory directory under a character."""
    parent = char_dir(char_key, create=create)
    if not parent:
        return None
    name = safe_dirname(npc_key)
    if not name:
        return None
    path = os.path.join(parent, "npcs", name)
    if create:
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as e:
            log(f"Failed to create npc dir {path}: {e}", "ERROR")
            return None
    return path


def db_path(char_key: str, npc_key: Optional[str] = None) -> Optional[str]:
    """Path to memory.db for either a char or one of its NPCs."""
    base = npc_dir(char_key, npc_key) if npc_key else char_dir(char_key)
    if not base:
        return None
    return os.path.join(base, "memory.db")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

# `embedding` is BLOB — raw bytes of a float32 vector. Nullable so entries
# inserted before the embedding model loads (or by manual GUI edits without
# embedding compute) are still valid; retrieval just falls back to keyword/
# importance ranking for those rows.
#
# `metadata` is TEXT holding JSON. SQLite has a native JSON1 extension but
# we use TEXT for portability — every Python sqlite3 build has TEXT, not all
# have JSON1 enabled.
_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS memories (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    type            TEXT NOT NULL,
    subject         TEXT,
    content         TEXT NOT NULL,
    intensity       INTEGER,
    importance      INTEGER NOT NULL DEFAULT 3,
    created_turn    INTEGER NOT NULL,
    last_seen_turn  INTEGER,
    last_acted_turn INTEGER,
    status          TEXT NOT NULL DEFAULT 'active',
    tags            TEXT,
    metadata        TEXT,
    embedding       BLOB
);

CREATE INDEX IF NOT EXISTS idx_type_status   ON memories(type, status);
CREATE INDEX IF NOT EXISTS idx_subject       ON memories(subject);
CREATE INDEX IF NOT EXISTS idx_last_seen     ON memories(last_seen_turn);
CREATE INDEX IF NOT EXISTS idx_importance    ON memories(importance, status);

CREATE TABLE IF NOT EXISTS turn_log (
    turn_number  INTEGER PRIMARY KEY,
    summary      TEXT,
    occurred_at  TEXT NOT NULL,
    message_hash TEXT
);

CREATE INDEX IF NOT EXISTS idx_turn_hash ON turn_log(message_hash);
"""


def ensure_schema(conn: sqlite3.Connection):
    """Create tables if missing, then run any version migrations.

    Called by `get_connection` on every connection acquire. Idempotent and
    fast — CREATE TABLE IF NOT EXISTS short-circuits when tables exist.
    """
    cur = conn.cursor()
    cur.execute("PRAGMA user_version")
    current = cur.fetchone()[0]
    if current == SCHEMA_VERSION:
        return
    if current == 0:
        cur.executescript(_SCHEMA_V1)
        cur.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
        return
    # Forward migrations land here as we add SCHEMA_VERSION 2, 3, ...
    if current > SCHEMA_VERSION:
        log(
            f"DB schema {current} is newer than module SCHEMA_VERSION {SCHEMA_VERSION} — "
            "you may be running an older bridge against a newer DB.",
            "WARN",
        )
        return
    # Future: while current < SCHEMA_VERSION: apply migrations[current+1]; current+=1
    log(
        f"No migration path from schema {current} to {SCHEMA_VERSION}; skipping.",
        "WARN",
    )


# ---------------------------------------------------------------------------
# Connection pool
# ---------------------------------------------------------------------------
# One persistent connection per (char_key, npc_key) pair, kept alive across
# requests. SQLite is single-writer but our access is serialized per char
# anyway (one RP turn at a time per character), so a single shared connection
# is fine and avoids the per-request open/close overhead.
#
# Connections are created lazily on first acquire and live until process exit.

_pool_lock = threading.RLock()
_pool: dict[str, sqlite3.Connection] = {}


def _pool_key(char_key: str, npc_key: Optional[str]) -> str:
    safe_c = safe_dirname(char_key) or ""
    safe_n = safe_dirname(npc_key) if npc_key else ""
    return f"{safe_c}::{safe_n}"


def get_connection(char_key: str, npc_key: Optional[str] = None) -> Optional[sqlite3.Connection]:
    """Return a live, schema-ensured connection to a char/NPC DB.

    Creates the directory and DB file on first call. Returns None if the
    char_key is unusable (empty/sanitized to nothing).
    """
    if not safe_dirname(char_key):
        return None
    if npc_key is not None and not safe_dirname(npc_key):
        return None

    key = _pool_key(char_key, npc_key)
    with _pool_lock:
        conn = _pool.get(key)
        if conn is not None:
            return conn

        path = db_path(char_key, npc_key)
        if not path:
            return None
        # Ensure parent dir exists (create=True on the dir helpers).
        if npc_key:
            npc_dir(char_key, npc_key, create=True)
        else:
            char_dir(char_key, create=True)

        try:
            # check_same_thread=False because the pool is shared across the
            # main Flask thread and background worker threads (post-turn
            # maintenance). All access is serialized via _pool_lock and
            # SQLite's own locking, so it's safe.
            conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            conn.row_factory = sqlite3.Row
            # WAL mode gives us better concurrent-read behavior and crash
            # safety. Cheap to enable, no downsides for our workload.
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA foreign_keys = ON")
            ensure_schema(conn)
        except sqlite3.Error as e:
            log(f"Failed to open DB at {path}: {e}", "ERROR")
            return None

        _pool[key] = conn
        return conn


def close_all_connections():
    """Close every pooled connection. Used by tests and on bridge shutdown."""
    with _pool_lock:
        for key, conn in list(_pool.items()):
            try:
                conn.close()
            except sqlite3.Error:
                pass
        _pool.clear()


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Atomic transaction wrapper. Rolls back on any exception.

    Use for grouped writes (e.g., applying a batch of Sonnet ops). Leaves
    isolation_level=None autocommit mode intact for single-statement writes
    elsewhere.
    """
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_type(t: str):
    if t not in MEMORY_TYPES:
        raise ValueError(f"Invalid memory type {t!r}; expected one of {MEMORY_TYPES}")


def _validate_status(s: str):
    if s not in MEMORY_STATUSES:
        raise ValueError(f"Invalid status {s!r}; expected one of {MEMORY_STATUSES}")


def _clamp(value: Optional[int], lo: int, hi: int) -> Optional[int]:
    if value is None:
        return None
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, v))


def _serialize_metadata(meta: Any) -> Optional[str]:
    if meta is None:
        return None
    if isinstance(meta, str):
        # Pre-serialized — trust caller.
        return meta
    try:
        return json.dumps(meta, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        log(f"metadata not JSON-serializable, dropping: {e}", "WARN")
        return None


def _deserialize_metadata(raw: Optional[str]) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw  # Return as string if it's not valid JSON; better than losing data.


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def insert_memory(
    conn: sqlite3.Connection,
    *,
    type: str,
    content: str,
    created_turn: int,
    subject: Optional[str] = None,
    intensity: Optional[int] = None,
    importance: int = 3,
    last_seen_turn: Optional[int] = None,
    last_acted_turn: Optional[int] = None,
    status: str = "active",
    tags: Optional[Iterable[str] | str] = None,
    metadata: Any = None,
    embedding: Optional[bytes] = None,
) -> int:
    """Insert a single memory row, returning the new id.

    Numeric ranges are clamped:
      - intensity: 1-6 (6 is the rare INEVITABLE tier — see _MAINT_PROMPT)
      - importance: 1-5
    Invalid enums raise ValueError so the caller knows their op was rejected.
    """
    _validate_type(type)
    _validate_status(status)

    if isinstance(tags, (list, tuple, set)):
        tags_str = ",".join(str(t).strip() for t in tags if str(t).strip())
    elif isinstance(tags, str):
        tags_str = tags
    else:
        tags_str = None

    cur = conn.execute(
        """
        INSERT INTO memories
            (type, subject, content, intensity, importance,
             created_turn, last_seen_turn, last_acted_turn,
             status, tags, metadata, embedding)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            type,
            subject,
            content,
            _clamp(intensity, 1, 6),
            _clamp(importance, 1, 5) or 3,
            int(created_turn),
            _clamp(last_seen_turn, 0, 10**9),
            _clamp(last_acted_turn, 0, 10**9),
            status,
            tags_str,
            _serialize_metadata(metadata),
            embedding,
        ),
    )
    return cur.lastrowid


# Fields that update_memory will accept and pass through to the UPDATE
# statement. Everything else (id, created_turn) is intentionally excluded —
# those should not change post-insert.
_UPDATABLE_FIELDS = {
    "type",
    "subject",
    "content",
    "intensity",
    "importance",
    "last_seen_turn",
    "last_acted_turn",
    "status",
    "tags",
    "metadata",
    "embedding",
}


def update_memory(conn: sqlite3.Connection, memory_id: int, **fields) -> bool:
    """Patch arbitrary fields on an existing memory.

    Returns True if a row was updated, False if no row matched.
    Fields not in `_UPDATABLE_FIELDS` are silently dropped — protects against
    Sonnet hallucinating column names.
    """
    if not fields:
        return False

    sets = []
    values = []
    for key, raw in fields.items():
        if key not in _UPDATABLE_FIELDS:
            continue
        if key == "type":
            _validate_type(raw)
            value = raw
        elif key == "status":
            _validate_status(raw)
            value = raw
        elif key == "intensity":
            value = _clamp(raw, 1, 6)
        elif key == "importance":
            value = _clamp(raw, 1, 5)
        elif key in ("last_seen_turn", "last_acted_turn"):
            value = _clamp(raw, 0, 10**9)
        elif key == "tags":
            if isinstance(raw, (list, tuple, set)):
                value = ",".join(str(t).strip() for t in raw if str(t).strip())
            else:
                value = raw
        elif key == "metadata":
            value = _serialize_metadata(raw)
        else:
            value = raw
        sets.append(f"{key} = ?")
        values.append(value)

    if not sets:
        return False
    values.append(int(memory_id))
    cur = conn.execute(
        f"UPDATE memories SET {', '.join(sets)} WHERE id = ?",
        values,
    )
    return cur.rowcount > 0


def get_memory(conn: sqlite3.Connection, memory_id: int) -> Optional[dict]:
    """Fetch one row by id, returned as dict (or None)."""
    row = conn.execute(
        "SELECT * FROM memories WHERE id = ?", (int(memory_id),)
    ).fetchone()
    return _row_to_dict(row) if row else None


def move_memory(
    char_key: str,
    row_id: int,
    source_npc_key: Optional[str] = None,
    target_npc_key: Optional[str] = None,
) -> tuple[Optional[int], Optional[str]]:
    """Move a memory row from one DB to another within the same character.

    `source_npc_key` / `target_npc_key`: None = main character DB, otherwise
    the NPC sub-DB. Returns (new_id, error_or_None). Preserves created_turn,
    last_seen_turn, last_acted_turn — those are facts about when the row was
    originally written, and shouldn't shift just because the user moved it.

    Atomicity: the insert into the target runs first, then the delete from
    the source. If the insert fails, the source row is preserved untouched.
    If the delete somehow fails after a successful insert (rare on SQLite),
    the row exists in both DBs and the user can manually clean up — flagged
    in the error string. We don't wrap both in a single transaction because
    they're separate connections.
    """
    if (source_npc_key or None) == (target_npc_key or None):
        return None, "source and target are the same"

    src_conn = get_connection(char_key, npc_key=source_npc_key) if source_npc_key else get_connection(char_key)
    if src_conn is None:
        return None, "source DB not found"
    tgt_conn = get_connection(char_key, npc_key=target_npc_key) if target_npc_key else get_connection(char_key)
    if tgt_conn is None:
        return None, "target DB not found"

    row = get_memory(src_conn, row_id)
    if not row:
        return None, "row not found in source"

    try:
        new_id = insert_memory(
            tgt_conn,
            type=row["type"],
            content=row["content"],
            subject=row.get("subject"),
            intensity=row.get("intensity"),
            importance=row.get("importance", 3),
            created_turn=row.get("created_turn", 0),
            last_seen_turn=row.get("last_seen_turn"),
            last_acted_turn=row.get("last_acted_turn"),
            status=row.get("status", "active"),
            tags=row.get("tags"),
            metadata=row.get("metadata"),
            embedding=row.get("embedding"),
        )
    except (ValueError, TypeError, sqlite3.Error) as e:
        return None, f"insert into target failed: {e}"

    try:
        src_conn.execute("DELETE FROM memories WHERE id = ?", (int(row_id),))
    except sqlite3.Error as e:
        return new_id, f"inserted into target as id={new_id} but failed to delete from source: {e}"

    return new_id, None


def query_memories(
    conn: sqlite3.Connection,
    *,
    types: Optional[Iterable[str]] = None,
    statuses: Optional[Iterable[str]] = ("active",),
    subjects: Optional[Iterable[str]] = None,
    min_importance: Optional[int] = None,
    min_intensity: Optional[int] = None,
    seen_within_turns: Optional[int] = None,
    current_turn: Optional[int] = None,
    tags_any: Optional[Iterable[str]] = None,
    limit: int = 100,
    order_by: str = "default",
) -> list[dict]:
    """Query memories with composable filters.

    Args:
      types: include only these type values; None = all
      statuses: include only these status values; default ('active',)
      subjects: include only memories about these subjects
      min_importance / min_intensity: floor filters
      seen_within_turns + current_turn: include rows seen within the last N turns
      tags_any: include if ANY of these tags appears in the comma-list
      limit: max rows returned
      order_by: 'default' (intensity DESC, importance DESC, last_seen DESC),
                'recent' (last_seen DESC), 'created' (created_turn DESC)
    """
    where = []
    params: list[Any] = []

    if types:
        types = tuple(types)
        where.append(f"type IN ({','.join('?' * len(types))})")
        params.extend(types)

    if statuses:
        statuses = tuple(statuses)
        where.append(f"status IN ({','.join('?' * len(statuses))})")
        params.extend(statuses)

    if subjects:
        subjects = tuple(subjects)
        where.append(f"subject IN ({','.join('?' * len(subjects))})")
        params.extend(subjects)

    if min_importance is not None:
        where.append("importance >= ?")
        params.append(int(min_importance))

    if min_intensity is not None:
        where.append("(intensity IS NOT NULL AND intensity >= ?)")
        params.append(int(min_intensity))

    if seen_within_turns is not None and current_turn is not None:
        where.append("(last_seen_turn IS NOT NULL AND last_seen_turn >= ?)")
        params.append(int(current_turn) - int(seen_within_turns))

    if tags_any:
        # SQLite has no array contains; LIKE on the comma-list is fine for
        # small tag sets. Wrap in commas so 'fear' doesn't match 'fearful'.
        # We normalize stored tags to comma-separated without spaces, but
        # we'll be defensive about spaces in queries.
        tag_clauses = []
        for t in tags_any:
            t = str(t).strip().lower()
            if not t:
                continue
            tag_clauses.append("(',' || LOWER(tags) || ',') LIKE ?")
            params.append(f"%,{t},%")
        if tag_clauses:
            where.append("(" + " OR ".join(tag_clauses) + ")")

    where_sql = "WHERE " + " AND ".join(where) if where else ""

    if order_by == "recent":
        order_sql = "ORDER BY last_seen_turn DESC NULLS LAST, id DESC"
    elif order_by == "created":
        order_sql = "ORDER BY created_turn DESC, id DESC"
    else:
        # 'default' — intensity (highest pressure first), then importance, then recency.
        # COALESCE turns NULL intensities into 0 so non-desire rows sort below pressing desires.
        order_sql = (
            "ORDER BY COALESCE(intensity, 0) DESC, importance DESC, "
            "COALESCE(last_seen_turn, 0) DESC, id DESC"
        )

    sql = f"SELECT * FROM memories {where_sql} {order_sql} LIMIT ?"
    params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def mark_seen(conn: sqlite3.Connection, ids: Iterable[int], turn: int) -> int:
    """Bump last_seen_turn on a batch of rows. Returns count updated."""
    ids = [int(i) for i in ids]
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    cur = conn.execute(
        f"UPDATE memories SET last_seen_turn = ? WHERE id IN ({placeholders})",
        [int(turn), *ids],
    )
    return cur.rowcount


def auto_dormant(conn: sqlite3.Connection, current_turn: int, threshold_turns: int = 30) -> int:
    """Mark active memories `dormant` if last_seen_turn is too far behind.

    Skips entries with importance >= 5 (those are explicitly "always relevant"
    and shouldn't auto-fade). Returns count transitioned.
    """
    cutoff = max(0, int(current_turn) - int(threshold_turns))
    cur = conn.execute(
        """
        UPDATE memories
        SET status = 'dormant'
        WHERE status = 'active'
          AND importance < 5
          AND (last_seen_turn IS NULL OR last_seen_turn < ?)
          AND created_turn < ?
        """,
        (cutoff, cutoff),
    )
    return cur.rowcount


def prune_mutated(conn: sqlite3.Connection, current_turn: int, threshold_turns: int = 20) -> int:
    """Hard-delete mutated rows older than `threshold_turns`.

    The `mutate` op preserves history by inserting a fresh active row carrying
    the new content and marking the old row `mutated`. After ~20 turns those
    old mutated rows are just noise — they're never injected into Opus, never
    queried by the GUI in the default view, never referenced by Sonnet. But
    they pile up: a heavily-evolving relationship row can spawn 30+ mutated
    copies in a long session and bloat the DB.

    We delete only mutated rows where `created_turn` is older than the cutoff.
    The most recent (active) row carrying that content stays; the chain of
    intermediate states gets compacted away.

    Returns count deleted.
    """
    cutoff = max(0, int(current_turn) - int(threshold_turns))
    cur = conn.execute(
        "DELETE FROM memories WHERE status = 'mutated' AND created_turn < ?",
        (cutoff,),
    )
    return cur.rowcount


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a sqlite3.Row into a plain dict, deserializing metadata."""
    d = dict(row)
    if "metadata" in d:
        d["metadata"] = _deserialize_metadata(d.get("metadata"))
    # embedding stays as raw bytes; callers that need it deserialize via
    # the embeddings module. Most callers don't read it.
    return d


# ---------------------------------------------------------------------------
# Turn log
# ---------------------------------------------------------------------------


def log_turn(
    conn: sqlite3.Connection,
    turn_number: int,
    summary: Optional[str] = None,
    message_hash: Optional[str] = None,
):
    """Insert/replace a row in turn_log. Used to detect re-rolls."""
    occurred_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    conn.execute(
        """
        INSERT INTO turn_log (turn_number, summary, occurred_at, message_hash)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(turn_number) DO UPDATE SET
            summary = excluded.summary,
            occurred_at = excluded.occurred_at,
            message_hash = excluded.message_hash
        """,
        (int(turn_number), summary, occurred_at, message_hash),
    )


def latest_turn(conn: sqlite3.Connection) -> int:
    """Return the highest turn_number recorded, or 0 if none."""
    row = conn.execute("SELECT MAX(turn_number) FROM turn_log").fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def is_swipe(conn: sqlite3.Connection, message_hash: str, lookback: int = 3) -> bool:
    """Return True if `message_hash` matches any of the last `lookback` turns.

    Used by post-turn maintenance to skip work on swipes/re-rolls so the DB
    doesn't get duplicate event entries.
    """
    if not message_hash:
        return False
    rows = conn.execute(
        "SELECT message_hash FROM turn_log ORDER BY turn_number DESC LIMIT ?",
        (int(lookback),),
    ).fetchall()
    return any(r[0] == message_hash for r in rows)


# =============================================================================
# Stage 2 — Embeddings
# =============================================================================
# sentence-transformers loaded lazily on first encode() call. Model files
# auto-download into ~/.cache/huggingface on first use (~80MB for the default
# all-MiniLM-L6-v2). After that it's local.

_EMBED_MODEL_NAME = "all-MiniLM-L6-v2"
_EMBED_DIM = 384  # known dimension for the default model

_embed_lock = threading.Lock()
_embed_model = None
_embed_load_failed = False


def _load_embed_model():
    """Lazy-load sentence-transformers; cache the singleton.

    Sets `_embed_load_failed` if the dependency is missing or the model
    can't load — caller code should treat embedding as silently disabled
    in that case (cosine_search returns empty, embed() returns None).
    """
    global _embed_model, _embed_load_failed
    if _embed_model is not None:
        return _embed_model
    if _embed_load_failed:
        return None
    with _embed_lock:
        if _embed_model is not None:
            return _embed_model
        if _embed_load_failed:
            return None
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
        except ImportError:
            log(
                "sentence-transformers not installed — embeddings disabled. "
                "Run: pip install sentence-transformers",
                "WARN",
            )
            _embed_load_failed = True
            return None
        try:
            log(f"Loading embedding model {_EMBED_MODEL_NAME} (first use may download ~80MB)...", "INFO")
            t0 = time.time()
            _embed_model = SentenceTransformer(_EMBED_MODEL_NAME)
            log(f"Embedding model loaded in {time.time() - t0:.1f}s", "SUCCESS")
        except Exception as e:
            log(f"Failed to load embedding model: {e}", "ERROR")
            _embed_load_failed = True
            return None
        return _embed_model


def embeddings_available() -> bool:
    """True if embeddings can be computed. Triggers lazy load on first call."""
    return _load_embed_model() is not None


def warmup_embeddings_async():
    """Trigger the sentence-transformers load on a background thread.

    Without this, the first user-facing prepare_turn pays a 5-30s blocking
    wait while the model loads (and possibly downloads ~80MB). Bridge
    startup calls this so the model is hot before the first request lands.
    """
    if _embed_model is not None or _embed_load_failed:
        return
    threading.Thread(
        target=_load_embed_model,
        daemon=True,
        name="memv2-embed-warmup",
    ).start()


def embed(text: str) -> Optional[bytes]:
    """Encode `text` into bytes suitable for the BLOB column.

    Returns None when the embedding model is unavailable (caller treats this
    as "no embedding for this row" — retrieval still works, just without
    the semantic-search dimension for this row).
    """
    if not text:
        return None
    model = _load_embed_model()
    if model is None:
        return None
    try:
        import numpy as np  # type: ignore

        vec = model.encode(text, normalize_embeddings=True)
        # Float32 keeps the BLOB size at 1.5KB per row (384 * 4B). Acceptable.
        return np.asarray(vec, dtype=np.float32).tobytes()
    except Exception as e:
        log(f"embed() failed: {e}", "WARN")
        return None


def embed_to_array(blob: Optional[bytes]):
    """Decode an embedding BLOB back to a 1-D numpy float32 array.

    Returns None if blob is empty or numpy isn't available.
    """
    if not blob:
        return None
    try:
        import numpy as np  # type: ignore

        return np.frombuffer(blob, dtype=np.float32)
    except Exception:
        return None


def cosine_search(
    query_text: str,
    candidates: list[dict],
    top_k: Optional[int] = None,
) -> list[tuple[dict, float]]:
    """Rank `candidates` (each a dict from `query_memories`) by cosine
    similarity to `query_text`.

    Candidates without an embedding are scored 0.0 and sink to the bottom.
    Returns list of (memory_dict, score) sorted descending by score.
    Returns [] if embeddings are disabled, query_text is empty, or numpy is
    missing.
    """
    if not query_text or not candidates:
        return []
    if not embeddings_available():
        return []
    try:
        import numpy as np  # type: ignore
    except ImportError:
        return []

    q_blob = embed(query_text)
    if not q_blob:
        return []
    q = np.frombuffer(q_blob, dtype=np.float32)
    # The model is configured with normalize_embeddings=True so vectors
    # are unit-length; cosine simplifies to a dot product.

    scored: list[tuple[dict, float]] = []
    for c in candidates:
        v = embed_to_array(c.get("embedding"))
        if v is None or v.shape != q.shape:
            scored.append((c, 0.0))
        else:
            scored.append((c, float(np.dot(q, v))))

    scored.sort(key=lambda x: x[1], reverse=True)
    if top_k is not None:
        scored = scored[: int(top_k)]
    return scored


# =============================================================================
# Stage 3 — Sonnet invocation + Bootstrap
# =============================================================================
# We talk to Sonnet via the same `claude -p` subprocess pattern the rest of
# the bridge already uses (see auto-lorebook). Kept local here so memory_v2
# stays self-contained; the only thing we depend on from outside is the path
# to the claude executable, which we read from an env hint or fall back to
# `claude` on PATH.

import hashlib
import subprocess

# Same default the bridge uses; can be overridden by setting CLAUDE_EXE in
# the environment before importing memory_v2 (the bridge does this in its
# own subprocess wrappers).
_CLAUDE_EXE = os.environ.get("CLAUDE_EXE", "claude")


def set_claude_exe(path: str):
    """Override the path to the claude CLI binary. Bridge calls this on import."""
    global _CLAUDE_EXE
    _CLAUDE_EXE = path


def _call_sonnet(prompt: str, timeout: int = 60, effort: Optional[str] = None) -> Optional[str]:
    """Run a one-shot Sonnet call and return the assistant text, or None.

    Mirrors claude_bridge's auto-lorebook subprocess pattern: stream-json
    output, parse for either a `result` event or the last `assistant` event's
    text content. Tolerant of partial output / parse errors per line.

    `effort` is forwarded to `--effort` when supplied. Pass "low" for simple
    structural tasks (ranking, JSON emission) to avoid burning thinking budget
    on work that doesn't need it. Note: Sonnet's narrative output collapses
    above "medium" effort, so we never pass anything above medium here even
    for the maintenance call.
    """
    cmd = [
        _CLAUDE_EXE,
        "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--model", "sonnet",
    ]
    if effort:
        cmd.extend(["--effort", effort])
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
    except FileNotFoundError:
        log(f"claude CLI not found at {_CLAUDE_EXE!r} — Sonnet calls disabled", "ERROR")
        return None

    try:
        stdout, stderr = proc.communicate(input=prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        log(f"Sonnet call timed out after {timeout}s", "WARN")
        return None
    except Exception as e:
        log(f"Sonnet subprocess error: {e}", "ERROR")
        return None

    text = ""
    for line in (stdout or "").strip().split("\n"):
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result":
            text = event.get("result", "") or text
            # `result` is the canonical final — stop scanning once seen.
            break
        if event.get("type") == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    text = block.get("text", "") or text
    return text or None


# JSON in Sonnet output is often wrapped in code fences or prose. This pulls
# the first balanced JSON object out without requiring a strict format.
_FENCE_RE = re.compile(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", re.IGNORECASE)


def _extract_json(text: str) -> Optional[dict]:
    """Best-effort JSON object extractor from a Sonnet response."""
    if not text:
        return None
    # 1) Try fenced ```json {...} ``` first.
    m = _FENCE_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # 2) Try the whole string.
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass
    # 3) Find the outermost balanced {...} using a brace counter.
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


def is_bootstrap_needed(char_key: str) -> bool:
    """True if this character has no DB yet, OR has an empty memories table."""
    path = db_path(char_key)
    if not path or not os.path.exists(path):
        return True
    conn = get_connection(char_key)
    if conn is None:
        return True
    row = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
    return (row[0] if row else 0) == 0


_BOOTSTRAP_SYSTEM = """You are a memory-system bootstrapper for an interactive RP system. Given a character card and the opening of a roleplay session, extract initial structured memory entries that will seed the character's persistent memory database.

You MUST respond with a single JSON object matching this exact shape:

{
  "traits": [
    {"content": "<one personality trait>", "importance": 1-5, "tags": "comma,separated"}
  ],
  "facts": [
    {"content": "<established fact about world/self/other>", "subject": "<user|self|named_thing|null>", "importance": 1-5}
  ],
  "places": [
    {"content": "<place name + state, e.g. 'the diner — closes at 9'>", "subject": "<place_key>", "importance": 1-5}
  ],
  "rules": [
    {"content": "<durable conclusion the character has formed>", "subject": "<who/what>", "importance": 1-5}
  ],
  "secrets": [
    {"content": "<something the character knows but hides>", "subject": "<who from>", "importance": 1-5}
  ],
  "body": [
    {"content": "<physical fact, scar, body memory>", "importance": 1-5}
  ],
  "relationships": {
    "user": {"closeness": 1-5, "trust": 1-5, "notes": "<one line summary>"},
    "<other_subject_key>": {"closeness": 1-5, "trust": 1-5, "notes": "..."}
  },
  "npcs": [
    {"name": "<NPC display name>", "bio": "<one paragraph from the card>", "introduced_in": "card"}
  ],
  "needs_init": {
    "physical":  {"hunger": 0.0-1.0, "fatigue": 0.0-1.0, "comfort": 0.0-1.0},
    "social":    {"connection": 0.0-1.0, "validation": 0.0-1.0, "autonomy": 0.0-1.0},
    "emotional": {"security": 0.0-1.0, "novelty": 0.0-1.0, "agency": 0.0-1.0},
    "custom":    {}
  }
}

RULES:
- Output ONLY the JSON object. No commentary, no fences, no explanation.
- Only include arrays/keys that have real content from the card. Empty arrays are fine; do not invent entries.
- For relationships.user, default to closeness=1, trust=1 unless the card establishes prior history.
- needs_init defaults: 0.7 across the board unless the card establishes a starting state (e.g. "exhausted" → fatigue 0.2).
- NPCs: include only NAMED supporting characters from the card. Skip generic roles ("the bartender", "a stranger").
- Importance scale: 1=trivia, 3=normal, 5=core to who they are.
- Subject keys for places/NPCs should be lowercase_snake_case derived from their name (e.g., "the_diner", "marcus").
- Be conservative — better to miss than to hallucinate. The system improves with use; don't try to capture everything in one pass.
"""


def _flatten_card(messages: list[dict]) -> str:
    """Pull what we believe is the character card out of a SillyTavern messages list.

    SillyTavern packs the character description into the system messages plus
    the first assistant message (the greeting). We concatenate up to ~12k
    chars of system content + the first assistant message + the first user
    message for context — that's enough for any realistic card.
    """
    sys_parts = []
    greeting = None
    first_user = None
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content", "")
        if isinstance(content, list):
            # Flatten OpenAI vision-format multipart by taking only text parts.
            content = "\n".join(
                p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
            )
        if not isinstance(content, str):
            content = str(content)
        if role == "system":
            sys_parts.append(content)
        elif role == "assistant" and greeting is None:
            greeting = content
        elif role == "user" and first_user is None:
            first_user = content

    sys_text = "\n\n".join(sys_parts)
    if len(sys_text) > 12000:
        sys_text = sys_text[:12000] + "\n[...truncated for bootstrap...]"

    parts = ["=== CHARACTER CARD / SYSTEM CONTEXT ===", sys_text]
    if greeting:
        parts += ["\n=== GREETING (first assistant message) ===", greeting[:4000]]
    if first_user:
        parts += ["\n=== FIRST USER MESSAGE ===", first_user[:2000]]
    return "\n".join(parts)


def run_bootstrap(char_key: str, messages: list[dict], current_turn: int = 0) -> Optional[dict]:
    """Run the Sonnet bootstrap pass and seed the character's DB.

    - Extracts the card+greeting from `messages`
    - Calls Sonnet with the bootstrap prompt
    - Parses the JSON
    - Writes initial rows to memories table + needs.json + card_seed.json
    - Embeddings: best-effort. If embeddings_available(), each row gets one;
      otherwise rows are inserted without embeddings (they still work for
      keyword/importance retrieval).

    Returns the parsed seed dict on success, None on failure (DB stays empty
    and bootstrap will be retried on the next turn).
    """
    conn = get_connection(char_key)
    if conn is None:
        log(f"bootstrap: no DB connection for {char_key}", "ERROR")
        return None

    card_text = _flatten_card(messages)
    if not card_text.strip():
        log(f"bootstrap: empty card for {char_key}, skipping", "WARN")
        return None

    log(f"bootstrap: calling Sonnet for {char_key}...", "INFO")
    t0 = time.time()
    raw = _call_sonnet(_BOOTSTRAP_SYSTEM + "\n\n" + card_text, timeout=120)
    if not raw:
        log(f"bootstrap: empty Sonnet response for {char_key}", "WARN")
        return None
    seed = _extract_json(raw)
    if not seed:
        log(f"bootstrap: failed to parse JSON from Sonnet response for {char_key}", "ERROR")
        # Save the raw output so the user can inspect what Sonnet produced.
        _write_error_log(char_key, "bootstrap", raw)
        return None

    log(f"bootstrap: Sonnet returned in {time.time() - t0:.1f}s, applying seed...", "SUCCESS")

    # Apply the seed atomically.
    inserted = 0
    try:
        with transaction(conn):
            # Generic type-bucket inserts.
            for type_name, key in (
                ("trait", "traits"),
                ("fact", "facts"),
                ("place", "places"),
                ("rule", "rules"),
                ("secret", "secrets"),
                ("body", "body"),
            ):
                for entry in (seed.get(key) or []):
                    if not isinstance(entry, dict):
                        continue
                    content = entry.get("content")
                    if not content:
                        continue
                    insert_memory(
                        conn,
                        type=type_name,
                        content=content,
                        subject=entry.get("subject"),
                        importance=int(entry.get("importance", 3)),
                        created_turn=current_turn,
                        tags=entry.get("tags"),
                        metadata={"source": "card_seed"},
                        embedding=embed(content),
                    )
                    inserted += 1

            # Relationships → one `relationship` row per subject. We store the
            # numeric closeness/trust in metadata so updates can patch them
            # without rewriting prose.
            rels = seed.get("relationships") or {}
            for subj, data in rels.items():
                if not isinstance(data, dict):
                    continue
                summary = data.get("notes") or ""
                meta = {
                    "source": "card_seed",
                    "closeness": _clamp(data.get("closeness"), 1, 5),
                    "trust": _clamp(data.get("trust"), 1, 5),
                }
                content = f"closeness={meta['closeness']} trust={meta['trust']} — {summary}"
                insert_memory(
                    conn,
                    type="relationship",
                    content=content,
                    subject=str(subj),
                    importance=4,  # relationships are always relevant
                    created_turn=current_turn,
                    metadata=meta,
                    embedding=embed(content),
                )
                inserted += 1

        # Persist the raw seed alongside the DB for audit + GUI re-run.
        cdir = char_dir(char_key, create=True)
        if cdir:
            seed_path = os.path.join(cdir, "card_seed.json")
            try:
                with open(seed_path, "w", encoding="utf-8") as f:
                    json.dump(seed, f, ensure_ascii=False, indent=2)
            except OSError as e:
                log(f"bootstrap: failed to write card_seed.json: {e}", "WARN")

            # Initialize needs.json from seed if needs_init present, else defaults.
            _write_initial_needs(char_key, seed.get("needs_init"))

        log(
            f"bootstrap: seeded {inserted} memories for {char_key} "
            f"({len(seed.get('npcs') or [])} NPCs noted but not yet registered)",
            "SUCCESS",
        )
        return seed
    except Exception as e:
        log(f"bootstrap: failed to apply seed: {e}", "ERROR")
        _write_error_log(char_key, "bootstrap_apply", json.dumps(seed) + "\n---\n" + str(e))
        return None


# ---------------------------------------------------------------------------
# needs.json (Stage 6 fully integrates this; written here so bootstrap can seed)
# ---------------------------------------------------------------------------

DEFAULT_NEEDS = {
    "physical":  {"hunger": 0.7, "fatigue": 0.7, "comfort": 0.7},
    "social":    {"connection": 0.7, "validation": 0.7, "autonomy": 0.7},
    "emotional": {"security": 0.7, "novelty": 0.7, "agency": 0.7},
    "custom":    {},
    "last_tick_turn": 0,
}


def _needs_path(char_key: str) -> Optional[str]:
    cdir = char_dir(char_key)
    return os.path.join(cdir, "needs.json") if cdir else None


def _write_initial_needs(char_key: str, seed_needs: Optional[dict]):
    """Write a fresh needs.json. Merges seed_needs over DEFAULT_NEEDS."""
    path = _needs_path(char_key)
    if not path:
        return
    needs = json.loads(json.dumps(DEFAULT_NEEDS))  # deep copy
    if isinstance(seed_needs, dict):
        for cat in ("physical", "social", "emotional", "custom"):
            src = seed_needs.get(cat)
            if isinstance(src, dict):
                # Coerce to floats clamped 0-1.
                for k, v in src.items():
                    try:
                        f = float(v)
                    except (TypeError, ValueError):
                        continue
                    needs.setdefault(cat, {})[str(k)] = max(0.0, min(1.0, f))
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(needs, f, ensure_ascii=False, indent=2)
    except OSError as e:
        log(f"failed to write needs.json: {e}", "WARN")


def load_needs(char_key: str) -> dict:
    """Return current needs dict for a character. Initializes with defaults if missing."""
    path = _needs_path(char_key)
    if not path:
        return json.loads(json.dumps(DEFAULT_NEEDS))
    if not os.path.exists(path):
        _write_initial_needs(char_key, None)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log(f"failed to load needs.json, using defaults: {e}", "WARN")
        return json.loads(json.dumps(DEFAULT_NEEDS))


def save_needs(char_key: str, needs: dict):
    path = _needs_path(char_key)
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(needs, f, ensure_ascii=False, indent=2)
    except OSError as e:
        log(f"failed to save needs.json: {e}", "WARN")


# Decay rates per category, per turn (Q3 default values).
DECAY_RATES = {
    "physical":  0.06,
    "social":    0.04,
    "emotional": 0.02,
    "custom":    0.03,
}


def tick_needs(char_key: str, current_turn: int) -> dict:
    """Apply per-turn decay since last tick. Returns the updated needs dict.

    Idempotent for repeated calls within the same turn (last_tick_turn prevents
    double-decay if a turn fails and retries).

    Per-character override: if needs.json contains a `decay_rates` dict, those
    values override the global DECAY_RATES for this character. Useful for
    characters where (e.g.) emotional needs should erode faster than physical.
    Missing categories fall back to globals.
    """
    needs = load_needs(char_key)
    last_tick = int(needs.get("last_tick_turn", 0) or 0)
    if current_turn <= last_tick:
        return needs
    elapsed = current_turn - last_tick
    overrides = needs.get("decay_rates") if isinstance(needs.get("decay_rates"), dict) else {}
    for cat, default_rate in DECAY_RATES.items():
        try:
            rate = float(overrides.get(cat, default_rate))
        except (TypeError, ValueError):
            rate = default_rate
        slots = needs.get(cat) or {}
        if not isinstance(slots, dict):
            continue
        for k, v in list(slots.items()):
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            slots[k] = max(0.0, fv - rate * elapsed)
        needs[cat] = slots
    needs["last_tick_turn"] = int(current_turn)
    save_needs(char_key, needs)
    return needs


def apply_needs_delta(char_key: str, delta: dict):
    """Apply a {category: {key: signed_delta}} update from Sonnet.

    Bounded to [0, 1]. Unknown categories under 'custom' are auto-created.
    """
    if not isinstance(delta, dict):
        return
    needs = load_needs(char_key)
    for cat, slots in delta.items():
        if not isinstance(slots, dict):
            continue
        bucket = needs.setdefault(cat if cat in DECAY_RATES else "custom", {})
        for k, v in slots.items():
            try:
                d = float(v)
            except (TypeError, ValueError):
                continue
            cur = float(bucket.get(k, 0.5) or 0.5)
            bucket[str(k)] = max(0.0, min(1.0, cur + d))
    save_needs(char_key, needs)


# ---------------------------------------------------------------------------
# Error log (Sonnet failures)
# ---------------------------------------------------------------------------


def _error_log_path(char_key: str) -> Optional[str]:
    cdir = char_dir(char_key)
    return os.path.join(cdir, "sonnet_errors.log") if cdir else None


def _write_error_log(char_key: str, kind: str, payload: str):
    path = _error_log_path(char_key)
    if not path:
        return
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    sep = "=" * 60
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n{sep}\n[{stamp}] {kind}\n{sep}\n{payload}\n")
    except OSError as e:
        log(f"failed to write error log: {e}", "WARN")


# =============================================================================
# NPC layer (Stage 7)
# =============================================================================
# An NPC is a recurring named secondary character that lives under a main
# character's folder. They get their own SQLite DB (same schema, but the type
# vocabulary in practice is narrower — typically `relationship`, `fact`,
# `event`, `trait`, `rule`. No needs.json — NPCs aren't simulated as deeply
# as the protagonist).
#
# Storage: character_memory/<main>/npcs/<npc_safe_name>/{memory.db, npc_card.json}
#
# Lifecycle:
#   1. `register_npc` op fires during post-turn maintenance OR seeded by
#      bootstrap when the card mentions a named supporting character.
#   2. We create the folder, npc_card.json (status="active"), and
#      synchronously seed a few `trait`/`fact` rows from the bio via Sonnet
#      (or fall back to a single `fact` row containing the bio if Sonnet
#      is unavailable).
#   3. From then on, when the NPC's name or alias appears in the recent
#      messages, the bridge queries that NPC's DB during prepare_turn and
#      merges entries into the injection.
#
# Status field: "active" (default) | "pending" (awaiting GUI confirmation —
# unused until Stage 8 GUI lands; reserved) | "dismissed" (user said no).


def _npc_card_path(char_key: str, npc_key: str) -> Optional[str]:
    d = npc_dir(char_key, npc_key)
    return os.path.join(d, "npc_card.json") if d else None


def load_npc_card(char_key: str, npc_key: str) -> Optional[dict]:
    p = _npc_card_path(char_key, npc_key)
    if not p or not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log(f"failed to load npc_card.json at {p}: {e}", "WARN")
        return None


def save_npc_card(char_key: str, npc_key: str, card: dict):
    p = _npc_card_path(char_key, npc_key)
    if not p:
        return
    try:
        # Ensure dir exists.
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(card, f, ensure_ascii=False, indent=2)
    except OSError as e:
        log(f"failed to save npc_card.json at {p}: {e}", "WARN")


_NPC_CARD_EDITABLE_FIELDS = {"name", "bio", "aliases", "status"}


def update_npc_card(char_key: str, npc_key: str, fields: dict) -> tuple[bool, Optional[str]]:
    """Patch the editable subset of an NPC card. Returns (ok, error_or_None).

    Editable fields: name, bio, aliases (list of strings), status. Anything
    else in `fields` is silently dropped — we don't expose seeded /
    seed_row_count / introduced_at_turn to the GUI because those are
    bookkeeping the system maintains itself.
    """
    card = load_npc_card(char_key, npc_key)
    if card is None:
        return False, "NPC card not found"
    changed = False
    for k, v in (fields or {}).items():
        if k not in _NPC_CARD_EDITABLE_FIELDS:
            continue
        if k == "aliases":
            if not isinstance(v, list):
                return False, "aliases must be a list of strings"
            v = [str(a).strip() for a in v if str(a).strip()]
        elif k == "status":
            if v not in ("active", "dismissed", "pending"):
                return False, f"invalid status: {v!r}"
        elif k in ("name", "bio"):
            v = str(v) if v is not None else ""
        if card.get(k) != v:
            card[k] = v
            changed = True
    if changed:
        save_npc_card(char_key, npc_key, card)
    return True, None


def list_npcs(char_key: str) -> list[dict]:
    """Return all NPC cards under a character. Each dict gets `npc_key` injected."""
    cdir = char_dir(char_key)
    if not cdir:
        return []
    npcs_root = os.path.join(cdir, "npcs")
    if not os.path.isdir(npcs_root):
        return []
    out = []
    for entry in sorted(os.listdir(npcs_root)):
        sub = os.path.join(npcs_root, entry)
        if not os.path.isdir(sub):
            continue
        card = load_npc_card(char_key, entry) or {}
        card["npc_key"] = entry
        out.append(card)
    return out


_NPC_BOOTSTRAP_PROMPT = """You are seeding the memory database for an NPC (supporting character) introduced in a roleplay.

INPUT: an NPC's display name and a one-paragraph bio.
OUTPUT: a single JSON object with seed entries — exactly the same schema as the main-character bootstrap, but most arrays will be empty since you only have a bio to work from. Be CONSERVATIVE.

{
  "traits": [{"content": "<one trait>", "importance": 1-5}],
  "facts": [{"content": "<bio fact>", "importance": 1-5}],
  "rules": [],
  "secrets": [],
  "body": [{"content": "<physical fact if mentioned>", "importance": 1-5}],
  "relationships": {
    "<main_char_key_or_user>": {"closeness": 1-5, "trust": 1-5, "notes": "..."}
  }
}

RULES:
- Output ONLY the JSON object. No commentary.
- Skip arrays/keys you have no content for. Empty arrays are fine.
- Don't invent traits not implied by the bio. Better to return mostly-empty than to hallucinate.
"""


def _npc_seed_from_bio(char_key: str, npc_key: str, name: str, bio: str, current_turn: int) -> int:
    """Run a fast Sonnet bootstrap on an NPC bio; returns count of seeded rows.

    Falls back to inserting a single `fact` row with the raw bio if Sonnet
    fails or isn't available. NPCs always have at least one row after
    registration so they're queryable from prepare_turn.
    """
    conn = get_connection(char_key, npc_key=npc_key)
    if conn is None:
        return 0

    # Try Sonnet; fall back to raw-bio fact on any failure.
    seeded = 0
    raw = _call_sonnet(
        _NPC_BOOTSTRAP_PROMPT
        + f"\n\nNPC NAME: {name}\nNPC BIO:\n{bio[:2000]}\n\nReturn ONLY the JSON.",
        timeout=45,
    )
    seed = _extract_json(raw) if raw else None
    try:
        with transaction(conn):
            if seed:
                for type_name, key in (
                    ("trait", "traits"),
                    ("fact", "facts"),
                    ("rule", "rules"),
                    ("secret", "secrets"),
                    ("body", "body"),
                ):
                    for entry in (seed.get(key) or []):
                        if not isinstance(entry, dict):
                            continue
                        content = entry.get("content")
                        if not content:
                            continue
                        insert_memory(
                            conn,
                            type=type_name,
                            content=content,
                            importance=int(entry.get("importance", 3)),
                            created_turn=current_turn,
                            metadata={"source": "npc_seed"},
                            embedding=embed(content),
                        )
                        seeded += 1
                rels = seed.get("relationships") or {}
                for subj, data in rels.items():
                    if not isinstance(data, dict):
                        continue
                    notes = data.get("notes") or ""
                    meta = {
                        "source": "npc_seed",
                        "closeness": _clamp(data.get("closeness"), 1, 5),
                        "trust": _clamp(data.get("trust"), 1, 5),
                    }
                    content = f"closeness={meta['closeness']} trust={meta['trust']} — {notes}"
                    insert_memory(
                        conn,
                        type="relationship",
                        content=content,
                        subject=str(subj),
                        importance=4,
                        created_turn=current_turn,
                        metadata=meta,
                        embedding=embed(content),
                    )
                    seeded += 1
            # Always include the raw bio as one anchor `fact`. Lets the NPC
            # appear in semantic search even if Sonnet returned nothing.
            insert_memory(
                conn,
                type="fact",
                content=f"{name}: {bio.strip()[:500]}",
                importance=3,
                created_turn=current_turn,
                tags="npc_bio",
                metadata={"source": "npc_bio"},
                embedding=embed(f"{name}: {bio[:500]}"),
            )
            seeded += 1
    except Exception as e:
        log(f"npc_seed_from_bio failed for {npc_key}: {e}", "WARN")

    return seeded


# Common honorifics/ranks/titles. Stripped before token-matching so
# "Cpl. Marsh" lines up with "Cpl. Reg Marsh" via "marsh", and
# "Mme. Joly" lines up with "Mme. Cloutier" only via title (which is
# discarded — so the surnames decide and they correctly DON'T match).
_NAME_TITLES = {
    "mr", "mrs", "ms", "mme", "mlle", "m", "dr", "sir", "lord", "lady",
    "capt", "cpt", "col", "lt", "sgt", "sgto", "cpl", "pvt", "pte",
    "maj", "gen", "adm", "fr", "rev", "monsieur", "madame", "mademoiselle",
    "the",
}


def _name_tokens(name: str) -> list[str]:
    """Tokenize a display name into lowercase non-title tokens of length >= 3.

    Drops short tokens ("a", "de") and known titles. Used for NPC dedup —
    so "Cpl. Reg Marsh" → ["reg", "marsh"], "Mme. Cloutier" → ["cloutier"],
    "Tom" → ["tom"].
    """
    if not name:
        return []
    raw = re.split(r"[\s\.\-,]+", name.lower())
    out = []
    for t in raw:
        t = t.strip(".,'\"`")
        if len(t) < 3:
            continue
        if t in _NAME_TITLES:
            continue
        out.append(t)
    return out


def _find_matching_npc(char_key: str, proposed_name: str) -> Optional[dict]:
    """Return an existing NPC card if `proposed_name` looks like the same
    person as an already-registered NPC, else None.

    Matches by:
      1. Direct npc_key match (exact slug).
      2. Any token of the proposed name shares with an existing name's tokens
         (after stripping titles). E.g. "Hélène Morel" matches existing "Hélène".
      3. Any alias of an existing NPC matches the proposed name (case-insensitive
         exact, OR token-overlap).

    Conservative: requires at least one shared token of length >= 3 that is
    NOT a title. Two NPCs with no shared meaningful token (e.g. "Mme. Joly" vs
    "Mme. Cloutier") will not match.
    """
    if not proposed_name:
        return None

    npcs = list_npcs(char_key)
    if not npcs:
        return None

    # 1) exact slug match
    target_key = safe_dirname(proposed_name.lower().replace(" ", "_"))
    for c in npcs:
        if c.get("npc_key") == target_key:
            return c

    proposed_tokens = set(_name_tokens(proposed_name))
    if not proposed_tokens:
        return None

    for c in npcs:
        existing_tokens = set(_name_tokens(c.get("name") or ""))
        # Add alias tokens too — if an alias was set as "Reg" we want
        # "Reg Marsh" to match.
        for a in (c.get("aliases") or []):
            existing_tokens.update(_name_tokens(a))
        if existing_tokens & proposed_tokens:
            return c

    return None


def register_npc(
    char_key: str,
    name: str,
    bio: str = "",
    introduced_at_turn: int = 0,
    aliases: Optional[list[str]] = None,
    status: str = "active",
) -> Optional[str]:
    """Create or update an NPC under `char_key`. Returns the npc_key.

    Dedup: if `name` matches an existing NPC by token overlap (e.g. "Henri"
    vs "Henri Morel", "Cpl. Marsh" vs "Cpl. Reg Marsh"), we add the new name
    as an alias on the existing entry instead of creating a duplicate. This
    prevents the NPC list from accumulating fragmentary variants of the same
    person as Sonnet learns more about them across turns.
    """
    if not name:
        return None

    # Dedup pass — try to match against an existing NPC first.
    matched = _find_matching_npc(char_key, name)
    if matched:
        existing_key = matched["npc_key"]
        # Add the proposed name (and any provided aliases) to the existing
        # entry's alias list. Don't re-seed — the existing NPC already has
        # whatever bootstrap rows it got.
        card = load_npc_card(char_key, existing_key) or matched
        card.setdefault("aliases", [])
        # Add the proposed name itself if it's not already the canonical name
        # or an existing alias.
        canonical = card.get("name") or ""
        if name != canonical and name not in card["aliases"]:
            card["aliases"].append(name)
        for a in (aliases or []):
            if a and a != canonical and a not in card["aliases"]:
                card["aliases"].append(a)
        # If the new bio is longer/richer than the existing one, prefer it.
        existing_bio = card.get("bio") or ""
        if bio and len(bio) > len(existing_bio):
            card["bio"] = bio
        save_npc_card(char_key, existing_key, card)
        log(
            f"NPC dedup: '{name}' merged into existing '{canonical}' ({existing_key}); "
            f"now has aliases={card.get('aliases')}",
            "INFO",
        )
        return existing_key

    npc_key = safe_dirname(name.lower().replace(" ", "_"))
    if not npc_key:
        return None
    # Ensure dir + DB exist.
    npc_dir(char_key, npc_key, create=True)
    conn = get_connection(char_key, npc_key=npc_key)
    if conn is None:
        return None

    existing = load_npc_card(char_key, npc_key)
    if existing:
        # Update mutable fields only; keep introduced_at_turn, seeded flag.
        if bio and bio != (existing.get("bio") or ""):
            existing["bio"] = bio
        if aliases:
            existing.setdefault("aliases", [])
            for a in aliases:
                if a and a not in existing["aliases"]:
                    existing["aliases"].append(a)
        if status and status != existing.get("status"):
            existing["status"] = status
        save_npc_card(char_key, npc_key, existing)
        return npc_key

    card = {
        "name": name,
        "npc_key": npc_key,
        "bio": bio or "",
        "introduced_at_turn": int(introduced_at_turn),
        "aliases": list(aliases) if aliases else [],
        "status": status,
        "seeded": False,
    }
    save_npc_card(char_key, npc_key, card)

    # Seed the NPC DB with what little we know. This is a blocking Sonnet
    # call (small, ~3-5s with a 45s timeout). It runs once per NPC ever, so
    # the cost amortizes immediately.
    if bio:
        seeded = _npc_seed_from_bio(char_key, npc_key, name, bio, current_turn=int(introduced_at_turn))
        card["seeded"] = True
        card["seed_row_count"] = seeded
        save_npc_card(char_key, npc_key, card)
        log(f"NPC registered: {name} ({npc_key}) — {seeded} seed rows", "SUCCESS")
    else:
        log(f"NPC registered: {name} ({npc_key}) — no bio, deferred seeding", "INFO")
    return npc_key


_NPC_INJECTION_PREFIXES = ("<turn>", "<latest_turn", "[ooc", "(ooc")
_NPC_INJECTION_SUBSTRINGS = (
    "vital that author",
    "past events:",
    "story summary",
    "=== story summary",
)


def _is_injection_message(role: str, content: str) -> bool:
    """True if this message looks like an ST tail-injection (persona, summary,
    preset narrator marker, lorebook block, OOC note) rather than substantive
    in-scene narrative. Used by find_npcs_in_scene to walk past the noise ST
    piles at the end of the messages list."""
    if role == "system":
        return True
    stripped = (content or "").strip()
    if len(stripped) < 30:
        return True
    low = stripped.lower()
    if any(low.startswith(p) for p in _NPC_INJECTION_PREFIXES):
        return True
    if any(s in low for s in _NPC_INJECTION_SUBSTRINGS):
        return True
    return False


def find_npcs_in_scene(char_key: str, messages: list[dict], lookback: int = 2) -> list[str]:
    """Return npc_keys whose name (or any alias) appears in the active scene.

    Walks backwards through `messages`, skipping system-role injections and
    known preset markers (Celia's `<latest_turn_end>` / `<turn>` / "Vital
    that author", auto-summary "Past events:" blocks, OOC notes), and
    collects the latest `lookback` substantive narrative messages. Then
    word-boundary scans those for registered NPC names.

    Why the filtering: ST piles tail injections (persona, lorebook entries,
    preset narrator instructions, "Past events" summaries) at the end of
    the messages list. A naive scan of the last N picks up Tuya from a
    "Past events" summary even when she's not in the current scene, or
    misses Khaemwaset because real narrative is two messages further back
    than the bridge's `messages[-1]`.
    """
    npcs = list_npcs(char_key)
    if not npcs:
        return []
    # Skip dismissed NPCs.
    candidates = [c for c in npcs if (c.get("status") or "active") != "dismissed"]
    if not candidates:
        return []

    # Walk backwards, skipping injection messages, collect lookback substantive ones.
    haystack_parts = []
    debug_lines = []
    for m in reversed(messages):
        if len(haystack_parts) >= lookback:
            break
        role = m.get("role", "?")
        content = m.get("content", "")
        if isinstance(content, list):
            content = "\n".join(
                p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
            )
        if not isinstance(content, str):
            continue
        if _is_injection_message(role, content):
            debug_lines.append(f"  SKIP [{role}] {content.replace(chr(10),' ')[:120]}")
            continue
        haystack_parts.insert(0, content)
        debug_lines.append(f"  KEEP [{role}] {content.replace(chr(10),' ')[:120]}")

    log(f"find_npcs_in_scene[{char_key}] walked backwards (lookback={lookback}, kept {len(haystack_parts)}):\n" + "\n".join(debug_lines), "INFO")

    haystack = " ".join(haystack_parts).lower()
    if not haystack.strip():
        return []

    found: list[str] = []
    for c in candidates:
        names_to_try = [c.get("name") or ""]
        names_to_try += list(c.get("aliases") or [])
        for nm in names_to_try:
            nm = (nm or "").strip()
            if not nm or len(nm) < 2:
                continue
            # Word-boundary match. \b doesn't always work for non-ASCII names;
            # use a simple boundary check via regex with re.IGNORECASE.
            pattern = r"\b" + re.escape(nm.lower()) + r"\b"
            if re.search(pattern, haystack):
                found.append(c["npc_key"])
                break  # one alias match is enough
    return found


def _query_npc_memories(char_key: str, npc_key: str, current_turn: int, cap: int = 15) -> list[dict]:
    """Pull a small high-importance slice of an NPC's DB for injection."""
    conn = get_connection(char_key, npc_key=npc_key)
    if conn is None:
        return []
    rows = query_memories(
        conn,
        statuses=("active",),
        limit=cap,
        order_by="default",
    )
    if rows:
        # Mark seen — keeps NPC entries from going dormant when they're being used.
        mark_seen(conn, [r["id"] for r in rows], current_turn)
    return rows


def _format_npc_section(npc_card: dict, rows: list[dict]) -> str:
    """Format a per-NPC injection block. Compact, types prefixed."""
    name = npc_card.get("name") or npc_card.get("npc_key") or "NPC"
    if not rows:
        return f"\n[NPC: {name.upper()}] (registered, no detailed memory yet)"
    lines = [f"\n[NPC: {name.upper()} — what {name} knows/feels]"]
    for r in rows:
        prefix = ""
        if r["type"] == "relationship" and r.get("subject"):
            prefix = f"toward {r['subject']}: "
        elif r["type"] == "trait":
            prefix = "trait: "
        elif r["type"] == "fact":
            prefix = ""
        else:
            prefix = f"{r['type']}: "
        lines.append(f"- {prefix}{r['content']}")
    return "\n".join(lines)


# =============================================================================
# Stage 4 — Pre-turn retrieval + injection
# =============================================================================
# The pre-turn flow:
#   1. tick_needs (mechanical decay)
#   2. auto_dormant (mechanical fade-out for stale entries)
#   3. cheap programmatic candidate pull
#   4. embedding semantic search to add candidates the keyword/recency pull missed
#   5. ALWAYS-on Sonnet relevance ranking (with timeout fallback)
#   6. format_injection -> string for the Opus prompt

DEFAULT_INJECT_ROW_BUDGET = 20
DEFAULT_INJECT_TOKEN_BUDGET = 2500  # rough chars/4 estimate; not strict
# Ranking is a simple "read list, return JSON" task — but Sonnet still does
# real thinking under default effort, which can take 20-30s on a busy
# machine. 30s gives reasonable headroom; we also pass --effort low to the
# rank call so the model spends less time deliberating before emitting.
SONNET_RANK_TIMEOUT_SECONDS = 30
SONNET_RANK_EFFORT = "low"
DEFAULT_DORMANCY_TURNS = 30


def _candidate_pull(
    conn: sqlite3.Connection,
    current_turn: int,
    active_subjects: list[str],
    cap: int = 30,
) -> list[dict]:
    """Cheap programmatic SQL pull of candidate memories for this turn.

    Composition (deduped by id):
      - All active desires with intensity >= 3
      - All active relationships for active subjects
      - All active traits
      - All active secrets (always-relevant)
      - Recent (last 10 turns seen)
      - High-importance (>= 4) regardless of subject
      - About active subjects regardless of recency
    """
    seen = {}  # id -> dict

    def take(rows: list[dict]):
        for r in rows:
            seen.setdefault(r["id"], r)

    # Intensity 6 = INEVITABLE. Always pull these first, regardless of subject
    # or recency. They override every other retrieval rule.
    take(query_memories(conn, types=["desire"], min_intensity=6, limit=cap))
    take(query_memories(conn, types=["desire"], min_intensity=3, limit=cap))
    if active_subjects:
        take(query_memories(
            conn,
            types=["relationship"],
            subjects=active_subjects,
            limit=cap,
        ))
    take(query_memories(conn, types=["trait"], limit=cap))
    take(query_memories(conn, types=["secret"], limit=cap))
    take(query_memories(
        conn,
        seen_within_turns=10,
        current_turn=current_turn,
        limit=cap,
    ))
    take(query_memories(conn, min_importance=4, limit=cap))
    if active_subjects:
        take(query_memories(conn, subjects=active_subjects, limit=cap))

    return list(seen.values())[:cap]


def _semantic_augment(
    conn: sqlite3.Connection,
    scene_query: str,
    existing_ids: set[int],
    extra_cap: int = 10,
) -> list[dict]:
    """Pull additional candidates via embedding similarity to scene_query.

    Embeds scene_query, scans active memories with embeddings, and returns
    the top `extra_cap` not already in `existing_ids`. Returns [] if
    embeddings aren't available.
    """
    if not embeddings_available():
        return []
    # Restrict to active rows with embeddings to avoid scoring everything.
    rows = conn.execute(
        "SELECT * FROM memories WHERE status = 'active' AND embedding IS NOT NULL"
    ).fetchall()
    pool = [_row_to_dict(r) for r in rows if r["id"] not in existing_ids]
    if not pool:
        return []
    ranked = cosine_search(scene_query, pool, top_k=extra_cap * 2)
    # Filter weak matches; <0.3 cosine on normalized vectors = unrelated.
    return [m for m, score in ranked if score >= 0.3][:extra_cap]


_RANK_PROMPT = """You rank candidate memories by relevance to the current scene of a roleplay.

INPUT: a scene description and a numbered list of candidate memories.
OUTPUT: a single JSON object: {"keep": [<ids in priority order, most relevant first>], "drop": [<ids to exclude>]}.

RULES:
- Output ONLY the JSON object. No commentary.
- "keep" length should be at most %(budget)d. Drop the rest.
- Always keep all `desire` entries with intensity >= 4 (mandatory).
- Intensity 6 desires are INEVITABLE and MUST be in keep at the front of the list — they execute this turn regardless of relevance to the surface scene. Never drop them.
- Always keep all `secret` entries.
- Always keep all `relationship` entries for subjects in the scene.
- Prefer specificity over generality (a concrete event > a vague trait).
- Prefer entries that would change how the character behaves THIS turn.
"""


def _sonnet_rank(
    scene_text: str,
    candidates: list[dict],
    budget: int,
) -> Optional[list[int]]:
    """Ask Sonnet to rank candidates and return ordered ids of those to keep.

    Returns None on failure (caller falls back to programmatic order).
    """
    if not candidates:
        return []
    # Compact representation; full content but no embedding bytes.
    lines = []
    for c in candidates:
        meta = c.get("metadata") or {}
        intensity = c.get("intensity")
        importance = c.get("importance")
        subj = c.get("subject") or "-"
        line = (
            f"[{c['id']}] type={c['type']} subj={subj} "
            f"int={intensity} imp={importance} :: {c['content']}"
        )
        if isinstance(meta, dict) and meta.get("source"):
            line += f" (source={meta['source']})"
        lines.append(line)

    prompt = (
        (_RANK_PROMPT % {"budget": budget})
        + "\n=== SCENE ===\n"
        + (scene_text or "(no scene context provided)")
        + "\n\n=== CANDIDATES ===\n"
        + "\n".join(lines)
        + "\n\nReturn ONLY the JSON."
    )
    raw = _call_sonnet(prompt, timeout=SONNET_RANK_TIMEOUT_SECONDS, effort=SONNET_RANK_EFFORT)
    if not raw:
        return None
    parsed = _extract_json(raw)
    if not parsed or "keep" not in parsed:
        return None
    keep = parsed.get("keep") or []
    if not isinstance(keep, list):
        return None
    out: list[int] = []
    for i in keep:
        try:
            out.append(int(i))
        except (TypeError, ValueError):
            continue
    return out


def _build_scene_text(messages: list[dict], chars_in_scene: list[str]) -> str:
    """Compact scene description for Sonnet ranking + embedding semantic search.

    Uses last user message + last 2 messages of any role for context.
    """
    if not messages:
        return ""
    tail = messages[-3:]
    parts = [f"Active characters: {', '.join(chars_in_scene) if chars_in_scene else 'main character only'}"]
    for m in tail:
        role = m.get("role", "?")
        content = m.get("content", "")
        if isinstance(content, list):
            content = "\n".join(
                p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
            )
        if not isinstance(content, str):
            content = str(content)
        if not content.strip():
            continue
        if len(content) > 1500:
            content = content[:1500] + "..."
        parts.append(f"[{role}] {content}")
    return "\n".join(parts)


def format_injection(memories: list[dict], needs: dict, char_name: str = "character") -> str:
    """Build the [CHARACTER MEMORY] block that gets appended to the Opus prompt.

    Groups by type, formats secrets in their own framed block (Q13), shows
    needs as a compact one-liner with arrows for below-threshold values.
    """
    if not memories and not needs:
        return ""

    by_type: dict[str, list[dict]] = {}
    secrets: list[dict] = []
    for m in memories:
        if m["type"] == "secret":
            secrets.append(m)
        else:
            by_type.setdefault(m["type"], []).append(m)

    def render_need_value(v) -> str:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return f"={v}"
        marker = ""
        if f < 0.2:
            marker = "↓↓"
        elif f < 0.4:
            marker = "↓"
        return f"={f:.2f}{marker}"

    needs_line = ""
    if isinstance(needs, dict):
        chunks = []
        for cat in ("physical", "social", "emotional", "custom"):
            slots = needs.get(cat) or {}
            if not isinstance(slots, dict) or not slots:
                continue
            inner = " ".join(f"{k}{render_need_value(v)}" for k, v in slots.items())
            chunks.append(inner)
        if chunks:
            needs_line = "[NEEDS] " + " | ".join(chunks)

    out_lines: list[str] = [f"=== CHARACTER MEMORY ({char_name.upper()}) ==="]
    if needs_line:
        out_lines.append(needs_line)

    # Surface intensity-6 desires in a dedicated front-of-block section so
    # Opus encounters them as a directive, not a list item. These are
    # narrative imperatives — vows, breaking points, things the character
    # has decided will happen this turn no matter what.
    desires = by_type.get("desire") or []
    inevitable = [d for d in desires if (d.get("intensity") or 0) >= 6]
    other_desires = [d for d in desires if (d.get("intensity") or 0) < 6]
    if inevitable:
        out_lines.append("\n[INEVITABLE — happens this turn regardless of cost]")
        for r in inevitable:
            subj = r.get("subject")
            subj_str = f"({subj}) " if subj else ""
            out_lines.append(f"- {subj_str}{r['content']}")

    # Pull out scene-active physical-state body rows (tagged 'scene' or
    # 'physical') and surface them at the front so Opus continues from a
    # concrete configuration instead of re-improvising indirection. Body
    # rows without those tags (general anatomy, scars, etc.) stay in the
    # default BODY / PHYSICAL block below.
    body_rows = by_type.get("body") or []
    def _is_scene_body(r):
        tags = (r.get("tags") or "").lower()
        return "scene" in tags or "physical" in tags
    scene_body = [r for r in body_rows if _is_scene_body(r)]
    other_body = [r for r in body_rows if not _is_scene_body(r)]
    if scene_body:
        out_lines.append("\n[CURRENT PHYSICAL STATE — continue from this configuration; do not re-improvise]")
        for r in scene_body:
            subj = r.get("subject")
            subj_str = f"({subj}) " if subj and subj != "self" else ""
            out_lines.append(f"- {subj_str}{r['content']}")
    # Stash so the per-type loop below uses the filtered list.
    if scene_body:
        by_type["body"] = other_body

    type_labels = (
        ("desire", "ACTIVE DESIRES"),
        ("relationship", "RELATIONSHIPS"),
        ("event", "RECENT EVENTS"),
        ("rule", "RULES"),
        ("trait", "ACTIVE TRAITS"),
        ("place", "PLACES"),
        ("possession", "POSSESSIONS"),
        ("body", "BODY / PHYSICAL"),
        ("fact", "FACTS"),
    )
    for tname, label in type_labels:
        rows = by_type.get(tname)
        if not rows:
            continue
        if tname == "desire":
            # Already handled inevitable; render only the < 6 here.
            rows = other_desires
            if not rows:
                continue
        out_lines.append(f"\n[{label}]")
        for r in rows:
            prefix = ""
            if tname == "desire" and r.get("intensity") is not None:
                tu = ""
                last_acted = r.get("last_acted_turn")
                if last_acted is not None:
                    tu = f" turns_unacted={(r.get('last_seen_turn') or 0) - last_acted}"
                prefix = f"intensity {r['intensity']}{tu} | "
            elif r.get("subject"):
                prefix = f"{r['subject']}: "
            out_lines.append(f"- {prefix}{r['content']}")

    if secrets:
        out_lines.append("\n[SECRETS — HIDDEN, do not reveal unless directly forced or contextually appropriate]")
        for s in secrets:
            subj = s.get("subject") or "-"
            out_lines.append(f"- (from {subj}) {s['content']}")

    out_lines.append("\n=== END CHARACTER MEMORY ===")
    return "\n".join(out_lines)


def prepare_turn(
    char_key: str,
    messages: list[dict],
    char_name: str = "character",
    active_subjects: Optional[list[str]] = None,
    inject_row_budget: int = DEFAULT_INJECT_ROW_BUDGET,
) -> tuple[str, list[int]]:
    """Top-level pre-turn entry point. Returns (injection_text, used_ids).

    Steps:
      1. Bootstrap if needed (blocks).
      2. Tick needs decay.
      3. Auto-dormant stale entries.
      4. Programmatic + semantic candidate pull.
      5. ALWAYS Sonnet ranking with timeout fallback.
      6. mark_seen on the kept ids.
      7. Format injection.

    `active_subjects` defaults to ['user'] — the most common case. NPCs in
    scene get added by the bridge before calling.
    """
    conn = get_connection(char_key)
    if conn is None:
        return "", []

    if active_subjects is None:
        active_subjects = ["user"]

    # First: decide what to do with the previous turn's staged buffer (if any).
    # Either commit (user accepted) or discard (swipe/regen detected).
    _flush_pending_if_accepted(char_key, messages)

    # If this character has v1 .md files lying around (state.md/diary.md/
    # rules.md from the original toggle), migrate them into the v2 DB once
    # before bootstrap. This converts them to typed rows and renames the
    # originals to .bak so they don't re-migrate. Idempotent on repeat calls.
    if has_v1_files(char_key):
        migrate_v1_to_v2(char_key)

    if is_bootstrap_needed(char_key):
        # Bootstrap is blocking on first turn. Subsequent turns skip this branch.
        run_bootstrap(char_key, messages, current_turn=0)

    current_turn = latest_turn(conn) + 1

    tick_needs(char_key, current_turn)
    dormant_count = auto_dormant(conn, current_turn, threshold_turns=DEFAULT_DORMANCY_TURNS)
    if dormant_count:
        log(f"prepare_turn[{char_key}]: marked {dormant_count} entries dormant", "INFO")
    pruned = prune_mutated(conn, current_turn, threshold_turns=20)
    if pruned:
        log(f"prepare_turn[{char_key}]: pruned {pruned} stale mutated rows", "INFO")

    # NPC scene detection: if any registered NPC's name/alias appears in
    # recent messages, treat them as an active subject too. Their DB will
    # be queried separately and merged into the injection below.
    npcs_in_scene = find_npcs_in_scene(char_key, messages)
    if npcs_in_scene:
        for nk in npcs_in_scene:
            if nk not in active_subjects:
                active_subjects.append(nk)
        log(f"prepare_turn[{char_key}]: NPCs in scene: {npcs_in_scene}", "INFO")

    # Expand active_subjects with each NPC's display name + aliases so legacy
    # rows (written before write-time subject normalization landed) still join
    # the candidate pull. New rows store the npc_key directly via
    # _normalize_subject, so this expansion is mostly defensive.
    pull_subjects = list(active_subjects)
    if npcs_in_scene:
        pull_subjects.extend(_subject_variants_for_npcs(char_key, npcs_in_scene))
        pull_subjects = list(dict.fromkeys(pull_subjects))  # dedupe, preserve order

    candidates = _candidate_pull(conn, current_turn, pull_subjects, cap=30)

    scene_text = _build_scene_text(messages, [char_name] + [s for s in active_subjects if s != "user"])
    if scene_text:
        existing_ids = {c["id"] for c in candidates}
        extras = _semantic_augment(conn, scene_text, existing_ids, extra_cap=10)
        if extras:
            log(f"prepare_turn[{char_key}]: +{len(extras)} via semantic search", "INFO")
        candidates.extend(extras)

    # ALWAYS run Sonnet ranking (Q2). Falls back to programmatic order on
    # failure or timeout.
    final_ids: Optional[list[int]] = None
    if candidates:
        ranked = _sonnet_rank(scene_text, candidates, inject_row_budget)
        if ranked is not None:
            final_ids = [i for i in ranked if any(c["id"] == i for c in candidates)][:inject_row_budget]
        else:
            log(f"prepare_turn[{char_key}]: Sonnet rank failed, using programmatic order", "WARN")
            final_ids = [c["id"] for c in candidates[:inject_row_budget]]
    else:
        final_ids = []

    # Re-fetch in ranked order. (We could re-use the candidates list, but
    # going through the DB ensures we get fresh row state in case maintenance
    # changed something concurrently.)
    final_rows: list[dict] = []
    for i in final_ids:
        r = get_memory(conn, i)
        if r:
            final_rows.append(r)

    if final_ids:
        mark_seen(conn, final_ids, current_turn)

    needs = load_needs(char_key)
    injection = format_injection(final_rows, needs, char_name=char_name)

    # Append per-NPC sections for any NPCs detected in scene. Each NPC gets
    # a small slice of their own DB (top ~10 entries by importance/recency)
    # appended to the main injection so Opus has each NPC's perspective.
    for nk in npcs_in_scene:
        card = load_npc_card(char_key, nk) or {}
        if (card.get("status") or "active") != "active":
            continue
        npc_rows = _query_npc_memories(char_key, nk, current_turn, cap=10)
        injection += _format_npc_section(card, npc_rows)

    return injection, final_ids


# =============================================================================
# Stage 5 — Post-turn Sonnet maintenance
# =============================================================================

_MAINT_PROMPT = """You maintain a character's persistent memory database based on what just happened in a roleplay turn.

INPUT: the current memory state, the just-completed turn, and the previous 2 turns for context.
OUTPUT: a single JSON object describing operations to apply.

Schema:
{
  "turn_summary": "<one sentence describing what happened this turn>",
  "operations": [
    {"op": "insert", "type": "<desire|event|fact|rule|relationship|trait|place|possession|body|secret>",
     "subject": "<user|self|other_subject|null>", "content": "<text>",
     "intensity": 1-6, "importance": 1-5, "tags": "comma,separated"},
    {"op": "update", "id": <int>, "intensity": 1-6, "importance": 1-5,
     "last_acted_turn": <int>, "tags": "..."},
    {"op": "resolve", "id": <int>, "reason": "<text>"},
    {"op": "mutate", "id": <int>, "new_content": "<text>", "reason": "<text>"},
    {"op": "register_npc", "name": "<display name>", "bio": "<one paragraph>", "introduced_at": <turn_num>},
    {"op": "needs_delta", "physical": {"hunger": -0.1, "fatigue": -0.05},
     "social": {"connection": +0.1}, "emotional": {"security": -0.05}, "custom": {}}
  ]
}

INTENSITY SCALE FOR DESIRES (1-6):
  1 — passing wish; the character notices it but rarely acts on it
  2 — recurring background pull; surfaces when context permits
  3 — present pressure; the character will act IF an opening appears
  4 — MUST act this turn; not a wish, a need
  5 — acts even at cost to scene comfort or relationship stability
  6 — INEVITABLE. The rarest tier. The character has crossed an internal
      threshold and the narrative MUST execute on this desire this turn,
      regardless of cost — relationship damage, user discomfort, scene
      rupture, story consequences. Do NOT soften, delay, or let
      circumstance prevent it. The character has decided. This is a vow
      level, not a wish level. Reserve for genuine breaking points: a
      grief that has finally found its expression; a rage that breaks
      restraint after months of swallowing; a confession that must
      happen now; a vow made internally that cannot be unmade. Use rarely.
      When you emit intensity 6, the next turn's narrative will execute
      it as established fact — so be deliberate.

RULES:
- Output ONLY the JSON object. No commentary.
- Be CONSERVATIVE. Most turns produce 0-3 ops. Don't fabricate to fill the list.
- Use `update` to bump intensities/turn-counts on existing entries before inserting near-duplicates.
- Use `resolve` when a desire was acted on / a question was answered / a tension was discharged.
- Use `mutate` when a desire transforms (flirtation -> frustration, longing -> withdrawal).
- Use `register_npc` ONLY for newly-named recurring characters. Skip one-offs.
- needs_delta: signed floats. Negative = need decreased (more pressure). Positive = need restored (less pressure). Bounded -1.0 to +1.0.
- Skip the operation entirely if you're not sure. Quality > quantity.

RELATIONSHIP UPDATES (do not skip these):
If THIS turn changed how the active character feels about someone — warmer, colder, owed something, owing something, more suspicious, more trusting, freshly grateful, freshly resentful — you MUST emit an `update` on that subject's `relationship` row (or `insert` a new one if none exists). Relationships that never update are a bug. Even small shifts ("noticed kindness, +1 closeness") should be recorded.

RELATIONSHIP OPS — REQUIRED FIELDS:
Every `insert` or `update` of a `relationship` row MUST include `closeness` and `trust` as top-level numeric fields (1-5), not just in the prose. The bridge merges them into metadata automatically. Example:
  {"op": "insert", "type": "relationship", "subject": "tom",
   "closeness": 2, "trust": 3, "importance": 4,
   "content": "First physical contact: Morgan put her hand on his shoulder; he covered it with his own."}
  {"op": "update", "id": 78, "closeness": 5, "trust": 5,
   "content": "...new prose summary..."}
Do NOT put closeness/trust inside a metadata dict — put them at the top level of the op. The bridge will move them into the row's metadata correctly.

NEEDS DELTAS (use them; the system depends on you):
The needs system is purely mechanical decay unless YOU restore values when narrative warrants. Anchor narrative events to need changes:
- Eating, drinking → physical.hunger up (+0.2 to +0.5 depending on amount)
- Rest, sleep → physical.fatigue up
- Comfort, warmth, safety → physical.comfort up
- Genuine connection, intimacy → social.connection up
- Praise, being seen, recognition → social.validation up
- Independence, agency reclaimed → social.autonomy up
- Threat, conflict, fear → emotional.security DOWN
- Discovery, surprise, new place → emotional.novelty up
- Meaningful action, decision honored → emotional.agency up
- Custom needs from card (vindication, faith, freedom, etc.) — adjust per relevant events
Emit a `needs_delta` op when narrative events ground a change. If the turn was emotionally/physically significant, emit one.

PHYSICAL STATE TRACKING (during intimate / physical scenes):
When the scene becomes physical — proximity, touching, undressing, sex — emit/update `body` rows that anchor the configuration concretely so the next turn's prose stays grounded. Without this, every turn re-improvises what's exposed and where contact is, producing vague indirection. With it, the next prepare_turn injects the current state and the writer continues from a known position.

Required `body` content when relevant (insert OR update existing rows):
- Clothing state: name what's on, what's open/undone, what's bunched, what's been removed. Be specific. "Nightgown bunched at her waist; bare from waist down; white cotton underwear still on; his shirt unbuttoned to mid-chest, still on his shoulders."
- Position/configuration: who is where, whose body parts are where, what's between them. "She's straddling his right thigh on the sofa; his back against the cushions; her left hand on his shoulder, his right hand on her bare hip."
- Active contact points: exactly where one body is touching another. "His mouth on her neck below the left ear; his right hand inside her underwear, palm cupping; her fingers gripping his collar."
- Arousal / response state: erections present, wetness, flushed skin, breath rate. Name it. Updates each turn as it escalates.

Use `mutate` (not insert) when configuration shifts substantially — clothes come off, position changes, penetration begins. Mutate the prior body row into the new one so history is preserved but only the current configuration is active.

These rows have `subject="self"` (the protagonist) or `subject="<other_char_key>"` for the other person. Importance 4-5 during scenes, 2-3 once the scene ends. Tag them `physical,scene` so retrieval can pull only intimate-scene context when needed.

NARRATOR-AS-CHARACTER CARDS (important):
Some cards establish the AI as a NARRATOR (Celia, Director, Storyteller, etc.) running an RP rather than as an in-fiction character themselves. In that case the active character whose memory you're maintaining is a meta-author with no in-story agency. When this is true:
- Track the PROTAGONIST'S internal state instead — the player character (e.g. "Morgan", "{{user}}'s persona") is the one whose desires, needs, and relationships matter.
- The protagonist's desires drive scenes (figure out the trenches, protect family, get home safely). Emit `desire` rows for THEM.
- The protagonist's needs decay and recover. Emit `needs_delta` based on what happened to THEM (they ate → hunger up, threatened → security down).
- Relationships are between the PROTAGONIST and the NPCs they meet, not between the narrator and anyone.
- Narrator traits/rules in the seed (about voice, formatting, in-role behavior) stay as-is — they ARE the narrator's stable rules. Don't update them per turn.
- You can tell it's a narrator card if seed entries say things like "must never insert herself", "co-author and narrator", "OOC mode", or similar meta-framing.

NPCs HAVE THEIR OWN MEMORY DBs (important — most maintenance passes miss this):
Every registered NPC has their own SQLite DB at character_memory/<char>/npcs/<npc_key>/memory.db. The main character's DB tracks what THIS character knows/feels/wants. The NPC's DB tracks what THE NPC knows/feels/wants. Without writes to the NPC DB, the NPC stays frozen at their initial seed forever and the system gradually loses fidelity to them as a separate entity.

Route an op to an NPC's DB by adding `"npc": "<name or alias>"` to the op:
  {"op": "insert", "type": "event", "subject": "user",
   "content": "Marcus saw Morgan flinch when she heard Tom's name",
   "importance": 4, "npc": "Marcus"}
  {"op": "update", "id": 12, "intensity": 5, "npc": "Marcus"}
  {"op": "resolve", "id": 7, "reason": "Marcus said it out loud", "npc": "Marcus"}

Use the NPC's display name (or any alias) — the bridge resolves it to the right DB. The `id` in update/resolve/mutate refers to a row in THAT NPC's DB, not main.

Routing rules:
- Write to MAIN when the experience/perception/desire/need belongs to the active character (the protagonist).
- Write to AN NPC's DB when the experience/perception/desire belongs to that NPC. "Marcus realized Morgan was lying" → event in Marcus's DB. "Marcus has decided he wants to take her in" → desire in Marcus's DB. "Marcus's relationship with Morgan deepened" → relationship row in Marcus's DB with subject="morgan" or subject="user".
- Cross-character relationships: each side gets their own row in their own DB. Morgan's view of Marcus → relationship in main with subject="marcus". Marcus's view of Morgan → relationship in Marcus's DB with subject="morgan" or subject="user". Both can exist; they're independent.
- `register_npc` always operates on main (it creates the NPC). `needs_delta` always operates on main (NPCs don't have needs files by design).
- Most turns where an NPC is on stage should produce AT LEAST ONE op for that NPC, in addition to the main-character ops. NPCs that "haven't been touched in many turns" are a sign you're forgetting to write their side.

DO NOT add `"npc"` for ops that are about the protagonist's view of the NPC. Those go to main with subject set to the NPC's name. The npc field is for ops that represent the NPC's OWN internal state.
"""


_OP_HANDLERS = {}


def _register_op(name):
    def deco(fn):
        _OP_HANDLERS[name] = fn
        return fn
    return deco


_RESERVED_SUBJECTS = {"user", "self", "narrator", "scene"}


def _normalize_subject(char_key: str, subject):
    """Map a free-form subject string to a registered NPC's npc_key when it
    refers to one. Returns the original value when there's no match.

    Sonnet emits ops with display-name subjects ("marcus", "Hélène") but
    `_candidate_pull` looks rows up by npc_key. Without this normalization,
    every relationship/event row Sonnet writes about an NPC fails to surface
    when that NPC is in scene. Reserved subjects ("user", "self", etc.) and
    None pass through unchanged.
    """
    if subject is None:
        return None
    s = str(subject).strip()
    if not s or s.lower() in _RESERVED_SUBJECTS:
        return subject
    matched = _find_matching_npc(char_key, s)
    if matched and matched.get("npc_key"):
        return matched["npc_key"]
    return subject


def _subject_variants_for_npcs(char_key: str, npc_keys: Iterable[str]) -> list[str]:
    """Return every subject string a row about these NPCs might use.

    Includes npc_key + canonical name + each alias, plus their lowercased
    forms. SQL `subject IN (...)` is exact-match and case-sensitive, so this
    expansion is what lets legacy rows (written before write-time
    normalization landed) still join the pre-turn pull.
    """
    out: list[str] = []
    seen: set[str] = set()
    npcs_by_key = {c.get("npc_key"): c for c in list_npcs(char_key)}
    for k in npc_keys:
        card = npcs_by_key.get(k)
        if not card:
            continue
        forms = [k, card.get("name") or ""]
        forms.extend(card.get("aliases") or [])
        for f in forms:
            f = (f or "").strip()
            if not f:
                continue
            for v in (f, f.lower()):
                if v and v not in seen:
                    seen.add(v)
                    out.append(v)
    return out


def _extract_relationship_meta(op: dict) -> dict:
    """Pull `closeness`/`trust` out of a top-level op dict and clamp them.

    Returns a dict suitable for merging into metadata. Empty if neither is
    present. Mutates `op` in place to remove these keys so the rest of the
    op handler doesn't mistake them for column updates.
    """
    out = {}
    if "closeness" in op:
        c = _clamp(op.pop("closeness"), 1, 5)
        if c is not None:
            out["closeness"] = c
    if "trust" in op:
        t = _clamp(op.pop("trust"), 1, 5)
        if t is not None:
            out["trust"] = t
    return out


@_register_op("insert")
def _op_insert(conn, op, current_turn, char_key):
    op = dict(op)  # don't mutate caller's dict
    rel_meta = _extract_relationship_meta(op)
    base_meta = {"source": "post_turn", "turn": current_turn}
    base_meta.update(rel_meta)
    insert_memory(
        conn,
        type=op["type"],
        content=op["content"],
        subject=_normalize_subject(char_key, op.get("subject")),
        intensity=op.get("intensity"),
        importance=op.get("importance", 3),
        created_turn=current_turn,
        last_seen_turn=current_turn,
        tags=op.get("tags"),
        metadata=base_meta,
        embedding=embed(op["content"]),
    )


@_register_op("update")
def _op_update(conn, op, current_turn, char_key):
    op = dict(op)
    mid = int(op["id"])
    rel_meta = _extract_relationship_meta(op)
    fields = {k: v for k, v in op.items() if k not in ("op", "id")}
    if "subject" in fields:
        fields["subject"] = _normalize_subject(char_key, fields["subject"])
    if "content" in fields:
        # If content changed, refresh the embedding too.
        fields["embedding"] = embed(fields["content"])
    if rel_meta:
        # Merge into existing metadata rather than replacing it. Preserves
        # source / origin info while letting closeness/trust be patched.
        existing = get_memory(conn, mid)
        existing_meta = (existing or {}).get("metadata") or {}
        if not isinstance(existing_meta, dict):
            existing_meta = {}
        merged = {**existing_meta, **rel_meta, "last_updated_turn": current_turn}
        # If caller also passed an explicit metadata dict, merge it on top.
        if isinstance(fields.get("metadata"), dict):
            merged.update(fields["metadata"])
        fields["metadata"] = merged
    update_memory(conn, mid, **fields)


@_register_op("resolve")
def _op_resolve(conn, op, current_turn, char_key):
    update_memory(
        conn,
        int(op["id"]),
        status="resolved",
        last_acted_turn=current_turn,
        metadata={"source": "post_turn", "resolved_turn": current_turn,
                  "reason": op.get("reason", "")},
    )


@_register_op("mutate")
def _op_mutate(conn, op, current_turn, char_key):
    mid = int(op["id"])
    new_content = op.get("new_content")
    if not new_content:
        return
    # Mark the old one mutated, insert a fresh one carrying forward.
    old = get_memory(conn, mid)
    update_memory(conn, mid, status="mutated",
                  metadata={"source": "post_turn", "mutated_turn": current_turn,
                            "reason": op.get("reason", ""), "into_content": new_content})
    if old:
        insert_memory(
            conn,
            type=old["type"],
            content=new_content,
            subject=old.get("subject"),
            intensity=old.get("intensity"),
            importance=old.get("importance", 3),
            created_turn=current_turn,
            last_seen_turn=current_turn,
            tags=old.get("tags"),
            metadata={"source": "post_turn", "mutated_from": mid, "turn": current_turn},
            embedding=embed(new_content),
        )


@_register_op("register_npc")
def _op_register_npc(conn, op, current_turn, char_key):
    """Create an NPC sub-folder + DB and run a quick Sonnet seed pass.

    Also drops a pointer `fact` row into the main char's DB so retrieval
    and the GUI can find newly-introduced NPCs without scanning the
    filesystem on every query.
    """
    name = op.get("name")
    bio = op.get("bio") or ""
    if not name:
        return
    npc_key = register_npc(
        char_key=char_key,
        name=name,
        bio=bio,
        introduced_at_turn=current_turn,
        aliases=op.get("aliases") or None,
        status="active",
    )
    if not npc_key:
        return
    # Slim pointer row in the main char's DB. The NPC's own DB carries the
    # full bio + traits/facts; we only need a one-line marker here so
    # `_candidate_pull` can find this NPC by subject and the GUI can list
    # them. Bio paragraphs were duplicating ~150 tokens per NPC injection
    # without adding signal, since the NPC DB is queried separately when
    # the NPC is in scene.
    insert_memory(
        conn,
        type="fact",
        content=f"{name} [{npc_key}] — registered NPC; full memory in NPC DB",
        subject=npc_key,
        importance=4,
        created_turn=current_turn,
        last_seen_turn=current_turn,
        tags="npc",
        metadata={"source": "post_turn", "kind": "npc_introduced",
                  "name": name, "introduced_at": current_turn,
                  "npc_key": npc_key},
        embedding=embed(f"{name}: {bio[:200]}") if bio else embed(name),
    )


@_register_op("needs_delta")
def _op_needs_delta(conn, op, current_turn, char_key):
    delta = {k: v for k, v in op.items() if k in ("physical", "social", "emotional", "custom")}
    if delta:
        apply_needs_delta(char_key, delta)


# Ops that always run against the main character's DB regardless of any
# `npc` field on them. register_npc creates the NPC (must run on main),
# and needs_delta updates needs.json which only exists on main.
_MAIN_ONLY_OPS = {"register_npc", "needs_delta"}


def _resolve_npc_target(char_key: str, op_npc) -> tuple[Optional[str], Optional[str]]:
    """Resolve an op's `npc` field to an existing npc_key. Sonnet usually
    emits the display name ("Marcus") but can sometimes emit the slug
    ("marcus_a3f9") — _find_matching_npc handles both via token overlap.

    Returns (npc_key, error). npc_key is None when no NPC matches; error
    is a human-readable string explaining what went wrong, or None on
    a clean None resolution.
    """
    if not op_npc:
        return None, None
    matched = _find_matching_npc(char_key, str(op_npc))
    if matched and matched.get("npc_key"):
        return matched["npc_key"], None
    return None, f"unknown npc {op_npc!r} — register_npc first or check the name"


def _apply_ops(conn, ops: list[dict], current_turn: int, char_key: str) -> tuple[int, list[str]]:
    """Apply a batch of ops, routing each to the appropriate DB.

    Each op may carry an `npc` field naming an NPC by display name, alias,
    or slug. When present, the op runs against that NPC's DB instead of
    the main character's. Without it, the op runs against main.

    Per-op transaction (vs. one batch transaction): NPC ops live in
    different SQLite connections than main, so wrapping the whole batch
    in a single transaction wouldn't actually be atomic across DBs. Each
    op is small and self-contained; per-op tx still gives us safety
    against partial state inside any single op while letting us route
    different ops to different connections.
    """
    applied = 0
    errors: list[str] = []
    for op in ops:
        if not isinstance(op, dict):
            errors.append(f"non-dict op: {op!r}")
            continue
        kind = op.get("op")
        handler = _OP_HANDLERS.get(kind)
        if not handler:
            errors.append(f"unknown op: {kind!r}")
            continue

        # Resolve the target connection: main, or a specific NPC.
        target_conn = conn
        op_npc = op.get("npc") if kind not in _MAIN_ONLY_OPS else None
        if op_npc:
            npc_key, npc_err = _resolve_npc_target(char_key, op_npc)
            if npc_err:
                errors.append(f"{kind}: {npc_err}")
                continue
            target_conn = get_connection(char_key, npc_key=npc_key)
            if target_conn is None:
                errors.append(f"{kind}: failed to open NPC DB for {npc_key!r}")
                continue

        try:
            with transaction(target_conn):
                handler(target_conn, op, current_turn, char_key)
            applied += 1
        except Exception as e:
            errors.append(f"{kind} failed: {e} — op={op!r}")
    return applied, errors


def record_turn(
    char_key: str,
    messages: list[dict],
    assistant_response: str,
    char_name: str = "character",
    current_turn: Optional[int] = None,
):
    """Post-turn maintenance entry point. Designed to be called from a background
    thread so the user's response time isn't gated on Sonnet.

    `current_turn`: when supplied, the caller has already stamped a turn_log
    row for this turn (typically `_flush_pending_if_accepted` does this so
    `prepare_turn` reads the new max immediately, even if Sonnet maintenance
    is still running or fails). Skips the swipe check in that case — the
    accept-vs-swipe staging buffer already verified this turn was accepted.
    When None, computes the next turn number and runs the legacy swipe check.

    Steps:
      1. (Legacy path only) Compute a swipe hash and skip if duplicate.
      2. Build the maintenance prompt.
      3. Call Sonnet (longer timeout — this is background).
      4. Parse JSON, apply ops atomically.
      5. Log turn summary + hash (UPDATE if pre-stamped, INSERT otherwise).
    """
    if not assistant_response or not assistant_response.strip():
        return

    conn = get_connection(char_key)
    if conn is None:
        return

    msg_hash = _hash_text(assistant_response)
    if current_turn is None:
        if is_swipe(conn, msg_hash):
            log(f"record_turn[{char_key}]: swipe detected (hash={msg_hash}), skipping", "INFO")
            return
        current_turn = latest_turn(conn) + 1

    needs = load_needs(char_key)
    recent_memories = query_memories(conn, statuses=("active",), limit=40)

    # Build context from the last 2 user/assistant exchanges.
    convo_tail = []
    for m in messages[-6:]:
        role = m.get("role", "?")
        content = m.get("content", "")
        if isinstance(content, list):
            content = "\n".join(
                p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
            )
        if isinstance(content, str) and content.strip():
            if len(content) > 1500:
                content = content[:1500] + "..."
            convo_tail.append(f"[{role}] {content}")

    memory_lines = []
    for m in recent_memories:
        memory_lines.append(
            f"[{m['id']}] type={m['type']} subj={m.get('subject') or '-'} "
            f"int={m.get('intensity')} imp={m['importance']} :: {m['content']}"
        )

    needs_compact = json.dumps(
        {k: v for k, v in needs.items() if k != "last_tick_turn"},
        ensure_ascii=False,
    )

    prompt = (
        _MAINT_PROMPT
        + f"\n=== CURRENT NEEDS ===\n{needs_compact}"
        + f"\n=== ACTIVE MEMORIES (id-prefixed) ===\n"
        + "\n".join(memory_lines)
        + f"\n=== RECENT TURNS ===\n"
        + "\n\n".join(convo_tail)
        + f"\n=== JUST-COMPLETED TURN (assistant) ===\n{assistant_response[:6000]}"
        + "\n\nReturn ONLY the JSON object."
    )

    log(f"record_turn[{char_key}]: calling Sonnet for maintenance...", "INFO")
    t0 = time.time()
    raw = _call_sonnet(prompt, timeout=90)
    elapsed = time.time() - t0
    if not raw:
        log(f"record_turn[{char_key}]: empty Sonnet response after {elapsed:.1f}s", "WARN")
        _write_error_log(char_key, "maintenance_empty", prompt[:2000] + "\n---\n(no response)")
        return

    parsed = _extract_json(raw)
    if not parsed:
        log(f"record_turn[{char_key}]: failed to parse Sonnet JSON", "ERROR")
        _write_error_log(char_key, "maintenance_parse", raw)
        return

    summary = parsed.get("turn_summary") or ""
    ops = parsed.get("operations") or []
    if not isinstance(ops, list):
        ops = []

    applied, errors = _apply_ops(conn, ops, current_turn, char_key)
    log_turn(conn, current_turn, summary=summary, message_hash=msg_hash)

    if errors:
        log(
            f"record_turn[{char_key}]: applied {applied}/{len(ops)} ops "
            f"({len(errors)} errors) in {elapsed:.1f}s",
            "WARN",
        )
        _write_error_log(char_key, "maintenance_op_errors",
                         "\n".join(errors) + "\n---\nraw:\n" + raw)
    else:
        log(
            f"record_turn[{char_key}]: applied {applied} ops in {elapsed:.1f}s — {summary[:80]}",
            "SUCCESS",
        )


def record_turn_async(
    char_key: str,
    messages: list[dict],
    assistant_response: str,
    char_name: str = "character",
):
    """Fire record_turn on a daemon background thread so the caller is not blocked."""
    t = threading.Thread(
        target=record_turn,
        args=(char_key, messages, assistant_response, char_name),
        daemon=True,
        name=f"mem-record-{safe_dirname(char_key) or 'unknown'}",
    )
    t.start()


# ---------------------------------------------------------------------------
# One-post-delay commit (swipe-safe staging)
# ---------------------------------------------------------------------------
# After each Opus turn we *stage* the response in a per-character buffer
# instead of firing maintenance immediately. On the NEXT prepare_turn we
# look at the new request's messages: if the staged response is present in
# the assistant history, the user accepted it → commit. If it's not (user
# swiped, regenerated, or edited), we discard the stale buffer. The newly-
# generated response will re-stage and the cycle continues.
#
# This avoids polluting the DB with rolled-over swipes — the v1 system had
# the same fundamental issue (different hash means swipe undetected) and
# it caused noticeable drift on long sessions.
#
# Failure modes:
#   - User closes the chat forever: buffer sits unflushed. No harm; the
#     next session either accepts (commits) or rejects (discards) it.
#   - Group chat with many cards: buffer is per-char_key, so cards don't
#     interfere with each other.
#   - First turn ever: buffer is empty, nothing to flush. Correct.

_PENDING_TURNS_LOCK = threading.Lock()
_PENDING_TURNS: dict[str, dict] = {}


def stage_turn(
    char_key: str,
    messages: list[dict],
    assistant_response: str,
    char_name: str = "character",
):
    """Buffer this turn for delayed maintenance commit.

    Replaces any prior pending turn for this character — that's what makes
    swipes safe: each new response just overwrites the buffer, and only
    the last (accepted) one ever gets committed.
    """
    if not assistant_response or not assistant_response.strip():
        return
    user_count = _user_msg_count(messages)
    with _PENDING_TURNS_LOCK:
        _PENDING_TURNS[char_key] = {
            "response": assistant_response,
            "messages": list(messages),  # snapshot, not a live ref
            "char_name": char_name,
            "staged_at": time.time(),
            "user_count_at_stage": user_count,
        }
    log(
        f"[memv2] staged turn for {char_key} "
        f"(user_count={user_count}); commits when next request has more user msgs",
        "INFO",
    )


def _user_msg_count(messages: list[dict]) -> int:
    """Count substantive user messages in a request.

    Used as the accept-vs-swipe signal for the staging buffer: if the new
    request has MORE user messages than the snapshot we staged, the user
    sent a follow-up (= accepted the previous response). If the count is
    the SAME, the user is regenerating / swiping the previous response.
    Less = conversation was rewound or messages were dropped.

    "Substantive" filters out empty/marker-only messages (ST sometimes
    sends short instruction tokens like "<turn>" or single dots that
    aren't real user input). Keeps any message with > 5 chars after strip.
    """
    n = 0
    for m in messages:
        if m.get("role") != "user":
            continue
        content = m.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        if not isinstance(content, str):
            content = str(content)
        stripped = content.strip()
        # Filter ST-injected instruction markers that aren't real user input.
        # Mirrors the filtering trigger_lorebook_analysis already does.
        if not stripped or len(stripped) < 10:
            continue
        if stripped.startswith("<turn>") or stripped.startswith("<latest_turn"):
            continue
        n += 1
    return n


# Used only by record_turn / is_swipe to dedupe identical-content turns
# in the turn_log. Not used for accept/discard decisions anymore.
_THINK_RE = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")


def _hash_text(text: str) -> str:
    """Lightweight hash of an assistant response for swipe-dedup in turn_log."""
    if not text:
        return ""
    cleaned = _THINK_RE.sub("", str(text))
    cleaned = _WS_RE.sub(" ", cleaned).strip().lower()
    if not cleaned:
        return ""
    return hashlib.md5(cleaned[:1500].encode("utf-8", "ignore")).hexdigest()[:16]


def _flush_pending_if_accepted(char_key: str, current_messages: list[dict]):
    """Decide what to do with the staged turn using user-message count.

    Semantic: "user accepted the previous response" === "user sent another
    user message after it". So we just compare user-message counts:
      - current > staged → user moved forward → COMMIT
      - current == staged → swipe / regenerate → DISCARD (new gen will re-stage)
      - current  < staged → rewind / edit / weirdness → DISCARD

    This sidesteps the brittle text-hash matching, which couldn't survive
    SillyTavern's history mangling (HTML, font tags, name prefixes,
    truncation, group-chat routing). The user message count is a much
    harder signal to mangle.
    """
    with _PENDING_TURNS_LOCK:
        pending = _PENDING_TURNS.get(char_key)
    if not pending:
        return

    staged_count = int(pending.get("user_count_at_stage", 0))
    current_count = _user_msg_count(current_messages)

    if current_count > staged_count:
        # User sent a new message → they accepted the previous response.
        with _PENDING_TURNS_LOCK:
            popped = _PENDING_TURNS.pop(char_key, None)
        if popped:
            log(
                f"[memv2] flushing accepted turn for {char_key} "
                f"(user_count {staged_count} → {current_count})",
                "SUCCESS",
            )
            # Pre-stamp the turn_log row synchronously so the caller's
            # subsequent `latest_turn()` read reflects this accepted turn
            # immediately. Without this, prepare_turn could compute the same
            # current_turn twice (race), and a chronically failing Sonnet
            # maintenance pass would leave latest_turn stuck forever. The
            # background record_turn updates the same row in place with the
            # summary + message hash once Sonnet returns.
            stamped_turn: Optional[int] = None
            try:
                conn = get_connection(char_key)
                if conn is not None:
                    stamped_turn = latest_turn(conn) + 1
                    log_turn(conn, stamped_turn, summary=None, message_hash=None)
            except Exception as e:
                log(f"[memv2] pre-stamp log_turn failed: {e}", "WARN")
                stamped_turn = None
            threading.Thread(
                target=record_turn,
                kwargs={
                    "char_key": char_key,
                    "messages": popped["messages"],
                    "assistant_response": popped["response"],
                    "char_name": popped["char_name"],
                    "current_turn": stamped_turn,
                },
                daemon=True,
                name=f"mem-record-{safe_dirname(char_key) or 'unknown'}",
            ).start()
        return

    # Same or fewer user messages: swipe, regen, or rewind. Discard.
    with _PENDING_TURNS_LOCK:
        popped = _PENDING_TURNS.pop(char_key, None)
    if popped:
        kind = "swipe/regen" if current_count == staged_count else "rewind/edit"
        log(
            f"[memv2] discarding staged turn for {char_key} "
            f"({kind}; user_count staged={staged_count} now={current_count})",
            "INFO",
        )


# =============================================================================
# Stage 9 — Reset + .md migration
# =============================================================================


def _close_pool_for_char(char_key: str):
    """Close every pooled connection that belongs to this char (incl. NPCs).

    Called before any destructive disk op (reset, delete) so SQLite releases
    its file handles and Windows lets us delete the .db files.
    """
    safe_c = safe_dirname(char_key) or ""
    if not safe_c:
        return
    prefix = f"{safe_c}::"
    with _pool_lock:
        for key in list(_pool.keys()):
            if key.startswith(prefix):
                try:
                    _pool[key].close()
                except sqlite3.Error:
                    pass
                _pool.pop(key, None)


def reset_character(char_key: str) -> bool:
    """Wipe a character's entire memory directory and clear its pool entries.

    Bridge can call this without restarting. Returns True on success.
    """
    safe_c = safe_dirname(char_key)
    if not safe_c:
        return False
    cdir = char_dir(char_key)
    if not cdir:
        return False
    _close_pool_for_char(char_key)
    if os.path.isdir(cdir):
        try:
            shutil.rmtree(cdir)
        except OSError as e:
            log(f"reset_character[{char_key}] rmtree failed: {e}", "ERROR")
            return False
    log(f"reset_character[{char_key}]: wiped {cdir}", "SUCCESS")
    return True


def _close_pool_for_npc(char_key: str, npc_key: str):
    """Close the pooled connection for one specific NPC under a character.

    Mirrors _close_pool_for_char but scoped to a single NPC — needed before
    rmtree of the NPC's folder on Windows, which won't release a file handle
    until the SQLite connection is closed.
    """
    safe_c = safe_dirname(char_key) or ""
    safe_n = safe_dirname(npc_key) or ""
    if not safe_c or not safe_n:
        return
    key = f"{safe_c}::{safe_n}"
    with _pool_lock:
        conn = _pool.pop(key, None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def delete_npc(char_key: str, npc_key: str) -> tuple[bool, Optional[str]]:
    """Delete an NPC: close pool entry, rmtree their sub-folder, prune the
    pointer fact rows from the main DB so the GUI / retrieval don't see
    ghost references.

    Returns (ok, error_or_None). Idempotent — deleting a non-existent NPC
    returns (True, None) so the GUI can call it without checking first.
    """
    safe_n = safe_dirname(npc_key)
    if not safe_n:
        return False, "invalid npc_key"
    ndir = npc_dir(char_key, npc_key)
    if not ndir:
        return False, "no NPC directory path"

    # Close the NPC's connection first so Windows lets us rmtree the .db.
    _close_pool_for_npc(char_key, npc_key)

    if os.path.isdir(ndir):
        try:
            shutil.rmtree(ndir)
        except OSError as e:
            log(f"delete_npc[{char_key}/{npc_key}] rmtree failed: {e}", "ERROR")
            return False, f"rmtree failed: {e}"

    # Prune the pointer fact row(s) the main DB carries for this NPC. Without
    # this, list_npcs() (which scans the npcs/ subfolder) won't show them but
    # _candidate_pull might still surface stale subject=<npc_key> rows.
    main_conn = get_connection(char_key)
    if main_conn is not None:
        try:
            with transaction(main_conn):
                main_conn.execute(
                    "DELETE FROM memories WHERE type='fact' AND tags LIKE '%npc%' AND subject = ?",
                    (safe_n,),
                )
        except sqlite3.Error as e:
            log(f"delete_npc[{char_key}/{npc_key}] prune pointer rows failed: {e}", "WARN")

    log(f"delete_npc[{char_key}/{npc_key}]: removed", "SUCCESS")
    return True, None


def _label_path(char_key: str) -> Optional[str]:
    cdir = char_dir(char_key)
    return os.path.join(cdir, "label.json") if cdir else None


def load_label(char_key: str) -> str:
    """Read the user-set display label for a character. Returns '' when
    no label has been set yet."""
    p = _label_path(char_key)
    if not p or not os.path.exists(p):
        return ""
    try:
        with open(p, "r", encoding="utf-8") as f:
            return (json.load(f).get("label") or "").strip()
    except (OSError, json.JSONDecodeError):
        return ""


def save_label(char_key: str, label: str) -> bool:
    """Write the user-set display label for a character. Empty string clears
    the label (file is removed)."""
    p = _label_path(char_key)
    if not p:
        return False
    label = (label or "").strip()
    try:
        if not label:
            if os.path.exists(p):
                os.remove(p)
            return True
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"label": label}, f, ensure_ascii=False, indent=2)
        return True
    except OSError as e:
        log(f"save_label[{char_key}] failed: {e}", "WARN")
        return False


def list_characters() -> list[dict]:
    """Return summary info for every character with a memory dir."""
    if not os.path.isdir(MEMORY_ROOT):
        return []
    out = []
    for entry in sorted(os.listdir(MEMORY_ROOT)):
        cdir = os.path.join(MEMORY_ROOT, entry)
        if not os.path.isdir(cdir):
            continue
        db_file = os.path.join(cdir, "memory.db")
        info = {
            "char_key": entry,
            "label": load_label(entry),
            "has_db": os.path.exists(db_file),
            "has_needs": os.path.exists(os.path.join(cdir, "needs.json")),
            "has_card_seed": os.path.exists(os.path.join(cdir, "card_seed.json")),
            "npc_count": 0,
            "memory_count": 0,
            "latest_turn": 0,
        }
        npcs_dir = os.path.join(cdir, "npcs")
        if os.path.isdir(npcs_dir):
            info["npc_count"] = sum(
                1 for n in os.listdir(npcs_dir)
                if os.path.isdir(os.path.join(npcs_dir, n))
            )
        if info["has_db"]:
            try:
                conn = get_connection(entry)
                if conn is not None:
                    row = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
                    info["memory_count"] = row[0] if row else 0
                    info["latest_turn"] = latest_turn(conn)
            except sqlite3.Error:
                pass
        out.append(info)
    return out


# v1 → v2 migration. v1 stored three markdown files (state.md, diary.md,
# rules.md). We parse them line by line and import as appropriate types,
# then archive the originals as .bak (safer than deleting in case the
# user wants to roll back).

_MD_BULLET = re.compile(r"^\s*[-*]\s+(.*)$")


def _read_md_lines(path: str) -> list[str]:
    """Read a markdown file, return non-empty bullet lines."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return []
    out = []
    for raw in text.splitlines():
        m = _MD_BULLET.match(raw)
        if m:
            line = m.group(1).strip()
            if line:
                out.append(line)
    return out


def has_v1_files(char_key: str) -> bool:
    cdir = char_dir(char_key)
    if not cdir:
        return False
    return any(
        os.path.exists(os.path.join(cdir, name))
        for name in ("state.md", "diary.md", "rules.md")
    )


def migrate_v1_to_v2(char_key: str) -> dict:
    """Read v1 .md files for `char_key`, import their contents into the v2 DB,
    then archive the originals.

    Returns a dict with counts: {"state": N, "diary": N, "rules": N, "errors": [...]}.
    Safe to call when no v1 files exist (returns zeros). Idempotent — running
    it twice won't double-import because the .md files get renamed to .bak.
    """
    cdir = char_dir(char_key, create=True)
    if not cdir:
        return {"state": 0, "diary": 0, "rules": 0, "errors": ["bad char_key"]}
    conn = get_connection(char_key)
    if conn is None:
        return {"state": 0, "diary": 0, "rules": 0, "errors": ["DB unavailable"]}

    counts = {"state": 0, "diary": 0, "rules": 0, "errors": []}
    current = latest_turn(conn) or 0

    pairs = (
        ("state.md", "desire", "state"),
        ("diary.md", "event",  "diary"),
        ("rules.md", "rule",   "rules"),
    )

    try:
        with transaction(conn):
            for filename, type_name, key in pairs:
                src = os.path.join(cdir, filename)
                if not os.path.exists(src):
                    continue
                lines = _read_md_lines(src)
                for line in lines:
                    try:
                        insert_memory(
                            conn,
                            type=type_name,
                            content=line,
                            importance=3,
                            created_turn=current,
                            tags=f"v1_migration,{key}",
                            metadata={"source": "v1_migration", "from_file": filename},
                            embedding=embed(line),
                        )
                        counts[key] += 1
                    except Exception as e:
                        counts["errors"].append(f"{filename}: {e}")
    except Exception as e:
        counts["errors"].append(f"transaction failed: {e}")
        return counts

    # Archive originals so we don't re-migrate next call.
    for filename, _t, _k in pairs:
        src = os.path.join(cdir, filename)
        if os.path.exists(src):
            try:
                os.rename(src, src + ".bak")
            except OSError as e:
                counts["errors"].append(f"archive {filename}: {e}")

    log(
        f"migrate_v1_to_v2[{char_key}]: imported "
        f"state={counts['state']} diary={counts['diary']} rules={counts['rules']} "
        f"({len(counts['errors'])} errors)",
        "SUCCESS",
    )
    return counts


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__ = [
    # constants
    "MEMORY_TYPES",
    "MEMORY_STATUSES",
    "SCHEMA_VERSION",
    "MEMORY_ROOT",
    # logger / exe injection
    "set_logger",
    "set_claude_exe",
    # paths
    "safe_dirname",
    "char_dir",
    "npc_dir",
    "db_path",
    # connections
    "get_connection",
    "close_all_connections",
    "transaction",
    "ensure_schema",
    # CRUD
    "insert_memory",
    "update_memory",
    "get_memory",
    "query_memories",
    "mark_seen",
    "auto_dormant",
    # turn log
    "log_turn",
    "latest_turn",
    "is_swipe",
    # embeddings
    "embeddings_available",
    "embed",
    "embed_to_array",
    "cosine_search",
    # bootstrap
    "is_bootstrap_needed",
    "run_bootstrap",
    # needs
    "DEFAULT_NEEDS",
    "DECAY_RATES",
    "load_needs",
    "save_needs",
    "tick_needs",
    "apply_needs_delta",
    # turn lifecycle (the main public surface for the bridge)
    "prepare_turn",
    "record_turn",
    "record_turn_async",
    "stage_turn",
    "format_injection",
    # NPCs
    "register_npc",
    "list_npcs",
    "load_npc_card",
    "save_npc_card",
    "find_npcs_in_scene",
    # admin / migration (Stage 9)
    "reset_character",
    "list_characters",
    "has_v1_files",
    "migrate_v1_to_v2",
    "prune_mutated",
]
