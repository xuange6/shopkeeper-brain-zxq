#!/bin/sh
set -eu

load_secret() {
    variable_name="$1"
    eval "current_value=\${$variable_name:-}"
    eval "secret_file=\${${variable_name}_FILE:-}"
    if [ -z "$current_value" ] && [ -n "$secret_file" ]; then
        if [ ! -r "$secret_file" ]; then
            echo "required secret file is not readable: $variable_name" >&2
            exit 78
        fi
        secret_value="$(cat "$secret_file")"
        if [ -z "$secret_value" ]; then
            echo "required secret file is empty: $variable_name" >&2
            exit 78
        fi
        export "$variable_name=$secret_value"
    fi
}

load_secret LIFECYCLE_DATABASE_URL
load_secret LIFECYCLE_ADMIN_TOKEN
load_secret ACCESS_CONTEXT_HMAC_SECRET
load_secret NEO4J_PASSWORD
load_secret MINIO_SECRET_KEY
load_secret MONGO_PASSWORD

if [ -z "${MONGO_URL:-}" ] && [ -n "${MONGO_PASSWORD:-}" ]; then
    : "${MONGO_USERNAME:?MONGO_USERNAME is required when MONGO_PASSWORD is set}"
    export MONGO_URL="mongodb://${MONGO_USERNAME}:${MONGO_PASSWORD}@mongodb:27017/?authSource=admin"
fi

exec "$@"
