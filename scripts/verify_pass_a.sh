#!/usr/bin/env bash
# Verify that pass A (the combined show refresh) landed — NEU-938.
#
# Usage: ./scripts/verify_pass_a.sh [prod|local]   (default: prod)
#
#   prod  : check the Coolify-managed prod database over SSH.
#   local : check the local tvbf database (use after `task db:refresh` to
#           confirm the mirror you pulled carries what prod has).
#
# Exit codes:
#   0  every check passed (any INVESTIGATE findings are reported, not fatal)
#   1  at least one check landed in a STOP band — do not start NEU-944
#   2  the run has not succeeded yet, so nothing can be verified
#
# Every threshold here is a COMPLETION threshold. The run-state gate below is
# what stops this script from reporting a healthy in-flight pass as a failure.

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)
ENV_FILE="${SCRIPT_DIR}/../.env.local"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

TARGET="${1:-prod}"
case "$TARGET" in
  prod|local) ;;
  *) echo "usage: $0 [prod|local]" >&2; exit 2 ;;
esac

LOCAL_PG_CONTAINER="${LOCAL_PG_CONTAINER:-tbc_postgresql_db}"
LOCAL_DB="${LOCAL_DB:-tvbf}"
LOCAL_DB_USER="${LOCAL_DB_USER:-root}"

if [[ "$TARGET" == "prod" ]]; then
  if [[ -z "${PROD_SSH:-}" ]]; then
    echo "ERROR: PROD_SSH is not set. Set it in tvbf-backend/.env.local" >&2
    exit 2
  fi
  echo "→ Locating prod Postgres container on $PROD_SSH..."
  PROD_CONTAINER=$(ssh "$PROD_SSH" \
    "docker ps --filter ancestor=postgres:18-alpine --format '{{.ID}}'" | head -1)
  if [[ -z "$PROD_CONTAINER" ]]; then
    echo "ERROR: no postgres:18-alpine container found on prod" >&2
    exit 2
  fi
  PROD_PG_USER="${PROD_PG_USER:-$(ssh "$PROD_SSH" "docker exec $PROD_CONTAINER printenv POSTGRES_USER")}"
  PROD_PG_DB="${PROD_PG_DB:-$(ssh "$PROD_SSH" "docker exec $PROD_CONTAINER printenv POSTGRES_DB")}"
  echo "  prod user=$PROD_PG_USER db=$PROD_PG_DB container=$PROD_CONTAINER"
fi

# Reads SQL on stdin, emits unaligned pipe-separated rows.
run_sql() {
  if [[ "$TARGET" == "local" ]]; then
    docker exec -i "$LOCAL_PG_CONTAINER" \
      psql -v ON_ERROR_STOP=1 -U "$LOCAL_DB_USER" -d "$LOCAL_DB" -tA -F '|'
  else
    ssh "$PROD_SSH" \
      "docker exec -i $PROD_CONTAINER psql -v ON_ERROR_STOP=1 -U $PROD_PG_USER -d $PROD_PG_DB -tA -F '|'"
  fi
}

# ---------------------------------------------------------------------------
# Step 0 — run state gate
# ---------------------------------------------------------------------------

echo "→ Checking pass A run state..."
RUN_ROW=$(run_sql <<'SQL'
SELECT status,
       shows_processed,
       shows_failed,
       coalesce(error, ''),
       coalesce(extract(epoch FROM (now() - last_progress_at))::bigint, -1)
FROM tvmaze.ingest_run
WHERE kind = 'show_refresh'
ORDER BY started_at DESC
LIMIT 1;
SQL
)

if [[ -z "$RUN_ROW" ]]; then
  echo "✗ No show_refresh run found. Pass A has never been triggered here." >&2
  exit 2
fi

IFS='|' read -r RUN_STATUS RUN_PROCESSED RUN_FAILED RUN_ERROR SINCE_PROGRESS <<<"$RUN_ROW"

echo "  status=$RUN_STATUS processed=$RUN_PROCESSED failed=$RUN_FAILED"

case "$RUN_STATUS" in
  succeeded) ;;
  running)
    echo
    echo "⏳ Pass A is still running — ${RUN_PROCESSED} shows processed, last progress ${SINCE_PROGRESS}s ago."
    echo "   Nothing can be verified yet: every threshold below is a completion threshold."
    exit 2
    ;;
  cancelled)
    echo
    echo "✗ The run was cancelled — the container restarted and the startup hook" >&2
    echo "  reaped it after INGEST_STALE_RUN_MINUTES without progress." >&2
    echo "  Re-trigger with POST /admin/refresh-shows (it resumes from" >&2
    echo "  credits_synced_at IS NULL) and poll the NEW run_id." >&2
    exit 2
    ;;
  failed)
    echo
    echo "✗ The run failed after ${RUN_PROCESSED} shows (${RUN_FAILED} failures)." >&2
    echo "  error: ${RUN_ERROR:-<none recorded>}" >&2
    exit 2
    ;;
  *)
    echo "✗ Unrecognised run status '$RUN_STATUS' — refusing to verify." >&2
    exit 2
    ;;
