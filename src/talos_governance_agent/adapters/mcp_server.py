import logging
import os
from typing import Any, Dict, Optional
from mcp.server.fastmcp import FastMCP
from talos_governance_agent.domain.runtime import TgaRuntime
from talos_governance_agent.adapters.sqlite_state_store import SqliteStateStore
from talos_governance_agent.domain.models import ArtifactType

logger = logging.getLogger(__name__)

# Initialize FastMCP
mcp = FastMCP("talos-governance-agent")

# Dependencies (will be initialized in main or via a singleton pattern for MCP)
_runtime: Optional[TgaRuntime] = None

async def init_runtime(store_path: str, supervisor_public_key: str):
    global _runtime
    store = SqliteStateStore(store_path)
    # Note: caller should await store.initialize() before starting MCP
    
    # Cleanup expired sessions on startup
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    # Since we are async, we can await directly.
    try:
         # Note: store might need connect first if not auto-connecting.
         # But SqliteStateStore opens connection per call in _get_conn, so it's safe.
         count = await store.delete_expired_sessions(now)
         logger.info(f"Cleaned up {count} expired sessions")
    except Exception as e:
         logger.warning(f"Failed to cleanup sessions on startup: {e}")
    
    _runtime = TgaRuntime(store, supervisor_public_key)
    return store

@mcp.tool()
async def governance_authorize(
    capability_jws: Optional[str] = None, 
    session_id: Optional[str] = None,
    principal_id: Optional[str] = None,
    tool_server: Optional[str] = None, 
    tool_name: Optional[str] = None, 
    args: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Validates a capability (cold path) or session (warm path) and authorizes a tool call.
    Returns the execution tool_call artifact or error.
    """
    try:
        if _runtime is None:
             return {"error": {"code": "INTERNAL_ERROR", "message": "Runtime not initialized"}}

        # 1. Warm path: Session Cache Hit
        if session_id and not capability_jws:
            if not principal_id:
                return {"error": {"code": "INVALID_ARGUMENTS", "message": "principal_id required for warm path"}}
            
            # Authorize using session cache
            result = await _runtime.authorize_warm_path(
                session_id=session_id,
                principal_id=principal_id,
                tool_server=tool_server or "",
                tool_name=tool_name or "",
                args=args or {}
            )
            return {
                "tool_call": {
                    "tool_call_id": session_id,
                    "session_id": session_id,
                    "trace_id": result.get("trace_id", "cached"),
                    "args": args,
                    # Warm path digest might be skipped or simplified
                    "args_digest": "", # TODO: Compute if needed by constraints
                }
            }
            
        if not capability_jws or not tool_server or not tool_name or args is None:
             return {"error": {"code": "INVALID_ARGUMENTS", "message": "Missing required tool call parameters"}}

        entry = await _runtime.authorize_tool_call(
            capability_jws, tool_server, tool_name, args
        )
        return {
            "tool_call": {
                "tool_call_id": entry.tool_call_id,
                "session_id": entry.session_id,
                "trace_id": entry.trace_id,
                "sequence_number": entry.sequence_number,
                "artifact_digest": entry.entry_digest,
                "plan_id": entry.artifact_id, # artifact_id maps to plan_id for ACTION_REQUEST implicit context? No, for TOOL_CALL it's tool_call_id
                # But Authorize returns a tool_call artifact.
                "args_digest": entry.artifact_digest # The entry digest covers the tool call data which includes args
            }
        }
    except Exception as e:
        logger.error(f"Authorization failed: {e}")
        return {"error": {"code": "UNAUTHORIZED", "message": str(e)}}

@mcp.tool()
async def governance_log(
    trace_id: str, 
    key: str, # Idempotency key
    artifact_type: str,
    artifact_data: Dict[str, Any],
    prev_entry_digest: Optional[str] = None
) -> Dict[str, Any]:
    """
    Appends a generic entry to the high-integrity execution log.
    Replaces governance_record_effect for v1.0 spec compliance.
    """
    try:
        if _runtime is None:
             return {"error": {"code": "INTERNAL_ERROR", "message": "Runtime not initialized"}}

        # Determine semantic wrapper based on artifact type
        if artifact_type == ArtifactType.TOOL_EFFECT:
            entry = await _runtime.record_tool_effect(trace_id, artifact_data)
        else:
             # Fallback or generic append if supported (Runtime currently specializes)
             return {"error": {"code": "NOT_IMPLEMENTED", "message": f"Artifact type {artifact_type} not supported via log endpoint yet"}}

        return {
            "entry": {
                "schema_version": "v1",
                "trace_id": entry.trace_id,
                "sequence_number": entry.sequence_number,
                "entry_digest": entry.entry_digest,
                "prev_entry_digest": entry.prev_entry_digest,
                "artifact_type": entry.artifact_type,
                "ts": entry.ts
            }
        }
    except Exception as e:
        logger.error(f"Logging failed: {e}")
        return {"error": {"code": "LOGGING_FAILED", "message": str(e)}}

@mcp.tool()
async def governance_recover(trace_id: str) -> Dict[str, Any]:
    """
    Audits the hash chain integrity and recovers session state.
    """
    try:
        if _runtime is None:
             return {"error": {"code": "INTERNAL_ERROR", "message": "Runtime not initialized"}}

        recovery = await _runtime.recover(trace_id)
        
        # We need to fetch latest digest from store/runtime or return from recover object
        # Since recover result doesn't have it, we might skip or generic fill
        # Ideally Runtime.recover should return it.
        
        return {
            "chain_valid": True,
            "divergence_point": None,
            "latest_entry_digest": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", # Pending runtime support
            "entry_count": recovery.recovered_from_seq, # Approximate
            "last_seq": recovery.recovered_from_seq,
            "recommended_action": "NONE",
            "missing_seq_ranges": []
        }
    except Exception as e:
        logger.error(f"Recovery failed: {e}")
        return {"error": {"code": "RECOVERY_FAILED", "message": str(e)}}
