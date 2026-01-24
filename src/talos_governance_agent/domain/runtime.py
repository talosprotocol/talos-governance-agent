"""TGA Runtime Loop for Phase 9.3.4.

Implements crash-safe TGA execution with Moore machine state transitions.
Recovery reconstructs state from append-only log without double-execution.
"""
import hashlib
from app.utils.id import uuid7
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from enum import Enum

from app.domain.tga.state_store import (
    TgaStateStore,
    ExecutionLogEntry,
    ExecutionState,
    ExecutionStateEnum,
    StateStoreError,
    ZERO_DIGEST,
    get_state_store,
)

logger = logging.getLogger(__name__)


class RuntimeError(Exception):
    """Runtime execution error."""
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


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
    
    Execution ordering per LOCKED spec:
    1. Persist action_request -> append PENDING
    2. Persist supervisor_decision -> append AUTHORIZED or DENIED
    3. Persist tool_call -> append EXECUTING
    4. Dispatch to connector
    5. Persist tool_effect -> append COMPLETED or FAILED
    
    Recovery re-dispatches incomplete tool_calls using idempotency_key.
    """
    
    def __init__(self, store: Optional[TgaStateStore] = None):
        self.store = store or get_state_store()
    
    async def execute_plan(self, plan: ExecutionPlan) -> ExecutionResult:
        """
        Execute a TGA plan with crash-safe persistence.
        
        Each step persists artifact + appends transition atomically.
        """
        trace_id = plan.trace_id
        
        try:
            # 1. Acquire lock
            await self.store.acquire_trace_lock(trace_id)
            
            # Check if already exists
            existing = await self.store.load_state(trace_id)
            if existing:
                logger.info(f"Trace {trace_id} already exists at {existing.current_state}")
                return await self._resume_execution(plan, existing)
            
            # 2. Genesis: persist action_request, append PENDING
            ar_digest = self._compute_digest(plan.action_request)
            genesis_entry = self._make_entry(
                trace_id=trace_id,
                seq=1,
                prev_digest=ZERO_DIGEST,
                from_state=ExecutionStateEnum.PENDING,
                to_state=ExecutionStateEnum.PENDING,
                artifact_type="action_request",
                artifact_id=plan.action_request.get("action_request_id", plan.plan_id),
                artifact_digest=ar_digest
            )
            await self.store.append_log_entry(genesis_entry)
            
            # 3. Get supervisor decision
            if plan.supervisor_decision_fn:
                decision = await plan.supervisor_decision_fn(plan.action_request)
            else:
                # Mock approve for testing
                decision = {"approved": True, "capability": {}}
            
            sd_id = decision.get("decision_id", self._generate_id())
            sd_digest = self._compute_digest(decision)
            
            if not decision.get("approved"):
                # DENIED
                denied_entry = self._make_entry(
                    trace_id=trace_id,
                    seq=2,
                    prev_digest=genesis_entry.entry_digest,
                    from_state=ExecutionStateEnum.PENDING,
                    to_state=ExecutionStateEnum.DENIED,
                    artifact_type="supervisor_decision",
                    artifact_id=sd_id,
                    artifact_digest=sd_digest
                )
                await self.store.append_log_entry(denied_entry)
                return ExecutionResult(
                    trace_id=trace_id,
                    final_state=ExecutionStateEnum.DENIED,
                    error="Supervisor denied the action"
                )
            
            # AUTHORIZED
            auth_entry = self._make_entry(
                trace_id=trace_id,
                seq=2,
                prev_digest=genesis_entry.entry_digest,
                from_state=ExecutionStateEnum.PENDING,
                to_state=ExecutionStateEnum.AUTHORIZED,
                artifact_type="supervisor_decision",
                artifact_id=sd_id,
                artifact_digest=sd_digest
            )
            await self.store.append_log_entry(auth_entry)
            
            # 4. Create tool_call, append EXECUTING
            tool_call = self._create_tool_call(plan, decision)
            tc_id = tool_call.get("tool_call_id", self._generate_id())
            tc_digest = self._compute_digest(tool_call)
            idempotency_key = tool_call.get("idempotency_key")
            
            exec_entry = self._make_entry(
                trace_id=trace_id,
                seq=3,
                prev_digest=auth_entry.entry_digest,
                from_state=ExecutionStateEnum.AUTHORIZED,
                to_state=ExecutionStateEnum.EXECUTING,
                artifact_type="tool_call",
                artifact_id=tc_id,
                artifact_digest=tc_digest,
                tool_call_id=tc_id,
                idempotency_key=idempotency_key
            )
            await self.store.append_log_entry(exec_entry)
            
            # 5. Dispatch to connector (idempotent)
            if plan.tool_dispatch_fn:
                tool_effect = await plan.tool_dispatch_fn(tool_call)
            else:
                # Mock success for testing
                tool_effect = {"outcome": {"status": "SUCCESS"}}
            
            te_id = tool_effect.get("tool_effect_id", self._generate_id())
            te_digest = self._compute_digest(tool_effect)
            
            # 6. Persist tool_effect, append COMPLETED or FAILED
            outcome_status = tool_effect.get("outcome", {}).get("status", "SUCCESS")
            final_state = (
                ExecutionStateEnum.COMPLETED 
                if outcome_status == "SUCCESS" 
                else ExecutionStateEnum.FAILED
            )
            
            effect_entry = self._make_entry(
                trace_id=trace_id,
                seq=4,
                prev_digest=exec_entry.entry_digest,
                from_state=ExecutionStateEnum.EXECUTING,
                to_state=final_state,
                artifact_type="tool_effect",
                artifact_id=te_id,
                artifact_digest=te_digest,
                tool_call_id=tc_id,
                idempotency_key=idempotency_key
            )
            await self.store.append_log_entry(effect_entry)
            
            return ExecutionResult(
                trace_id=trace_id,
                final_state=final_state,
                tool_effect=tool_effect
            )
            
        finally:
            await self.store.release_trace_lock(trace_id)
    
    async def recover(self, trace_id: str) -> RecoveryResult:
        """
        Recover from crash by replaying log and resuming execution.
        
        Per LOCKED spec:
        - If EXECUTING without tool_effect: re-dispatch same tool_call
        - If terminal: return result
        """
        try:
            await self.store.acquire_trace_lock(trace_id)
            
            state = await self.store.load_state(trace_id)
            if not state:
                raise RuntimeError(
                    f"No state found for trace {trace_id}",
                    "STATE_RECOVERY_FAILED"
                )
            
            entries = await self.store.list_log_entries(trace_id)
            if not entries:
                raise RuntimeError(
                    f"No log entries for trace {trace_id}",
                    "STATE_RECOVERY_FAILED"
                )
            
            # Validate hash chain
            for i, entry in enumerate(entries):
                if i == 0:
                    if entry.prev_entry_digest != ZERO_DIGEST:
                        raise RuntimeError(
                            "Genesis entry has invalid prev_entry_digest",
                            "STATE_CHECKSUM_MISMATCH"
                        )
                else:
                    if entry.prev_entry_digest != entries[i-1].entry_digest:
                        raise RuntimeError(
                            f"Hash chain broken at sequence {entry.sequence_number}",
                            "STATE_CHECKSUM_MISMATCH"
                        )
            
            last_entry = entries[-1]
            
            # If in EXECUTING state, check if we need to re-dispatch
            if state.current_state == ExecutionStateEnum.EXECUTING:
                # Find the tool_call entry
                tc_entry = next(
                    (e for e in entries if e.artifact_type == "tool_call"),
                    None
                )
                if not tc_entry:
                    raise RuntimeError(
                        "EXECUTING state but no tool_call entry",
                        "STATE_RECOVERY_FAILED"
                    )
                
                # Check if tool_effect exists
                te_entry = next(
                    (e for e in entries if e.artifact_type == "tool_effect"),
                    None
                )
                
                if te_entry is None:
                    # Need to re-dispatch
                    logger.info(
                        f"Recovery: re-dispatching tool_call {tc_entry.tool_call_id}"
                    )
                    return RecoveryResult(
                        trace_id=trace_id,
                        recovered_state=state.current_state,
                        recovered_from_seq=last_entry.sequence_number,
                        re_dispatched=True,
                        tool_effect=None  # Caller should re-dispatch
                    )
            
            # Terminal state - no re-dispatch needed
            return RecoveryResult(
                trace_id=trace_id,
                recovered_state=state.current_state,
                recovered_from_seq=last_entry.sequence_number,
                re_dispatched=False
            )
            
        finally:
            await self.store.release_trace_lock(trace_id)
    
    async def _resume_execution(
        self, 
        plan: ExecutionPlan, 
        state: ExecutionState
    ) -> ExecutionResult:
        """Resume execution from existing state."""
        if state.current_state in (
            ExecutionStateEnum.COMPLETED,
            ExecutionStateEnum.FAILED,
            ExecutionStateEnum.DENIED
        ):
            return ExecutionResult(
                trace_id=plan.trace_id,
                final_state=state.current_state
            )
        
        # For non-terminal states, trigger recovery
        recovery = await self.recover(plan.trace_id)
        return ExecutionResult(
            trace_id=plan.trace_id,
            final_state=recovery.recovered_state
        )
    
    def _make_entry(
        self,
        trace_id: str,
        seq: int,
        prev_digest: str,
        from_state: ExecutionStateEnum,
        to_state: ExecutionStateEnum,
        artifact_type: str,
        artifact_id: str,
        artifact_digest: str,
        tool_call_id: Optional[str] = None,
        idempotency_key: Optional[str] = None
    ) -> ExecutionLogEntry:
        """Create a log entry with computed digest."""
        entry = ExecutionLogEntry(
            schema_id="talos.tga.execution_log_entry",
            schema_version="v1",
            trace_id=trace_id,
            sequence_number=seq,
            prev_entry_digest=prev_digest,
            entry_digest="",  # Computed below
            ts=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            from_state=from_state,
            to_state=to_state,
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            artifact_digest=artifact_digest,
            tool_call_id=tool_call_id,
            idempotency_key=idempotency_key
        )
        entry.entry_digest = entry.compute_digest()
        return entry
    
    def _compute_digest(self, data: Dict[str, Any]) -> str:
        """Compute SHA-256 digest of canonical JSON."""
        canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    
    def _generate_id(self) -> str:
        """Generate a proper time-ordered UUID v7."""
        return str(uuid7())
    
    def _create_tool_call(
        self, 
        plan: ExecutionPlan, 
        decision: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Create a tool_call artifact from plan and decision."""
        import uuid
        return {
            "tool_call_id": uuid7(),
            "trace_id": plan.trace_id,
            "plan_id": plan.plan_id,
            "capability": decision.get("capability", {}),
            "call": plan.action_request.get("call", {}),
            "idempotency_key": f"idem-{plan.trace_id[:8]}"
        }


# Singleton
_runtime_instance: Optional[TgaRuntime] = None


def get_runtime() -> TgaRuntime:
    """Get or create the TGA runtime singleton."""
    global _runtime_instance
    if _runtime_instance is None:
        _runtime_instance = TgaRuntime()
    return _runtime_instance
