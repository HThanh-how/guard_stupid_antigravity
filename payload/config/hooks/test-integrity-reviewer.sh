#!/usr/bin/env bash
# Prepare bounded review state only; never inject steps into the app.
set -euo pipefail
exec python3 "$(dirname "$0")/review-gate.py" dispatch
