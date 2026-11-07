"""Durable, provenance-aware memory for AI-AM.

The v2 tables coexist with the original Chitta tables. Migration copies legacy
text as explicitly unverified history; it never deletes or rewrites old rows.
All methods own and explicitly close their SQLite connections.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import unicodedata
import uuid


MEMORY_KINDS = frozenset({
    "user_statement", "observation", "inference", "reflection",
    "commitment", "legacy_unverified",
})
EVENT_STATUSES = frozenset({"pending", "processing", "complete", "failed"})
STOP_WORDS = frozenset(
    "a an and are as at be been but by can could did do does for from had has "
    "have how i if in is it its me my of on or our please that the their them "
    "there these they this to was we were what when where which who why will "
    "with would you your".split()
)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _text(value, name, maximum=100_000):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    if len(value) > maximum:
        raise ValueError(f"{name} exceeds {maximum} characters")
    return value.strip()


def _normalized(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _tokens(value):
    words = re.findall(r"[^\W_]+(?:['-][^\W_]+)*", _normalized(value), re.UNICODE)
    return [word for word in words if word not in STOP_WORDS]


def _limit(value, maximum=1000):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("limit must be a nonnegative integer")
    return min(value, maximum)


class MindStore:
    """SQLite event journal, state snapshots, and immutable memory history."""

    def __init__(self, db_path):
        self.db_path = str(db_path)
        self._anchor = None
        self._closed = False
        self._uri = self.db_path == ":memory:"
        if self._uri:
            # The anchor keeps the explicitly requested in-memory DB alive.
            self._target = f"file:aiam-{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._anchor = sqlite3.connect(
                self._target, uri=True, check_same_thread=False
            )
        else:
            path = Path(self.db_path).expanduser().resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            self.db_path = str(path)
            self._target = str(path)
        self._initialize()

    @contextmanager
    def _connection(self, write=False):
        if self._closed:
            raise RuntimeError("MindStore is closed")
        conn = sqlite3.connect(self._target, timeout=15, uri=self._uri)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 15000")
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self):
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
        with self._connection(write=True) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS mind_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            version = conn.execute(
                "SELECT value FROM mind_meta WHERE key = 'schema_version'"
            ).fetchone()
            if version and int(version["value"]) > 2:
                raise RuntimeError("This database was created by a newer AI-AM version")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mind_events (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source TEXT NOT NULL,
                    episode_id TEXT REFERENCES mind_events(id),
                    metadata TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL CHECK(status IN ('pending','processing','complete','failed')),
                    error TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mind_memories (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    normalized_content TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN
                        ('user_statement','observation','inference','reflection','commitment','legacy_unverified')),
                    source TEXT NOT NULL,
                    source_event_ids TEXT NOT NULL,
                    confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
                    supersedes TEXT REFERENCES mind_memories(id),
                    superseded_by TEXT REFERENCES mind_memories(id),
                    imported_legacy_id TEXT UNIQUE,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mind_state (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS mind_events_pending ON mind_events(status, kind)")
            conn.execute("CREATE INDEX IF NOT EXISTS mind_events_episode ON mind_events(episode_id, kind)")
            conn.execute("CREATE INDEX IF NOT EXISTS mind_memories_active ON mind_memories(superseded_by, kind)")
            conn.execute("""
                CREATE INDEX IF NOT EXISTS mind_memories_dedup
                ON mind_memories(kind, source, normalized_content)
            """)
            conn.execute(
                "INSERT INTO mind_meta(key,value) VALUES('schema_version','2') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )
            self._import_legacy(conn)

    @staticmethod
    def _event(row):
        if row is None:
            return None
        value = dict(row)
        value.pop("rowid", None)
        value["metadata"] = json.loads(value["metadata"])
        return value

    @staticmethod
    def _memory(row):
        if row is None:
            return None
        value = dict(row)
        value.pop("rowid", None)
        value.pop("normalized_content", None)
        value["source_event_ids"] = json.loads(value["source_event_ids"])
        return value

    def _insert_event(self, conn, kind, content, source="system",
                      episode_id=None, metadata=None, status="complete"):
        kind = _text(kind, "kind", 80)
        content = _text(content, "content", 200_000)
        source = _text(source, "source", 100)
        if status not in EVENT_STATUSES:
            raise ValueError("Invalid event status")
        if status in {"pending", "processing"} and kind not in {"user", "reflection"}:
            raise ValueError("Only user and reflection events enter the input queue")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("metadata must be a dictionary")
        if episode_id is not None and not conn.execute(
            "SELECT 1 FROM mind_events WHERE id=?", (episode_id,)
        ).fetchone():
            raise ValueError("episode_id must reference an existing event")
        event_id = str(uuid.uuid4())
        created_at = _now()
        conn.execute("""
            INSERT INTO mind_events
                (id,kind,content,source,episode_id,metadata,status,created_at,completed_at)
            VALUES(?,?,?,?,?,?,?,?,?)
        """, (
            event_id, kind, content, source, episode_id, _json(metadata or {}),
            status, created_at, created_at if status == "complete" else None,
        ))
        return self._event(conn.execute(
            "SELECT * FROM mind_events WHERE id=?", (event_id,)
        ).fetchone())

    def _import_legacy(self, conn):
        if conn.execute(
            "SELECT 1 FROM mind_meta WHERE key='legacy_import_complete'"
        ).fetchone():
            return
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_nodes'"
        ).fetchone()
        imported = 0
        if exists:
            columns = {r["name"] for r in conn.execute("PRAGMA table_info(memory_nodes)")}
            if {"node_id", "content"}.issubset(columns):
                rows = conn.execute("SELECT * FROM memory_nodes ORDER BY node_id").fetchall()
                for row in rows:
                    old = dict(row)
                    content = old.get("content")
                    if not isinstance(content, str) or not content.strip():
                        continue
                    event = self._insert_event(
                        conn, "legacy", content[:200_000], "legacy",
                        metadata={
                            "legacy_node_id": str(old["node_id"]),
                            "legacy_timestamp": old.get("timestamp"),
                            "verification": "unverified",
                        },
                    )
                    self._insert_memory(
                        conn, content[:100_000], "legacy_unverified", "legacy",
                        [event["id"]], confidence=0.0,
                        imported_legacy_id=str(old["node_id"]),
                    )
                    imported += 1
        conn.execute(
            "INSERT INTO mind_meta(key,value) VALUES('legacy_import_complete',?)",
            (_now(),),
        )
        conn.execute(
            "INSERT INTO mind_meta(key,value) VALUES('legacy_import_count',?)",
            (str(imported),),
        )

    def import_legacy(self, path):
        """Import an external v1 database through a read-only source connection.

        Each source-path/node pair is imported once. Later calls can import new
        legacy rows without duplicating prior rows. The source is never migrated.
        """
        legacy_path = Path(path).expanduser().resolve()
        if not legacy_path.is_file():
            raise FileNotFoundError(legacy_path)
        if not self._uri and legacy_path == Path(self.db_path):
            return 0
        source_key = hashlib.sha256(str(legacy_path).encode("utf-8")).hexdigest()
        source_conn = sqlite3.connect(legacy_path.as_uri() + "?mode=ro", uri=True)
        source_conn.row_factory = sqlite3.Row
        try:
            exists = source_conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_nodes'"
            ).fetchone()
            if not exists:
                return 0
            columns = {row["name"] for row in source_conn.execute("PRAGMA table_info(memory_nodes)")}
            if not {"node_id", "content"}.issubset(columns):
                return 0
            with self._connection(write=True) as conn:
                imported = 0
                for row in source_conn.execute("SELECT * FROM memory_nodes ORDER BY node_id"):
                    old = dict(row)
                    import_key = f"external:{source_key}:{old['node_id']}"
                    if conn.execute(
                        "SELECT 1 FROM mind_memories WHERE imported_legacy_id=?", (import_key,)
                    ).fetchone():
                        continue
                    content = old.get("content")
                    if not isinstance(content, str) or not content.strip():
                        continue
                    event = self._insert_event(
                        conn, "legacy", content[:200_000], "legacy",
                        metadata={
                            "legacy_node_id": str(old["node_id"]),
                            "legacy_database": str(legacy_path),
                            "legacy_timestamp": old.get("timestamp"),
                            "legacy_content_truncated": len(content) > 200_000,
                            "verification": "unverified",
                        },
                    )
                    self._insert_memory(
                        conn, content[:100_000], "legacy_unverified", "legacy",
                        [event["id"]], confidence=0.0, imported_legacy_id=import_key,
                    )
                    imported += 1
                previous = conn.execute(
                    "SELECT value FROM mind_meta WHERE key='legacy_import_count'"
                ).fetchone()
                total = (int(previous["value"]) if previous else 0) + imported
                conn.execute(
                    "INSERT INTO mind_meta(key,value) VALUES('legacy_import_count',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(total),),
                )
                return imported
        finally:
            source_conn.close()

    def append_event(self, kind, content, source="system", episode_id=None,
                     metadata=None, status="complete"):
        with self._connection(write=True) as conn:
            return self._insert_event(conn, kind, content, source, episode_id, metadata, status)

    def enqueue(self, content, kind="user", source="user", metadata=None):
        return self.append_event(kind, content, source, metadata=metadata, status="pending")

    def get_event(self, event_id):
        with self._connection() as conn:
            return self._event(conn.execute(
                "SELECT * FROM mind_events WHERE id=?", (event_id,)
            ).fetchone())

    def list_events(self, limit=50):
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM mind_events ORDER BY rowid DESC LIMIT ?", (_limit(limit),)
            ).fetchall()
            return [self._event(row) for row in reversed(rows)]

    def claim_next(self):
        with self._connection(write=True) as conn:
            # Conversation takes priority over queued background reflection.
            row = conn.execute("""
                SELECT * FROM mind_events WHERE status='pending' AND kind IN ('user','reflection')
                ORDER BY CASE WHEN kind='user' THEN 0 ELSE 1 END, rowid LIMIT 1
            """).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE mind_events SET status='processing' WHERE id=?", (row["id"],))
            event = self._event(row)
            event["status"] = "processing"
            return event

    def recover_pending(self):
        with self._connection(write=True) as conn:
            cursor = conn.execute("""
                UPDATE mind_events SET status='pending'
                WHERE status='processing' AND kind IN ('user','reflection')
            """)
            return cursor.rowcount

    def fail_event(self, event_id, error):
        message = _text(str(error), "error", 10_000)
        with self._connection(write=True) as conn:
            row = conn.execute("SELECT status FROM mind_events WHERE id=?", (event_id,)).fetchone()
            if row is None:
                raise KeyError(event_id)
            if row["status"] == "complete":
                raise ValueError("A completed event cannot be failed")
            conn.execute(
                "UPDATE mind_events SET status='failed',error=?,completed_at=? WHERE id=?",
                (message, _now(), event_id),
            )

    def retry_event(self, event_id):
        with self._connection(write=True) as conn:
            row = conn.execute("SELECT kind,status FROM mind_events WHERE id=?", (event_id,)).fetchone()
            if row is None:
                raise KeyError(event_id)
            if row["status"] != "failed" or row["kind"] not in {"user", "reflection"}:
                raise ValueError("Only failed input events can be retried")
            conn.execute(
                "UPDATE mind_events SET status='pending',error=NULL,completed_at=NULL WHERE id=?",
                (event_id,),
            )
            return self._event(conn.execute(
                "SELECT * FROM mind_events WHERE id=?", (event_id,)
            ).fetchone())

    def load_state(self):
        with self._connection() as conn:
            row = conn.execute("SELECT state_json FROM mind_state WHERE singleton=1").fetchone()
            return json.loads(row["state_json"]) if row else None

    @staticmethod
    def _save_state(conn, state):
        if not isinstance(state, dict):
            raise ValueError("state must be a dictionary")
        conn.execute("""
            INSERT INTO mind_state(singleton,state_json,updated_at) VALUES(1,?,?)
            ON CONFLICT(singleton) DO UPDATE
            SET state_json=excluded.state_json,updated_at=excluded.updated_at
        """, (_json(state), _now()))

    def save_state(self, state):
        with self._connection(write=True) as conn:
            self._save_state(conn, state)

    def _insert_memory(self, conn, content, kind, source, source_event_ids,
                       confidence=0.5, supersedes=None, imported_legacy_id=None):
        content = _text(content, "memory content")
        source = _text(source, "memory source", 100)
        if kind not in MEMORY_KINDS:
            raise ValueError("Invalid memory kind")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise ValueError("confidence must be numeric")
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("confidence must be between zero and one")
        if not isinstance(source_event_ids, list) or any(
            not isinstance(ref, str) or not ref for ref in source_event_ids
        ):
            raise ValueError("source_event_ids must be a list of event IDs")
        evidence = list(dict.fromkeys(source_event_ids))
        if not evidence:
            raise ValueError("Every memory requires at least one source event")
        if len(evidence) > 100:
            raise ValueError("Too many source events")
        for event_id in evidence:
            if not conn.execute("SELECT 1 FROM mind_events WHERE id=?", (event_id,)).fetchone():
                raise ValueError(f"Unknown source event: {event_id}")
        if supersedes is not None:
            previous = conn.execute(
                "SELECT superseded_by FROM mind_memories WHERE id=?", (supersedes,)
            ).fetchone()
            if previous is None:
                raise ValueError("supersedes must reference an existing memory")
            if previous["superseded_by"] is not None:
                raise ValueError("Cannot correct an already superseded memory")
        memory_id = str(uuid.uuid4())
        conn.execute("""
            INSERT INTO mind_memories
                (id,content,normalized_content,kind,source,source_event_ids,confidence,
                 supersedes,imported_legacy_id,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
        """, (
            memory_id, content, _normalized(content), kind, source, _json(evidence),
            float(confidence), supersedes, imported_legacy_id, _now(),
        ))
        if supersedes is not None:
            conn.execute(
                "UPDATE mind_memories SET superseded_by=? WHERE id=?", (memory_id, supersedes)
            )
        return self._memory(conn.execute(
            "SELECT * FROM mind_memories WHERE id=?", (memory_id,)
        ).fetchone())

    def add_memory(self, content, kind, source, source_event_ids,
                   confidence=0.5, supersedes=None):
        with self._connection(write=True) as conn:
            return self._insert_memory(
                conn, content, kind, source, source_event_ids, confidence, supersedes
            )

    def get_memory(self, memory_id):
        with self._connection() as conn:
            return self._memory(conn.execute(
                "SELECT * FROM mind_memories WHERE id=?", (memory_id,)
            ).fetchone())

    def list_memories(self, limit=50):
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM mind_memories ORDER BY rowid DESC LIMIT ?", (_limit(limit),)
            ).fetchall()
            return [self._memory(row) for row in rows]

    def retrieve(self, query, limit=6, include_superseded=False):
        """BM25-style local lexical retrieval; never invent semantic evidence.

        This transparent baseline scans active memories. It can later be replaced
        by an indexed retriever without changing evidence IDs or memory semantics.
        """
        if not isinstance(query, str):
            raise ValueError("query must be a string")
        limit = _limit(limit, maximum=100)
        if limit == 0:
            return []
        clause = "" if include_superseded else "WHERE superseded_by IS NULL"
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM mind_memories {clause} ORDER BY rowid DESC"
            ).fetchall()
        if not query.strip():
            return [dict(self._memory(row), score=0.0) for row in rows[:limit]]
        query_terms = set(_tokens(query))
        if not query_terms or not rows:
            return []
        documents = [Counter(_tokens(row["content"])) for row in rows]
        lengths = [sum(document.values()) for document in documents]
        average_length = max(1.0, sum(lengths) / len(lengths))
        frequency = {
            term: sum(term in document for document in documents)
            for term in query_terms
        }
        kind_weights = {"inference": 0.85, "reflection": 0.75, "legacy_unverified": 0.6}
        scored = []
        for row, document, length in zip(rows, documents, lengths):
            score = 0.0
            for term in query_terms:
                count = document.get(term, 0)
                if not count:
                    continue
                inverse = math.log(1 + (len(rows) - frequency[term] + 0.5) / (frequency[term] + 0.5))
                denominator = count + 1.2 * (0.25 + 0.75 * length / average_length)
                score += inverse * count * 2.2 / denominator
            if score > 0:
                score *= kind_weights.get(row["kind"], 1.0)
                scored.append(dict(self._memory(row), score=round(score, 8)))
        # Python's stable sort preserves newest-first order for equal scores.
        scored.sort(key=lambda memory: memory["score"], reverse=True)
        return scored[:limit]

    def commit_cycle(self, event_id, state, reply, memories, trace):
        """Commit the entire cognitive result, or leave every component unchanged."""
        if not isinstance(memories, list) or not isinstance(trace, list):
            raise ValueError("memories and trace must be lists")
        reply = _text(reply, "reply", 200_000)
        with self._connection(write=True) as conn:
            event = conn.execute("SELECT * FROM mind_events WHERE id=?", (event_id,)).fetchone()
            if event is None:
                raise KeyError(event_id)
            if event["kind"] not in {"user", "reflection"}:
                raise ValueError("Only input events can complete a cognitive cycle")
            if event["status"] == "complete":
                previous = conn.execute(
                    "SELECT * FROM mind_events WHERE episode_id=? AND kind='assistant' ORDER BY rowid LIMIT 1",
                    (event_id,),
                ).fetchone()
                if previous and previous["content"] == reply:
                    return self._event(previous)
                raise ValueError("This event was already completed")
            if event["status"] not in {"pending", "processing"}:
                raise ValueError("Retry the failed event before committing it")
            stored = []
            for proposal in memories:
                if not isinstance(proposal, dict):
                    raise ValueError("Each memory must be a dictionary")
                stored.append(self._insert_memory(
                    conn, proposal["content"], proposal["kind"], proposal["source"],
                    proposal["source_event_ids"], proposal.get("confidence", 0.5),
                    proposal.get("supersedes"),
                ))
            assistant = self._insert_event(
                conn, "assistant", reply, "buddhi", episode_id=event_id,
                metadata={"trace": trace, "memory_ids": [memory["id"] for memory in stored]},
            )
            self._save_state(conn, state)
            conn.execute(
                "UPDATE mind_events SET status='complete',error=NULL,completed_at=? WHERE id=?",
                (_now(), event_id),
            )
            return assistant

    def consolidate(self):
        """Consolidate exact duplicates without training or deleting history."""
        with self._connection(write=True) as conn:
            rows = conn.execute("""
                SELECT id,kind,source,normalized_content FROM mind_memories
                WHERE superseded_by IS NULL ORDER BY rowid DESC
            """).fetchall()
            survivors = {}
            duplicates = 0
            for row in rows:
                key = (row["kind"], row["source"], row["normalized_content"])
                keeper = survivors.get(key)
                if keeper is None:
                    survivors[key] = row["id"]
                else:
                    conn.execute(
                        "UPDATE mind_memories SET superseded_by=? WHERE id=?", (keeper, row["id"])
                    )
                    duplicates += 1
            result = {
                "status": "complete", "duplicates_superseded": duplicates,
                "active_memories": len(survivors),
                "method": "exact_content_deduplication", "weights_updated": False,
            }
            self._insert_event(
                conn, "consolidation",
                f"Consolidated {duplicates} duplicate memories; preserved all history.",
                "chitta", metadata=result,
            )
            return result

    def stats(self):
        with self._connection() as conn:
            counts = {
                "events": conn.execute("SELECT COUNT(*) FROM mind_events").fetchone()[0],
                "memories": conn.execute("SELECT COUNT(*) FROM mind_memories").fetchone()[0],
                "active_memories": conn.execute(
                    "SELECT COUNT(*) FROM mind_memories WHERE superseded_by IS NULL"
                ).fetchone()[0],
                "pending": conn.execute(
                    "SELECT COUNT(*) FROM mind_events WHERE status IN ('pending','processing')"
                ).fetchone()[0],
                "failed": conn.execute(
                    "SELECT COUNT(*) FROM mind_events WHERE status='failed'"
                ).fetchone()[0],
            }
            legacy = conn.execute(
                "SELECT value FROM mind_meta WHERE key='legacy_import_count'"
            ).fetchone()
            counts["legacy_imported"] = int(legacy["value"]) if legacy else 0
            return counts

    def close(self):
        if self._anchor is not None:
            self._anchor.close()
            self._anchor = None
        self._closed = True
