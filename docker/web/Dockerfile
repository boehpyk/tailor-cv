# syntax=docker/dockerfile:1
#
# `development` runs the Vite dev server (hot reload — the frontend feedback loop is the thing a
# learner interacts with most, ADR-0001). `production` builds the bundle and copies it into the
# volume nginx serves, then exits: it is a build artifact, not a long-running service.

FROM node:22-alpine AS base
WORKDIR /app
ENV NODE_ENV=production

# ---------------------------------------------------------------------------
FROM base AS deps
COPY web/package.json web/package-lock.json ./
RUN npm ci --include=dev

# ---------------------------------------------------------------------------
FROM base AS development
ENV NODE_ENV=development
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ /app/
EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]

# ---------------------------------------------------------------------------
FROM deps AS build
COPY web/ /app/
# A build failure here is a deploy failure, on purpose — it is the last place a type error can be
# caught before it becomes a blank page in production.
RUN npm run build

# ---------------------------------------------------------------------------
FROM base AS production
COPY --from=build /app/dist /opt/dist
# Publish the bundle into the shared volume for nginx, then exit. `restart: "no"` in compose.
CMD ["sh", "-c", "rm -rf /mnt/dist/* && cp -r /opt/dist/* /mnt/dist/ && echo 'web bundle published'"]
