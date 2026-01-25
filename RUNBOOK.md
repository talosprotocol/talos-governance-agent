# Talos Governance Agent (TGA) Runbook

## Overview

The Talos Governance Agent (TGA) is a standalone supervision service that acts as a secure sidecar for AI agents. It intercepts Model Context Protocol (MCP) tool calls, enforces capability constraints, and maintains a crash-safe, tamper-evident execution log.

## Deployment

TGA is designed to be deployed as a sidecar container or a local daemon.

### Environment Variables

| Variable | Description | Required | Default |
| :--- | :--- | :--- | :--- |
| `TGA_SUPERVISOR_PUBLIC_KEY` | PEM-encoded Ed25519 public key. Used to verify capability tokens. | Yes (Prod) | Dev Key |
| `TGA_DB_PATH` | Absolute path to the SQLite state store. | No | `governance_agent.db` |
| `PYTHONPATH` | Python path configuration. | No | `/app/src` |

### Storage Configuration

TGA uses a **Hardened SQLite** database for persistence.

- **Volume Mount**: Ensure `TGA_DB_PATH` resides on a persistent volume if containerized.
- **Permissions**: The service will automatically enforce `0600` permissions on the DB file. Ensure the running user has filesystem ownership.
- **WAL Mode**: Write-Ahead Logging is enabled automatically. Do not disable it; it is required for crash safety.

### Running via Docker

```bash
docker build -t talos-governance-agent .
docker run \
  -e TGA_SUPERVISOR_PUBLIC_KEY="..." \
  -v ./data:/data \
  -e TGA_DB_PATH="/data/tga.db" \
  talos-governance-agent
```

## Operational Safety

### Crash Recovery

TGA employs a strict **Moore Machine** for state transitions. If the service crashes:

1. **Auto-Recovery**: On restart, TGA automatically reloads the state from `TGA_DB_PATH`.
2. **Integrity Check**: The `recover` tool runs a deep integrity check, re-verifying the SHA-256 hash chain of the entire execution log.
3. **Session Resumption**: Pending sessions (in `EXECUTING` state) can be recovered and re-dispatched if the tool call was not confirmed.

### Key Rotation

To rotate the Supervisor Key:

1. Update the `TGA_SUPERVISOR_PUBLIC_KEY` environment variable.
2. Restart the TGA service.
3. Old capability tokens signed by the previous key will be rejected immediately.

## Troubleshooting

### Common Errors

| Error Code | Meaning | Remediation |
| :--- | :--- | :--- |
| `MISSING_CREDENTIALS` | No capability token or valid session ID provided. | Client must authenticate with a fresh JWS. |
| `UNAUTHORIZED` | Capability token signature invalid or constraints (e.g., `read_only`) violated. | Check Supervisor key config; check token constraints. |
| `Hash chain broken` | The SQLite database has been tampered with or corrupted. | **CRITICAL**: Investigate file access logs. Restore from backup if valid. |
| `Sequence gap` | Missing entries in the execution log. | Indicates potential data loss or tampering. |

### Debugging

Inspect the logs for validation failures:

```bash
docker logs <tga-container-id> | grep -i error
```

To manually inspect the database state:

```bash
sqlite3 data/tga.db "SELECT * FROM execution_states;"
```
