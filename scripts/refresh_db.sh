#!/usr/bin/env bash
# Refresh local tvbf from prod via SSH + docker exec.
# Usage: ./scripts/refresh_db.sh [tvmaze|app|both]   (default: tvmaze)
#
# tvmaze : refresh tvmaze schema only; local app data is preserved.
# app    : refresh app schema only; local tvmaze must already be present.
# both   : drop+recreate both schemas from prod.
#
# App-schema restores are anonymized by default. Set ANONYMIZE=0 to opt out.

set -euo pipefail

# Source .env.local from the backend dir (one level up from this script) if
# present. Lets PROD_SSH and other overrides live in a gitignored file.
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)
ENV_FILE="${SCRIPT_DIR}/../.env.local"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

MODE="${1:-tvmaze}"

if [[ -z "${PROD_SSH:-}" ]]; then
  echo "ERROR: PROD_SSH is not set." >&2
  echo "  Set it in tvbf-backend/.env.local (see .env.example), e.g.:" >&2
  echo "    PROD_SSH=user@host" >&2
  exit 1
fi

LOCAL_PG_CONTAINER="${LOCAL_PG_CONTAINER:-tbc_postgresql_db}"
LOCAL_DB="${LOCAL_DB:-tvbf}"
LOCAL_DB_USER="${LOCAL_DB_USER:-root}"
ANONYMIZE="${ANONYMIZE:-1}"

case "$MODE" in
  tvmaze|app|both) ;;
  *) echo "usage: $0 [tvmaze|app|both]" >&2; exit 1 ;;
esac

echo "→ Locating prod Postgres container on $PROD_SSH..."
PROD_CONTAINER=$(ssh "$PROD_SSH" \
  "docker ps --filter ancestor=postgres:18-alpine --format '{{.ID}}'" | head -1)
if [[ -z "$PROD_CONTAINER" ]]; then
  echo "ERROR: no postgres:18-alpine container found on prod" >&2
  exit 1
fi

echo "→ Resolving prod Postgres credentials..."
# Default to the container's POSTGRES_USER/POSTGRES_DB env vars, but allow
# overrides via PROD_PG_USER / PROD_PG_DB. Coolify-managed Postgres often
# starts with POSTGRES_DB=postgres and the real database is created on top.
PROD_PG_USER="${PROD_PG_USER:-$(ssh "$PROD_SSH" "docker exec $PROD_CONTAINER printenv POSTGRES_USER")}"
PROD_PG_DB="${PROD_PG_DB:-$(ssh "$PROD_SSH" "docker exec $PROD_CONTAINER printenv POSTGRES_DB")}"
echo "  prod user=$PROD_PG_USER db=$PROD_PG_DB container=$PROD_CONTAINER"

DUMP_FLAGS=(--format=custom --no-owner --no-acl)
case "$MODE" in
  tvmaze) DUMP_FLAGS+=(--schema=tvmaze) ;;
  app)    DUMP_FLAGS+=(--schema=app) ;;
  both)   DUMP_FLAGS+=(--schema=tvmaze --schema=app) ;;
esac

DUMP_FILE=$(mktemp -t tvbf-refresh.XXXXXX.dump)
trap 'rm -f "$DUMP_FILE"; docker exec -i "$LOCAL_PG_CONTAINER" rm -f /tmp/refresh.dump 2>/dev/null || true' EXIT

echo "→ Dumping schemas [$MODE] from prod (this streams; may take a moment for tvmaze)..."
ssh "$PROD_SSH" \
  "docker exec -i $PROD_CONTAINER pg_dump ${DUMP_FLAGS[*]} -U $PROD_PG_USER $PROD_PG_DB" \
  > "$DUMP_FILE"

