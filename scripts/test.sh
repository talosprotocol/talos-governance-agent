#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# Governance Agent - Standardized Test Entrypoint
# =============================================================================

COMMAND=${1:-"--unit"}
HOST_PYTHON="${TALOS_PYTHON:-python3}"
VENV_DIR=""

pick_python() {
    local candidates=()
    if [[ -n "${TALOS_PYTHON:-}" ]]; then
        candidates+=("${TALOS_PYTHON}")
    fi
    candidates+=(python3.13 python3.12 python3.11 python3.10 python3 python3.14)

    local candidate
    for candidate in "${candidates[@]}"; do
        command -v "$candidate" >/dev/null 2>&1 || continue
        if "$candidate" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
        then
            echo "$candidate"
            return 0
        fi
    done

    echo "Talos governance-agent tests require Python >= 3.10. Set TALOS_PYTHON to a compatible interpreter." >&2
    exit 1
}

HOST_PYTHON="$(pick_python)"
PYTHON_TAG="$("$HOST_PYTHON" - <<'PY'
import sys
print(f"{sys.version_info[0]}.{sys.version_info[1]}")
PY
)"
VENV_DIR=".venv-test-py${PYTHON_TAG}"

ensure_virtualenv() {
    if [[ ! -x "$VENV_DIR/bin/python" ]]; then
        echo "Creating local governance-agent test virtualenv with $("$HOST_PYTHON" --version 2>&1)..."
        "$HOST_PYTHON" -m venv "$VENV_DIR"
    fi
}

ensure_virtualenv
PYTHON_BIN="$VENV_DIR/bin/python"

ensure_test_dependencies() {
    if "$PYTHON_BIN" - <<'PY' >/dev/null 2>&1
import importlib.util

required = ["pytest", "pytest_asyncio", "pydantic", "jsonschema"]
missing = [name for name in required if importlib.util.find_spec(name) is None]
raise SystemExit(0 if not missing else 1)
PY
    then
        return 0
    fi

    echo "Installing governance-agent test dependencies with $("$PYTHON_BIN" --version 2>&1)..."
    PIP_DISABLE_PIP_VERSION_CHECK=1 "$PYTHON_BIN" -m pip install -e . pytest pytest-asyncio
}

ensure_test_dependencies

run_unit() {
    echo "--- Running Unit Tests ---"
    if [ -d "tests" ] && find tests -name "test_*.py" | grep -q .; then
        PYTHONPATH=src "$PYTHON_BIN" -m pytest tests/ --maxfail=2 -q
    else
        echo "  ⚠ No test files found. Checking imports..."
        PYTHONPATH=src "$PYTHON_BIN" -c "from app.domain.tga.runtime import TgaRuntime" \
            && echo "  ✓ TGA runtime imports OK"
    fi
}

run_smoke() {
    echo "--- Running Smoke Tests ---"
    if [ -d "tests" ] && find tests -name "test_*.py" | grep -q .; then
        set +e
        PYTHONPATH=src "$PYTHON_BIN" -m pytest tests/ -m smoke --maxfail=1 -q
        local status=$?
        set -e
        if [[ $status -eq 0 || $status -eq 5 ]]; then
            if [[ $status -eq 5 ]]; then
                echo "No smoke tests collected. Skipping."
            fi
            return 0
        fi
        return "$status"
    fi
    echo "  ⚠ No smoke tests found. Skipping."
}

case "$COMMAND" in
    --smoke) run_smoke ;;
    --unit) run_unit ;;
    --integration) echo "--- Skipping Integration (Not configured) ---" ;;
    --coverage) run_unit ;;
    --ci)
        run_smoke
        run_unit
        ;;
    --full)
        run_smoke
        run_unit
        ;;
    *) echo "Usage: $0 {--smoke|--unit|--integration|--coverage|--ci|--full}"; exit 1 ;;
esac

echo "governance-agent tests completed."
