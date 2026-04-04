#!/usr/bin/env bash
# install_agent_extension.sh
# ---------------------------
# Run on the AGENT SERVER to wire in Next.js frontend deployment support.
# Usage:  sudo bash install_agent_extension.sh [AGENT_REPO_PATH]
# Default: /var/frappe/agent/repo
#
# Architecture (no NextjsMixin, no server.py changes):
#   frontend_patch.py registers Flask Blueprint routes via sys.modules lookup.
#   One import line in web.py triggers route registration at startup.
#   web.py, server.py, job.py, base.py — NONE of these are modified.
#
# Routes registered:
#   POST   /frontends/<name>/deploy  — build + start container + nginx
#   DELETE /frontends/<name>         — stop container + remove nginx
#
# Idempotent — safe to run multiple times.

set -euo pipefail

AGENT_REPO="${1:-/var/frappe/agent/repo}"
AGENT_PKG="${AGENT_REPO}/agent"
WEB_PY="${AGENT_PKG}/web.py"
NFP_DIR="$(cd "$(dirname "$0")" && pwd)"
AGENT_ENV="/home/frappe/agent/env/bin/python"
AGENT_CFG="/var/frappe/agent/config.json"

echo "==> Agent repo : ${AGENT_REPO}"
echo "==> Agent pkg  : ${AGENT_PKG}"
echo "==> NFP dir    : ${NFP_DIR}"
echo ""

# ── Guard: ensure we are on the right server ─────────────────────────
if [ ! -f "${AGENT_CFG}" ]; then
    echo "ERROR: ${AGENT_CFG} not found."
    echo "This script must be run on the agent server (f1.evoq.app)."
    exit 1
fi

if [ ! -f "${WEB_PY}" ]; then
    echo "ERROR: ${WEB_PY} not found."
    echo "Check the AGENT_REPO path: ${AGENT_REPO}"
    exit 1
fi

# ── 1. Remove any previous NextjsMixin installation ───────────────────
# The mixin approach is obsolete. Clean it up if present so it does
# not conflict with the Blueprint approach.
echo "[1/5] Cleaning up any previous mixin-based installation"
SERVER_PY="${AGENT_PKG}/server.py"
JOB_PY="${AGENT_PKG}/job.py"

"${AGENT_ENV}" - "${SERVER_PY}" "${JOB_PY}" << 'PYEOF'
import re, sys

server_path, job_path = sys.argv[1], sys.argv[2]

server = open(server_path).read()
orig   = server
server = re.sub(r'\nfrom agent\.nextjs_jobs import NextjsMixin\n', '\n', server)
server = re.sub(r'\nfrom agent\.nextjs_jobs import[^\n]+\n',        '\n', server)
server = re.sub(r'\bclass Server\(NextjsMixin,\s*Base\)', 'class Server(Base)', server)
if server != orig:
    open(server_path, 'w').write(server)
    print("  Cleaned server.py  (NextjsMixin removed)")
else:
    print("  server.py          (already clean)")

job   = open(job_path).read()
orig  = job
job   = re.sub(r'\nfrom agent\.nextjs_jobs import[^\n]+\n', '\n', job)
if job != orig:
    open(job_path, 'w').write(job)
    print("  Cleaned job.py     (stale nextjs_jobs import removed)")
else:
    print("  job.py             (already clean)")
PYEOF

# ── 2. Copy extension files ───────────────────────────────────────────
echo ""
echo "[2/5] Copying extension files into agent package"

REQUIRED_FILES=(nginx_utils.py template_injector.py frontend_patch.py)
MISSING=0
for f in "${REQUIRED_FILES[@]}"; do
    src="${NFP_DIR}/agent_extension/${f}"
    dst="${AGENT_PKG}/${f}"
    if [ -f "${src}" ]; then
        cp "${src}" "${dst}"
        echo "  -> ${f}"
    else
        echo "  WARNING: ${src} not found — skipping"
        MISSING=$((MISSING + 1))
    fi
