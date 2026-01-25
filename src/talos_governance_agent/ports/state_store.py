from typing import List, Optional, Protocol, runtime_checkable, Dict, Any
from talos_governance_agent.domain.models import (
    ExecutionLogEntry,
    ExecutionState,
    ExecutionCheckpoint,
)

@runtime_checkable
class TgaStateStore(Protocol):
    """
    Port protocol for TGA state persistence.
    Following Ports and Adapters, the domain logic depends on this interface.
    """
    
    async def acquire_trace_lock(self, trace_id: str) -> None:
        """Acquire single-writer lock for a trace."""
        ...
    
    async def release_trace_lock(self, trace_id: str) -> None:
        """Release single-writer lock for a trace."""
        ...
    
    async def load_state(self, trace_id: str) -> Optional[ExecutionState]:
        """Load current execution state for a trace."""
        ...
    
    async def append_log_entry(self, entry: ExecutionLogEntry) -> None:
        """Append a log entry with validation."""
        ...
    
    async def list_log_entries(
        self, 
        trace_id: str, 
        after_seq: int = 0
    ) -> List[ExecutionLogEntry]:
        """List log entries after a sequence number."""
        ...
    
    async def write_checkpoint(self, checkpoint: ExecutionCheckpoint) -> None:
        """Write a checkpoint for fast recovery."""
        ...
    
    async def load_latest_checkpoint(
        self, 
        trace_id: str
    ) -> Optional[ExecutionCheckpoint]:
        """Load the latest valid checkpoint."""
        ...

    async def put_session(self, session: Dict[str, Any]) -> None:
        """Persist a new session record."""
        ...

    async def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a session by ID."""
        ...

    async def touch_session(self, session_id: str, now: str) -> None:
        """Update last_seen_at synchronously."""
        ...

    async def delete_expired_sessions(self, now: str) -> int:
        """Remove expired sessions."""
        ...
