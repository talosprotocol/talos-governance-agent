from pydantic import BaseModel, Field, ConfigDict, field_validator
from typing import List, Optional, Dict, Any, Annotated
from uuid import UUID
from enum import Enum
import hashlib
import json
import base64
import re

# Strict Validation Patterns
# UUIDv7: 8 chars, 4 chars, 4 chars (started 7), 4 chars (variant 89ab), 12 chars
UUIDV7_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
# Base64Url: Alphanumeric + -_ without padding =
BASE64URL_PATTERN = r"^[A-Za-z0-9_-]+$"

class ExecutionStateEnum(str, Enum):
    PENDING = "PENDING"
    AUTHORIZED = "AUTHORIZED"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    DENIED = "DENIED"

class ArtifactType(str, Enum):
    ACTION_REQUEST = "action_request"
    SUPERVISOR_DECISION = "supervisor_decision"
    TOOL_CALL = "tool_call"
    TOOL_EFFECT = "tool_effect"

class TgaBaseModel(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        validate_assignment=True,
        arbitrary_types_allowed=False, # Enforce strict serialization types
        extra="forbid" # No unknown fields allowed for strict schema compliance
    )

    def compute_digest(self, exclude_fields: Optional[List[str]] = None) -> str:
        """
        Compute base64url SHA-256 digest from canonical JSON preimage.
        Following RFC 8785 for canonicalization.
        """
        if exclude_fields is None:
            # Standard exclusions for self-referential digests
            exclude_fields = ["entry_digest", "state_digest", "checkpoint_digest", "digest"]
        
        preimage = self.model_dump(mode='json', by_alias=True, exclude_none=True)
        # Manually filter out exclusions
        for field in exclude_fields:
            if field in preimage:
                del preimage[field]
        # Remove internal underscore fields
        keys_to_remove = [k for k in preimage.keys() if k.startswith("_")]
        for k in keys_to_remove:
            del preimage[k]
        
        # Canonical JSON: strict sorting, no whitespace separators
        canonical = json.dumps(preimage, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).digest()
        # base64url without padding
        return base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")

class ExecutionLogEntry(TgaBaseModel):
    """Append-only log entry with hash-chain integrity."""
    schema_id: str = "talos.tga.execution_log_entry"
    schema_version: str = "v1"
    trace_id: Annotated[str, Field(pattern=UUIDV7_PATTERN)]
    principal_id: Annotated[str, Field(pattern=UUIDV7_PATTERN)] # Using str for strict schema matching
    sequence_number: int = Field(ge=1)
    prev_entry_digest: Annotated[str, Field(pattern=BASE64URL_PATTERN)]
    entry_digest: Annotated[str, Field(pattern=BASE64URL_PATTERN)]
    ts: str # ISO8601 strict
    from_state: ExecutionStateEnum
    to_state: ExecutionStateEnum
    artifact_type: ArtifactType
    artifact_id: str
    artifact_digest: Annotated[str, Field(pattern=BASE64URL_PATTERN)]
    
    # Optional context fields
    tool_call_id: Optional[str] = None
    idempotency_key: Optional[Annotated[str, Field(pattern=UUIDV7_PATTERN)]] = None
    session_id: Optional[Annotated[str, Field(pattern=UUIDV7_PATTERN)]] = None
    
    # Internal metadata
    digest_alg: str = Field(default="sha256", alias="_digest_alg")

class ExecutionState(TgaBaseModel):
    """Derived view of current execution state."""
    schema_id: str = "talos.tga.execution_state"
    schema_version: str = "v1"
    trace_id: Annotated[str, Field(pattern=UUIDV7_PATTERN)]
    plan_id: Annotated[str, Field(pattern=UUIDV7_PATTERN)]
    current_state: ExecutionStateEnum
    last_sequence_number: int = Field(ge=0)
    last_entry_digest: Annotated[str, Field(pattern=BASE64URL_PATTERN)]
    state_digest: Annotated[str, Field(pattern=BASE64URL_PATTERN)]

class ExecutionCheckpoint(TgaBaseModel):
    """Checkpoint for fast recovery."""
    schema_id: str = "talos.tga.execution_checkpoint"
    schema_version: str = "v1"
    trace_id: Annotated[str, Field(pattern=UUIDV7_PATTERN)]
    checkpoint_sequence_number: int = Field(ge=1)
    checkpoint_state: Dict[str, Any]
    checkpoint_digest: Annotated[str, Field(pattern=BASE64URL_PATTERN)]
    ts: str

class TgaCapabilityConstraints(BaseModel):
    """Deep constraints enforced at the Gateway."""
    model_config = ConfigDict(extra="forbid")
    
    tool_server: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    target_allowlist: List[str]
    # base64url digest of args schema
    arg_constraints_digest: Optional[Annotated[str, Field(pattern=BASE64URL_PATTERN)]] = Field(None, alias="arg_constraints")
    read_only: bool = False

class TgaCapability(BaseModel):
    """Signed capability token structure."""
    model_config = ConfigDict(extra="forbid")
    
    iss: str
    aud: str = "talos-gateway"
    iat: int
    nbf: Optional[int] = None
    exp: int
    nonce: str
    trace_id: UUID # Keep as UUID object for internal use, serializer handles stringify?
    plan_id: UUID
    constraints: TgaCapabilityConstraints
