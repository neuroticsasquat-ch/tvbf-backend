#!/usr/bin/env bash
# Refresh local tvbf from prod via SSH + docker exec.
# Usage: ./scripts/refresh_db.sh [catalog|app|both]   (default: catalog)
#
# catalog : refresh catalog schema only; local app data is preserved.
# app     : refresh app schema only; local catalog must already be present.
# both    : drop+recreate both schemas from prod.
#
# The `tvmaze` mode went with the schema in NEU-1051. `catalog` replaces it and
# is the spine every read path uses; note it is far larger than the old mirror,
# so a full refresh streams for a while.
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

MODE="${1:-catalog}"

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
  catalog|app|both) ;;
  *) echo "usage: $0 [catalog|app|both]" >&2; exit 1 ;;
esac

# Schemas this run drops and restores from the prod dump.
case "$MODE" in
  catalog) RESTORED_SCHEMAS="'catalog'" ;;
  app)     RESTORED_SCHEMAS="'app'" ;;
  both)    RESTORED_SCHEMAS="'catalog','app'" ;;
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
  catalog) DUMP_FLAGS+=(--schema=catalog) ;;
  app)     DUMP_FLAGS+=(--schema=app) ;;
  both)    DUMP_FLAGS+=(--schema=catalog --schema=app) ;;
esac

DUMP_FILE=$(mktemp -t tvbf-refresh.XXXXXX.dump)
trap 'rm -f "$DUMP_FILE"; docker exec -i "$LOCAL_PG_CONTAINER" rm -f /tmp/refresh.dump 2>/dev/null || true' EXIT

echo "→ Dumping schemas [$MODE] from prod (this streams; may take a while for catalog)..."
ssh "$PROD_SSH" \
  "docker exec -i $PROD_CONTAINER pg_dump ${DUMP_FLAGS[*]} -U $PROD_PG_USER $PROD_PG_DB" \
  > "$DUMP_FILE"

# `DROP SCHEMA ... CASCADE` below silently drops every foreign key pointing
# into the dropped schemas, including ones defined on tables we are NOT
# restoring (app.user_show_rating, import_ne.show_resolution, ...). Those do
# not come back with the dump, so snapshot their definitions first and replay
# them after the restore.
#
# Only constraints whose OWN table lives outside the restored set need this —
# anything defined on a restored table is recreated by pg_restore.
echo "→ Snapshotting cross-schema foreign keys..."
FK_RESTORE_SQL=$(docker exec -i "$LOCAL_PG_CONTAINER" \
  psql -U "$LOCAL_DB_USER" -d "$LOCAL_DB" -tA <<SQL
SELECT format(
         'ALTER TABLE %I.%I ADD CONSTRAINT %I %s;',
         rn.nspname, rt.relname, c.conname, pg_get_constraintdef(c.oid)
       )
FROM pg_constraint c
JOIN pg_class     rt ON rt.oid = c.conrelid
JOIN pg_namespace rn ON rn.oid = rt.relnamespace
JOIN pg_class     ft ON ft.oid = c.confrelid
JOIN pg_namespace fn ON fn.oid = ft.relnamespace
WHERE c.contype = 'f'
  AND fn.nspname IN ($RESTORED_SCHEMAS)
  AND rn.nspname NOT IN ($RESTORED_SCHEMAS)
ORDER BY rn.nspname, rt.relname, c.conname;
SQL
)

if [[ -n "$FK_RESTORE_SQL" ]]; then
  echo "$FK_RESTORE_SQL" | sed 's/^/    /'
else
  echo "    (none)"
fi

echo "→ Preparing local schemas..."
DROP_SQL=""
case "$MODE" in
  catalog) DROP_SQL="DROP SCHEMA IF EXISTS catalog CASCADE;" ;;
  app)     DROP_SQL="DROP SCHEMA IF EXISTS app CASCADE;" ;;
  both)    DROP_SQL="DROP SCHEMA IF EXISTS app CASCADE; DROP SCHEMA IF EXISTS catalog CASCADE;" ;;
esac
docker exec -i "$LOCAL_PG_CONTAINER" \
  psql -v ON_ERROR_STOP=1 -U "$LOCAL_DB_USER" -d "$LOCAL_DB" -c "$DROP_SQL"