esac

# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

echo "→ Collecting metrics (the episode scan takes a few seconds)..."
METRICS=$(run_sql <<'SQL'
WITH sh AS (
  SELECT count(*)                    AS shows,
         count(externals_tvdb)       AS tvdb,
         count(externals_imdb)       AS imdb,
         count(credits_synced_at)    AS synced
  FROM tvmaze.show
), ep AS (
  SELECT count(*)                                  AS episodes,
         count(*) FILTER (WHERE number IS NULL)    AS specials
  FROM tvmaze.episode
), greys AS (
  SELECT
    coalesce((SELECT p.name
              FROM tvmaze.show_cast sc
              JOIN tvmaze.person p ON p.id = sc.person_id
              WHERE sc.show_id = 67
              ORDER BY sc.sort_order
              LIMIT 1), '') AS top_billed,
    (SELECT count(*) FROM tvmaze.show_cast sc
       JOIN tvmaze.person p ON p.id = sc.person_id
      WHERE sc.show_id = 67 AND p.name = 'Patrick Dempsey') AS dempsey,
    (SELECT count(*) FROM tvmaze.show_cast sc
       JOIN tvmaze.person p ON p.id = sc.person_id
      WHERE sc.show_id = 67 AND p.name = 'Sandra Oh') AS oh
)
-- One row, fixed column order. The free-text field goes LAST so that a stray
-- delimiter inside it cannot shift any numeric column (read gives the final
-- variable the remainder of the line).
SELECT
  (SELECT shows    FROM sh),
  (SELECT tvdb     FROM sh),
  (SELECT imdb     FROM sh),
  (SELECT synced   FROM sh),
  (SELECT episodes FROM ep),
  (SELECT specials FROM ep),
  (SELECT count(*) FROM tvmaze.show_cast),
  (SELECT count(*) FROM tvmaze.show_crew),
  (SELECT count(*) FROM tvmaze.person),
  (SELECT count(*) FROM tvmaze.character),
  (SELECT count(*) FROM tvmaze.show s
    WHERE s.credits_synced_at IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM tvmaze.show_crew c WHERE c.show_id = s.id)),
  (SELECT dempsey  FROM greys),
  (SELECT oh       FROM greys),
  (SELECT count(*) FROM tvmaze.show_cast WHERE show_id = 83),
  (SELECT top_billed FROM greys);
SQL
)

# Deliberately not an associative array: macOS ships bash 3.2, which has none.
IFS='|' read -r SHOWS TVDB IMDB SYNCED EPISODES SPECIALS CAST_ROWS CREW_ROWS \
  PEOPLE CHARACTERS ZERO_CREW GREYS_DEMP GREYS_OH SIMPSONS GREYS_TOP <<<"$METRICS"

if [[ -z "${SIMPSONS:-}" ]]; then
  echo "✗ Metrics query returned an unexpected shape:" >&2
  echo "  $METRICS" >&2
  exit 2
fi

# ---------------------------------------------------------------------------
# Bands
# ---------------------------------------------------------------------------

STOPS=0
INVESTIGATES=0

report() { # verdict label detail
  local verdict=$1 label=$2 detail=$3 mark
  case "$verdict" in
    PASS)        mark="✓" ;;
    INVESTIGATE) mark="!"; INVESTIGATES=$((INVESTIGATES + 1)) ;;
    STOP)        mark="✗"; STOPS=$((STOPS + 1)) ;;
  esac
  printf '  %s %-22s %-11s %s\n' "$mark" "$label" "$verdict" "$detail"
}

echo
echo "Pass A verification (${TARGET})"
echo "──────────────────────────────────────────────────────────────────────"

# 1. External id coverage. Baseline: 87,395 shows / 47,119 IMDb / 0 TVDB.
tvdb=$TVDB
if   (( tvdb >= 40000 )); then report PASS "external ids"  "tvdb=$tvdb imdb=$IMDB of $SHOWS shows"
elif (( tvdb >= 10000 )); then report INVESTIGATE "external ids" "tvdb=$tvdb — lower than expected but non-trivial"
else report STOP "external ids" "tvdb=$tvdb — NEU-922's thetvdb alias fix did not take effect"
fi

# 2. Specials. Baseline 0; expected ~6% of rows.
episodes=$EPISODES; specials=$SPECIALS
if (( episodes == 0 )); then
  report STOP "specials" "no episodes at all"
