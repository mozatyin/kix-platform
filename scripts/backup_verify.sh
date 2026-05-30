#!/usr/bin/env bash
# backup_verify.sh — daily verification that PG backups restore successfully.
#
# Designed to be run from cron at 03:30 SGT:
#   30 3 * * *  /path/to/scripts/backup_verify.sh >> /var/log/kix/backup_verify.log 2>&1
#
# Behaviour:
#   1. Find the most recent base backup in s3://${BACKUP_BUCKET}/pg/.
#   2. Refuse to proceed if the most-recent backup is older than 24h.
#   3. Download to a tmp dir.
#   4. Restore into a throwaway docker container running postgres:15.
#   5. Run pg_amcheck on a representative table list.
#   6. Compute a schema checksum (table names + columns) and compare with
#      the last known-good checksum stored alongside the backup.
#   7. Email ops on any failure.
#   8. Clean up.
#
# Required env:
#   BACKUP_BUCKET            — S3 bucket, e.g. kix-pg-backups
#   OPS_EMAIL                — destination for failure alerts
#   AWS_REGION               — region for `aws` cli
# Optional env:
#   MAX_BACKUP_AGE_HOURS=24
#   PG_CHECK_TABLES="users orders payments"
#   PG_IMAGE=postgres:15

set -Eeuo pipefail

BACKUP_BUCKET="${BACKUP_BUCKET:?BACKUP_BUCKET required}"
OPS_EMAIL="${OPS_EMAIL:?OPS_EMAIL required}"
MAX_BACKUP_AGE_HOURS="${MAX_BACKUP_AGE_HOURS:-24}"
PG_CHECK_TABLES="${PG_CHECK_TABLES:-users orders payments}"
PG_IMAGE="${PG_IMAGE:-postgres:15}"

TMPDIR="$(mktemp -d -t pg-restore-XXXXXX)"
CONTAINER="pg-restore-$$"
LOG_PREFIX="[backup_verify $(date -u +%FT%TZ)]"

cleanup() {
    rc=$?
    echo "${LOG_PREFIX} cleanup rc=${rc}"
    docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true
    rm -rf "${TMPDIR}"
    exit "${rc}"
}
trap cleanup EXIT

fail() {
    local subject="$1"; shift
    local body="$*"
    echo "${LOG_PREFIX} FAIL: ${subject}"
    echo "${body}"
    # Try mail; if not available, write a sentinel file we can monitor.
    if command -v mail >/dev/null 2>&1; then
        printf '%s\n' "${body}" | mail -s "[KiX backup] ${subject}" "${OPS_EMAIL}"
    else
        mkdir -p /var/lib/kix/alerts
        printf '%s\n%s\n' "${subject}" "${body}" \
            > "/var/lib/kix/alerts/backup-$(date -u +%FT%TZ).txt"
    fi
    exit 1
}

echo "${LOG_PREFIX} starting verification against s3://${BACKUP_BUCKET}/pg/"

# 1. Find most recent base backup
latest_key=$(aws s3 ls "s3://${BACKUP_BUCKET}/pg/" --recursive \
    | grep -E 'base_backup.*\.tar\.gz$' \
    | sort | tail -n 1 | awk '{print $4}')

if [[ -z "${latest_key}" ]]; then
    fail "no backups found" \
        "Listing s3://${BACKUP_BUCKET}/pg/ returned no base_backup files."
fi

echo "${LOG_PREFIX} latest backup: ${latest_key}"

# 2. Age check
last_modified=$(aws s3 ls "s3://${BACKUP_BUCKET}/${latest_key}" \
    | awk '{print $1" "$2}')
last_modified_epoch=$(date -u -d "${last_modified}" +%s 2>/dev/null \
    || date -u -j -f "%Y-%m-%d %H:%M:%S" "${last_modified}" +%s)
now_epoch=$(date -u +%s)
age_hours=$(( (now_epoch - last_modified_epoch) / 3600 ))

echo "${LOG_PREFIX} backup age: ${age_hours}h"

if (( age_hours > MAX_BACKUP_AGE_HOURS )); then
    fail "backup too old (${age_hours}h > ${MAX_BACKUP_AGE_HOURS}h)" \
        "Latest backup: ${latest_key}\nLast modified: ${last_modified}"
fi

# 3. Download
echo "${LOG_PREFIX} downloading..."
aws s3 cp "s3://${BACKUP_BUCKET}/${latest_key}" "${TMPDIR}/backup.tar.gz" --quiet

# 4. Restore into throwaway container
echo "${LOG_PREFIX} starting restore container ${CONTAINER}..."
docker run -d --name "${CONTAINER}" \
    -e POSTGRES_PASSWORD=verify \
    -e POSTGRES_DB=verify \
    "${PG_IMAGE}" >/dev/null

# Wait for postgres ready
for i in {1..30}; do
    if docker exec "${CONTAINER}" pg_isready -U postgres >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

# Copy and restore
docker cp "${TMPDIR}/backup.tar.gz" "${CONTAINER}:/tmp/backup.tar.gz"
docker exec "${CONTAINER}" bash -lc \
    "cd /var/lib/postgresql && tar -xzf /tmp/backup.tar.gz" \
    || fail "tar extract failed" "see logs"

# Bring up the data dir (this varies by backup tool; placeholder for
# pg_basebackup output — adapt to your backup pipeline).
docker exec "${CONTAINER}" bash -lc \
    "pg_ctl -D /var/lib/postgresql/data restart -m fast" \
    || fail "pg_ctl restart failed" "$(docker logs --tail=100 ${CONTAINER} 2>&1)"

# 5. pg_amcheck on critical tables
for t in ${PG_CHECK_TABLES}; do
    echo "${LOG_PREFIX} pg_amcheck on ${t}"
    docker exec "${CONTAINER}" pg_amcheck \
        --username=postgres --dbname=verify --table="${t}" \
        || fail "pg_amcheck failed on ${t}" \
            "$(docker logs --tail=50 ${CONTAINER} 2>&1)"
done

# 6. Schema checksum
schema_sum=$(docker exec "${CONTAINER}" psql -U postgres -d verify -At -c "
    SELECT md5(string_agg(table_name || ':' || column_name || ':' || data_type,
                          ',' ORDER BY table_name, ordinal_position))
    FROM information_schema.columns
    WHERE table_schema='public';
")

echo "${LOG_PREFIX} schema checksum: ${schema_sum}"

expected_sum_key="pg/expected_schema_sum.txt"
expected_sum=$(aws s3 cp "s3://${BACKUP_BUCKET}/${expected_sum_key}" - 2>/dev/null \
    | tr -d '[:space:]' || echo "")

if [[ -z "${expected_sum}" ]]; then
    echo "${LOG_PREFIX} no expected checksum recorded; seeding"
    printf '%s' "${schema_sum}" | aws s3 cp - "s3://${BACKUP_BUCKET}/${expected_sum_key}"
elif [[ "${expected_sum}" != "${schema_sum}" ]]; then
    fail "schema checksum mismatch" \
        "expected=${expected_sum} got=${schema_sum}\nSchema drift between backups — investigate."
fi

echo "${LOG_PREFIX} OK — backup verified successfully"