# Nothing between here and the anonymiser may exit on its own (NEU-1195). Both
# steps below can legitimately fail against a local database whose `catalog` is
# behind prod's, and both used to `exit` where they now record a flag -- which
# meant `set -e` tore the script down *after* pg_restore had written every row
# and *before* the anonymiser ran, leaving production PII in the local database
# and reporting a failure that read like nothing had happened. Observed
# 2026-08-19: five real users, one display name that is an email address, five
# `auth_token` rows and 9,359 `watch_archive` rows, all sitting locally. The
# run still ends non-zero, at the consolidated gate below -- but it makes the
# data safe first, because a partial restore holds exactly the same PII a whole
# one does.
RESTORE_FAILED=""
FK_READD_FAILED=""
ANONYMIZED=""

echo "→ Restoring dump..."
docker cp "$DUMP_FILE" "$LOCAL_PG_CONTAINER:/tmp/refresh.dump"
docker exec -i "$LOCAL_PG_CONTAINER" pg_restore \
  --no-owner --no-acl -U "$LOCAL_DB_USER" -d "$LOCAL_DB" /tmp/refresh.dump \
  || RESTORE_FAILED=1

if [[ -n "$FK_RESTORE_SQL" ]]; then
  echo "→ Re-adding cross-schema foreign keys..."
  if ! printf '%s\n' "$FK_RESTORE_SQL" \
    | docker exec -i "$LOCAL_PG_CONTAINER" \
        psql -v ON_ERROR_STOP=1 -U "$LOCAL_DB_USER" -d "$LOCAL_DB"; then
    echo "ERROR: could not re-add a foreign key after the restore." >&2
    echo "  This usually means a local row references a row that is not in the" >&2
    echo "  prod dump — e.g. a My Shows entry for a show prod no longer has." >&2
    echo "  Delete the offending rows and re-add the constraint by hand:" >&2
    printf '%s\n' "$FK_RESTORE_SQL" | sed 's/^/    /' >&2
    FK_READD_FAILED=1
  fi
fi

if [[ ( "$MODE" == "app" || "$MODE" == "both" ) && "$ANONYMIZE" == "1" ]]; then
  echo "→ Anonymizing app data..."
  ANON_HASH=$(docker compose exec -T tvbf-backend python -c \
    "from tvbf.app.passwords import hash_password; print(hash_password('localdev'))")
  ADMIN_EMAIL_VAL="${ADMIN_EMAIL:-}"
  docker exec -i "$LOCAL_PG_CONTAINER" \
    psql -v ON_ERROR_STOP=1 -U "$LOCAL_DB_USER" -d "$LOCAL_DB" \
    -v "anon_hash=$ANON_HASH" \
    -v "admin_email=$ADMIN_EMAIL_VAL" <<'SQL'
UPDATE app."user" SET
  email = CASE
    WHEN nullif(:'admin_email', '') IS NOT NULL AND email = :'admin_email' THEN email
    ELSE 'user-' || substring(id::text, 1, 8) || '@anon.local'
  END,
  -- `display_name` is rewritten unconditionally (NEU-1195), never only where it
  -- looks like an address. A conditional rule would be a second copy of
  -- NEU-1194's email-shaped test living in shell SQL, and it would still leave
  -- real people's real names sitting in a local copy -- the same class of data
  -- this statement exists to strip. Nulling the column is not an option either:
  -- it is NOT NULL and renders as an `h1`, so a refresh would produce blank
  -- headings across the friends surfaces and every list that names a user.
  --
  -- The eight characters are deliberately the same eight `email` takes above,
  -- which buys two things. `User 3f4a2b1c` is visibly the same account as
  -- `user-3f4a2b1c@anon.local`, which is what you want when you are staring at
  -- a local friends list working out who is who. And because `email` carries a
  -- unique index while `display_name` does not, a prefix collision is one event
  -- rather than two: it surfaces at `uq_user_email`, so this column's
  -- uniqueness is enforced by that constraint rather than merely hoped for.
  -- Changing the email derivation alone silently removes the only collision
  -- check this one has.
  display_name = CASE
    WHEN nullif(:'admin_email', '') IS NOT NULL AND email = :'admin_email' THEN display_name
    ELSE 'User ' || substring(id::text, 1, 8)
  END,
  password_hash = :'anon_hash';
