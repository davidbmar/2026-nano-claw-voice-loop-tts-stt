# Stage 1: Build nano-claw TypeScript
FROM node:20-slim AS builder
WORKDIR /app
COPY package.json tsconfig.json ./
COPY src/ src/
RUN npm install && npm run build

# Stage 2: Runtime with Python + Node
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs npm curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy built nano-claw
COPY --from=builder /app/dist/ dist/
COPY --from=builder /app/node_modules/ node_modules/
COPY package.json ./

# Copy voice pipeline
COPY voice/ voice/
RUN pip install --no-cache-dir -r voice/requirements.txt

# Create dirs for runtime data
RUN mkdir -p /root/.nano-claw/memory /app/voice/models

# Default config (API keys come via env vars)
COPY docker/default-config.json /root/.nano-claw/config.json

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8080
ENTRYPOINT ["/entrypoint.sh"]
