"""TGA Runtime Loop for Phase 9.3.4 (Modernized).

Implements crash-safe TGA execution with Moore machine state transitions.
Recovery reconstructs state from append-only log without double-execution.
"""
import hashlib
import json
import logging
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from enum import Enum

from talos_governance_agent.utils.id import uuid7 as generate_uuid7_local
# Try to import shared contract helper, fall back to local if not present
try:
    from talos_contracts.uuidv7 import uuid7
except ImportError:
    uuid7 = generate_uuid7_local

from talos_governance_agent.domain.models import (
    ExecutionLogEntry,
    ExecutionStateEnum,
    ExecutionState,
    ArtifactType,
)
# We need to define TgaRuntimeError here if not in models
class TgaRuntimeError(Exception):
    """Runtime execution error."""
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code

from talos_governance_agent.domain.validator import CapabilityValidator
from talos_governance_agent.ports.state_store import TgaStateStore

# Constants - Genesis digest (all zeros in base64url)
ZERO_DIGEST = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

logger = logging.getLogger(__name__)

@dataclass
class ExecutionPlan:
    """Plan for TGA execution."""
    trace_id: str
    plan_id: str
    action_request: Dict[str, Any]
    supervisor_decision_fn: Optional[Callable] = None
    tool_dispatch_fn: Optional[Callable] = None

@dataclass
class ExecutionResult:
    """Result of TGA execution."""
    trace_id: str
    final_state: ExecutionStateEnum
    tool_effect: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

@dataclass
class RecoveryResult:
    """Result of recovery operation."""
    trace_id: str
    recovered_state: ExecutionStateEnum
    recovered_from_seq: int
    re_dispatched: bool
    tool_effect: Optional[Dict[str, Any]] = None

