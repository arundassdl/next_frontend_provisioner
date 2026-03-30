#!/bin/bash
# Run on the agent server to read the key source files
echo "=== job.py — how job_record is set ===" 
head -60 /var/frappe/agent/repo/agent/job.py

echo ""
echo "=== Base.__init__ — what it requires ==="
grep -n "class Base\|def __init__\|job_record\|self\.directory" \
  /var/frappe/agent/repo/agent/base.py | head -30

echo ""
echo "=== Frontend.__init__ — full class ==="
cat /var/frappe/agent/repo/agent/frontend.py

echo ""
echo "=== Server.__init__ example — how other classes do it ==="
grep -n "class Server\|def __init__\|job_record\|directory\|super()" \
  /var/frappe/agent/repo/agent/server.py | head -30

echo ""
echo "=== web.py — all existing working job dispatch patterns ==="
grep -n "\.deploy\|\.new_bench\|Server(\|Bench(\|Site(" \
  /var/frappe/agent/repo/agent/web.py | head -30
