# The Next.js UI, for Amazon ECR -> Amazon ECS (Fargate).
#
# NOTE: the API URL is baked in at BUILD time, not run time. `NEXT_PUBLIC_*`
# variables are inlined into the client bundle during `next build`, so the
# browser cannot read a task-definition environment variable set later.
#
# In production this is built as `/api` — a relative path. The load balancer
# serves the UI at / and the API at /api/*, so one origin covers both and the
# image stays valid whatever hostname the ALB (or a custom domain) ends up
# with. Point it at an absolute URL only when the API is genuinely elsewhere,
# and remember that then the API's ALLOWED_ORIGINS has to list this UI.

FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci


FROM node:22-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# Must be present at build time — see the note above.
ARG NEXT_PUBLIC_API_BASE_URL
ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL
ENV NEXT_TELEMETRY_DISABLED=1

RUN npm run build


FROM node:22-alpine AS runtime
WORKDIR /app

RUN apk add --no-cache curl \
    && addgroup -g 10001 -S nodejs \
    && adduser -S -u 10001 -G nodejs nextjs

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# `output: "standalone"` emits a server bundle with only the modules actually
# reached, so node_modules never ships.
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/" || exit 1

CMD ["node", "server.js"]