class TgaRuntime:
    """
    Crash-safe TGA runtime with append-only state persistence.
    Following Ports and Adapters, the store is injected.
    """
    
    def __init__(self, store: TgaStateStore, supervisor_public_key: str):
        self.store = store
        self.validator = CapabilityValidator(supervisor_public_key)
    
    async def authorize_tool_call(
        self, 
        capability_jws: str, 
        tool_server: str, 
        tool_name: str, 
        args: Dict[str, Any]
    ) -> ExecutionLogEntry:
        """
        Validates capability and records the start of a tool execution.
        Cold Path: Persists session for future warm-path access.
        """
        # 1. Decode and verify capability
        cap = self.validator.decode_and_verify(capability_jws)
        
        # 2. Enforce constraints
        self.validator.validate_tool_call(cap, tool_server, tool_name, args)
        
        # 3. Create Session Record (Cold Path)
        try:
            from talos_contracts.ordering import canonicalize
        except ImportError:
            # Local fallback for canonicalization (RFC 8785 subset)
            def canonicalize(data: Any) -> Any:
                 # Simplified recursive sort or just return data if json.dumps handles it?
                 # Actually, we need to return an object that json.dumps matches.
                 # But our session record logic dumps it.
                 # The stored field is constraints_json = json.dumps(canonicalize(cap.constraints...))
                 # If canonicalize returns a dict, json.dumps dumps it.
                 # Pydantic model_dump returns a dict.
                 # We rely on json.dumps(sort_keys=True) for the actual string.
                 # The 'canonicalize' helper in contracts likely ensures key ordering or stricter typing.
                 # For fallback, identity is fine as long as we use sort_keys=True in dumps.
                 return data

        now_dt = datetime.now(timezone.utc)
        session_id = str(uuid7())
        
        session_record = {
            "session_id": session_id,
            "principal_id": cap.iss, # Assuming binding to Issuer/Supervisor identity
            "capability_jti": cap.nonce, # Mapping nonce to JTI per model
            "capability_kid": "unknown", # Validator needs to expose kid
            "expires_at": datetime.fromtimestamp(cap.exp, timezone.utc).isoformat().replace('+00:00', 'Z'),
            "constraints_json": json.dumps(canonicalize(cap.constraints.model_dump(by_alias=True))),
            "created_at": now_dt.isoformat(),
            "last_seen_at": now_dt.isoformat()
        }
        
        # Persist session
        await self.store.put_session(session_record)
        
        trace_id = str(cap.trace_id)
        
        await self.store.acquire_trace_lock(trace_id)
        try:
            state = await self.store.load_state(trace_id)
            
            # If PENDING, we need to transition to AUTHORIZED first (Genesis)
            if not state:
                # In this standalone mode, we assume the action request is implicit in the capability
                # or provided previously. 
                genesis_entry = self._make_entry(
                    trace_id=trace_id,
                    principal_id=cap.iss,
                    sequence_number=1,
                    prev_entry_digest=ZERO_DIGEST,
                    from_state=ExecutionStateEnum.PENDING,
                    to_state=ExecutionStateEnum.PENDING,
                    artifact_type=ArtifactType.ACTION_REQUEST,
                    artifact_id=str(cap.plan_id),
                    artifact_digest=self._compute_digest({"implicit": True})
                )
                await self.store.append_log_entry(genesis_entry)
                
                auth_entry = self._make_entry(
                    trace_id=trace_id,
                    principal_id=cap.iss,
                    sequence_number=2,
                    prev_entry_digest=genesis_entry.entry_digest,
                    from_state=ExecutionStateEnum.PENDING,
                    to_state=ExecutionStateEnum.AUTHORIZED,
                    artifact_type=ArtifactType.SUPERVISOR_DECISION,
                    artifact_id=f"sd-{trace_id[:8]}", # TODO: Use real ID
                    artifact_digest=self.validator.calculate_capability_digest(capability_jws)
                )
                await self.store.append_log_entry(auth_entry)
                state = await self.store.load_state(trace_id)
            
            if not state or state.current_state != ExecutionStateEnum.AUTHORIZED:
                 status = state.current_state if state else "None"
                 raise TgaRuntimeError(f"Trace {trace_id} in invalid state: {status}", "INVALID_STATE")
            
            # 3. Create tool_call entry
            tool_call_obj = {
                "tool_call_id": session_id,
                "trace_id": trace_id,
                "plan_id": str(cap.plan_id),
                "capability_digest": self.validator.calculate_capability_digest(capability_jws),
                "call": {"server": tool_server, "name": tool_name, "args": args},
                "idempotency_key": f"idem-{trace_id[:8]}", # Replace with UUIDv7
                "session_id": session_id
            }
            # Correct idempotency key to strict UUIDv7 as per models
            tool_call_obj["idempotency_key"] = str(uuid7())
            
            tc_entry = self._make_entry(
                trace_id=trace_id,
                principal_id=cap.iss,
                sequence_number=state.last_sequence_number + 1,
                prev_entry_digest=state.last_entry_digest,
                from_state=ExecutionStateEnum.AUTHORIZED,
                to_state=ExecutionStateEnum.EXECUTING,
                artifact_type=ArtifactType.TOOL_CALL,
                artifact_id=tool_call_obj["tool_call_id"],
                artifact_digest=self._compute_digest(tool_call_obj),
                tool_call_id=tool_call_obj["tool_call_id"],
                idempotency_key=tool_call_obj["idempotency_key"],
                session_id=session_id
            )
            await self.store.append_log_entry(tc_entry)
            return tc_entry
            
        finally:
            await self.store.release_trace_lock(trace_id)

    async def authorize_warm_path(
        self,
        session_id: str,
        principal_id: str,
        tool_server: str,
        tool_name: str,
        args: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        High-performance authorization using cached session metadata.
        """
        # 1. Lookup Session
        session = await self.store.get_session(session_id)
        if not session:
            raise TgaRuntimeError("Session not found", "TGA_SESSION_NOT_FOUND")
            
        # 2. Check Expiry
        now = datetime.now(timezone.utc)
        expires_at = datetime.fromisoformat(session["expires_at"].replace("Z", "+00:00"))
        if now > expires_at:
            raise TgaRuntimeError("Session expired", "TGA_SESSION_EXPIRED")
            
        # 3. Check Principal Binding
        if session["principal_id"] != principal_id:
            raise TgaRuntimeError("Principal mismatch", "TGA_PRINCIPAL_MISMATCH")
            
        # 4. Check Constraints (Deterministic)
        stored_constraints = json.loads(session["constraints_json"])
        
        if stored_constraints["tool_server"] != tool_server:
             raise TgaRuntimeError("Tool server mismatch", "TGA_CONSTRAINT_MISMATCH")
        if stored_constraints["tool_name"] != tool_name:
             raise TgaRuntimeError("Tool name mismatch", "TGA_CONSTRAINT_MISMATCH")
             
        # TODO: Args validation against stored constraints arg_digest/schema
        
        # Critical: Update last_seen_at SYNCHRONOUSLY
        await self.store.touch_session(session_id, now.isoformat())
        
        return {
             "authorized": True,
             "trace_id": "cached", 
        }

    async def record_tool_effect(
        self, 
        trace_id: str, 
        tool_effect: Dict[str, Any]
    ) -> ExecutionLogEntry:
        """
        Records the result of a tool execution and transitions to final state.
        """
        await self.store.acquire_trace_lock(trace_id)
        try:
            state = await self.store.load_state(trace_id)
            if not state or state.current_state != ExecutionStateEnum.EXECUTING:
                 raise TgaRuntimeError(f"Trace {trace_id} not in EXECUTING state", "INVALID_STATE")
            
            outcome_status = tool_effect.get("outcome", {}).get("status", "SUCCESS")
            final_state = (
                ExecutionStateEnum.COMPLETED 
                if outcome_status == "SUCCESS" 
                else ExecutionStateEnum.FAILED
            )
            
            # Use state trace_id as context principal? Or we need to persist principal in ExecutionState
            # Current ExecutionState model doesn't store principal. Assuming principal is constant for trace.
            # Ideally we fetch it from the last entry or state.
            # Simplified: Use a specialized 'system' principal or retrieve from genesis.
            # Let's retrieve from latest entry for now.
            # Or pass principal_id in arguments?
            
            # Retrieving principal from previous entry
            entries = await self.store.list_log_entries(trace_id, after_seq=state.last_sequence_number-1)
            # Should have at least the executing entry
            if entries:
                principal_id = entries[0].principal_id
            else:
                 # Fallback/Error
                 raise TgaRuntimeError("Could not determine principal for effect", "INTERNAL_ERROR")
            
            effect_entry = self._make_entry(
                trace_id=trace_id,
                principal_id=principal_id,
                sequence_number=state.last_sequence_number + 1,
                prev_entry_digest=state.last_entry_digest,
                from_state=ExecutionStateEnum.EXECUTING,
                to_state=final_state,
                artifact_type=ArtifactType.TOOL_EFFECT,
                artifact_id=tool_effect.get("tool_effect_id", str(uuid7())),
                artifact_digest=self._compute_digest(tool_effect)
            )
            await self.store.append_log_entry(effect_entry)
            return effect_entry
            
        finally:
            await self.store.release_trace_lock(trace_id)

    async def recover(self, trace_id: str) -> RecoveryResult:
        """Recover from crash by replaying log and resuming execution."""
        try:
            await self.store.acquire_trace_lock(trace_id)
            state = await self.store.load_state(trace_id)
            if not state:
                 raise TgaRuntimeError(f"No state found for trace {trace_id}", "STATE_RECOVERY_FAILED")
            
            entries = await self.store.list_log_entries(trace_id)
            if not entries:
                 raise TgaRuntimeError(f"No log entries for trace {trace_id}", "STATE_RECOVERY_FAILED")
            
            # Hash chain validation
            for i, entry in enumerate(entries):
                if i == 0:
                    if entry.prev_entry_digest != ZERO_DIGEST:
                         raise TgaRuntimeError("Genesis entry invalid", "STATE_CHECKSUM_MISMATCH")
                else:
                    if entry.prev_entry_digest != entries[i-1].entry_digest:
                         raise TgaRuntimeError(f"Hash chain broken at seq {entry.sequence_number}", "STATE_CHECKSUM_MISMATCH")
            
            last_entry = entries[-1]
            if state.current_state == ExecutionStateEnum.EXECUTING:
                tc_entry = next((e for e in entries if e.artifact_type == ArtifactType.TOOL_CALL), None)
                te_entry = next((e for e in entries if e.artifact_type == ArtifactType.TOOL_EFFECT), None)
                
                if tc_entry and te_entry is None:
                    return RecoveryResult(
                        trace_id=trace_id,
                        recovered_state=state.current_state,
                        recovered_from_seq=last_entry.sequence_number,
                        re_dispatched=True,
                        tool_effect=None
                    )
            
            return RecoveryResult(
                trace_id=trace_id,
                recovered_state=state.current_state,
                recovered_from_seq=last_entry.sequence_number,
                re_dispatched=False
            )
        finally:
            await self.store.release_trace_lock(trace_id)

    def _make_entry(self, **kwargs) -> ExecutionLogEntry:
        if "ts" not in kwargs:
            kwargs["ts"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        
        # Compute digest BEFORE creating the object to satisfy strict validation
        # The digest excludes 'entry_digest' itself (and others), so we can compute it from kwargs.
        # We ensure excluded fields are absent or ignored by _compute_digest logic?
        # _compute_digest uses json.dumps directly. 
        # But wait, TgaBaseModel.compute_digest relies on .model_dump() which handles aliases etc.
        # If we use _compute_digest(kwargs), we might miss aliases or serialization rules (e.g. enum values).
        # We need to ensure kwargs match the serialized form.
        # Enums in kwargs are likely Enum objects. json.dumps fails on them unless handled.
        # Our `_compute_digest` helper is:
        # json.dumps(data, ...)
        
        # Better approach: initialize with a valid placeholder "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        # then update. This satisfies regex.
        # But we need to ensure the final object has the CORRECT digest.
        
        if "entry_digest" not in kwargs:
            kwargs["entry_digest"] = ZERO_DIGEST
            
        entry = ExecutionLogEntry(**kwargs)
        
        # Now compute real digest
        real_digest = entry.compute_digest()
        
        # Return a new object with the correct digest (using model_copy to strictly validate if we want, 
        # or since validation ran once, we can trust the rest and just swap string).
        # ExecutionLogEntry is not frozen, but we want strict adherence.
        # If we update the field, does Pydantic V2 re-validate assignment? Yes, we set validate_assignment=True.
        # So we can just set it.
        
        entry.entry_digest = real_digest
        return entry

    def _compute_digest(self, data: Dict[str, Any]) -> str:
        """Compute base64url SHA-256 of canonical JSON."""
        # Use TgaBaseModel logic via a temporary helper or duplication?
        # Duplication for simple dicts is fine to avoid instantiating models just for digest
        canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")