done

if [ "${MISSING}" -gt 0 ]; then
    echo "  ${MISSING} file(s) missing. Make sure agent_extension/ is complete."
fi

echo "  NOTE: web.py, server.py, job.py, base.py are NOT modified"

# ── 3. Write Dockerfile template into agent package ───────────────────
echo ""
echo "[3/5] Writing Dockerfile template"

TMPL_DIR="${AGENT_PKG}/templates/docker"
mkdir -p "${TMPL_DIR}/health_route"

cat > "${TMPL_DIR}/Dockerfile" << 'DOCKERFILE'
# Managed by next_frontend_provisioner — do not remove this header.
# 3-stage standalone build: deps → builder → runner
FROM node:20-alpine AS base
ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /app

# ── Stage 1: Install dependencies ─────────────────────────────────────
FROM base AS deps
RUN apk add --no-cache libc6-compat
COPY package.json yarn.lock* package-lock.json* pnpm-lock.yaml* ./
RUN \
  if [ -f yarn.lock ]; then \
    yarn install --frozen-lockfile --ignore-scripts; \
  elif [ -f package-lock.json ]; then \
    npm ci --ignore-scripts; \
  elif [ -f pnpm-lock.yaml ]; then \
    corepack enable pnpm && pnpm install --frozen-lockfile --ignore-scripts; \
  else \
    npm install --ignore-scripts --prefer-offline --no-audit; \
  fi

# ── Stage 2: Build ─────────────────────────────────────────────────────
FROM base AS builder
ARG NEXT_PUBLIC_FRAPPE_URL
ARG NEXT_PUBLIC_FRAPPE_HOSTNAME
ENV NEXT_PUBLIC_FRAPPE_URL=${NEXT_PUBLIC_FRAPPE_URL}
ENV NEXT_PUBLIC_FRAPPE_HOSTNAME=${NEXT_PUBLIC_FRAPPE_HOSTNAME}
ENV NEXT_PRIVATE_STANDALONE=true
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

# ── Stage 3: Runner ────────────────────────────────────────────────────
FROM node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV PORT=3000
ENV HOSTNAME=0.0.0.0
RUN addgroup --system --gid 1001 nodejs && adduser --system --uid 1001 nextjs
COPY --from=builder --chown=nextjs:nodejs /app/public           ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/static     ./.next/static
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
USER nextjs
EXPOSE 3000
CMD ["node", "server.js"]
DOCKERFILE

cat > "${TMPL_DIR}/health_route/route.ts" << 'TSEOF'
// Injected by next_frontend_provisioner — do not remove.
// Uses NextResponse for Next.js 14 compatibility (Response.json not available).
import { NextResponse } from 'next/server'

export async function GET() {
  return NextResponse.json({ status: 'ok', ts: Date.now() })
}
TSEOF

echo "  -> templates/docker/Dockerfile"
echo "  -> templates/docker/health_route/route.ts"

# ── 4. Inject single import line into web.py ─────────────────────────
echo ""
echo "[4/5] Injecting frontend_patch import into web.py"

IMPORT_LINE='import agent.frontend_patch  # noqa: F401 — registers /frontends routes for NFP'

if grep -qF "frontend_patch" "${WEB_PY}"; then
    echo "  -> already present — skipping"
    grep -n "frontend_patch" "${WEB_PY}" | sed 's/^/    line /'