-- `watch_archive` denormalises the real email and display name onto every row
-- (NEU-1029), so anonymising `app."user"` alone would leave them sitting in a
-- local copy. It cannot be UPDATEd -- the append-only trigger forbids that --
-- and TRUNCATE is the right answer anyway: the archive is a production
-- artifact, and `task archive:watches` regenerates a local one on demand.
-- `user_recommendation_set.compiled_payload` is the same problem one table over
-- (NEU-1106): it is a second copy of the user's watch history, and anonymising
-- `app."user"` renames the account without touching what the set holds about
-- them. `raw_response` carries the model's prose about that history too. The
-- sets are cheap to regenerate and worthless out of their own week, so they
-- truncate rather than being rewritten; `user_recommendation` goes with them on
-- the CASCADE.
-- `auth_token.payload` is a third copy one table over (NEU-1195): the email
-- change flow stores `{"new_email": ...}` there, so a pending change carries a
-- real address straight past the rewrite above. Truncated rather than
-- rewritten because a token is a production artifact worthless outside its own
-- expiry window, and `session` goes in the same statement -- so nothing local
-- is left holding a token it could still redeem.
-- `auth_attempt` is the IP-keyed throttle's counter (NEU-1160). It carries no
-- email, but it does carry real users' addresses, which is the same class of
-- copy `login_attempt.ip` is and which this statement already truncates one
-- table over. It is also a counter over a window measured in minutes, so a
-- restored one says nothing true locally -- there is nothing to preserve and a
-- reason not to keep it.
TRUNCATE app.session, app.login_attempt, app.auth_attempt, app.invite,
         app.auth_token, app.watch_archive, app.user_recommendation_set CASCADE;
SQL

  # ON_ERROR_STOP above catches a statement that raised. It cannot catch a CASE
  # a later edit breaks into matching every row, because wrong data is not an
  # error -- so assert the result. Asserting the derived form rather than the
  # absence of an address is deliberate: counting '%@%.%' survivors tests only
  # the one shape we happened to fear, says nothing about real names, and
  # contradicts the admin carve-out whenever the operator's own display name is
  # an address.
  ANON_LEFT=$(docker exec -i "$LOCAL_PG_CONTAINER" \
    psql -v ON_ERROR_STOP=1 -U "$LOCAL_DB_USER" -d "$LOCAL_DB" -tA \
    -v "admin_email=$ADMIN_EMAIL_VAL" <<'SQL'
SELECT count(*) FROM app."user"
 WHERE display_name !~ '^User [0-9a-f]{8}$'
   AND (nullif(:'admin_email', '') IS NULL OR email <> :'admin_email');
SQL
  )
  if [[ "$ANON_LEFT" != "0" ]]; then
    echo "ERROR: anonymization left $ANON_LEFT display_name value(s) unrewritten." >&2
    echo "  The local database still holds production display names. Do not use it." >&2
    exit 1
  fi

  ANONYMIZED=1

  if [[ -n "$ADMIN_EMAIL_VAL" ]]; then
    echo "  ✓ Admin user preserved (email and display name): log in as $ADMIN_EMAIL_VAL / 'localdev'."
  else
    echo "  ✓ All users now have email user-<short>@anon.local, display name"
    echo "    'User <short>', and password 'localdev'."
    echo "    Set ADMIN_EMAIL in .env.local to keep your real email next time."
  fi
fi

# The consolidated gate the two deferred failures above land on. It sits ahead
# of `task migrate` deliberately: a schema that did not restore cleanly is not
# one to apply migrations to, and the exit code is the only thing standing
# between a half-restored database and a developer who believes it is whole.
if [[ -n "$RESTORE_FAILED" || -n "$FK_READD_FAILED" ]]; then
  echo "ERROR: the restore did not complete cleanly (see the errors above)." >&2
  if [[ -n "$ANONYMIZED" ]]; then
    echo "  The app schema WAS anonymized before this check, so the local database" >&2
    echo "  holds no production PII -- but it is missing whatever could not be" >&2
    echo "  applied, and no migrations have been run against it." >&2
  elif [[ "$MODE" == "app" || "$MODE" == "both" ]]; then
    echo "  ANONYMIZE=0 was set, so this database holds production data as-is." >&2
  fi
  # The cause -- and so the fix -- differs by mode, and naming only one of them
  # sends you in a circle: an earlier draft of this message told a `catalog` run
  # to fix itself by running `task db:refresh`, which is the command that had
  # just failed.
  echo "  The usual cause is a row referencing a catalog row the prod dump does not" >&2
  echo "  have. Which row is stale depends on the mode:" >&2
  echo "    app     - the local catalog is behind prod's; run 'task db:refresh' first." >&2
  echo "    catalog - the referencing row is itself stale (often import_ne staging" >&2
  echo "              data); delete it, then re-add the constraint printed above." >&2
  exit 1
fi

echo "→ Applying any newer migrations from dev branch..."
task migrate

echo "✓ Refresh complete (mode=$MODE)."