else
  pct_x100=$(( 10000 * specials / episodes ))
  pct=$(( pct_x100 / 100 )).$(printf '%02d' $(( pct_x100 % 100 )))
  # NEU-938 predicted ~6%, but that appears to have borrowed NEU-942's "5.7% of
  # episodes REFERENCED BY GUEST CREDITS are specials" — a different and much
  # smaller population. Measured at 28.8% through the run: ~1.75% of all rows.
  # What actually matters here is that specials are non-zero, since pass C's
  # guest credits FK to episodes that only this pass fetches.
  if   (( specials == 0 ));                          then report STOP "specials" "0 specials — pass C would drop guest credits on missing episodes"
  elif (( pct_x100 >= 100 && pct_x100 <= 400 ));     then report PASS "specials" "$specials of $episodes episodes (${pct}%)"
  else report INVESTIGATE "specials" "$specials of $episodes episodes (${pct}%) — expected 1–4%"
  fi
fi

# 3. Credit volume.
#
# NEU-938's "~640k cast / ~1.3M crew" came from a 45-show sample that was
# clearly weighted toward large shows. Measured against real data at 28.8%
# through the run: 77.7% of refreshed shows have NO crew rows at all (avg 3.3
# crew, 4.9 cast), while big shows do get full lists (631, 567, 533). That
# extrapolates to ~438k cast and ~292k crew — cast-heavy, where the sample
# predicted crew-heavy by 2:1. Bands below follow the measurement, not the
# sample. The real "did credits ingest work" signal is check 5.
cast_rows=$CAST_ROWS; crew_rows=$CREW_ROWS
zero_crew_pct=0
(( $SYNCED > 0 )) && zero_crew_pct=$(( 100 * $ZERO_CREW / $SYNCED ))
vol_detail="cast=$cast_rows crew=$crew_rows people=$PEOPLE chars=$CHARACTERS; ${zero_crew_pct}% of shows have no crew"
if   (( cast_rows < 150000 || crew_rows < 100000 )); then
  report STOP "credit volume" "$vol_detail — far below the measured trajectory"
elif (( cast_rows >= 350000 && cast_rows <= 600000 && crew_rows >= 200000 && crew_rows <= 420000 )); then
  report PASS "credit volume" "$vol_detail"
else
  report INVESTIGATE "credit volume" "$vol_detail — outside the ~438k/~292k projection"
fi

# 4. Billing order. sort_order is the only place upstream billing order lives.
if [[ "$GREYS_TOP" == "Ellen Pompeo" ]] && (( $GREYS_DEMP > 0 && $GREYS_OH > 0 )); then
  report PASS "billing order (67)" "Ellen Pompeo top-billed; Dempsey and Oh both present"
elif [[ "$GREYS_TOP" != "Ellen Pompeo" ]]; then
  report STOP "billing order (67)" "top-billed is '$GREYS_TOP', expected Ellen Pompeo — sort_order lost"
else
  report STOP "billing order (67)" "departed cast missing (Dempsey=$GREYS_DEMP Oh=$GREYS_OH) — fetch returns current cast only"
fi

# 5. Tail behaviour. The Simpsons exercises the batch path; expect ~1,420.
simpsons=$SIMPSONS
if   (( simpsons == 1000 )); then report STOP "tail (83)" "exactly 1000 cast rows — silent batch truncation"
elif (( simpsons >= 1200 && simpsons <= 1600 )); then report PASS "tail (83)" "$simpsons cast rows"
else report INVESTIGATE "tail (83)" "$simpsons cast rows — expected ~1,420"
fi

# 6. Failure rate. 'succeeded' already implies no 10-consecutive-failure streak.
if   (( RUN_FAILED > 450 )); then report STOP "failure rate" "$RUN_FAILED failures (>0.5%)"
elif (( RUN_FAILED >= 100 )); then report INVESTIGATE "failure rate" "$RUN_FAILED failures — sample the log for reasons"
else report PASS "failure rate" "$RUN_FAILED failures of $RUN_PROCESSED processed"
fi

# 7. Coverage completeness.
gap=$(( $SHOWS - $SYNCED - RUN_FAILED ))
(( gap < 0 )) && gap=$(( -gap ))
if (( gap <= 50 )); then
  report PASS "coverage" "$SYNCED synced + $RUN_FAILED failed ≈ $SHOWS shows"
else
  report INVESTIGATE "coverage" "$gap shows neither synced nor counted as failed"
fi

echo "──────────────────────────────────────────────────────────────────────"

# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

echo
if (( STOPS > 0 )); then
  echo "✗ $STOPS check(s) in a STOP band. Do NOT start NEU-944 (pass C, ~75h)"
  echo "  until the cause is understood."
  exit 1
fi

if (( INVESTIGATES > 0 )); then
  echo "✓ No blocking failures, but $INVESTIGATES check(s) want a look."
else
  echo "✓ All checks passed."
fi

if [[ "$TARGET" == "prod" ]]; then
  cat <<'NEXT'

Remaining post-run steps (not automated — they change repo state):
  1. Re-enable the daily-update cron:
       gh workflow enable "Daily TV Maze update"
  2. Unfreeze main — merging release/v0.2.0 is safe again.
  3. Record the numbers above as a comment on NEU-938 and close it.
NEXT
fi
