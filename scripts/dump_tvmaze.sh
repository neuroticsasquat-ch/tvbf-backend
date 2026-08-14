#!/usr/bin/env bash
# Take the pre-drop archival dump of the prod `tvmaze` schema, and verify it.
# Usage: ./scripts/dump_tvmaze.sh [OUTPUT_DIR]   (default: ./tvmaze-archive)
#
# NEU-1051 drops `tvmaze` on the next deploy after it merges — the migration is
# the drop, and Coolify runs migrations automatically. That migration refuses to
# run unless TVBF_TVMAZE_DUMP_VERIFIED=yes is set, and this script is what earns
# the right to set it. Run it, copy the output somewhere that survives the VM,
# then set the variable and deploy.
#
# The verification is a real restore into a throwaway database, compared to the
# source table-for-table with exact counts — not a `pg_restore --list`, and not a
# count of table names, either of which would pass a restore that recreated all
# 16 tables empty.
#
# Everything happens on the prod host and the dump travels exactly once, at the
# end. Streaming it down and piping it back up for the restore would move several
# GB twice over the link that is the whole cost of this operation.
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
COUNTS_FILE="${OUT_DIR}/tvmaze-${STAMP}.counts.txt"
# Lowercased deliberately. `STAMP` carries ISO-8601's `T` and `Z`, and Postgres
# folds an unquoted identifier to lower case — so `CREATE DATABASE` would make
# `..._20260814t003038z` while `pg_restore -d` asks for the mixed-case name
# verbatim as a connection parameter and is told it does not exist.
VERIFY_DB="tvmaze_restore_check_$(printf '%s' "$STAMP" | tr '[:upper:]' '[:lower:]')"
REMOTE_DUMP="/tmp/tvmaze-${STAMP}.dump"

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

# Both the throwaway database and the remote dump go away on any exit path, so a
# failed verification cannot leave either behind.
cleanup_remote() {
  ssh "$PROD_SSH" "docker exec -i $PROD_CONTAINER psql -U $PROD_PG_USER -d postgres \
    -c 'DROP DATABASE IF EXISTS $VERIFY_DB'" >/dev/null 2>&1 || true
  ssh "$PROD_SSH" "docker exec -i $PROD_CONTAINER rm -f $REMOTE_DUMP" >/dev/null 2>&1 || true
}
trap cleanup_remote EXIT

echo "→ Dumping tvmaze on prod (3.5M episodes; this takes a while)..."
ssh "$PROD_SSH" "docker exec -i $PROD_CONTAINER pg_dump --format=custom --no-owner --no-acl \
  --schema=tvmaze -U $PROD_PG_USER -f $REMOTE_DUMP $PROD_PG_DB"

REMOTE_SIZE=$(ssh "$PROD_SSH" "docker exec -i $PROD_CONTAINER stat -c %s $REMOTE_DUMP" 2>/dev/null || echo 0)
if [[ "$REMOTE_SIZE" -eq 0 ]]; then
  echo "ERROR: the dump is empty. Nothing has been dropped; investigate before retrying." >&2
  exit 1
fi
echo "  ✓ $REMOTE_SIZE bytes written on prod"

echo "→ Test-restoring into a throwaway database on prod ($VERIFY_DB)..."
ssh "$PROD_SSH" "docker exec -i $PROD_CONTAINER psql -v ON_ERROR_STOP=1 -U $PROD_PG_USER \
  -d postgres -c 'CREATE DATABASE $VERIFY_DB'" >/dev/null

CREATED=$(ssh "$PROD_SSH" "docker exec -i $PROD_CONTAINER psql -U $PROD_PG_USER -d postgres -tA \
  -c \"SELECT count(*) FROM pg_database WHERE datname = '$VERIFY_DB'\"")
if [[ "$CREATED" -ne 1 ]]; then
  echo "ERROR: $VERIFY_DB was not created. Nothing has been dropped." >&2
  exit 1
fi

# A `--schema=tvmaze` dump carries the schema and nothing else, so the throwaway
# database has none of the `public` objects three of its indexes are built on:
# `pg_trgm` supplies `gin_trgm_ops`, and `immutable_unaccent` is this repo's own
# wrapper (see `sql_fold.py` and the `c2e451aa1ec6` migration). Creating them here
# is not a workaround — it is the verification doing its job, because **a real
# recovery needs exactly these three statements first**, and a restore that
# skipped the indexes would be reported as fine while being incomplete.
ssh "$PROD_SSH" "docker exec -i $PROD_CONTAINER psql -v ON_ERROR_STOP=1 -U $PROD_PG_USER -d $VERIFY_DB -c \"
  CREATE EXTENSION IF NOT EXISTS pg_trgm;
  CREATE EXTENSION IF NOT EXISTS unaccent;
  CREATE OR REPLACE FUNCTION public.immutable_unaccent(text) RETURNS text
    LANGUAGE sql IMMUTABLE STRICT AS \\\$\\\$ SELECT public.unaccent(\\\$1) \\\$\\\$;\"" >/dev/null

