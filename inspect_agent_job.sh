#!/usr/bin/env bash
# Run this on the agent server to show how job.py registers job types
AGENT_REPO="${1:-/var/frappe/agent/repo}"
JOB_PY="${AGENT_REPO}/agent/job.py"

echo "=== job.py — full file ==="
cat "${JOB_PY}"
