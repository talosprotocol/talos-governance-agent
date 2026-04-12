import asyncio
import json
import logging
import os
import aiosqlite
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any, AsyncIterator
from contextlib import asynccontextmanager

from talos_governance_agent.domain.models import (
    ExecutionLogEntry,
    ExecutionState,
    ExecutionStateEnum,
    ExecutionCheckpoint,
)
from talos_governance_agent.ports.state_store import TgaStateStore

logger = logging.getLogger(__name__)

ALLOWED_TRANSITIONS = {
    (ExecutionStateEnum.PENDING, ExecutionStateEnum.AUTHORIZED),
    (ExecutionStateEnum.PENDING, ExecutionStateEnum.DENIED),
    (ExecutionStateEnum.AUTHORIZED, ExecutionStateEnum.EXECUTING),
    (ExecutionStateEnum.EXECUTING, ExecutionStateEnum.COMPLETED),
    (ExecutionStateEnum.EXECUTING, ExecutionStateEnum.FAILED),
}

ZERO_DIGEST = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

class SqliteStateStore(TgaStateStore):
    """
    Hardened SQLite adapter for TGA state persistence.
    Implements Phase 2 requirements (WAL, 0600 permissions, schema tracking).
    """
    
    def __init__(self, db_path: str = "governance_agent.db") -> None:
        self.db_path = db_path
        self._locks: Dict[str, asyncio.Lock] = {}

    async def initialize(self) -> None:
        """Initialize database with WAL and secure permissions."""
        # 1. Enforce secure permissions (0600)
        if not os.path.exists(self.db_path):
            open(self.db_path, 'a').close()
            os.chmod(self.db_path, 0o600)

        async with aiosqlite.connect(self.db_path) as db:
            # 2. Enable WAL mode
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=NORMAL")

            # 3. Create tables
            await db.execute("""
                CREATE TABLE IF NOT EXISTS schema_versions (
                    version TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
            """)
            
            await db.execute("""
                CREATE TABLE IF NOT EXISTS execution_logs (
                    trace_id TEXT,
                    sequence_number INTEGER,
                    data TEXT,
                    PRIMARY KEY (trace_id, sequence_number)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS execution_states (
                    trace_id TEXT PRIMARY KEY,
                    data TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS checkpoints (
                    trace_id TEXT,
                    sequence_number INTEGER,
                    data TEXT,
                    PRIMARY KEY (trace_id, sequence_number)
                )
            """)
            
            # 4. Create SESSIONS table (Secure Session Cache)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    principal_id TEXT NOT NULL,
                    capability_jti TEXT NOT NULL,
                    capability_kid TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    constraints_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                )
            """)
            
            # Indexes for performance and uniqueness
            await db.execute("CREATE INDEX IF NOT EXISTS idx_sessions_principal ON sessions(principal_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at)")
            await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_sessions_principal_jti ON sessions(principal_id, capability_jti)")

            # Record initial version
            await db.execute(
                "INSERT OR IGNORE INTO schema_versions (version, applied_at) VALUES (?, ?)",
                ("1.2.0", datetime.now(timezone.utc).isoformat())
                if 'datetime' in globals() else ("1.2.0", "2026-01-24T00:00:00Z")
            )
            await db.commit()

    @asynccontextmanager
    async def _get_conn(self) -> AsyncIterator[aiosqlite.Connection]:
        """Helper to get a connection with required PRAGMAs."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout = 5000")
            yield db

    async def acquire_trace_lock(self, trace_id: str) -> None:
        if trace_id not in self._locks:
            self._locks[trace_id] = asyncio.Lock()
        await self._locks[trace_id].acquire()

    async def release_trace_lock(self, trace_id: str) -> None:
        if trace_id in self._locks and self._locks[trace_id].locked():
            self._locks[trace_id].release()

    async def load_state(self, trace_id: str) -> Optional[ExecutionState]:
        async with self._get_conn() as db:
            async with db.execute(
                "SELECT data FROM execution_states WHERE trace_id = ?", (trace_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    data = json.loads(row[0])
                    return ExecutionState.model_validate(data)
        return None

    async def append_log_entry(self, entry: ExecutionLogEntry) -> None:
        trace_id = entry.trace_id
        
        # 1. Validation Logic (Integrity)
        async with self._get_conn() as db:
            async with db.execute(
                "SELECT sequence_number, data FROM execution_logs WHERE trace_id = ? ORDER BY sequence_number DESC LIMIT 1",
                (trace_id,)
            ) as cursor:
                last_row = await cursor.fetchone()
                
            expected_seq = (last_row[0] + 1) if last_row else 1
            if entry.sequence_number != expected_seq:
                raise ValueError(f"Sequence gap: expected {expected_seq}, got {entry.sequence_number}")
            
            if last_row:
                last_entry_data = json.loads(last_row[1])
                if entry.prev_entry_digest != last_entry_data["entry_digest"]:
                    raise ValueError("Hash chain broken")
            elif entry.prev_entry_digest != ZERO_DIGEST:
                 raise ValueError("Genesis entry invalid")
                 
            # High-integrity state transition check
            if not (entry.from_state == ExecutionStateEnum.PENDING and entry.to_state == ExecutionStateEnum.PENDING):
                if (entry.from_state, entry.to_state) not in ALLOWED_TRANSITIONS:
                    raise ValueError(f"Invalid transition: {entry.from_state} -> {entry.to_state}")
            
            if entry.entry_digest != entry.compute_digest():
                raise ValueError("Entry digest mismatch")

            # 2. Persist log entry
            await db.execute(
                "INSERT INTO execution_logs (trace_id, sequence_number, data) VALUES (?, ?, ?)",
                (trace_id, entry.sequence_number, entry.model_dump_json(by_alias=True))
            )
            
            # 3. Update state
            state = await self._load_state_internal(db, trace_id)
            if state is None:
                state = ExecutionState(
                    trace_id=trace_id,
                    plan_id=entry.artifact_id,
                    current_state=entry.to_state,
                    last_sequence_number=entry.sequence_number,
                    last_entry_digest=entry.entry_digest,
                    state_digest=ZERO_DIGEST
                )
            else:
                state.current_state = entry.to_state
                state.last_sequence_number = entry.sequence_number
                state.last_entry_digest = entry.entry_digest
            
            state.state_digest = state.compute_digest()
            
            await db.execute(
                "INSERT OR REPLACE INTO execution_states (trace_id, data) VALUES (?, ?)",
                (trace_id, state.model_dump_json(by_alias=True))
            )
            await db.commit()

    async def _load_state_internal(self, db, trace_id: str) -> Optional[ExecutionState]:
        async with db.execute(
            "SELECT data FROM execution_states WHERE trace_id = ?", (trace_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return ExecutionState.model_validate(json.loads(row[0]))
        return None

    async def list_log_entries(self, trace_id: str, after_seq: int = 0) -> List[ExecutionLogEntry]:
        async with self._get_conn() as db:
            async with db.execute(
                "SELECT data FROM execution_logs WHERE trace_id = ? AND sequence_number > ? ORDER BY sequence_number ASC",
                (trace_id, after_seq)
            ) as cursor:
                entries = []
                async for row in cursor:
                    entries.append(ExecutionLogEntry.model_validate(json.loads(row[0])))
                return entries

    async def write_checkpoint(self, checkpoint: ExecutionCheckpoint) -> None:
        async with self._get_conn() as db:
            await db.execute(
                "INSERT INTO checkpoints (trace_id, sequence_number, data) VALUES (?, ?, ?)",
                (checkpoint.trace_id, checkpoint.checkpoint_sequence_number, checkpoint.model_dump_json(by_alias=True))
            )
            await db.commit()

    async def load_latest_checkpoint(self, trace_id: str) -> Optional[ExecutionCheckpoint]:
        async with self._get_conn() as db:
            async with db.execute(
                "SELECT data FROM checkpoints WHERE trace_id = ? ORDER BY sequence_number DESC LIMIT 1",
                (trace_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return ExecutionCheckpoint.model_validate(json.loads(row[0]))
        return None

    # --- Session Cache Operations ---

    async def put_session(self, session: Dict[str, Any]) -> None:
        """Persist a new session record."""
        async with self._get_conn() as db:
            await db.execute(
                """
                INSERT INTO sessions (
                    session_id, principal_id, capability_jti, capability_kid, 
                    expires_at, constraints_json, created_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session["session_id"],
                    session["principal_id"],
                    session["capability_jti"],
                    session["capability_kid"],
                    session["expires_at"],
                    session["constraints_json"],
                    session["created_at"],
                    session["last_seen_at"]
                )
            )
            await db.commit()

    async def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a session by ID."""
        async with self._get_conn() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return dict(row)
        return None

    async def touch_session(self, session_id: str, now: str) -> None:
        """Update last_seen_at synchronously."""
        async with self._get_conn() as db:
            await db.execute(
                "UPDATE sessions SET last_seen_at = ? WHERE session_id = ?",
                (now, session_id)
            )
            await db.commit()

    async def delete_expired_sessions(self, now: str) -> int:
        """Remove expired sessions."""
        async with self._get_conn() as db:
            cursor = await db.execute(
                "DELETE FROM sessions WHERE expires_at < ?", (now,)
            )
            await db.commit()
            return cursor.rowcount