echo "→ Preparing local schemas..."
case "$MODE" in
  tvmaze)
    # Preserve local app data. Drop the cross-schema FKs from app and drop the
    # tvmaze schema; pg_restore will recreate the schema + tables. We re-add
    # the FKs after restore. App rows referencing tvmaze IDs must still exist
    # in prod (true under normal use).
    docker exec -i "$LOCAL_PG_CONTAINER" psql -U "$LOCAL_DB_USER" -d "$LOCAL_DB" <<'SQL'
ALTER TABLE app.user_show_watch    DROP CONSTRAINT IF EXISTS fk_usw_show;
ALTER TABLE app.user_episode_watch DROP CONSTRAINT IF EXISTS fk_uew_episode;
DROP SCHEMA IF EXISTS tvmaze CASCADE;
SQL
    ;;
  app)
    docker exec -i "$LOCAL_PG_CONTAINER" psql -U "$LOCAL_DB_USER" -d "$LOCAL_DB" <<'SQL'
DROP SCHEMA IF EXISTS app CASCADE;
SQL
    ;;
  both)
    docker exec -i "$LOCAL_PG_CONTAINER" psql -U "$LOCAL_DB_USER" -d "$LOCAL_DB" <<'SQL'
DROP SCHEMA IF EXISTS app CASCADE;
DROP SCHEMA IF EXISTS tvmaze CASCADE;
SQL
    ;;
esac

echo "→ Restoring dump..."
docker cp "$DUMP_FILE" "$LOCAL_PG_CONTAINER:/tmp/refresh.dump"
docker exec -i "$LOCAL_PG_CONTAINER" pg_restore \
  --no-owner --no-acl -U "$LOCAL_DB_USER" -d "$LOCAL_DB" /tmp/refresh.dump

if [[ "$MODE" == "tvmaze" ]]; then
  echo "→ Re-adding cross-schema foreign keys..."
  docker exec -i "$LOCAL_PG_CONTAINER" psql -U "$LOCAL_DB_USER" -d "$LOCAL_DB" <<'SQL'
ALTER TABLE app.user_show_watch
  ADD CONSTRAINT fk_usw_show FOREIGN KEY (show_id)
  REFERENCES tvmaze.show(id) ON DELETE CASCADE;
ALTER TABLE app.user_episode_watch
  ADD CONSTRAINT fk_uew_episode FOREIGN KEY (episode_id)
  REFERENCES tvmaze.episode(id) ON DELETE CASCADE;
SQL
fi

if [[ ( "$MODE" == "app" || "$MODE" == "both" ) && "$ANONYMIZE" == "1" ]]; then
  echo "→ Anonymizing app data..."
  ANON_HASH=$(docker compose exec -T tvbf-backend python -c \
    "from tvbf.app.passwords import hash_password; print(hash_password('localdev'))")
  ADMIN_EMAIL_VAL="${ADMIN_EMAIL:-}"
  docker exec -i "$LOCAL_PG_CONTAINER" \
    psql -U "$LOCAL_DB_USER" -d "$LOCAL_DB" \
    -v "anon_hash=$ANON_HASH" \
    -v "admin_email=$ADMIN_EMAIL_VAL" <<'SQL'
UPDATE app."user" SET
  email = CASE
    WHEN nullif(:'admin_email', '') IS NOT NULL AND email = :'admin_email' THEN email
    ELSE 'user-' || substring(id::text, 1, 8) || '@anon.local'
  END,
  password_hash = :'anon_hash';
TRUNCATE app.session, app.login_attempt, app.invite CASCADE;
SQL
  if [[ -n "$ADMIN_EMAIL_VAL" ]]; then
    echo "  ✓ Admin user preserved: log in as $ADMIN_EMAIL_VAL / 'localdev'."
  else
    echo "  ✓ All users now have email user-<short>@anon.local and password 'localdev'."
    echo "    Set ADMIN_EMAIL in .env.local to keep your real email next time."
  fi
fi

echo "→ Applying any newer migrations from dev branch..."
task migrate

echo "✓ Refresh complete (mode=$MODE)."
