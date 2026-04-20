import pytest
import asyncio
import os
import json
import jwt
from datetime import datetime, timezone, timedelta
from talos_governance_agent.domain.runtime import TgaRuntime, TgaRuntimeError
from talos_governance_agent.adapters.sqlite_state_store import SqliteStateStore
from talos_governance_agent.adapters import mcp_server
from talos_governance_agent.utils.id import uuid7

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

@pytest.fixture
async def store():
    db_path = "test_hardening.db"
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

def create_capability(trace_id, plan_id, priv_pem, arg_constraints=None):
    payload = {
        "iss": str(uuid7()),
        "aud": "talos-gateway",
        "iat": int(datetime.now(timezone.utc).timestamp()),
        "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
        "nonce": str(uuid7()),
        "trace_id": str(trace_id),
        "plan_id": str(plan_id),
        "constraints": {
            "tool_server": "mcp-github",
            "tool_name": "create-pr",
            "target_allowlist": ["*"],
            "read_only": False,
            "arg_constraints": arg_constraints
        }
    }
    return jwt.encode(payload, priv_pem, algorithm="EdDSA")

@pytest.mark.asyncio
async def test_warm_path_arg_constraints_enforcement(runtime, keys):
    priv_pem, _ = keys
    trace_id = uuid7()
    plan_id = uuid7()
    
    args = {"repo": "talosprotocol/talos", "title": "feat: hardening"}
    args_digest = runtime.validator._compute_args_digest(args)
    
    cap_jws = create_capability(trace_id, plan_id, priv_pem, arg_constraints=args_digest)
    
    # 1. Cold path to establish session
    entry = await runtime.authorize_tool_call(
        cap_jws, "mcp-github", "create-pr", args
    )
    session_id = entry.session_id
    principal_id = entry.principal_id
    
    # 2. Warm path success
    result = await runtime.authorize_warm_path(
        session_id=session_id,
        principal_id=principal_id,
        tool_server="mcp-github",
        tool_name="create-pr",
        args=args
    )
    assert result["authorized"] is True
    
    # 3. Warm path failure (mismatching args)
    wrong_args = {"repo": "talosprotocol/talos", "title": "feat: something else"}
    with pytest.raises(TgaRuntimeError) as excinfo:
        await runtime.authorize_warm_path(
            session_id=session_id,
            principal_id=principal_id,
            tool_server="mcp-github",
            tool_name="create-pr",
            args=wrong_args
        )
    assert excinfo.value.code == "TGA_CONSTRAINT_MISMATCH"
    assert "Arguments unauthorized by constraints" in str(excinfo.value)

@pytest.mark.asyncio
async def test_mcp_governance_authorize_warm_path_digest(runtime, keys, store):
    priv_pem, pub_pem = keys
    # Initialize mcp_server runtime
    mcp_server._runtime = runtime
    
    trace_id = uuid7()
    plan_id = uuid7()
    args = {"cmd": "echo hello"}
    args_digest = runtime.validator._compute_args_digest(args)
    
    cap_jws = create_capability(trace_id, plan_id, priv_pem, arg_constraints=args_digest)
    
    # Cold path via MCP
    resp = await mcp_server.governance_authorize(
        capability_jws=cap_jws,
        tool_server="mcp-github",
        tool_name="create-pr",
        args=args
    )
    assert "error" not in resp
    session_id = resp["tool_call"]["session_id"]
    
    # Extract principal_id from session in store
    session = await store.get_session(session_id)
    principal_id = session["principal_id"]
    
    # Warm path via MCP
    resp_warm = await mcp_server.governance_authorize(
        session_id=session_id,
        principal_id=principal_id,
        tool_server="mcp-github",
        tool_name="create-pr",
        args=args
    )
    
    assert "error" not in resp_warm
    assert resp_warm["tool_call"]["args_digest"] == args_digest
    assert resp_warm["tool_call"]["session_id"] == session_id
