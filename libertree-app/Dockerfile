FROM node:24-bookworm-slim AS dependencies

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --include=dev

FROM dependencies AS build

COPY . ./
RUN npm run build

FROM node:24-bookworm-slim AS runtime

WORKDIR /app
ENV NODE_ENV=production

COPY package.json package-lock.json ./
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/.next ./.next
COPY scripts/verify-runtime-mounts.mjs ./scripts/verify-runtime-mounts.mjs
COPY scripts/docker-entrypoint.sh ./scripts/docker-entrypoint.sh

RUN npm prune --omit=dev && chmod 0555 scripts/docker-entrypoint.sh

USER node
EXPOSE 3001
ENTRYPOINT ["./scripts/docker-entrypoint.sh"]
CMD ["./node_modules/.bin/next", "start", "-p", "3001"]
