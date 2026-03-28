#!/bin/bash
# inspect_agent_job.sh — test the agent deploy endpoint directly
# Usage: bash inspect_agent_job.sh [site_name]
# site_name is the exact value stored in the Nextjs Site DocType (e.g. "crm")

set -euo pipefail

SITE="${1:-crm}"
SLUG="${SITE//./-}"
AGENT_URL="http://127.0.0.1:25052"
BACKEND_URL="${2:-https://crmapp.evoq.app}"

echo "Site name : $SITE"
echo "URL slug  : $SLUG  (used in /frontends/$SLUG/deploy)"
echo "Agent URL : $AGENT_URL"
echo ""

# Extract agent password
PASS=$(cd /var/sdlpress/frappe-bench && \
  ./env/bin/python - << 'PY' 2>/dev/null
import sys, os
os.chdir('/var/sdlpress/frappe-bench')
sys.path.insert(0, 'apps/frappe')
import frappe
frappe.init(site='sdlpress.evoq.app', sites_path='sites')
frappe.connect()
doc = frappe.get_doc('Server', 'f1.evoq.app')
print(doc.get_password('agent_password'), end='')
frappe.destroy()
PY
)

if [ -z "$PASS" ]; then
    echo "ERROR: Could not extract agent_password from Press DB."
    echo "Set it manually: export PASS='your_password' then re-run"
    exit 1
fi

echo "Password  : ${PASS:0:4}...${PASS: -4}"
echo ""

echo "=== POST /frontends/$SLUG/deploy ==="
curl -sv -X POST \
  -H "Authorization: Bearer $PASS" \
  -H "Content-Type: application/json" \
  -d "{
    \"repo\":            \"https://github.com/arundassdl/crm-frontend.git\",
    \"branch\":          \"main\",
    \"port\":            3100,
    \"env_vars\": {
      \"FRAPPE_URL\":             \"$BACKEND_URL\",
      \"NEXT_PUBLIC_FRAPPE_URL\": \"$BACKEND_URL\",
      \"NODE_ENV\":               \"production\",
      \"PORT\":                   \"3100\",
      \"HOSTNAME\":               \"0.0.0.0\"
    },
    \"deployment_mode\": \"Frontend Only\",
    \"backend_url\":     \"$BACKEND_URL\"
  }" \
  "$AGENT_URL/frontends/$SLUG/deploy" 2>&1

echo ""
echo "=== Last 10 lines of agent error log ==="
tail -10 /var/frappe/agent/logs/web.error.log 2>/dev/null
