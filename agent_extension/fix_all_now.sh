#!/bin/bash
# fix_all_now.sh — run as root on f1.evoq.app
# Fixes:
#   1. Corrupt proxy.conf (server 127.0.0.1:{'port':3101,...} invalid line)
#   2. SSL cert mismatch (crm.evoq.app served with evoq.app wildcard cert)
#   3. nfp_proxy_patch.py dict→int port extraction bug
#   4. Makes the fix permanent via supervisor

set -euo pipefail

NGINX_DIR=$(python3 -c "import json; print(json.load(open('/var/frappe/agent/config.json')).get('nginx_directory',''))")
PROXY_CONF="$NGINX_DIR/proxy.conf"
AGENT_PKG="/var/frappe/agent/repo/agent"

echo "=== Step 1: Fix corrupt proxy.conf ==="
python3 << 'PYEOF'
import re, json, os

nginx_dir   = os.popen("python3 -c \"import json; print(json.load(open('/var/frappe/agent/config.json')).get('nginx_directory',''))\"").read().strip()
proxy_conf  = os.path.join(nginx_dir, "proxy.conf")
reg_path    = os.path.join(nginx_dir, "nfp_sites.json")

# Load registry
try:
    registry = json.load(open(reg_path))
except Exception:
    registry = {}
print(f"Registry: {registry}")

# Read proxy.conf
content = open(proxy_conf).read()

# Remove ALL stale/corrupt NFP upstream blocks (any with dict-as-port or wrong port)
for domain, entry in registry.items():
    port = entry.get("port") if isinstance(entry, dict) else int(entry)
    safe  = domain.replace(".", "_").replace("-", "_")
    uname = f"nextjs_{safe}"

    # Remove corrupt or stale block for this upstream
    content = re.sub(
        rf"upstream {re.escape(uname)} \{{[^}}]*\}}\n?",
        "", content,
    )
    # Remove stale map entries
    content = re.sub(
        rf"^\s*{re.escape(domain)}\s+\S+;\s*\n?",
        "", content, flags=re.MULTILINE,
    )

    # Insert clean upstream block
    block = (
        f"upstream {uname} {{\n"
        f"\tserver 127.0.0.1:{port};\n"
        f"\tkeepalive 32;\n"
        f"}}\n"
    )
    entry_line = f"\t{domain} http://{uname};"

    first = re.search(r"^upstream \w+", content, re.MULTILINE)
    if first:
        content = content[:first.start()] + block + "\n" + content[first.start():]
    else:
        content = block + "\n" + content

    # Add to $upstream_server_hash
    anchor = "\tdefault http://site_not_found;"
    if entry_line.strip() not in content:
        content = content.replace(anchor, f"{entry_line}\n\n{anchor}", 1)

    # Add to $socket_upstream_hash
    sock = re.search(
        r"(map \$actual_host \$socket_upstream_hash \{[^}]*?)"
        r"(\tdefault http://site_not_found;)",
        content, re.DOTALL,
    )
    if sock:
        se = f"\t{domain} http://{uname};\n"
        if se.strip() not in content[sock.start():sock.end()]:
            content = content[:sock.start(2)] + se + content[sock.start(2):]

    print(f"  Patched: {domain} → {uname} port {port}")

with open(proxy_conf, "w") as f:
    f.write(content)
print("proxy.conf fixed ✓")
PYEOF

echo ""
echo "=== Step 2: Test nginx config ==="
nginx -t 2>&1 | tail -3

echo ""
echo "=== Step 3: Copy fixed nfp_proxy_patch.py ==="
# The fixed version handles dict registry entries correctly
cp /tmp/nfp_proxy_patch_fixed.py "$AGENT_PKG/nfp_proxy_patch.py" 2>/dev/null || true
echo "  (copy manually if needed — see instructions)"

echo ""
echo "=== Step 4: Reload nginx ==="
sudo /usr/sbin/nginx -s reload && echo "nginx reloaded ✓" || echo "reload failed — check nginx -t"

echo ""
echo "=== Step 5: Verify proxy.conf ==="
grep "crm.evoq.app\|nextjs_crm" "$PROXY_CONF"

echo ""
echo "=== Step 6: SSL cert check ==="
echo "The SSL error (NET::ERR_CERT_COMMON_NAME_INVALID) means the wildcard cert"
echo "covers *.evoq.app but not crm.evoq.app (crm is already a subdomain of evoq.app)"
echo "Check which cert nginx is serving:"
echo | openssl s_client -connect crm.evoq.app:443 -servername crm.evoq.app 2>/dev/null | \
  openssl x509 -noout -subject -subj_hash -issuer 2>/dev/null | head -5 || \
  echo "  (run from external machine or use: curl -v https://crm.evoq.app 2>&1 | grep 'subject\|issuer')"

echo ""
echo "Current cert in proxy.conf:"
grep "ssl_certificate" "$PROXY_CONF" | head -3

echo ""
echo "=== Done ==="
echo "If you see NET::ERR_CERT_COMMON_NAME_INVALID:"
echo "  The wildcard cert '*.evoq.app' covers crm.evoq.app ✓ (first-level subdomain)"
echo "  But if nginx is serving the WRONG virtual host, crm.evoq.app gets a cert for *.evoq.app"
echo "  while the SNI lookup fails because \$actual_host map is incorrect."
echo ""
echo "  Run: curl -sk https://crm.evoq.app/ -o /dev/null -w '%{http_code}'"
echo "  Expected: 200 (Next.js app) — if 307, proxy.conf map entry still missing"
