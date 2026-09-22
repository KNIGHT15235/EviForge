"""Transactional DAG checkpoints with a single live owner and fenced writes."""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any
from eviforge.dag.graph import DAGError, DriftError


class SQLiteJournal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self.db.close()
            raise DAGError("Unsupported DAG journal schema version")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        PRAGMA user_version=1;
        CREATE TABLE IF NOT EXISTS runs (
          id TEXT PRIMARY KEY, graph_hash TEXT NOT NULL, capability_hash TEXT NOT NULL,
          status TEXT NOT NULL, owner TEXT NOT NULL, pid INTEGER NOT NULL, fence INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS nodes (
          run_id TEXT NOT NULL, id TEXT NOT NULL, status TEXT NOT NULL,
          attempt INTEGER NOT NULL DEFAULT 0, input_hash TEXT, output TEXT, error TEXT NOT NULL DEFAULT '',
          PRIMARY KEY(run_id,id));
        CREATE TABLE IF NOT EXISTS events (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, node_id TEXT,
          attempt INTEGER, timestamp REAL NOT NULL, type TEXT NOT NULL, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS artifacts (
          run_id TEXT NOT NULL, node_id TEXT NOT NULL, attempt INTEGER NOT NULL,
          sha256 TEXT NOT NULL, data TEXT NOT NULL,
          PRIMARY KEY(run_id,node_id,attempt,sha256,data));
        CREATE TABLE IF NOT EXISTS workspace (
          run_id TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL,
          PRIMARY KEY(run_id,path));
        """)
        self.owner = uuid.uuid4().hex
        self.run_id = ""
        self.fence = 0

    def close(self) -> None:
        self.db.close()

    def begin_run(self, run_id: str, graph_hash: str, capability_hash: str,
                  node_ids: list[str], *, resume: bool = False) -> None:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            old = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if old:
                if not resume:
                    raise DAGError("Run exists; use resume")
                if old["graph_hash"] != graph_hash or old["capability_hash"] != capability_hash:
                    raise DriftError("Graph or effective capabilities changed")
                if old["status"] == "running":
                    try:
                        os.kill(old["pid"], 0)
                    except ProcessLookupError:
                        pass
                    else:
                        raise DAGError("Run already has a live owner")
                self.fence = old["fence"] + 1
                self.db.execute("UPDATE runs SET owner=?,pid=?,fence=?,status='running' WHERE id=?",
                                (self.owner, os.getpid(), self.fence, run_id))
            else:
                if resume:
                    raise DAGError("Unknown run")
                self.fence = 1
                self.db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?)",
                                (run_id, graph_hash, capability_hash, "running", self.owner,
                                 os.getpid(), self.fence))
                self.db.executemany("INSERT INTO nodes(run_id,id,status) VALUES(?,?,'pending')",
                                    [(run_id, node_id) for node_id in node_ids])
            self.run_id = run_id

    def _check_owner(self) -> None:
        row = self.db.execute("SELECT owner,fence FROM runs WHERE id=?", (self.run_id,)).fetchone()
        if row is None or row["owner"] != self.owner or row["fence"] != self.fence:
            raise DAGError("Stale journal owner")

    def _event(self, node: str | None, attempt: int | None, kind: str, data: dict) -> None:
        self.db.execute("INSERT INTO events(run_id,node_id,attempt,timestamp,type,data) VALUES(?,?,?,?,?,?)",
                        (self.run_id, node, attempt, time.time(), kind,
                         json.dumps(data, ensure_ascii=False, sort_keys=True)))

    def append_event(self, node: str | None, attempt: int | None, kind: str, data: dict) -> None:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self._check_owner()
            self._event(node, attempt, kind, data)

    def claim_node(self, node: str, input_hash: str) -> int:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self._check_owner()
            row = self.db.execute("SELECT * FROM nodes WHERE run_id=? AND id=?", (self.run_id, node)).fetchone()
            if row is None or row["status"] != "pending":
                raise DAGError("Node is not claimable")
            attempt = row["attempt"] + 1
            self.db.execute("UPDATE nodes SET status='running',attempt=?,input_hash=? WHERE run_id=? AND id=?",
                            (attempt, input_hash, self.run_id, node))
            self._event(node, attempt, "node_started", {"input_hash": input_hash})
            return attempt

    def artifact(self, ref: Any) -> None:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self._check_owner()
            self.db.execute("INSERT OR IGNORE INTO artifacts VALUES(?,?,?,?,?)",
                            (ref.run_id, ref.node_id, ref.attempt, ref.sha256, ref.model_dump_json()))
            self._event(ref.node_id, ref.attempt, "artifact_captured", ref.model_dump())

    def has_artifact(self, ref: Any) -> bool:
        return self.db.execute("SELECT 1 FROM artifacts WHERE run_id=? AND node_id=? AND attempt=? AND sha256=? AND data=?",
                               (ref.run_id, ref.node_id, ref.attempt, ref.sha256, ref.model_dump_json())).fetchone() is not None

    def set_node(self, node: str, status: str, *, output: Any = None, error: str = "",
                 workspace: dict[str, str] | None = None) -> None:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self._check_owner()
            self.db.execute("UPDATE nodes SET status=?,output=?,error=? WHERE run_id=? AND id=?",
                            (status, output.model_dump_json() if output else None, error, self.run_id, node))
            for path, digest in (workspace or {}).items():
                self.db.execute("INSERT OR REPLACE INTO workspace VALUES(?,?,?)", (self.run_id, path, digest))
            self._event(node, None, "node_" + status,
                        {"error": error, "output": output.model_dump() if output else None})

    def finish(self, status: str) -> None:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self._check_owner()
            self.db.execute("UPDATE runs SET status=? WHERE id=?", (status, self.run_id))
            self._event(None, None, "run_" + status, {})

    def checkpoint(self) -> dict[str, dict]:
        return {r["id"]: dict(r) for r in self.db.execute("SELECT * FROM nodes WHERE run_id=?", (self.run_id,))}

    def expected_workspace(self) -> dict[str, str]:
        return dict(self.db.execute("SELECT path,sha256 FROM workspace WHERE run_id=?", (self.run_id,)))

    def events(self, run_id: str) -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,))]
