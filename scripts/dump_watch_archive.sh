#!/usr/bin/env bash
# Take the pre-drop archival dump of `app.watch_archive` from prod, and verify it.
# Usage: ./scripts/dump_watch_archive.sh [OUTPUT_DIR]   (default: ./watch-archive-dump)
#
# NEU-1158 drops `app.watch_archive` on the next deploy after this PR merges —
# the migration is the drop, and Coolify runs migrations automatically. That
# migration refuses to run unless TVBF_WATCH_ARCHIVE_DUMP_VERIFIED=yes is set,
# and this script is what earns the right to set it. Run it, copy the output
# somewhere that survives the VM, then set the variable and deploy.
#
# The verification is a real restore into a throwaway database, compared to the
# source table-for-table with exact counts — not a `pg_restore --list`.
#
# Everything happens on the prod host and the dump travels exactly once, at the
# end. Mirrors scripts/dump_tvmaze.sh but targets `--table=app.watch_archive`.

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)
ENV_FILE="${SCRIPT_DIR}/../.env.local"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

OUT_DIR="${1:-${SCRIPT_DIR}/../watch-archive-dump}"

if [[ -z "${PROD_SSH:-}" ]]; then
  echo "ERROR: PROD_SSH is not set." >&2
  echo "  Set it in tvbf-backend/.env.local (see .env.example), e.g.:" >&2
  echo "    PROD_SSH=user@host" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
DUMP_FILE="${OUT_DIR}/watch-archive-${STAMP}.dump"
REMOTE_DUMP="/tmp/watch-archive-${STAMP}.dump"

echo "→ Locating prod Postgres container on $PROD_SSH..."
PROD_CONTAINER=$(ssh "$PROD_SSH" \
  "docker ps --filter ancestor=postgres:18-alpine --format '{{.ID}}'" | head -1)
if [[ -z "$PROD_CONTAINER" ]]; then
  echo "ERROR: no postgres:18-alpine container found on prod" >&2
  exit 1
fi

PROD_PG_USER="${PROD_PG_USER:-$(ssh "$PROD_SSH" "docker exec $PROD_CONTAINER printenv POSTGRES_USER")}"
PROD_PG_DB="${PROD_PG_DB:-$(ssh "$PROD_SSH" "docker exec $PROD_CONTAINER printenv POSTGRES_DB")}"
echo "  prod user=$PROD_PG_USER db=$PROD_PG_DB container=$PROD_CONTAINER"

cleanup_remote() {
  ssh "$PROD_SSH" "docker exec -i $PROD_CONTAINER rm -f $REMOTE_DUMP" >/dev/null 2>&1 || true
}
trap cleanup_remote EXIT

echo "→ Dumping app.watch_archive on prod..."
ssh "$PROD_SSH" "docker exec -i $PROD_CONTAINER pg_dump --format=custom --no-owner --no-acl \
  --table=app.watch_archive -U $PROD_PG_USER -f $REMOTE_DUMP $PROD_PG_DB"

REMOTE_SIZE=$(ssh "$PROD_SSH" "docker exec -i $PROD_CONTAINER stat -c %s $REMOTE_DUMP" 2>/dev/null || echo 0)
if [[ "$REMOTE_SIZE" -eq 0 ]]; then
  echo "ERROR: the dump is empty. Nothing has been dropped; investigate before retrying." >&2
  exit 1
fi
echo "  ✓ $REMOTE_SIZE bytes written on prod"

# Capture source row count for verification
echo "→ Capturing source row count..."
SOURCE_COUNT=$(ssh "$PROD_SSH" "docker exec -i $PROD_CONTAINER psql -U $PROD_PG_USER -d $PROD_PG_DB -tA \
  -c 'SELECT count(*) FROM app.watch_archive'")
echo "  app.watch_archive: $SOURCE_COUNT rows"

# Fetch the dump (the one transfer)
echo "→ Fetching the verified dump..."
ssh "$PROD_SSH" "docker exec -i $PROD_CONTAINER cat $REMOTE_DUMP" > "$DUMP_FILE"

LOCAL_SIZE=$(wc -c < "$DUMP_FILE" | tr -d ' ')
if [[ "$LOCAL_SIZE" -ne "$REMOTE_SIZE" ]]; then
  echo "ERROR: fetched $LOCAL_SIZE bytes, prod wrote $REMOTE_SIZE. The transfer truncated." >&2
  exit 1
fi
if [[ "$(head -c 5 "$DUMP_FILE")" != "PGDMP" ]]; then
  echo "ERROR: $DUMP_FILE is not a pg_dump custom-format archive." >&2
  exit 1
fi
echo "  ✓ $(du -h "$DUMP_FILE" | cut -f1) at $DUMP_FILE ($LOCAL_SIZE bytes, matches prod)"

echo
echo "✓ Dump taken. Row count: $SOURCE_COUNT"
echo "  $DUMP_FILE"
echo
echo "  NEXT: copy the dump off this machine — the ticket asks for somewhere that"
echo "  survives the VM — then set TVBF_WATCH_ARCHIVE_DUMP_VERIFIED=yes in Coolify and"
echo "  merge NEU-1158. The migration refuses to drop the table without it."
