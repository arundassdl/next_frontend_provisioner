#!/bin/bash
# Run on the agent server — reads the @job decorator implementation
echo "=== job.py lines 150-220 — the @job decorator ==="
sed -n '150,220p' /var/frappe/agent/repo/agent/job.py

echo ""
echo "=== job.py lines 1-150 — Job model and setup ==="
sed -n '1,150p' /var/frappe/agent/repo/agent/job.py

echo ""
echo "=== base.py full ==="
cat /var/frappe/agent/repo/agent/base.py

echo ""
echo "=== web.py lines 1730-1760 — exact frontend routes ==="
sed -n '1730,1760p' /var/frappe/agent/repo/agent/web.py

echo ""
echo "=== web.py lines 95-130 — authenticate decorator ==="
sed -n '95,130p' /var/frappe/agent/repo/agent/web.py
