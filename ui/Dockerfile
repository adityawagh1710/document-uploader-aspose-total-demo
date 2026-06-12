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

CMD ["node", "server.js"]
