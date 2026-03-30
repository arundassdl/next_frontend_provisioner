"""
fix_agent.py — run as root on the agent server.
Fixes two issues:
  1. Removes # syntax=docker/dockerfile:1 from Dockerfile template
     (causes Docker Hub pull attempt that fails without internet)
  2. Writes the Dockerfile template directly into the agent repo so
     template_injector can find it at runtime without NFP_TEMPLATES_DIR
  3. Updates frontend_patch.py TEMPLATES_DIR to use the agent-local copy
"""
import os, shutil

AGENT_PKG = "/var/frappe/agent/repo/agent"
TMPL_DIR  = os.path.join(AGENT_PKG, "templates", "docker")

os.makedirs(TMPL_DIR, exist_ok=True)
os.makedirs(os.path.join(TMPL_DIR, "health_route"), exist_ok=True)

# ── 1. Write Dockerfile WITHOUT the syntax= line ─────────────────────
dockerfile = """\
# Managed by next_frontend_provisioner — do not remove this header.
ARG NODE_VERSION=20

FROM node:${NODE_VERSION}-alpine AS base
ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /app

FROM base AS deps
RUN apk add --no-cache libc6-compat
COPY package.json package-lock.json* ./
RUN npm ci

FROM base AS builder
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ARG NEXT_PUBLIC_FRAPPE_URL
ENV NEXT_PUBLIC_FRAPPE_URL=${NEXT_PUBLIC_FRAPPE_URL}
RUN npm run build

FROM node:${NODE_VERSION}-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV HOSTNAME=0.0.0.0
ENV PORT=3000
RUN addgroup --system --gid 1001 nodejs && adduser --system --uid 1001 nextjs
COPY --from=builder /app/public ./public
RUN mkdir .next && chown nextjs:nodejs .next
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
USER nextjs
EXPOSE ${PORT}
CMD ["node","server.js"]
"""

with open(os.path.join(TMPL_DIR, "Dockerfile"), "w") as f:
    f.write(dockerfile)
print("Dockerfile written (no syntax= line)")

# ── 2. Write next.config.js template ─────────────────────────────────
next_config = """\
/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'standalone',
  images: {
    remotePatterns: [{
      protocol: 'https',
      hostname: process.env.FRAPPE_HOSTNAME || 'localhost',
    }],
  },
  env: {
    NEXT_PUBLIC_FRAPPE_URL: process.env.NEXT_PUBLIC_FRAPPE_URL,
  },
}
module.exports = nextConfig
"""
with open(os.path.join(TMPL_DIR, "next.config.js"), "w") as f:
    f.write(next_config)
print("next.config.js written")

# ── 3. Write health route template ───────────────────────────────────
health_route = """\
// Injected by next_frontend_provisioner — do not remove.
export async function GET() {
  return Response.json({ status: 'ok', ts: Date.now() })
}
"""
with open(os.path.join(TMPL_DIR, "health_route", "route.ts"), "w") as f:
    f.write(health_route)
print("health route written")

# ── 4. Patch frontend_patch.py TEMPLATES_DIR to use agent-local path ─
patch_path = os.path.join(AGENT_PKG, "frontend_patch.py")
with open(patch_path) as f:
    content = f.read()

old = """_HERE         = Path(__file__).parent
TEMPLATES_DIR = Path(os.environ.get(
    "NFP_TEMPLATES_DIR",
    _HERE / "templates" / "docker",
))"""

new = """_HERE         = Path(__file__).parent
# Resolve templates directory: use NFP_TEMPLATES_DIR env var if set,
# then the agent-local copy (written by install_agent_extension.sh),
# then fall back to the sibling path.
_AGENT_TMPL = Path("/var/frappe/agent/repo/agent/templates/docker")
TEMPLATES_DIR = Path(os.environ.get(
    "NFP_TEMPLATES_DIR",
    str(_AGENT_TMPL) if _AGENT_TMPL.exists() else str(_HERE / "templates" / "docker"),
))"""

if old in content:
    content = content.replace(old, new)
    with open(patch_path, "w") as f:
        f.write(content)
    print("frontend_patch.py TEMPLATES_DIR updated")
else:
    # Just prepend a hardcoded override at the top of the file
    override = f'\nimport os as _os_override\nos.environ.setdefault("NFP_TEMPLATES_DIR", "{TMPL_DIR}")\n'
    content = content.replace(
        "from flask import Blueprint, jsonify, request",
        "from flask import Blueprint, jsonify, request" + override,
        1
    )
    with open(patch_path, "w") as f:
        f.write(content)
    print("frontend_patch.py: NFP_TEMPLATES_DIR override injected")

# ── 5. Also remove any existing bad Dockerfile in /tmp/nfp-crm ───────
import glob
for tmp_dir in glob.glob("/tmp/nfp-*"):
    df = os.path.join(tmp_dir, "Dockerfile")
    if os.path.exists(df):
        size = os.path.getsize(df)
        if size < 100:
            os.remove(df)
            print(f"Removed bad Dockerfile ({size}B) from {tmp_dir}")
        else:
            # Remove syntax= line from existing Dockerfile
            with open(df) as f:
                lines = f.readlines()
            clean = [l for l in lines if not l.strip().startswith("# syntax=")]
            if len(clean) < len(lines):
                with open(df, "w") as f:
                    f.writelines(clean)
                print(f"Removed syntax= line from {df}")

print("\nAll done. Run: supervisorctl restart agent:web")
