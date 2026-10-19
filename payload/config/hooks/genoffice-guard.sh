#!/usr/bin/env bash
# Test edits allowed. Gate test runs and git add/commit on independent review.
set -euo pipefail
exec python3 "$(dirname "$0")/review-gate.py" pre
