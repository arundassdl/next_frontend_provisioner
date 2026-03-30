# Injected by next_frontend_provisioner — managed by NFP
# 4-stage build: deps → builder → runner → final

FROM node:20-alpine AS base
WORKDIR /app

# ── Stage 1: Install dependencies ────────────────────────────────────
FROM base AS deps
COPY package.json yarn.lock* package-lock.json* pnpm-lock.yaml* ./
RUN \
  if [ -f yarn.lock ]; then yarn install --frozen-lockfile; \
  elif [ -f package-lock.json ]; then npm ci; \
  elif [ -f pnpm-lock.yaml ]; then corepack enable pnpm && pnpm install --frozen-lockfile; \
  else npm install; \
  fi

# ── Stage 2: Build ────────────────────────────────────────────────────
FROM base AS builder
ARG NEXT_PUBLIC_FRAPPE_URL
ARG NEXT_PUBLIC_FRAPPE_HOSTNAME

COPY --from=deps /app/node_modules ./node_modules
COPY . .

# Ensure standalone output is enabled even if next.config.js doesn't have it
ENV NEXT_PRIVATE_STANDALONE=true

RUN npm run build

# ── Stage 3: Runner ───────────────────────────────────────────────────
FROM node:20-alpine AS runner
WORKDIR /app

ENV NODE_ENV=production
ENV PORT=3000
ENV HOSTNAME=0.0.0.0

RUN addgroup --system --gid 1001 nodejs && \
    adduser  --system --uid 1001 nextjs

# Copy standalone output if it exists, otherwise copy full .next
RUN mkdir -p .next/standalone .next/static public

COPY --from=builder /app/public* ./public/
COPY --from=builder /app/.next/static ./.next/static

# Try standalone first, fall back to full next start
COPY --from=builder /app/.next/standalone* ./.next/standalone/
COPY --from=builder /app/.next/server* ./.next/server/
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/package.json ./package.json
COPY --from=builder /app/.next ./.next

RUN chown -R nextjs:nodejs /app

USER nextjs

EXPOSE 3000

# Start: prefer standalone server.js, fall back to `next start`
CMD ["sh", "-c", "if [ -f .next/standalone/server.js ]; then node .next/standalone/server.js; else npx next start; fi"]
