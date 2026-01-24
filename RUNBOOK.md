# Talos Governance Agent (TGA) Runbook

## Overview

The Talos Governance Agent (TGA) is a standalone service responsible for supervising tool execution. It validates capabilities minted by the Supervisor and maintains a crash-safe, append-only execution log.

## Deployment

TGA is deployed as a Docker container.

### Environment Variables

- `TGA_SUPERVISOR_PUBLIC_KEY`: PEM-encoded Ed25519 public key.
- `PYTHONPATH`: Should be set to `/app/src`.

### Running Locally

```bash
docker build -t talos-governance-agent .
docker run -e TGA_SUPERVISOR_PUBLIC_KEY="..." talos-governance-agent
```

## Configuration & Key Rotation

1. **Key Rotation**: To rotate the Supervisor key, update the `TGA_SUPERVISOR_PUBLIC_KEY` environment variable in the deployment manifest.
2. **State Storage**: Currently uses an in-memory adapter. For production, swap with a Postgres-backed adapter (see `adapters/`).

## Debugging

### Check Logs

```bash
docker logs <tga-container-id>
```

### Common Issues

- **CAPABILITY_INVALID**: The signature or audience of the capability token is incorrect.
- **STATE_CHECKSUM_MISMATCH**: The hash chain in the state log is broken (integrity violation).
- **EXPIRED**: The capability token has expired.

## Operational Safety

- TGA uses a single-writer lock per `trace_id` to prevent concurrent execution conflicts.
- Recovery logic is built-in; restarting a crashed container will resume pending executions from the last persisted state.
