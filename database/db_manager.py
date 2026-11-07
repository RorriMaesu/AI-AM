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


ACTION_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "outcome_unknown"})
ACTION_TRANSITIONS = {
    "proposed": frozenset({"authorized", "cancelled"}),
    "authorized": frozenset({"attempted", "cancelled"}),
    "attempted": frozenset({"succeeded", "failed", "cancelled", "outcome_unknown"}),
}
ACTION_ACTORS = frozenset({"self", "user", "environment"})


def _bounded_json(value, name, maximum=65_536, *, mapping=False):
    """Accept only bounded, finite JSON data; never invoke arbitrary encoders."""
    if mapping and not isinstance(value, dict):
        raise ValueError(f"{name} must be a dictionary")
    remaining = 10_000

    def visit(item, depth):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 16:
            raise ValueError(f"{name} is too deeply nested or contains too many values")
        if item is None or type(item) is bool:
            return
        if type(item) is int:
            if item.bit_length() > 256:
                raise ValueError(f"{name} contains an oversized integer")
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise ValueError(f"{name} contains a nonfinite number")
            return
        if type(item) is str:
            if len(item) > maximum:
                raise ValueError(f"{name} exceeds its size limit")
            return
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str or len(key) > 256:
                    raise ValueError(f"{name} keys must be strings of at most 256 characters")
                visit(child, depth + 1)
            return
        if type(item) is list:
            for child in item:
                visit(child, depth + 1)
            return
        raise ValueError(f"{name} must contain only JSON values")

    visit(value, 0)
    encoded = _json(value)
    if len(encoded.encode("utf-8")) > maximum:
        raise ValueError(f"{name} exceeds {maximum} bytes")
    return encoded


