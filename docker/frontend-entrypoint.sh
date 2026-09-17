#!/bin/sh
set -e

# Wait for backend to generate .env + certs
echo "[frontend] Waiting for backend to finish setup..."
while [ ! -f /app/data/.env ]; do sleep 1; done
while [ ! -f /app/data/cert/cert.pem ]; do sleep 1; done
echo "[frontend] Backend config ready"

# Symlinks so Vite finds .env at /app/.env (envDir: '../')
# and certs at /app/.cert/
ln -sf /app/data/.env /app/.env
ln -sf /app/data/cert /app/.cert

# Fill in the CSP connect-src extra origin. With the same-origin nginx
# proxy, 'self' covers the default setup; __API_ORIGIN__ only matters
# when a custom VITE_API_BASE_URL points the client at another origin.
API_ORIGIN=$(grep '^VITE_API_BASE_URL=' /app/data/.env | cut -d= -f2- | sed 's|/*$||')
# Sanitize: only accept a plain https origin (no spaces, quotes, or CSP
# metacharacters) so a malformed .env value cannot break the nginx config.
if [ -z "$API_ORIGIN" ]; then
    API_ORIGIN="https://localhost:8000"
elif ! echo "$API_ORIGIN" | grep -qE '^https://[A-Za-z0-9.:-]+$'; then
    echo "[frontend] WARNING: VITE_API_BASE_URL '${API_ORIGIN}' is not a valid https origin; falling back to https://localhost:8000"
    API_ORIGIN="https://localhost:8000"
fi
sed -i "s|__API_ORIGIN__|${API_ORIGIN}|g" /etc/nginx/conf.d/default.conf
echo "[frontend] CSP connect-src allows 'self' and ${API_ORIGIN}"

# Build frontend if needed (skip if env and source haven't changed since last build)
SRC_HASH=$(find /app/frontend/src /app/examples -type f -exec md5sum {} + | sort | md5sum | cut -d' ' -f1)
ENV_HASH=$(md5sum /app/data/.env | cut -d' ' -f1)
ENV_HASH="${ENV_HASH}-${SRC_HASH}"
NEEDS_BUILD=false
[ ! -f /app/frontend/dist/index.html ] && NEEDS_BUILD=true
[ ! -f /app/data/.frontend-env-hash ] && NEEDS_BUILD=true
if [ "$NEEDS_BUILD" = false ]; then
    STORED_HASH=$(cat /app/data/.frontend-env-hash 2>/dev/null || echo "")
    [ "$ENV_HASH" != "$STORED_HASH" ] && NEEDS_BUILD=true
fi

if [ "$NEEDS_BUILD" = true ]; then
    echo "[frontend] Building frontend..."
    cd /app/frontend
    npx vite build 2>&1 | tail -5
    echo "$ENV_HASH" > /app/data/.frontend-env-hash
    echo "[frontend] Build complete"
else
    echo "[frontend] Using cached build"
fi

# Copy to nginx html dir
rm -rf /usr/share/nginx/html/*
cp -r /app/frontend/dist/* /usr/share/nginx/html/

# Copy certs for nginx SSL
mkdir -p /etc/nginx/certs
cp /app/data/cert/cert.pem /etc/nginx/certs/
cp /app/data/cert/key.pem /etc/nginx/certs/

echo "[frontend] Starting nginx on https://0.0.0.0:5173"
exec nginx -g 'daemon off;'
