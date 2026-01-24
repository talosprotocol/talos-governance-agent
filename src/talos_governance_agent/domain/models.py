from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from uuid import UUID
from enum import Enum
from dataclasses import dataclass, asdict
import hashlib
import json

class ExecutionStateEnum(str, Enum):
    PENDING = "PENDING"
    AUTHORIZED = "AUTHORIZED"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    DENIED = "DENIED"

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

class TgaCapabilityConstraints(BaseModel):
    """Deep constraints enforced at the Gateway for TGA tool calls."""
    tool_server: str = Field(..., description="Target tool server (e.g. mcp-github)")
    tool_name: str = Field(..., description="Target tool name (e.g. create-pr)")
    target_allowlist: List[str] = Field(..., description="List of allowed repos, paths, or service IDs")
    arg_constraints: Optional[str] = Field(None, description="SHA-256 digest of the JSON Schema fragment for args")
    read_only: bool = Field(False, description="If True, deny any mutation tools")

class TgaCapability(BaseModel):
    iss: str = Field(..., description="Issuer (Supervisor identity/DID)")
    aud: str = Field("talos-gateway", description="Audience (must be talos-gateway)")
    iat: int = Field(..., description="Issued at (Unix epoch)")
    nbf: Optional[int] = Field(None, description="Not before (Unix epoch)")
    exp: int = Field(..., description="Expiration (Unix epoch)")
    nonce: str = Field(..., description="Unique nonce for replay protection")
    trace_id: UUID = Field(..., description="The shared trace identifier")
    plan_id: UUID = Field(..., description="The planned goal identifier")
    constraints: TgaCapabilityConstraints = Field(..., description="Deep execution constraints")
