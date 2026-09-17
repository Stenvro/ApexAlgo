FROM nginx:alpine

# Node.js needed to build the frontend at startup
# (VITE_* env vars are only known at runtime, after .env is generated)
RUN apk add --no-cache nodejs npm

WORKDIR /app/frontend

# Install node_modules (cached layer)
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts

# Copy frontend source + the example strategies it bundles
COPY frontend/ ./
COPY examples/ /app/examples/

COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY docker/frontend-entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 5173
ENTRYPOINT ["/entrypoint.sh"]
