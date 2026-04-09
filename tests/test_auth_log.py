import pytest
import asyncio
import os
import json
import jwt
import base64
import aiosqlite
from datetime import datetime, timezone, timedelta
from talos_governance_agent.domain.models import ExecutionStateEnum, ArtifactType
from talos_governance_agent.domain.runtime import TgaRuntime
from talos_governance_agent.adapters.sqlite_state_store import SqliteStateStore
from talos_governance_agent.utils.id import uuid7

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

# Test constants
ZERO_DIGEST = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

@pytest.fixture
async def store():
    db_path = "test_audit.db"
    if os.path.exists(db_path):
        os.remove(db_path)
    store = SqliteStateStore(db_path)
    await store.initialize()
    yield store
    if os.path.exists(db_path):
        os.remove(db_path)

@pytest.fixture
def keys():
    private_key = ed25519.Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    
    priv_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    ).decode('utf-8')
    
    pub_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode('utf-8')
    
    return priv_pem, pub_pem

@pytest.fixture
def runtime(store, keys):
    _, pub_pem = keys
    return TgaRuntime(store, pub_pem)

def create_capability(trace_id, plan_id, priv_pem, constraints=None):
    if constraints is None:
        constraints = {
            "tool_server": "mcp-github",
            "tool_name": "create-pr",
            "target_allowlist": ["talosprotocol/*"],
            "read_only": False
        }
    
    payload = {
        "iss": str(uuid7()),
        "aud": "talos-gateway",
        "iat": int(datetime.now(timezone.utc).timestamp()),
        "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
        "nonce": str(uuid7()),
        "trace_id": str(trace_id),
        "plan_id": str(plan_id),
        "constraints": constraints
    }
    return jwt.encode(payload, priv_pem, algorithm="EdDSA")

@pytest.mark.asyncio
async def test_full_audit_chain_completeness(runtime, keys):
    """Verify that all artifact types are correctly logged during a full execution."""
    priv_pem, _ = keys
    trace_id = uuid7()
    plan_id = uuid7()
    cap_jws = create_capability(trace_id, plan_id, priv_pem)
    
    # 1. Authorize Tool Call
    entry = await runtime.authorize_tool_call(
        cap_jws, "mcp-github", "create-pr", {"repo": "talosprotocol/talos", "title": "test"}
    )
    
    # Check log entries
    entries = await runtime.store.list_log_entries(str(trace_id))
    
    # Should have: ACTION_REQUEST (1), SUPERVISOR_DECISION (2), TOOL_CALL (3)
    artifact_types = [e.artifact_type for e in entries]
    assert ArtifactType.ACTION_REQUEST in artifact_types
    assert ArtifactType.SUPERVISOR_DECISION in artifact_types
    assert ArtifactType.TOOL_CALL in artifact_types
    
    # 2. Record Effect
    effect = {
        "tool_effect_id": str(uuid7()),
        "outcome": {"status": "SUCCESS"},
        "result": {"pr_url": "https://github.com/talosprotocol/talos/pull/1"}
    }
    await runtime.record_tool_effect(str(trace_id), effect)
    
    entries = await runtime.store.list_log_entries(str(trace_id))
    artifact_types = [e.artifact_type for e in entries]
    assert ArtifactType.TOOL_EFFECT in artifact_types
    
    # Verify hash chain integrity
    for i in range(1, len(entries)):
        assert entries[i].prev_entry_digest == entries[i-1].entry_digest
        assert entries[i].sequence_number == entries[i-1].sequence_number + 1

@pytest.mark.asyncio
async def test_deterministic_constraint_validation(runtime, keys):
    """Verify that all constraints in the capability are enforced."""
    priv_pem, _ = keys
    trace_id = uuid7()
    plan_id = uuid7()
    
    # Capability with restricted target_allowlist
    constraints = {
        "tool_server": "mcp-github",
        "tool_name": "create-pr",
        "target_allowlist": ["talosprotocol/core"],
        "read_only": False
    }
    cap_jws = create_capability(trace_id, plan_id, priv_pem, constraints)
    
    # 1. Valid call
    await runtime.authorize_tool_call(
        cap_jws, "mcp-github", "create-pr", {"repo": "talosprotocol/core", "title": "test"}
    )
    
    # 2. Invalid target (should fail if implemented)
    trace_id_2 = uuid7()
    cap_jws_2 = create_capability(trace_id_2, plan_id, priv_pem, constraints)
    
    # This is expected to FAIL currently because target_allowlist is not enforced
    with pytest.raises(Exception) as excinfo:
        await runtime.authorize_tool_call(
            cap_jws_2, "mcp-github", "create-pr", {"repo": "other/repo", "title": "test"}
        )
    assert "target" in str(excinfo.value).lower()

def compute_digest(data: dict) -> str:
    import hashlib
    import base64
    import json
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")

@pytest.mark.asyncio
async def test_arg_constraints_digest_validation(runtime, keys):
    """Verify that arg_constraints_digest is enforced."""
    priv_pem, _ = keys
    trace_id = uuid7()
    plan_id = uuid7()
    
    # 1. Successful validation (pinning)
    valid_args = {"repo": "talosprotocol/core", "title": "test"}
    arg_digest = compute_digest(valid_args)
    
    constraints = {
        "tool_server": "mcp-github",
        "tool_name": "create-pr",
        "target_allowlist": ["*"],
        "arg_constraints": arg_digest,
        "read_only": False
    }
    cap_jws = create_capability(trace_id, plan_id, priv_pem, constraints)
    
    await runtime.authorize_tool_call(cap_jws, "mcp-github", "create-pr", valid_args)
    
    # 2. Failed validation (mismatched args)
    trace_id_2 = uuid7()
    cap_jws_2 = create_capability(trace_id_2, plan_id, priv_pem, constraints)
    
    with pytest.raises(Exception) as excinfo:
        await runtime.authorize_tool_call(
            cap_jws_2, "mcp-github", "create-pr", {"repo": "talosprotocol/core", "title": "different"}
        )
    assert "Arguments do not match constraints" in str(excinfo.value)
