#!/usr/bin/env bash
# Take the pre-drop archival dump of the prod `tvmaze` schema, and verify it.
# Usage: ./scripts/dump_tvmaze.sh [OUTPUT_DIR]   (default: ./tvmaze-archive)
#
# NEU-1051 drops `tvmaze` on the next deploy after it merges — the migration is
# the drop, and Coolify runs migrations automatically. This script is the
# recovery path, and the ticket's first acceptance criterion is that it has both
# run and been test-restored *before* that merge. Run it, keep the output
# somewhere that survives the VM, and only then merge.
#
# The verification is a real restore into a throwaway database on the prod
# Postgres, not a `pg_restore --list`. A dump that lists cleanly and fails to
# restore is the exact failure this is insuring against.
#
# Needs PROD_SSH, same as scripts/refresh_db.sh.

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)
ENV_FILE="${SCRIPT_DIR}/../.env.local"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

OUT_DIR="${1:-${SCRIPT_DIR}/../tvmaze-archive}"

if [[ -z "${PROD_SSH:-}" ]]; then
  echo "ERROR: PROD_SSH is not set." >&2
  echo "  Set it in tvbf-backend/.env.local (see .env.example), e.g.:" >&2
  echo "    PROD_SSH=user@host" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
DUMP_FILE="${OUT_DIR}/tvmaze-${STAMP}.dump"
VERIFY_DB="tvmaze_restore_check_${STAMP}"

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

echo "→ Recording pre-dump row counts..."
COUNTS_FILE="${OUT_DIR}/tvmaze-${STAMP}.counts.txt"
ssh "$PROD_SSH" "docker exec -i $PROD_CONTAINER psql -U $PROD_PG_USER -d $PROD_PG_DB -tA -F',' -c \"
  SELECT c.relname, c.reltuples::bigint
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname = 'tvmaze' AND c.relkind = 'r'
   ORDER BY c.relname\"" > "$COUNTS_FILE"
sed 's/^/    /' "$COUNTS_FILE"

echo "→ Dumping tvmaze from prod (3.5M episodes; this takes a while)..."
ssh "$PROD_SSH" \
  "docker exec -i $PROD_CONTAINER pg_dump --format=custom --no-owner --no-acl --schema=tvmaze -U $PROD_PG_USER $PROD_PG_DB" \
  > "$DUMP_FILE"

if [[ ! -s "$DUMP_FILE" ]]; then
  echo "ERROR: the dump is empty. Nothing has been dropped; investigate before retrying." >&2
  exit 1
fi
echo "  ✓ $(du -h "$DUMP_FILE" | cut -f1) written to $DUMP_FILE"

echo "→ Test-restoring into a throwaway database on prod ($VERIFY_DB)..."
# The restore runs on the prod host so the dump does not have to travel twice.
# The database is dropped again below, in a trap, so a failed verification
# cannot leave one behind.
cleanup_verify_db() {
  ssh "$PROD_SSH" "docker exec -i $PROD_CONTAINER psql -U $PROD_PG_USER -d postgres \
    -c 'DROP DATABASE IF EXISTS $VERIFY_DB'" >/dev/null 2>&1 || true
}
trap cleanup_verify_db EXIT

ssh "$PROD_SSH" "docker exec -i $PROD_CONTAINER psql -v ON_ERROR_STOP=1 -U $PROD_PG_USER -d postgres \
  -c 'CREATE DATABASE $VERIFY_DB'" >/dev/null

ssh "$PROD_SSH" "docker exec -i $PROD_CONTAINER pg_restore --no-owner --no-acl \
  -U $PROD_PG_USER -d $VERIFY_DB" < "$DUMP_FILE"

echo "→ Comparing restored row counts against the source..."
RESTORED=$(ssh "$PROD_SSH" "docker exec -i $PROD_CONTAINER psql -U $PROD_PG_USER -d $VERIFY_DB -tA -c \"
  SELECT count(*) FROM information_schema.tables WHERE table_schema = 'tvmaze'\"")
SOURCE=$(wc -l < "$COUNTS_FILE" | tr -d ' ')
echo "    tables in dump: $RESTORED, tables at source: $SOURCE"
if [[ "$RESTORED" -ne "$SOURCE" ]]; then
  echo "ERROR: the restore has $RESTORED tables against the source's $SOURCE." >&2
  echo "  Do NOT merge NEU-1051 until this reconciles." >&2
  exit 1
fi

# The two tables the drop is really about: the credits nothing else holds a
# copy of, and the spine every id in `app.watch_archive` was written from.
for tbl in show episode show_cast episode_guest_cast; do
  n=$(ssh "$PROD_SSH" "docker exec -i $PROD_CONTAINER psql -U $PROD_PG_USER -d $VERIFY_DB -tA \
    -c 'SELECT count(*) FROM tvmaze.$tbl'")
  echo "    tvmaze.$tbl restored: $n rows"
  if [[ "$n" -eq 0 ]]; then
    echo "ERROR: tvmaze.$tbl restored empty. Do NOT merge NEU-1051." >&2
    exit 1
  fi
done

echo
echo "✓ Dump taken and test-restored."
echo "  $DUMP_FILE"
echo "  $COUNTS_FILE"
echo
echo "  NEXT: copy both off this machine — the ticket asks for somewhere that"
echo "  survives the VM — and only then merge NEU-1051."
