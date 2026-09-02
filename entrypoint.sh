#!/bin/sh
set -eu

echo "Running database migrations..."
python manage.py migrate --noinput

echo "Collecting static files..."
python manage.py collectstatic --noinput

if [ "${BOOTSTRAP_LOCAL:-0}" = "1" ]; then
  python manage.py bootstrap_saas \
    --tenant-name "${BOOTSTRAP_TENANT_NAME:-Myraid CRM}" \
    --tenant-slug "${BOOTSTRAP_TENANT_SLUG:-myraid}" \
    --admin-email "${BOOTSTRAP_ADMIN_EMAIL:-admin@myraid.local}" \
    --admin-phone "${BOOTSTRAP_ADMIN_PHONE:-9999999999}" \
    --admin-password "${BOOTSTRAP_ADMIN_PASSWORD:-ChangeMe123!}"
fi

exec "$@"
