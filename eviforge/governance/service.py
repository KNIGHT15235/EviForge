from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

MEMORY_BUDGET = 16_000
SCOPES = ("user", "project")


class GovernanceError(ValueError):
    pass


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('memory','skill')),
    name TEXT NOT NULL, scope TEXT NOT NULL CHECK(scope IN ('user','project')),
    version INTEGER NOT NULL, source_task TEXT NOT NULL, source_trace TEXT NOT NULL,
    content TEXT NOT NULL, content_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('quarantine','verified','published','superseded','revoked')),
    ever_published INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
    UNIQUE(kind,name,version)
);
CREATE TABLE IF NOT EXISTS verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT, entry_id TEXT NOT NULL REFERENCES entries(id),
    content_hash TEXT NOT NULL, outcome TEXT NOT NULL CHECK(outcome IN ('pass','fail')),
    evidence_json TEXT NOT NULL, evidence_hash TEXT NOT NULL,
    validator TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS confirmations (
    id INTEGER PRIMARY KEY AUTOINCREMENT, entry_id TEXT NOT NULL REFERENCES entries(id),
    content_hash TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT, entry_id TEXT NOT NULL REFERENCES entries(id),
    sentiment TEXT NOT NULL CHECK(sentiment IN ('positive','negative')),
    reason TEXT NOT NULL, source_task TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS active_refs (
    kind TEXT NOT NULL, name TEXT NOT NULL, entry_id TEXT NOT NULL REFERENCES entries(id),
    PRIMARY KEY(kind,name)
);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, entry_id TEXT NOT NULL REFERENCES entries(id),
    action TEXT NOT NULL, actor TEXT NOT NULL, detail_json TEXT NOT NULL, created_at TEXT NOT NULL
);
PRAGMA user_version=1;
"""


def _fair_allocations(lengths: list[int], budget: int) -> list[int]:
    """Max-min allocation; short records give unused capacity to longer ones."""
    allocations = [0] * len(lengths)
    remaining = max(0, budget)
    pending = list(range(len(lengths)))
    while pending and remaining:
        share, extra = divmod(remaining, len(pending))
        next_pending = []
        for position, index in enumerate(pending):
            amount = min(lengths[index] - allocations[index], share + (position < extra))
            allocations[index] += amount
            remaining -= amount
            if allocations[index] < lengths[index]:
                next_pending.append(index)
        pending = next_pending
    return allocations


def _fit(text: str, count: int) -> str:
    if len(text) <= count:
        return text
    marker = "\n[truncated]\n"
    if count <= len(marker):
        return text[:count]
    return text[:count - len(marker)] + marker


class GovernanceService:
    """Scope-routed SQLite transactions; no model or network access.

    ``user_root`` is the user data directory itself, useful for isolated tests.
    Connections are short-lived; reads do not create a missing user database.
    """

    def __init__(self, work_dir: str | Path, user_root: str | Path | None = None) -> None:
        self.work_dir = Path(work_dir).resolve()
        self.paths = {
            "project": self.work_dir / ".eviforge" / "governance.sqlite3",
            "user": (Path(user_root) if user_root is not None else Path.home() / ".eviforge") / "governance-user.sqlite3",
        }

    def _path(self, scope: str) -> Path:
        if scope not in SCOPES:
            raise GovernanceError(f"Invalid scope: {scope}")
        return self.paths[scope]

    @contextmanager
    def _transaction(self, scope: str) -> Iterator[sqlite3.Connection]:
        path = self._path(scope)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=10000")
            connection.executescript(_SCHEMA)
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _read(self, scope: str) -> Iterator[sqlite3.Connection | None]:
        path = self._path(scope)
        if not path.exists():
            yield None
            return
        connection = sqlite3.connect(path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _entry(connection: sqlite3.Connection, entry_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        if row is None:
            raise GovernanceError(f"Unknown entry: {entry_id}")
        entry = dict(row)
        if _hash(entry["content"]) != entry["content_hash"]:
            raise GovernanceError(f"Content hash mismatch: {entry_id}")
        return entry

    @staticmethod
    def _audit(connection: sqlite3.Connection, entry_id: str, action: str, actor: str, **detail: Any) -> None:
        connection.execute(
            "INSERT INTO audit_events(entry_id,action,actor,detail_json,created_at) VALUES(?,?,?,?,?)",
            (entry_id, action, actor, _json(detail), _now()),
        )

    def _propose(self, kind: str, content: str, scope: str, name: str, source_task: str, source_trace: str) -> dict[str, Any]:
        if not all(isinstance(value, str) and value.strip() for value in (content, name, source_task, source_trace)):
            raise GovernanceError("Content, name, source_task and source_trace are required")
        if len(content) > 1_000_000:
            raise GovernanceError("Candidate content exceeds the 1,000,000-character limit")
        with self._transaction(scope) as connection:
            version = connection.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM entries WHERE kind=? AND name=?", (kind, name),
            ).fetchone()[0]
            entry_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO entries(id,kind,name,scope,version,source_task,source_trace,content,content_hash,status,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,'quarantine',?)",
                (entry_id, kind, name, scope, version, source_task, source_trace, content, _hash(content), _now()),
            )
            self._audit(connection, entry_id, "propose", source_trace, source_task=source_task)
            return self._entry(connection, entry_id)

    def propose_memory(self, content: str, *, scope: str = "project", name: str,
                       source_task: str, source_trace: str) -> dict[str, Any]:
        return self._propose("memory", content, scope, name, source_task, source_trace)

    def propose_skill(self, markdown: str, *, scope: str = "project", name: str,
                      source_task: str, source_trace: str) -> dict[str, Any]:
        from eviforge.skills.parser import _validate_meta, parse_frontmatter

        meta, _ = parse_frontmatter(markdown)
        if not isinstance(meta.get("description"), str) or not meta["description"].strip():
            raise GovernanceError("Skill description must be a nonempty string")
        if any(not isinstance(meta.get(key, default), str) for key, default in (("mode", "inline"), ("context", "full"))):
            raise GovernanceError("Skill mode and context must be strings")
        if meta.get("model") is not None and not isinstance(meta["model"], str):
            raise GovernanceError("Skill model must be a string or null")
        _validate_meta(meta)
        if meta["name"] != name:
            raise GovernanceError("Skill frontmatter name must match the candidate name")
        allowed = meta.get("allowedTools", [])
        if not isinstance(allowed, list) or any(not isinstance(item, str) for item in allowed):
            raise GovernanceError("Skill allowedTools must be a list of tool names")
        return self._propose("skill", markdown, scope, name, source_task, source_trace)

    def get_entry(self, entry_id: str, *, scope: str = "project") -> dict[str, Any]:
        with self._read(scope) as connection:
            if connection is None:
                raise GovernanceError(f"Unknown entry: {entry_id}")
            entry = self._entry(connection, entry_id)
            for key, table in (("verifications", "verifications"), ("confirmations", "confirmations"),
                               ("feedback", "feedback"), ("audit", "audit_events")):
                entry[key] = [dict(row) for row in connection.execute(
                    f"SELECT * FROM {table} WHERE entry_id=? ORDER BY id", (entry_id,),
                )]
            return entry

    def list_entries(self, *, scope: str | None = None, status: str | None = None,
                     kind: str | None = None) -> list[dict[str, Any]]:
        results = []
        for current in (scope,) if scope else SCOPES:
            with self._read(current) as connection:
                if connection is None:
                    continue
                for row in connection.execute("SELECT * FROM entries ORDER BY created_at,id"):
                    entry = dict(row)
                    if (status is None or entry["status"] == status) and (kind is None or entry["kind"] == kind):
                        entry["integrity_valid"] = _hash(entry["content"]) == entry["content_hash"]
                        results.append(entry)
        return results

    def record_verification(self, entry_id: str, *, scope: str = "project", content_hash: str,
                            outcome: str, evidence: dict[str, Any], validator: str) -> dict[str, Any]:
        if (outcome not in ("pass", "fail") or not isinstance(evidence, dict)
                or not evidence or not isinstance(validator, str) or not validator.strip()):
            raise GovernanceError("Verification requires pass/fail, nonempty evidence and a validator")
        evidence_json = _json(evidence)
        with self._transaction(scope) as connection:
            entry = self._entry(connection, entry_id)
            if entry["content_hash"] != content_hash:
                raise GovernanceError("Verification refers to a different content hash")
            connection.execute(
                "INSERT INTO verifications(entry_id,content_hash,outcome,evidence_json,evidence_hash,validator,created_at) "
                "VALUES(?,?,?,?,?,?,?)", (entry_id, content_hash, outcome, evidence_json, _hash(evidence_json), validator, _now()),
            )
            if outcome == "fail":
                self._revoke(connection, entry_id)
            elif entry["status"] == "quarantine":
                connection.execute("UPDATE entries SET status='verified' WHERE id=?", (entry_id,))
            self._audit(connection, entry_id, "verify", validator, outcome=outcome, evidence_hash=_hash(evidence_json))
            return self._entry(connection, entry_id)

    def confirm(self, entry_id: str, *, scope: str = "project", content_hash: str, actor: str) -> dict[str, Any]:
        if not actor.strip():
            raise GovernanceError("Human confirmation requires an actor")
        with self._transaction(scope) as connection:
            entry = self._entry(connection, entry_id)
            if entry["content_hash"] != content_hash:
                raise GovernanceError("Confirmation refers to a different content hash")
            connection.execute(
                "INSERT INTO confirmations(entry_id,content_hash,actor,created_at) VALUES(?,?,?,?)",
                (entry_id, content_hash, actor, _now()),
            )
            self._audit(connection, entry_id, "confirm", actor, content_hash=content_hash)
            return entry

    @staticmethod
    def _eligible(connection: sqlite3.Connection, entry: dict[str, Any]) -> None:
        if entry["status"] not in ("verified", "published", "superseded"):
            raise GovernanceError("Entry is not verified or has been revoked")
        verification = connection.execute(
            "SELECT * FROM verifications WHERE entry_id=? ORDER BY id DESC LIMIT 1", (entry["id"],),
        ).fetchone()
        if (verification is None or verification["outcome"] != "pass"
                or verification["content_hash"] != entry["content_hash"]
                or _hash(verification["evidence_json"]) != verification["evidence_hash"]):
            raise GovernanceError("A passing verification bound to this content is required")
        confirmation = connection.execute(
            "SELECT 1 FROM confirmations WHERE entry_id=? AND content_hash=? LIMIT 1",
            (entry["id"], entry["content_hash"]),
        ).fetchone()
        if confirmation is None:
            raise GovernanceError("Human confirmation of this content hash is required")
        if connection.execute(
            "SELECT 1 FROM feedback WHERE entry_id=? AND sentiment='negative' LIMIT 1", (entry["id"],),
        ).fetchone():
            raise GovernanceError("Negative feedback blocks this version; propose a corrected version")

    def publish(self, entry_id: str, *, scope: str = "project", actor: str,
                _action: str = "publish") -> dict[str, Any]:
        if not actor.strip():
            raise GovernanceError("Publication requires an actor")
        with self._transaction(scope) as connection:
            entry = self._entry(connection, entry_id)
            self._eligible(connection, entry)
            old = connection.execute(
                "SELECT entry_id FROM active_refs WHERE kind=? AND name=?", (entry["kind"], entry["name"]),
            ).fetchone()
            if old and old[0] != entry_id:
                connection.execute("UPDATE entries SET status='superseded' WHERE id=?", (old[0],))
            connection.execute("UPDATE entries SET status='published',ever_published=1 WHERE id=?", (entry_id,))
            connection.execute(
                "INSERT INTO active_refs(kind,name,entry_id) VALUES(?,?,?) "
                "ON CONFLICT(kind,name) DO UPDATE SET entry_id=excluded.entry_id",
                (entry["kind"], entry["name"], entry_id),
            )
            self._audit(connection, entry_id, _action, actor, previous=old[0] if old else None)
            return self._entry(connection, entry_id)

    @staticmethod
    def _revoke(connection: sqlite3.Connection, entry_id: str) -> None:
        connection.execute("UPDATE entries SET status='revoked' WHERE id=?", (entry_id,))
        connection.execute("DELETE FROM active_refs WHERE entry_id=?", (entry_id,))

    def feedback(self, entry_id: str, *, scope: str = "project", sentiment: str,
                 reason: str, source_task: str) -> dict[str, Any]:
        if sentiment not in ("positive", "negative") or not reason.strip() or not source_task.strip():
            raise GovernanceError("Feedback requires positive/negative, reason and source_task")
        with self._transaction(scope) as connection:
            self._entry(connection, entry_id)
            connection.execute(
                "INSERT INTO feedback(entry_id,sentiment,reason,source_task,created_at) VALUES(?,?,?,?,?)",
                (entry_id, sentiment, reason, source_task, _now()),
            )
            if sentiment == "negative":
                self._revoke(connection, entry_id)
            self._audit(connection, entry_id, "feedback", source_task, sentiment=sentiment, reason=reason)
            return self._entry(connection, entry_id)

    def revoke(self, entry_id: str, *, scope: str = "project", actor: str, reason: str) -> dict[str, Any]:
        if not actor.strip() or not reason.strip():
            raise GovernanceError("Revocation requires an actor and reason")
        with self._transaction(scope) as connection:
            self._entry(connection, entry_id)
            self._revoke(connection, entry_id)
            self._audit(connection, entry_id, "revoke", actor, reason=reason)
            return self._entry(connection, entry_id)

    def rollback(self, *, kind: str, name: str, target_version: int, scope: str = "project",
                 actor: str) -> dict[str, Any]:
        with self._read(scope) as connection:
            row = connection.execute(
                "SELECT id FROM entries WHERE kind=? AND name=? AND version=? AND ever_published=1",
                (kind, name, target_version),
            ).fetchone() if connection is not None else None
        if row is None:
            raise GovernanceError("Rollback target must be a previously published version")
        return self.publish(row[0], scope=scope, actor=actor, _action="rollback")

    def clear_memories(self, *, actor: str = "user") -> None:
        for scope in SCOPES:
            if not self._path(scope).exists():
                continue
            with self._transaction(scope) as connection:
                for row in connection.execute("SELECT id FROM entries WHERE kind='memory' AND status!='revoked'").fetchall():
                    self._revoke(connection, row[0])
                    self._audit(connection, row[0], "revoke", actor, reason="clear memories")

    def active_entries(self, *, kind: str, scope: str | None = None) -> list[dict[str, Any]]:
        results = []
        for current in (scope,) if scope else SCOPES:
            with self._read(current) as connection:
                if connection is None:
                    continue
                for row in connection.execute("SELECT entry_id FROM active_refs WHERE kind=? ORDER BY name", (kind,)).fetchall():
                    try:
                        entry = self._entry(connection, row[0])
                        if entry["status"] != "published" or entry["kind"] != kind or entry["scope"] != current:
                            continue
                        self._eligible(connection, entry)
                    except GovernanceError:
                        continue  # Corrupt/stale entries never enter model context.
                    results.append(entry)
        return results

    def managed_skill_names(self, *, scope: str | None = None) -> set[str]:
        names = set()
        for current in (scope,) if scope else SCOPES:
            with self._read(current) as connection:
                if connection is not None:
                    names.update(row[0] for row in connection.execute(
                        "SELECT DISTINCT name FROM entries WHERE kind='skill' AND ever_published=1",
                    ))
        return names

    def get_published_skill(self, name: str) -> dict[str, Any] | None:
        for scope in ("project", "user"):
            if name in self.managed_skill_names(scope=scope):
                return next((entry for entry in self.active_entries(kind="skill", scope=scope) if entry["name"] == name), None)
        return None

    def render_memory_context(self, budget: int = MEMORY_BUDGET) -> str:
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0:
            raise GovernanceError("Memory budget must be a nonnegative character count")
        groups = []
        for scope in SCOPES:
            groups.append([
                f"[memory scope={scope} id={entry['id']} hash={entry['content_hash']}]\n{entry['content']}\n\n"
                for entry in self.active_entries(kind="memory", scope=scope)
            ])
        scope_budgets = _fair_allocations([sum(map(len, group)) for group in groups], budget)
        return "".join(
            _fit(text, allowance)
            for group, available in zip(groups, scope_budgets)
            for text, allowance in zip(group, _fair_allocations(list(map(len, group)), available))
        )

    def close(self) -> None:
        """Connections close at each operation; retained for runtime lifecycle symmetry."""
