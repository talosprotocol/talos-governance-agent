"""TGA State Store - Append-only persistence layer for Phase 9.3.

This module implements crash-safe state persistence with:
- Append-only log entries with hash-chain integrity
- Checkpoints for fast recovery
- Single-writer locking per trace_id
"""
import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
import uuid

logger = logging.getLogger(__name__)


class ExecutionStateEnum(str, Enum):
    PENDING = "PENDING"
    AUTHORIZED = "AUTHORIZED"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    DENIED = "DENIED"


class StateStoreError(Exception):
    """Base exception for state store errors."""
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


# Allowed transitions (Moore machine)
ALLOWED_TRANSITIONS = {
    (ExecutionStateEnum.PENDING, ExecutionStateEnum.AUTHORIZED),
    (ExecutionStateEnum.PENDING, ExecutionStateEnum.DENIED),
    (ExecutionStateEnum.AUTHORIZED, ExecutionStateEnum.EXECUTING),
    (ExecutionStateEnum.EXECUTING, ExecutionStateEnum.COMPLETED),
    (ExecutionStateEnum.EXECUTING, ExecutionStateEnum.FAILED),
}

ZERO_DIGEST = "0" * 64


@dataclass
class ExecutionLogEntry:
    """Append-only log entry with hash-chain integrity."""
    schema_id: str
    schema_version: str
    trace_id: str
    sequence_number: int
    prev_entry_digest: str
    entry_digest: str
    ts: str
    from_state: ExecutionStateEnum
    to_state: ExecutionStateEnum
    artifact_type: str
    artifact_id: str
    artifact_digest: str
    tool_call_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    
    def compute_digest(self) -> str:
        """Compute entry_digest from canonical JSON preimage."""
        preimage = {k: v for k, v in asdict(self).items() if k != "entry_digest"}
        # Convert enums to strings for JSON
        preimage["from_state"] = self.from_state.value if isinstance(self.from_state, Enum) else self.from_state
        preimage["to_state"] = self.to_state.value if isinstance(self.to_state, Enum) else self.to_state
        canonical = json.dumps(preimage, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class ExecutionState:
    """Derived view of current execution state."""
    schema_id: str
    schema_version: str
    trace_id: str
    plan_id: str
    current_state: ExecutionStateEnum
    last_sequence_number: int
    last_entry_digest: str
    state_digest: str
    
    def compute_digest(self) -> str:
        """Compute state_digest from canonical JSON preimage."""
        preimage = {k: v for k, v in asdict(self).items() if k != "state_digest"}
        preimage["current_state"] = self.current_state.value if isinstance(self.current_state, Enum) else self.current_state
        canonical = json.dumps(preimage, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class ExecutionCheckpoint:
    """Checkpoint for fast recovery."""
    schema_id: str
    schema_version: str
    trace_id: str
    checkpoint_sequence_number: int
    checkpoint_state: Dict[str, Any]
    checkpoint_digest: str
    ts: str


class TgaStateStore:
    """
    Append-only state store for TGA execution.
    
    Security invariants:
    - Log entries are never overwritten
    - Hash-chain ensures integrity
    - Single-writer locking prevents concurrent execution
    """
    
    def __init__(self):
        # In-memory storage for development (replace with SQLite/Postgres in production)
        self._log_entries: Dict[str, List[ExecutionLogEntry]] = {}
        self._checkpoints: Dict[str, List[ExecutionCheckpoint]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._states: Dict[str, ExecutionState] = {}
    
    async def acquire_trace_lock(self, trace_id: str) -> None:
        """Acquire single-writer lock for a trace."""
        if trace_id not in self._locks:
            self._locks[trace_id] = asyncio.Lock()
        
        acquired = self._locks[trace_id].locked()
        if acquired:
            raise StateStoreError(
                f"Lock already held for trace {trace_id}",
                "STATE_LOCK_ACQUIRE_FAILED"
            )
        await self._locks[trace_id].acquire()
    
    async def release_trace_lock(self, trace_id: str) -> None:
        """Release single-writer lock for a trace."""
        if trace_id in self._locks and self._locks[trace_id].locked():
            self._locks[trace_id].release()
    
    async def load_state(self, trace_id: str) -> Optional[ExecutionState]:
        """Load current execution state for a trace."""
        return self._states.get(trace_id)
    
    async def append_log_entry(self, entry: ExecutionLogEntry) -> None:
        """
        Append a log entry with validation.
        
        Raises:
            StateStoreError: If transition invalid or sequence violated
        """
        trace_id = entry.trace_id
        
        # Initialize log if needed
        if trace_id not in self._log_entries:
            self._log_entries[trace_id] = []
        
        entries = self._log_entries[trace_id]
        
        # Validate sequence number
        expected_seq = len(entries) + 1
        if entry.sequence_number != expected_seq:
            raise StateStoreError(
                f"Sequence gap: expected {expected_seq}, got {entry.sequence_number}",
                "STATE_SEQUENCE_GAP"
            )
        
        # Validate prev_entry_digest (genesis uses zeros)
        if entries:
            if entry.prev_entry_digest != entries[-1].entry_digest:
                raise StateStoreError(
                    f"Hash chain broken at sequence {entry.sequence_number}",
                    "STATE_CHECKSUM_MISMATCH"
                )
        else:
            if entry.prev_entry_digest != ZERO_DIGEST:
                raise StateStoreError(
                    "Genesis entry must have zero prev_entry_digest",
                    "STATE_CHECKSUM_MISMATCH"
                )
        
        # Validate transition (skip for genesis PENDING->PENDING)
        if not (entry.from_state == ExecutionStateEnum.PENDING and entry.to_state == ExecutionStateEnum.PENDING):
            transition = (entry.from_state, entry.to_state)
            if transition not in ALLOWED_TRANSITIONS:
                raise StateStoreError(
                    f"Invalid transition: {entry.from_state} -> {entry.to_state}",
                    "STATE_INVALID_TRANSITION"
                )
        
        # Validate entry digest
        computed = entry.compute_digest()
        if entry.entry_digest != computed:
            raise StateStoreError(
                f"Entry digest mismatch: expected {computed}",
                "STATE_CHECKSUM_MISMATCH"
            )
        
        # Append to log
        entries.append(entry)
        
        # Update derived state
        await self._update_state(trace_id, entry)
    
    async def _update_state(self, trace_id: str, entry: ExecutionLogEntry) -> None:
        """Update derived state after log append."""
        state = self._states.get(trace_id)
        
        if state is None:
            # Create initial state
            state = ExecutionState(
                schema_id="talos.tga.execution_state",
                schema_version="v1",
                trace_id=trace_id,
                plan_id=entry.artifact_id,  # First artifact is the plan
                current_state=entry.to_state,
                last_sequence_number=entry.sequence_number,
                last_entry_digest=entry.entry_digest,
                state_digest=""
            )
        else:
            state.current_state = entry.to_state
            state.last_sequence_number = entry.sequence_number
            state.last_entry_digest = entry.entry_digest
        
        state.state_digest = state.compute_digest()
        self._states[trace_id] = state
    
    async def list_log_entries(
        self, 
        trace_id: str, 
        after_seq: int = 0
    ) -> List[ExecutionLogEntry]:
        """List log entries after a sequence number."""
        entries = self._log_entries.get(trace_id, [])
        return [e for e in entries if e.sequence_number > after_seq]
    
    async def write_checkpoint(self, checkpoint: ExecutionCheckpoint) -> None:
        """Write a checkpoint for fast recovery."""
        trace_id = checkpoint.trace_id
        
        if trace_id not in self._checkpoints:
            self._checkpoints[trace_id] = []
        
        self._checkpoints[trace_id].append(checkpoint)
    
    async def load_latest_checkpoint(
        self, 
        trace_id: str
    ) -> Optional[ExecutionCheckpoint]:
        """Load the latest valid checkpoint."""
        checkpoints = self._checkpoints.get(trace_id, [])
        if not checkpoints:
            return None
        
        # Return most recent
        return checkpoints[-1]
    
    async def validate_checkpoint(self, checkpoint: ExecutionCheckpoint) -> bool:
        """Validate checkpoint digest."""
        canonical = json.dumps(
            checkpoint.checkpoint_state, 
            sort_keys=True, 
            separators=(",", ":")
        )
        computed = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return computed == checkpoint.checkpoint_digest


# Singleton instance
_store_instance: Optional[TgaStateStore] = None


def get_state_store() -> TgaStateStore:
    """Get or create the state store singleton."""
    global _store_instance
    if _store_instance is None:
        _store_instance = TgaStateStore()
    return _store_instance
