# Multi-stage build for the Next.js operator dashboard.
#
# Build (from the REPO ROOT, context = ui/):
#     docker build -t office-convert-ui:dev ui/
#
# API_URL is baked into the standalone build (next.config rewrites are
# serialized at `next build` time). The default matches the compose service
# name; override for other topologies:
#     docker build --build-arg API_URL=http://my-api:8080 -t office-convert-ui:dev ui/

# ---- deps: install the full dependency tree once, cached on lockfile ----
FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

# ---- builder: compile the standalone production bundle ----
FROM node:22-alpine AS builder
WORKDIR /app
ARG API_URL=http://office-convert:8080
ENV API_URL=${API_URL} \
    NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

# ---- runner: minimal runtime, non-root (BR-UI-8) ----
FROM node:22-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# `output: 'standalone'` emits a self-contained server.js + pruned node_modules.
COPY --from=builder --chown=node:node /app/.next/standalone ./
COPY --from=builder --chown=node:node /app/.next/static ./.next/static

USER node
EXPOSE 3000

# Liveness probe (Trivy DS-0026). Uses Node 22's built-in global fetch — no curl
# or wget dependency — and honours $PORT. A 2xx/3xx from the root route means the
# standalone server is up and serving.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:'+(process.env.PORT||3000)+'/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "server.js"]
