# --- Build stage ---------------------------------------------------------
FROM node:20-slim AS build

WORKDIR /app

COPY package.json ./
RUN npm install

COPY . .

# Vite inlines VITE_* env vars at BUILD time, so this has to be a build arg
# (not a runtime `environment:` in docker-compose.yml) - see
# frontend/src/lib/config.ts and README.md's "Environment variables"
# section. The browser talks to the backend directly, hence the
# localhost:8000 default.
ARG VITE_API_BASE_URL=http://localhost:8000
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL

RUN npm run build

# --- Serve stage -----------------------------------------------------------
FROM node:20-slim

WORKDIR /app

RUN npm install -g serve

COPY --from=build /app/dist ./dist

EXPOSE 4173

CMD ["serve", "-s", "dist", "-l", "4173"]
