# Managed by next_frontend_provisioner — do not remove this header.
# 3-stage standalone build: deps → builder → runner

FROM node:20-alpine AS base
ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /app

# ── Stage 1: Install dependencies ────────────────────────────────────
FROM base AS deps
RUN apk add --no-cache libc6-compat

# Copy all lockfiles so the correct package manager is auto-detected
COPY package.json yarn.lock* package-lock.json* pnpm-lock.yaml* ./

# Install with whichever lockfile is present.
# --ignore-scripts skips postinstall hooks (e.g. build:icons) that
# reference files not present at install time.
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

# ── Stage 2: Build ────────────────────────────────────────────────────
FROM base AS builder

# Build-time public vars — baked into the client bundle
ARG NEXT_PUBLIC_FRAPPE_URL
ARG NEXT_PUBLIC_FRAPPE_HOSTNAME
ENV NEXT_PUBLIC_FRAPPE_URL=${NEXT_PUBLIC_FRAPPE_URL}
ENV NEXT_PUBLIC_FRAPPE_HOSTNAME=${NEXT_PUBLIC_FRAPPE_HOSTNAME}

# Tell Next.js to emit standalone output even if next.config.js
# does not yet have output:'standalone' set.
ENV NEXT_PRIVATE_STANDALONE=true

COPY --from=deps /app/node_modules ./node_modules
COPY . .

# ── Copy example config files to their real locations if missing ─────
# Handles repos that ship *-example.ts files instead of committing
# environment-specific configs (e.g. app-config-example.ts → app-config.ts)
RUN find . -name '*-example.ts' -o -name '*-example.js' | while read src; do       dest=$(echo "$src" | sed 's/-example\.\(ts\|js\)$/\.\1/');       if [ ! -f "$dest" ]; then         echo "[NFP] copying $src -> $dest";         cp "$src" "$dest";       fi;     done

RUN npm run build

# ── Stage 3: Runner (minimal production image) ────────────────────────
FROM node:20-alpine AS runner
WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV PORT=3000
ENV HOSTNAME=0.0.0.0

RUN addgroup --system --gid 1001 nodejs \
 && adduser  --system --uid 1001 nextjs

# public/ and .next/static/ are served by server.js directly
COPY --from=builder --chown=nextjs:nodejs /app/public            ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/static      ./.next/static

# Standalone output contains server.js + minimal node_modules
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone  ./

USER nextjs
EXPOSE 3000

CMD ["node", "server.js"]
