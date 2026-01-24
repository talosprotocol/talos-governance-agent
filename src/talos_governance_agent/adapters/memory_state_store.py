import asyncio
import hashlib
import json
from typing import Any, Dict, List, Optional
from talos_governance_agent.domain.models import (
    ExecutionLogEntry,
    ExecutionState,
    ExecutionStateEnum,
    ExecutionCheckpoint,
)
from talos_governance_agent.ports.state_store import TgaStateStore

ALLOWED_TRANSITIONS = {
    (ExecutionStateEnum.PENDING, ExecutionStateEnum.AUTHORIZED),
    (ExecutionStateEnum.PENDING, ExecutionStateEnum.DENIED),
    (ExecutionStateEnum.AUTHORIZED, ExecutionStateEnum.EXECUTING),
    (ExecutionStateEnum.EXECUTING, ExecutionStateEnum.COMPLETED),
    (ExecutionStateEnum.EXECUTING, ExecutionStateEnum.FAILED),
}

ZERO_DIGEST = "0" * 64

class MemoryStateStore:
    """
    In-memory adapter for TGA state persistence.
    Used for development and testing.
    """
    
    def __init__(self) -> None:
        self._log_entries: Dict[str, List[ExecutionLogEntry]] = {}
        self._checkpoints: Dict[str, List[ExecutionCheckpoint]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._states: Dict[str, ExecutionState] = {}
    
    async def acquire_trace_lock(self, trace_id: str) -> None:
        if trace_id not in self._locks:
            self._locks[trace_id] = asyncio.Lock()
        await self._locks[trace_id].acquire()
    
    async def release_trace_lock(self, trace_id: str) -> None:
        if trace_id in self._locks and self._locks[trace_id].locked():
            self._locks[trace_id].release()
    
    async def load_state(self, trace_id: str) -> Optional[ExecutionState]:
        return self._states.get(trace_id)
    
    async def append_log_entry(self, entry: ExecutionLogEntry) -> None:
        trace_id = entry.trace_id
        if trace_id not in self._log_entries:
            self._log_entries[trace_id] = []
        
        entries = self._log_entries[trace_id]
        expected_seq = len(entries) + 1
        if entry.sequence_number != expected_seq:
            raise ValueError(f"Sequence gap: expected {expected_seq}, got {entry.sequence_number}")
        
        if entries:
            if entry.prev_entry_digest != entries[-1].entry_digest:
                raise ValueError("Hash chain broken")
        elif entry.prev_entry_digest != ZERO_DIGEST:
             raise ValueError("Genesis entry invalid")
             
        if not (entry.from_state == ExecutionStateEnum.PENDING and entry.to_state == ExecutionStateEnum.PENDING):
            if (entry.from_state, entry.to_state) not in ALLOWED_TRANSITIONS:
                raise ValueError("Invalid transition")
        
        if entry.entry_digest != entry.compute_digest():
            raise ValueError("Entry digest mismatch")
            
        entries.append(entry)
        await self._update_state(trace_id, entry)
    
    async def _update_state(self, trace_id: str, entry: ExecutionLogEntry) -> None:
        state = self._states.get(trace_id)
        if state is None:
            state = ExecutionState(
                schema_id="talos.tga.execution_state",
                schema_version="v1",
                trace_id=trace_id,
                plan_id=entry.artifact_id,
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

    async def list_log_entries(self, trace_id: str, after_seq: int = 0) -> List[ExecutionLogEntry]:
        entries = self._log_entries.get(trace_id, [])
        return [e for e in entries if e.sequence_number > after_seq]

    async def write_checkpoint(self, checkpoint: ExecutionCheckpoint) -> None:
        trace_id = checkpoint.trace_id
        if trace_id not in self._checkpoints:
            self._checkpoints[trace_id] = []
        self._checkpoints[trace_id].append(checkpoint)

    async def load_latest_checkpoint(self, trace_id: str) -> Optional[ExecutionCheckpoint]:
        checkpoints = self._checkpoints.get(trace_id, [])
        return checkpoints[-1] if checkpoints else None