def _revision(value):
    if type(value) is not int or not 0 <= value < 2**63 - 1:
        raise ValueError("expected_revision must be a nonnegative integer")
    return value


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

            # Additive research tables deliberately preserve v2 memory semantics.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mind_actions (
                    id TEXT PRIMARY KEY,
                    episode_id TEXT NOT NULL REFERENCES mind_events(id),
                    tool TEXT NOT NULL,
                    parameters TEXT NOT NULL,
                    actor TEXT NOT NULL CHECK(actor IN ('self','user','environment')),
                    prediction TEXT,
                    source_event_ids TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN
                        ('proposed','authorized','attempted','succeeded','failed','cancelled','outcome_unknown')),
                    result TEXT,
                    error TEXT,
                    outcome_event_id TEXT UNIQUE REFERENCES mind_events(id),
                    experiment_outcome TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mind_action_transitions (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    action_id TEXT NOT NULL REFERENCES mind_actions(id),
                    previous_status TEXT,
                    status TEXT NOT NULL CHECK(status IN
                        ('proposed','authorized','attempted','succeeded','failed','cancelled','outcome_unknown')),
                    result TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS mind_actions_episode
                ON mind_actions(episode_id, status)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS mind_action_transitions_action
                ON mind_action_transitions(action_id, sequence)
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS mind_action_transitions_no_update
                BEFORE UPDATE ON mind_action_transitions
                BEGIN SELECT RAISE(ABORT, 'Action transitions are immutable'); END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS mind_action_transitions_no_delete
                BEFORE DELETE ON mind_action_transitions
                BEGIN SELECT RAISE(ABORT, 'Action transitions are immutable'); END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS mind_actions_proposal_immutable
                BEFORE UPDATE OF id,episode_id,tool,parameters,actor,prediction,source_event_ids,created_at
                ON mind_actions
                BEGIN SELECT RAISE(ABORT, 'Action proposals are immutable'); END
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mind_experiment (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    revision INTEGER NOT NULL CHECK(revision >= 0),
                    snapshot TEXT,
                    updated_at TEXT
                )
            """)
            conn.execute("""
                INSERT OR IGNORE INTO mind_experiment(singleton,revision,snapshot,updated_at)
                VALUES(1,0,NULL,NULL)
            """)
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
        if status in {"pending", "processing"} and kind not in {"user", "reflection", "experiment"}:
            raise ValueError("Only user, reflection, and experiment events enter the input queue")
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
                SELECT * FROM mind_events WHERE status='pending' AND kind IN ('user','reflection','experiment')
                ORDER BY CASE WHEN kind='user' THEN 0 WHEN kind='experiment' THEN 1 ELSE 2 END,
                         rowid LIMIT 1
            """).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE mind_events SET status='processing' WHERE id=?", (row["id"],))
            event = self._event(row)
            event["status"] = "processing"
            return event

    def recover_pending(self):
        with self._connection(write=True) as conn:
            self._recover_actions(conn)
            cursor = conn.execute("""
                UPDATE mind_events SET status='pending'
                WHERE status='processing' AND kind IN ('user','reflection','experiment')
            """)
            return cursor.rowcount

    def cancel_pending_experiments(self, reason):
        """Stop queued/interrupted experiment inputs without touching user work."""
        reason = _text(reason, "reason", 10_000)
        with self._connection(write=True) as conn:
            cursor = conn.execute("""
                UPDATE mind_events SET status='failed',error=?,completed_at=?
                WHERE kind='experiment' AND status IN ('pending','processing')
            """, (reason, _now()))
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
            if row["status"] != "failed" or row["kind"] not in {"user", "reflection", "experiment"}:
                raise ValueError("Only failed input events can be retried")
            conn.execute(
                "UPDATE mind_events SET status='pending',error=NULL,completed_at=NULL WHERE id=?",
                (event_id,),
            )
            return self._event(conn.execute(
                "SELECT * FROM mind_events WHERE id=?", (event_id,)
            ).fetchone())


    @staticmethod
    def _action(conn, row):
        if row is None:
            return None
        action = dict(row)
        action.pop("rowid", None)
        action.pop("experiment_outcome", None)
        for key in ("parameters", "prediction", "source_event_ids", "result"):
            action[key] = json.loads(action[key]) if action[key] is not None else None
        transitions = conn.execute(
            "SELECT * FROM mind_action_transitions WHERE action_id=? ORDER BY sequence",
            (action["id"],),
        ).fetchall()
        action["transitions"] = []
        for transition in transitions:
            entry = dict(transition)
            entry["result"] = json.loads(entry["result"]) if entry["result"] is not None else None
            action["transitions"].append(entry)
        return action

    @staticmethod
    def _append_action_transition(conn, action_id, previous, status, result=None, error=None):
        conn.execute("""
            INSERT INTO mind_action_transitions
                (action_id,previous_status,status,result,error,created_at)
            VALUES(?,?,?,?,?,?)
        """, (action_id, previous, status, result, error, _now()))

    def propose_action(self, action_id, episode_id, tool, parameters, actor="self",
                       prediction=None, source_event_ids=None):
        """Record intent, not execution. Caller controls stable idempotency IDs."""
        action_id = _text(action_id, "action_id", 160)
        episode_id = _text(episode_id, "episode_id", 160)
        tool = _text(tool, "tool", 100)
        if not isinstance(actor, str) or actor not in ACTION_ACTORS:
            raise ValueError("actor must be self, user, or environment")
        parameters_json = _bounded_json(parameters, "parameters", 16_384, mapping=True)
        prediction_json = None if prediction is None else _bounded_json(
            prediction, "prediction", 16_384
        )
        sources = [episode_id] if source_event_ids is None else source_event_ids
        if not isinstance(sources, list) or not 1 <= len(sources) <= 32:
            raise ValueError("source_event_ids must contain 1 to 32 event IDs")
        sources = list(dict.fromkeys(_text(ref, "source event ID", 160) for ref in sources))
        if episode_id not in sources:
            sources.insert(0, episode_id)
        if len(sources) > 32:
            raise ValueError("Too many source event IDs")
        with self._connection(write=True) as conn:
            for ref in sources:
                if not conn.execute("SELECT 1 FROM mind_events WHERE id=?", (ref,)).fetchone():
                    raise ValueError(f"Unknown source event: {ref}")
            previous = conn.execute("SELECT * FROM mind_actions WHERE id=?", (action_id,)).fetchone()
            if previous:
                old = self._action(conn, previous)
                if (old["episode_id"], old["tool"], old["parameters"], old["actor"],
                        old["prediction"], old["source_event_ids"]) != (
                        episode_id, tool, parameters, actor, prediction, sources):
                    raise ValueError("action_id already belongs to a different proposal")
                return old
            timestamp = _now()
            conn.execute("""
                INSERT INTO mind_actions
                    (id,episode_id,tool,parameters,actor,prediction,source_event_ids,
                     status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,'proposed',?,?)
            """, (action_id, episode_id, tool, parameters_json, actor,
                  prediction_json, _json(sources), timestamp, timestamp))
            self._append_action_transition(conn, action_id, None, "proposed")
            return self._action(conn, conn.execute(
                "SELECT * FROM mind_actions WHERE id=?", (action_id,)
            ).fetchone())

    def get_action(self, action_id):
        action_id = _text(action_id, "action_id", 160)
        with self._connection() as conn:
            return self._action(conn, conn.execute(
                "SELECT * FROM mind_actions WHERE id=?", (action_id,)
            ).fetchone())

    def list_actions(self, limit=50):
        """Newest first; callers must filter unfinished/future episode visibility."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM mind_actions ORDER BY rowid DESC LIMIT ?", (_limit(limit),)
            ).fetchall()
            return [self._action(conn, row) for row in rows]

    def transition_action(self, action_id, status, result=None, error=None):
        """Nonterminal transitions never imply execution or produce a tool result."""
        action_id = _text(action_id, "action_id", 160)
        if not isinstance(status, str):
            raise ValueError("Invalid action status")
        if status in ACTION_TERMINAL_STATUSES:
            return self.commit_action_outcome(action_id, status, result, error=error)["action"]
        if status not in ACTION_TRANSITIONS:
            raise ValueError("Invalid action status")
        if result is not None or error is not None:
            raise ValueError("Only terminal transitions may include result or error")
        with self._connection(write=True) as conn:
            row = conn.execute("SELECT * FROM mind_actions WHERE id=?", (action_id,)).fetchone()
            if row is None:
                raise KeyError(action_id)
            if row["status"] == status:
                return self._action(conn, row)
            if status not in ACTION_TRANSITIONS.get(row["status"], ()):
                raise ValueError(f"Invalid action transition: {row['status']} -> {status}")
            conn.execute(
                "UPDATE mind_actions SET status=?,updated_at=? WHERE id=?",
                (status, _now(), action_id),
            )
            self._append_action_transition(conn, action_id, row["status"], status)
            return self._action(conn, conn.execute(
                "SELECT * FROM mind_actions WHERE id=?", (action_id,)
            ).fetchone())

    @staticmethod
    def _load_experiment(conn):
        row = conn.execute(
            "SELECT revision,snapshot,updated_at FROM mind_experiment WHERE singleton=1"
        ).fetchone()
        if row is None:
            return {"revision": 0, "snapshot": None, "updated_at": None}
        return {"revision": row["revision"],
                "snapshot": json.loads(row["snapshot"]) if row["snapshot"] is not None else None,
                "updated_at": row["updated_at"]}

    def load_experiment(self):
        with self._connection() as conn:
            return self._load_experiment(conn)

    @classmethod
    def _save_experiment(cls, conn, snapshot, expected_revision):
        expected_revision = _revision(expected_revision)
        encoded = _bounded_json(snapshot, "experiment snapshot", 262_144, mapping=True)
        current = cls._load_experiment(conn)
        if current["revision"] != expected_revision:
            raise ValueError("Experiment revision conflict")
        conn.execute("""
            INSERT INTO mind_experiment(singleton,revision,snapshot,updated_at)
            VALUES(1,?,?,?)
            ON CONFLICT(singleton) DO UPDATE SET
                revision=excluded.revision,snapshot=excluded.snapshot,updated_at=excluded.updated_at
        """, (expected_revision + 1, encoded, _now()))
        return cls._load_experiment(conn)

    def save_experiment(self, snapshot, expected_revision):
        """Replace isolated experiment state with an optimistic revision check."""
        with self._connection(write=True) as conn:
            return self._save_experiment(conn, snapshot, expected_revision)

    def _commit_action_outcome(self, conn, action_id, status, result,
                               experiment_snapshot=None, expected_revision=None, error=None):
        row = conn.execute("SELECT * FROM mind_actions WHERE id=?", (action_id,)).fetchone()
        if row is None:
            raise KeyError(action_id)
        if row["status"] in ACTION_TERMINAL_STATUSES:
            # A retry cannot reapply effects, rewrite history, or invent a new outcome.
            event = self._event(conn.execute(
                "SELECT * FROM mind_events WHERE id=?", (row["outcome_event_id"],)
            ).fetchone())
            return {"action": self._action(conn, row), "event": event,
                    "experiment": json.loads(row["experiment_outcome"])
                    if row["experiment_outcome"] is not None else None}
        if status not in ACTION_TRANSITIONS.get(row["status"], ()):
            raise ValueError(f"Invalid action transition: {row['status']} -> {status}")
        result_json = _bounded_json(result, "action result", 65_536)
        if error is not None:
            error = _text(error, "action error", 10_000)
        if experiment_snapshot is None and expected_revision is not None:
            raise ValueError("expected_revision requires an experiment snapshot")
        if experiment_snapshot is not None and status != "succeeded":
            raise ValueError("Only successful actions may commit experiment changes")
        experiment = None
        if experiment_snapshot is not None:
            experiment = self._save_experiment(conn, experiment_snapshot, expected_revision)
        action = self._action(conn, row)
        metadata = {
            "action_id": action_id, "actor": action["actor"], "status": status,
            "parameters": action["parameters"], "prediction": action["prediction"],
            "result": json.loads(result_json), "error": error,
            "source_event_ids": action["source_event_ids"],
        }
        if experiment is not None:
            metadata["experiment_revision"] = experiment["revision"]
        content = _json({"tool": action["tool"], "status": status,
                         "result": json.loads(result_json), "error": error})
        if (action["tool"] == "calculator" and status == "succeeded"
                and isinstance(action["parameters"].get("expression"), str)
                and type(result) in (int, float)):
            content = f"{action['parameters']['expression']} = {result}"
        event = self._insert_event(
            conn, "tool", content,
            action["tool"], episode_id=action["episode_id"], metadata=metadata,
        )
        conn.execute("""
            UPDATE mind_actions
            SET status=?,result=?,error=?,outcome_event_id=?,experiment_outcome=?,updated_at=? WHERE id=?
        """, (status, result_json, error, event["id"],
              _json(experiment) if experiment is not None else None, _now(), action_id))
        self._append_action_transition(
            conn, action_id, row["status"], status, result_json, error
        )
        return {"action": self._action(conn, conn.execute(
                    "SELECT * FROM mind_actions WHERE id=?", (action_id,)
                ).fetchone()),
                "event": event, "experiment": experiment}

    def commit_action_outcome(self, action_id, status, result, experiment_snapshot=None,
                              expected_revision=None, error=None):
        """Atomically journal outcome, source event, and optional simulated effects.

        Simulated actions must compute a proposed snapshot without modifying the
        world, then commit it here. Terminal retries return the recorded outcome.
        This is not exactly-once execution for external devices or network tools.
        """
        action_id = _text(action_id, "action_id", 160)
        if not isinstance(status, str) or status not in ACTION_TERMINAL_STATUSES:
            raise ValueError("An action outcome must have a terminal status")
        with self._connection(write=True) as conn:
            return self._commit_action_outcome(
                conn, action_id, status, result, experiment_snapshot, expected_revision, error
            )

    def _recover_actions(self, conn):
        rows = conn.execute(
            "SELECT id FROM mind_actions WHERE status='attempted' ORDER BY rowid"
        ).fetchall()
        for row in rows:
            self._commit_action_outcome(
                conn, row["id"], "outcome_unknown", None,
                error="Processing stopped after the attempt was recorded; no outcome was committed.",
            )
        return len(rows)

    def recover_actions(self):
        """Call only after acquiring the runtime lease; never replay unknown work."""
        with self._connection(write=True) as conn:
            return self._recover_actions(conn)


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
            if event["kind"] not in {"user", "reflection", "experiment"}:
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
