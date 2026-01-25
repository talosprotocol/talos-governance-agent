import pytest
import asyncio
import os
import json
import jwt
import base64
import aiosqlite
from datetime import datetime, timezone, timedelta
from talos_governance_agent.domain.models import ExecutionStateEnum
from talos_governance_agent.domain.runtime import TgaRuntime
from talos_governance_agent.adapters.sqlite_state_store import SqliteStateStore
from talos_governance_agent.utils.id import uuid7

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

# Test constants
ZERO_DIGEST = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

@pytest.fixture
async def store():
    db_path = "test_gov.db"
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

def create_capability(trace_id, plan_id, priv_pem):
    payload = {
        "iss": str(uuid7()), # Use strict UUIDv7 for principal
        "aud": "talos-gateway",
        "iat": int(datetime.now(timezone.utc).timestamp()),
        "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
        "nonce": str(uuid7()),
        "trace_id": str(trace_id),
        "plan_id": str(plan_id),
        "constraints": {
            "tool_server": "mcp-github",
            "tool_name": "create-pr",
            "target_allowlist": ["talosprotocol/*"],
            "read_only": False
        }
    }
    return jwt.encode(payload, priv_pem, algorithm="EdDSA")

@pytest.mark.asyncio
async def test_authorization_and_effect_flow(runtime, keys):
    priv_pem, _ = keys
    trace_id = uuid7()
    plan_id = uuid7()
    cap_jws = create_capability(trace_id, plan_id, priv_pem)
    
    # 1. Authorize
    entry = await runtime.authorize_tool_call(
        cap_jws, "mcp-github", "create-pr", {"repo": "talosprotocol/talos", "title": "test"}
    )
    
    assert entry.trace_id == str(trace_id)
    assert entry.to_state == ExecutionStateEnum.EXECUTING
    assert entry.sequence_number == 3 # PENDING -> AUTHORIZED -> EXECUTING
    
    # Verify base64url digest
    assert len(entry.entry_digest) == 43
    assert not entry.entry_digest.endswith("=")
    
    state = await runtime.store.load_state(str(trace_id))
    assert state.current_state == ExecutionStateEnum.EXECUTING
    
    # 2. Record Effect
    effect = {
        "tool_effect_id": str(uuid7()),
        "outcome": {"status": "SUCCESS"},
        "result": {"pr_url": "https://github.com/talosprotocol/talos/pull/1"}
    }
    effect_entry = await runtime.record_tool_effect(str(trace_id), effect)
    
    assert effect_entry.to_state == ExecutionStateEnum.COMPLETED
    assert effect_entry.sequence_number == 4
    assert effect_entry.prev_entry_digest == entry.entry_digest
    
    state = await runtime.store.load_state(str(trace_id))
    assert state.current_state == ExecutionStateEnum.COMPLETED

@pytest.mark.asyncio
async def test_recovery_and_hash_chain(runtime, keys):
    priv_pem, _ = keys
    trace_id = uuid7()
    plan_id = uuid7()
    cap_jws = create_capability(trace_id, plan_id, priv_pem)
    
    # Authorize but don't record effect (simulating crash)
    await runtime.authorize_tool_call(
        cap_jws, "mcp-github", "create-pr", {"repo": "talosprotocol/talos"}
    )
    
    # Recover
    recovery = await runtime.recover(str(trace_id))
    assert recovery.recovered_state == ExecutionStateEnum.EXECUTING
    assert recovery.re_dispatched is True
    assert recovery.recovered_from_seq == 3

@pytest.mark.asyncio
async def test_hash_chain_tamper_detection(runtime, keys):
    priv_pem, _ = keys
    trace_id = uuid7()
    plan_id = uuid7()
    cap_jws = create_capability(trace_id, plan_id, priv_pem)
    
    # Authorize
    entry = await runtime.authorize_tool_call(
        cap_jws, "mcp-github", "create-pr", {"repo": "talosprotocol/talos"}
    )
    
    # Manually tamper with the log in DB
    async with aiosqlite.connect(runtime.store.db_path) as db:
        await db.execute(
            "UPDATE execution_logs SET data = ? WHERE trace_id = ? AND sequence_number = 3",
            (json.dumps({"tampered": True}), str(trace_id))
        )
        await db.commit()
    
    # Recovery should fail due to hash chain break or validation error
    with pytest.raises(Exception) as excinfo:
        await runtime.recover(str(trace_id))
    error_msg = str(excinfo.value)
    assert any(msg in error_msg for msg in ["Hash chain broken", "Genesis entry invalid", "validation error"])

@pytest.mark.asyncio
async def test_invalid_transition(runtime):
    trace_id = str(uuid7())
    # Try to record effect for non-existent trace
    with pytest.raises(Exception) as excinfo:
        await runtime.record_tool_effect(trace_id, {"outcome": {"status": "SUCCESS"}})
    assert "not in EXECUTING state" in str(excinfo.value)