else
    # Insert after the last contiguous agent import block
    LAST_LINE=$(grep -n "^from agent\.\|^import agent\." "${WEB_PY}" | tail -1 | cut -d: -f1)
    if [ -n "${LAST_LINE}" ]; then
        sed -i "${LAST_LINE}a ${IMPORT_LINE}" "${WEB_PY}"
        echo "  -> inserted at line $((LAST_LINE + 1))"
    else
        # No existing agent imports found — append at end of imports block
        LAST_IMPORT=$(grep -n "^import \|^from " "${WEB_PY}" | tail -1 | cut -d: -f1)
        if [ -n "${LAST_IMPORT}" ]; then
            sed -i "${LAST_IMPORT}a ${IMPORT_LINE}" "${WEB_PY}"
            echo "  -> inserted at line $((LAST_IMPORT + 1))"
        else
            echo "${IMPORT_LINE}" >> "${WEB_PY}"
            echo "  -> appended to end of web.py"
        fi
    fi
fi

# ── 5. Verify import and restart agent:web ───────────────────────────
echo ""
echo "[5/5] Verifying import and restarting agent"

cd "${AGENT_REPO}"

# Syntax check all modified files
for f in "${AGENT_PKG}/frontend_patch.py" "${AGENT_PKG}/nginx_utils.py" "${WEB_PY}"; do
    "${AGENT_ENV}" -m py_compile "${f}" 2>&1 && echo "  syntax OK  : $(basename ${f})" \
        || { echo "  SYNTAX ERROR in ${f}"; exit 1; }
done

# Verify the import resolves correctly
"${AGENT_ENV}" -c "
import sys
sys.path.insert(0, '${AGENT_REPO}')
import os
os.chdir('${AGENT_REPO}')
# Verify frontend_patch imports cleanly on its own
import agent.frontend_patch as fp
print('  import OK  : agent.frontend_patch')
print('  TEMPLATES  :', str(fp.TEMPLATES_DIR))
tmpl_ok = (fp.TEMPLATES_DIR / 'Dockerfile').exists()
print('  Dockerfile :', 'FOUND' if tmpl_ok else 'MISSING — will use fallback')
" || {
    echo ""
    echo "  Import verification failed — check the error above."
    exit 1
}

# Restart
if supervisorctl restart agent:web 2>/dev/null; then
    sleep 3
    STATUS=$(supervisorctl status agent:web 2>/dev/null | awk '{print $2}')
    echo "  agent:web  : ${STATUS}"
    if [ "${STATUS}" != "RUNNING" ]; then
        echo ""
        echo "  ERROR: agent:web failed to start. Check logs:"
        echo "    tail -30 /var/frappe/agent/logs/web.error.log"
        exit 1
    fi
else
    echo "  supervisorctl not available — restart the agent manually"
fi

# ── Quick smoke test ─────────────────────────────────────────────────
echo ""
echo "=== Smoke test ==="
WEB_PORT=$(python3 -c "import json; print(json.load(open('${AGENT_CFG}')).get('web_port', 25052))" 2>/dev/null || echo 25052)
AGENT_PASS=$("${AGENT_ENV}" - << 'PYEOF' 2>/dev/null
import sys, os
os.chdir('/var/sdlpress/frappe-bench')
sys.path.insert(0, 'apps/frappe')
import frappe
frappe.init(site='cloud.evoq.app', sites_path='sites')
frappe.connect()
doc = frappe.get_doc('Server', 'f1.evoq.app')
print(doc.get_password('agent_password'), end='')
frappe.destroy()
PYEOF
)

if [ -n "${AGENT_PASS}" ]; then
    HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
        -H "Authorization: Bearer ${AGENT_PASS}" \
        "http://127.0.0.1:${WEB_PORT}/ping" 2>/dev/null || echo "000")
    echo "  GET /ping  : HTTP ${HTTP}  (200 = agent healthy)"
else
    echo "  Skipped (could not extract agent_password)"
fi

echo ""
echo "✓ Installation complete."
echo ""
echo "  Next steps:"
echo "  1. Deploy from Frappe desk → Actions → Deploy Frontend"
echo "  2. Monitor: tail -f /var/frappe/agent/logs/worker.log"
echo "  3. After deploy, configure nginx on proxy server (n1.evoq.app)"
echo "     See: ansible/proxy_nextjs.yml"
