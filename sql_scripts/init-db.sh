#!/bin/bash
set -e

# Wait for database to start, then restore dump
echo "Initializing database from backup dump..."
pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" /tmp/isoworks_dev_pg_2026_06_29.dump
echo "Database restore completed successfully!"
