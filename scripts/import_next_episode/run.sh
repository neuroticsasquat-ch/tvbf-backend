#!/usr/bin/env bash
# Drive the Next Episode import pipeline. See README.md.
#
# Usage:
#   ./run.sh stage   <export-file>          parse + load staging, then match
#   ./run.sh review  [out.csv]              export titles needing a human pick
#   ./run.sh resolve <reviewed.csv>         load picks, re-run episode match
#   ./run.sh status                         re-print the summary
#   ./run.sh apply   <user_id> <now|airdate|airdate_floor> [--commit]
#
# Everything runs against Postgres through docker exec. Nothing here needs
# the backend container: the pipeline is SQL plus one stdlib python script.

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)

# Defaults target the local dev Postgres. Override to aim elsewhere -- see
# "Running against prod" in README.md.
PG_CONTAINER="${LOCAL_PG_CONTAINER:-tbc_postgresql_db}"
DB="${LOCAL_DB:-tvbf}"
DB_USER="${LOCAL_DB_USER:-root}"

# Set REMOTE_SSH to run the same commands against a Postgres container on
# another host, e.g. prod. stdin is forwarded, so every .sql file below works
# unchanged; only the transport differs.
REMOTE_SSH="${REMOTE_SSH:-}"

if [[ -n "$REMOTE_SSH" ]]; then
  psql_run() {
    local args=""
    for a in "$@"; do args+=" $(printf '%q' "$a")"; done
    ssh "$REMOTE_SSH" "docker exec -i ${PG_CONTAINER} psql -U ${DB_USER} -d ${DB} -v ON_ERROR_STOP=1${args}"
  }
  echo "   target: REMOTE ${REMOTE_SSH} container=${PG_CONTAINER} db=${DB} user=${DB_USER}" >&2
else
  psql_run() { docker exec -i "$PG_CONTAINER" psql -U "$DB_USER" -d "$DB" -v ON_ERROR_STOP=1 "$@"; }
  echo "   target: local container=${PG_CONTAINER} db=${DB} user=${DB_USER}" >&2
fi

usage() { sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'; exit 1; }

cmd="${1:-}"
shift || true

case "$cmd" in

stage)
  export_file="${1:-}"
  [[ -f "$export_file" ]] || { echo "ERROR: export file not found: $export_file" >&2; exit 1; }
  echo "==> parsing $export_file"
  python3 "$SCRIPT_DIR/parse_export.py" "$export_file" > "$SCRIPT_DIR/staging.sql"
  head -3 "$SCRIPT_DIR/staging.sql" | sed 's/^-- /    /'
  echo "==> loading staging schema"
  psql_run -q < "$SCRIPT_DIR/staging.sql"
  echo "==> matching shows"
  psql_run -q < "$SCRIPT_DIR/match.sql"
  echo "==> disambiguating by watch-mark coverage"
  psql_run -q < "$SCRIPT_DIR/disambiguate.sql"
  psql_run -q < "$SCRIPT_DIR/match_episodes_only.sql"
  ;;

disambiguate)
  psql_run -q < "$SCRIPT_DIR/disambiguate.sql"
  psql_run -q < "$SCRIPT_DIR/match_episodes_only.sql"
  ;;

review)
  out="${1:-$SCRIPT_DIR/review.csv}"
  psql_run -q -c "\copy (SELECT * FROM import_ne.show_review) TO STDOUT WITH CSV HEADER" > "$out"
  echo "wrote $out ($(( $(wc -l < "$out") - 1 )) candidate rows)"
  echo
  echo "Fill in chosen_show_id on ONE row per title, then:"
  echo "  ./run.sh resolve $out"
  echo "Titles you deliberately abandon: leave every row blank and they are skipped."
  ;;

resolve)
  csv="${1:-}"
  [[ -f "$csv" ]] || { echo "ERROR: reviewed csv not found: $csv" >&2; exit 1; }
  # Land the CSV in a scratch table, then take only the rows a human filled in.
  psql_run -q <<SQL
DROP TABLE IF EXISTS import_ne.review_inbox;
CREATE TABLE import_ne.review_inbox (
  title text, n_candidates int, candidate_show_id int, candidate_name text,
  premiered date, language text, status text, method text, chosen_show_id int
);
SQL
  psql_run -q -c "\copy import_ne.review_inbox FROM STDIN WITH CSV HEADER" < "$csv"
  psql_run -q <<'SQL'
INSERT INTO import_ne.show_resolution (title, show_id, source)
SELECT DISTINCT ON (title) title, chosen_show_id, 'manual'
FROM import_ne.review_inbox
WHERE chosen_show_id IS NOT NULL
ORDER BY title, chosen_show_id
ON CONFLICT (title) DO UPDATE SET show_id = EXCLUDED.show_id, source = 'manual';
SQL
  echo "==> re-running episode match with the new resolutions"
  psql_run -q < "$SCRIPT_DIR/match_episodes_only.sql"
  ;;

status)
  psql_run -q < "$SCRIPT_DIR/match_episodes_only.sql"
  ;;

apply)
  user_id="${1:-}"; mode="${2:-}"; commit="${3:-}"
  [[ -n "$user_id" ]] || { echo "ERROR: user_id required" >&2; exit 1; }
  case "$mode" in
    now|airdate|airdate_floor) ;;
    *) echo "ERROR: mode must be 'now', 'airdate', or 'airdate_floor'" >&2; exit 1 ;;
  esac
  apply=0
  [[ "$commit" == "--commit" ]] && apply=1
  psql_run -q -v "user_id=$user_id" -v "watched_at=$mode" -v "apply=$apply" < "$SCRIPT_DIR/apply.sql"
  ;;

*) usage ;;
esac