# `--exit-on-error`, so the restore has to be clean rather than merely mostly
# clean. Without it pg_restore reports "errors ignored" and carries on, which is
# how a dump missing its indexes passes for a good one.
ssh "$PROD_SSH" "docker exec -i $PROD_CONTAINER pg_restore --no-owner --no-acl --exit-on-error \
  -U $PROD_PG_USER -d $VERIFY_DB $REMOTE_DUMP"

echo "→ Comparing the restore against the source, table by table..."
# One generated UNION ALL of exact `count(*)`s, run against both databases. Exact
# counts rather than `reltuples`, which is a planner estimate and can be wrong by
# a lot on a table that has not been analysed.
COUNT_SQL=$(ssh "$PROD_SSH" "docker exec -i $PROD_CONTAINER psql -U $PROD_PG_USER -d $PROD_PG_DB -tA -c \"
  SELECT string_agg(
           format('SELECT %L AS t, count(*) AS n FROM tvmaze.%I', c.relname, c.relname),
           ' UNION ALL ')
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname = 'tvmaze' AND c.relkind = 'r'\"")

if [[ -z "$COUNT_SQL" ]]; then
  echo "ERROR: no tables found in tvmaze at the source. Investigate before retrying." >&2
  exit 1
fi

for target in "$PROD_PG_DB:source" "$VERIFY_DB:restored"; do
  ssh "$PROD_SSH" "docker exec -i $PROD_CONTAINER psql -U $PROD_PG_USER -d ${target%%:*} -tA -F',' \
    -c \"SELECT t, n FROM ($COUNT_SQL) x ORDER BY t\"" > "${OUT_DIR}/.counts.${target##*:}"
done

if ! diff -u "${OUT_DIR}/.counts.source" "${OUT_DIR}/.counts.restored"; then
  echo "ERROR: the restore does not match the source, table for table (diff above)." >&2
  echo "  Do NOT set TVBF_TVMAZE_DUMP_VERIFIED and do NOT merge NEU-1051." >&2
  rm -f "${OUT_DIR}/.counts.source" "${OUT_DIR}/.counts.restored"
  exit 1
fi
mv "${OUT_DIR}/.counts.source" "$COUNTS_FILE"
rm -f "${OUT_DIR}/.counts.restored"
sed 's/^/    /' "$COUNTS_FILE"

# An exact match of zero against zero is still a match, so the tables the drop is
# really about are named explicitly: the credits nothing else holds a copy of,
# and the spine every id in `app.watch_archive` was written from.
for tbl in show episode show_cast episode_guest_cast; do
  n=$(ssh "$PROD_SSH" "docker exec -i $PROD_CONTAINER psql -U $PROD_PG_USER -d $VERIFY_DB -tA \
    -c 'SELECT count(*) FROM tvmaze.$tbl'")
  if [[ "$n" -eq 0 ]]; then
    echo "ERROR: tvmaze.$tbl restored empty. Do NOT merge NEU-1051." >&2
    exit 1
  fi
done

echo "→ Fetching the verified dump (this is the one transfer)..."
ssh "$PROD_SSH" "docker exec -i $PROD_CONTAINER cat $REMOTE_DUMP" > "$DUMP_FILE"

# Exact byte count against what prod wrote, not merely non-empty. A truncated
# transfer is the realistic failure of this step — the link drops, the pipe ends
# early, and the file looks like a dump right up until the day it is needed.
LOCAL_SIZE=$(wc -c < "$DUMP_FILE" | tr -d ' ')
if [[ "$LOCAL_SIZE" -ne "$REMOTE_SIZE" ]]; then
  echo "ERROR: fetched $LOCAL_SIZE bytes, prod wrote $REMOTE_SIZE. The transfer truncated." >&2
  echo "  Do NOT set TVBF_TVMAZE_DUMP_VERIFIED and do NOT merge NEU-1051." >&2
  exit 1
fi
if [[ "$(head -c 5 "$DUMP_FILE")" != "PGDMP" ]]; then
  echo "ERROR: $DUMP_FILE is not a pg_dump custom-format archive." >&2
  exit 1
fi
echo "  ✓ $(du -h "$DUMP_FILE" | cut -f1) at $DUMP_FILE ($LOCAL_SIZE bytes, matches prod)"

echo
echo "✓ Dump taken, test-restored, and reconciled table-for-table."
echo "  $DUMP_FILE"
echo "  $COUNTS_FILE"
echo
echo "  NEXT: copy both off this machine — the ticket asks for somewhere that"
echo "  survives the VM — then set TVBF_TVMAZE_DUMP_VERIFIED=yes in Coolify and"
echo "  merge NEU-1051. The migration refuses to drop the schema without it."
