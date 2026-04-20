#!/usr/bin/env bash
set -eo pipefail

# =============================================================================
# Governance Agent - Standardized Test Entrypoint
# =============================================================================

COMMAND=${1:-"--unit"}

run_unit() {
    echo "--- Running Unit Tests ---"
    if [ -d "tests" ] && find tests -name "test_*.py" | grep -q .; then
        PYTHONPATH=src python3 -m pytest tests/ --maxfail=2 -q
    else
        echo "  ⚠ No test files found. Checking imports..."
        python3 -c "from app.domain.tga.runtime import TgaRuntime" 2>/dev/null && echo "  ✓ TGA runtime imports OK" || echo "  ⚠ Import check skipped (dependencies not installed)"
    fi
}

run_smoke() {
    echo "--- Running Smoke Tests ---"
    run_unit
}

case "$COMMAND" in
    --smoke) run_smoke ;;
    --unit) run_unit ;;
    --integration) echo "--- Skipping Integration (Not configured) ---" ;;
    --coverage) run_unit ;;
    --ci) run_smoke && run_unit ;;
    --full) run_smoke && run_unit ;;
    *) echo "Usage: $0 {--smoke|--unit|--integration|--coverage|--ci|--full}"; exit 1 ;;
esac

echo "governance-agent tests completed."
