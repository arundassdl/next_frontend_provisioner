#!/bin/bash
# get_token.sh — Run on f1.evoq.app to extract the correct agent token
# and test authentication against the running agent.

echo "=== Method 1: agent config.json (hashed token — for reference only) ==="
sudo python3 -c "
import json
cfg = json.load(open('/var/frappe/agent/config.json'))
print('access_token (hashed):', cfg.get('access_token','NOT FOUND')[:40], '...')
print('web_port:             ', cfg.get('web_port'))
print('nginx_directory:      ', cfg.get('nginx_directory'))
"

echo ""
echo "=== Method 2: Plain-text password from Press database ==="
DB_NAME=$(python3 -c "
import json
cfg = json.load(open('/var/sdlpress/frappe-bench/sites/sdlpress.evoq.app/site_config.json'))
print(cfg.get('db_name','frappe_press'))
" 2>/dev/null || echo "frappe_press")

DB_PASS=$(python3 -c "
import json
cfg = json.load(open('/var/sdlpress/frappe-bench/sites/sdlpress.evoq.app/site_config.json'))
print(cfg.get('db_password',''))
" 2>/dev/null || echo "")

mysql -u root -p"${DB_PASS}" "${DB_NAME}" 2>/dev/null \
  -e "SELECT name, SUBSTRING(agent_password,1,60) as agent_password_preview FROM \`tabServer\` WHERE name='f1.evoq.app';" \
  || mysql -u root "${DB_NAME}" 2>/dev/null \
  -e "SELECT name, agent_password FROM \`tabServer\` WHERE name='f1.evoq.app';" \
  || echo "Direct DB access failed — try Method 3"

echo ""
echo "=== Method 3: bench --site python snippet ==="
cd /var/sdlpress/frappe-bench
PASS=$(./env/bin/python -c "
import sys, os
os.chdir('/var/sdlpress/frappe-bench')
sys.path.insert(0, 'apps/frappe')
import frappe
frappe.init(site='sdlpress.evoq.app', sites_path='sites')
frappe.connect()
doc = frappe.get_doc('Server', 'f1.evoq.app')
pw = doc.get_password('agent_password')
frappe.destroy()
print(pw)
" 2>/dev/null)

if [ -n "$PASS" ]; then
    echo "Got password: ${PASS:0:6}...${PASS: -4}"
    echo ""
    echo "=== Testing auth against agent ==="
    RESULT=$(curl -s -o /dev/null -w "%{http_code}" \
      -H "Authorization: Bearer $PASS" \
      http://127.0.0.1:25052/frontends)
    echo "GET /frontends → HTTP $RESULT  (200 = auth OK, 401 = wrong token)"

    if [ "$RESULT" = "200" ]; then
        echo ""
        echo "SUCCESS. Now set this in bench site config:"
        echo "  bench --site sdlpress.evoq.app set-config nfp_agent_password '$PASS'"
        echo ""
        echo "=== Test deploy endpoint ==="
        curl -s -w "\nHTTP:%{http_code}" \
          -X POST \
          -H "Authorization: Bearer $PASS" \
          -H "Content-Type: application/json" \
          -d '{
            "repo":   "https://github.com/arundassdl/crm-frontend.git",
            "branch": "main",
            "port":   3100,
            "env_vars": {
              "FRAPPE_URL":              "https://crmapp.evoq.app",
              "NEXT_PUBLIC_FRAPPE_URL":  "https://crmapp.evoq.app",
              "NODE_ENV":                "production",
              "PORT":                    "3100",
              "HOSTNAME":                "0.0.0.0"
            },
            "deployment_mode": "Frontend Only",
            "backend_url":     "https://crmapp.evoq.app"
          }' \
          http://127.0.0.1:25052/frontends/crm-evoq-app/deploy
    fi
else
    echo "Could not extract password via Python. Try Method 4."
    echo ""
    echo "=== Method 4: Read encrypted password field directly ==="
    ./env/bin/python -c "
import sys, os
os.chdir('/var/sdlpress/frappe-bench')
sys.path.insert(0, 'apps/frappe')
import frappe
frappe.init(site='sdlpress.evoq.app', sites_path='sites')
frappe.connect()
# Get raw encrypted value
raw = frappe.db.get_value('Server', 'f1.evoq.app', 'agent_password')
print('Raw (encrypted):', repr(raw)[:80])
frappe.destroy()
" 2>&1 | head -20
fi
