#!/usr/bin/env bash
# Verify that the episode-credits backfill landed — NEU-961.
#
# Usage: ./scripts/verify_episode_credits.sh [prod|local]   (default: prod)
#
#   prod  : check the Coolify-managed prod database over SSH.
#   local : check the local tvbf database (use after `task db:refresh` to
#           confirm the mirror you pulled carries what prod has).
#
# Exit codes:
#   0  every check passed (any INVESTIGATE findings are reported, not fatal)
#   1  at least one check landed in a STOP band — do not ship the frontend
#   2  the run has not succeeded yet, so nothing can be verified
#
# Every threshold here is a COMPLETION threshold, so the run-state gate below
# is what stops a healthy in-flight pass from reporting as a failure.
#
# The "order fidelity" section at the end matters most and is the one most
# likely to be skipped: it is the only acceptance criterion needing a live
# TV Maze call, and credit order is the whole reason the person axis was
# abandoned. Encoding it here is the point of the script.

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
TVMAZE_BASE_URL="${TVMAZE_BASE_URL:-https://api.tvmaze.com}"

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

echo "→ Checking episode-credits run state..."
RUN_ROW=$(run_sql <<'SQL'
SELECT status,
       shows_processed,
       shows_failed,
       coalesce(extract(epoch FROM (now() - last_progress_at))::bigint, -1),
       coalesce(error, '')
FROM tvmaze.ingest_run
WHERE kind = 'episode_credits_backfill'
ORDER BY started_at DESC
LIMIT 1;
SQL
)

if [[ -z "$RUN_ROW" ]]; then
  echo "✗ No episode_credits_backfill run found. The pass has never been triggered here." >&2
  exit 2
fi

# The free-text error field goes LAST so a stray '|' inside it cannot shift a
# numeric column — read gives the final variable the remainder of the line.
IFS='|' read -r RUN_STATUS RUN_PROCESSED RUN_FAILED SINCE_PROGRESS RUN_ERROR <<<"$RUN_ROW"

echo "  status=$RUN_STATUS seasons_processed=$RUN_PROCESSED seasons_failed=$RUN_FAILED"

case "$RUN_STATUS" in
  succeeded) ;;
  running)
    echo
    echo "⏳ Still running — ${RUN_PROCESSED} seasons processed, last progress ${SINCE_PROGRESS}s ago."
    echo "   Nothing can be verified yet: every threshold below is a completion threshold."
    exit 2
    ;;
  cancelled)
    echo
    echo "✗ The run was cancelled — the container restarted and the startup hook" >&2
    echo "  reaped it after INGEST_STALE_RUN_MINUTES without progress." >&2
    echo "  Re-trigger with POST /admin/backfill-episode-credits (it resumes from" >&2
    echo "  season.credits_synced_at IS NULL) and poll the NEW run_id. NEU-966's" >&2
    echo "  guard refuses the re-trigger until the dead run goes stale — up to 15 min." >&2
    exit 2
    ;;
  failed)
    echo
    echo "✗ The run failed after ${RUN_PROCESSED} seasons (${RUN_FAILED} failures)." >&2
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

echo "→ Collecting metrics (the credit scans take a few seconds)..."
METRICS=$(run_sql <<'SQL'
WITH se AS (
  SELECT count(*)                  AS seasons,
         count(credits_synced_at)  AS stamped
  FROM tvmaze.season
), ep AS (
  SELECT count(*) AS episodes FROM tvmaze.episode
), ord AS (
  -- sort_order must start at 0 and be dense per episode, on BOTH credit tables.
  -- A gap or a non-zero floor means the array index was not what got written.
  -- Crew is checked too because it is the capability that exists by no other
  -- route (ADR-0003) and nothing else would catch a fault in it.
  -- (No apostrophes in this heredoc: bash 3.2 mis-parses one inside $(...).)
  SELECT
    (SELECT count(*) FROM (
       SELECT episode_id FROM tvmaze.episode_guest_cast
       GROUP BY episode_id
       HAVING min(sort_order) <> 0 OR max(sort_order) <> count(*) - 1
     ) y) AS bad_cast,
    (SELECT count(*) FROM (
       SELECT episode_id FROM tvmaze.episode_crew
       GROUP BY episode_id
       HAVING min(sort_order) <> 0 OR max(sort_order) <> count(*) - 1
     ) z) AS bad_crew
)
SELECT
  (SELECT seasons  FROM se),
  (SELECT stamped  FROM se),
  (SELECT episodes FROM ep),
  (SELECT count(*) FROM tvmaze.episode_guest_cast),
  (SELECT count(*) FROM tvmaze.episode_crew),
  (SELECT count(*) FROM tvmaze.episode_crew_role),
  (SELECT count(DISTINCT episode_id) FROM tvmaze.episode_crew),
  (SELECT bad_cast FROM ord),
  (SELECT bad_crew FROM ord);
SQL
)

IFS='|' read -r SEASONS STAMPED EPISODES CAST_ROWS CREW_ROWS CREW_ROLES CREW_EPISODES \
  BAD_CAST_ORDER BAD_CREW_ORDER <<<"$METRICS"

if [[ -z "${BAD_CREW_ORDER:-}" ]]; then
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
  printf '  %s %-24s %-11s %s\n' "$mark" "$label" "$verdict" "$detail"
}

echo
echo "Episode-credits verification (${TARGET})"
echo "──────────────────────────────────────────────────────────────────────"

# 1. Coverage. The acceptance criterion is literally zero unstamped seasons;
#    the failed count is the only legitimate remainder.
UNSTAMPED=$(( SEASONS - STAMPED ))
if   (( UNSTAMPED == 0 )); then
  report PASS "coverage" "all $SEASONS seasons stamped"
elif (( UNSTAMPED <= RUN_FAILED )); then
  report INVESTIGATE "coverage" "$UNSTAMPED unstamped, all accounted for by $RUN_FAILED failures"
else
  report STOP "coverage" "$UNSTAMPED of $SEASONS seasons unstamped, only $RUN_FAILED failures recorded"
fi

# Contiguity — the work list being ordered by (show_id, number, id) rather than
# by season id — is deliberately NOT checked here. Step 0 refuses to verify
# anything but a succeeded run, at which point every season is stamped and a
# half-credited show is unobservable by construction; a check counting
# partially-stamped shows post-run would only re-report the coverage number
# under an ordering label, and diagnose it wrongly. The ordering property is
# asserted directly by
# tests/integration/tvmaze/test_episode_credits_backfill.py::
# test_work_list_is_ordered_by_show_then_season_number, which is where the
# acceptance criterion is actually met.

# 2. Credit volume. 77.5% of sampled episodes carry crew; guest cast is
#    sparser. Bands are deliberately wide — this check exists to catch "nothing
#    was written at all", not to police the catalogue's shape.
if (( EPISODES == 0 )); then
  report STOP "credit volume" "no episodes at all"
else
  crew_pct=$(( 100 * CREW_EPISODES / EPISODES ))
  detail="cast=$CAST_ROWS crew=$CREW_ROWS roles=$CREW_ROLES; ${crew_pct}% of episodes have crew"
  if   (( CREW_ROWS == 0 )); then
    report STOP "credit volume" "$detail — the guestcrew embed wrote nothing"
  elif (( crew_pct >= 60 && crew_pct <= 90 )); then
    report PASS "credit volume" "$detail"
  else
    report INVESTIGATE "credit volume" "$detail — expected 60–90% (sampled 77.5%)"
  fi
fi

# 3. sort_order density, both tables. The array index is the honest value only
#    if it starts at 0 with no gaps; anything else means a dedup dropped rows
#    after ranking.
if   (( BAD_CAST_ORDER == 0 && BAD_CREW_ORDER == 0 )); then
  report PASS "sort_order density" "every episode is 0..n-1 for both cast and crew"
else
  report STOP "sort_order density" \
    "$BAD_CAST_ORDER cast + $BAD_CREW_ORDER crew episodes have a gap or a non-zero floor"
fi

# 4. Failure rate. 'succeeded' already implies no consecutive-show streak hit
#    the threshold. 188,189 seasons: 0.5% is ~940.
if   (( RUN_FAILED > 940 )); then report STOP "failure rate" "$RUN_FAILED failures (>0.5%)"
elif (( RUN_FAILED >= 200 )); then report INVESTIGATE "failure rate" "$RUN_FAILED failures — sample the log for reasons"
else report PASS "failure rate" "$RUN_FAILED failures of $RUN_PROCESSED processed"
fi

# ---------------------------------------------------------------------------
# Order fidelity — the one check that needs a live API call
# ---------------------------------------------------------------------------
#
# Correct credit *order* is why the person axis was abandoned: it wrote
# sort_order as the index within that person's own credit list, so an episode's
# guest cast came out ordered by how many other gigs each actor had. Nothing in
# the database can distinguish a correctly-ordered list from a plausibly-wrong
# one — only upstream can. Automated here precisely because a manual step this
# fiddly is the one that gets skipped.

echo "→ Spot-checking credit order against TV Maze..."

# SQL passed as an argument, not a here-document: bash 3.2 (what macOS ships,
# and what runs this) mis-parses a heredoc inside `$(...)` once the whole thing
# sits inside an if-block, which is where every query below lives.
#
# The sample is picked from episodes carrying BOTH kinds of credit, so one
# request checks both embeds. Override with SPOT_EPISODE=<id> to re-check a
# specific episode.
SPOT_PICK_SQL="SELECT gc.episode_id
FROM tvmaze.episode_guest_cast gc
JOIN tvmaze.episode_crew ec ON ec.episode_id = gc.episode_id
GROUP BY gc.episode_id
HAVING count(DISTINCT gc.person_id) >= 3
ORDER BY gc.episode_id LIMIT 1;"

# Reads upstream episode JSON on stdin, prints the person ids of one embed in
# array order. The embed name is argv[1].
UPSTREAM_IDS_PY='import json,sys
d = json.load(sys.stdin)
print(",".join(str(e["person"]["id"]) for e in d.get("_embedded", {}).get(sys.argv[1], [])))'

# Same list with duplicates removed: a credit sent twice upstream is written
# once, so upstream may legitimately be the longer list.
DEDUPE_PY='import sys
seen, out = set(), []
for pid in sys.argv[1].split(","):
    if pid and pid not in seen:
        seen.add(pid); out.append(pid)
print(",".join(out))'

# compare_order <label> <ours> <upstream>
compare_order() {
  local label=$1 ours=$2 upstream=$3
  if [[ "$ours" == "$upstream" ]]; then
    report PASS "order fidelity ($label)" "episode $SPOT_EPISODE matches upstream exactly"
  elif [[ "$ours" == "$(python3 -c "$DEDUPE_PY" "$upstream")" ]]; then
    report PASS "order fidelity ($label)" "episode $SPOT_EPISODE matches upstream after dedup"
  else
    report STOP "order fidelity ($label)" \
      "episode $SPOT_EPISODE: ours=[$ours] upstream=[$upstream]"
  fi
}

if [[ -z "${SPOT_EPISODE:-}" ]]; then
  SPOT_EPISODE=$(printf '%s\n' "$SPOT_PICK_SQL" | run_sql)
fi

if [[ -z "$SPOT_EPISODE" ]]; then
  report INVESTIGATE "order fidelity" "no episode with 3+ guest cast rows and crew to sample"
elif ! command -v curl >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then
  report INVESTIGATE "order fidelity" "curl and python3 are both required for the live check"
else
  OURS_CAST=$(printf '%s\n' \
    "SELECT string_agg(person_id::text, ',' ORDER BY sort_order)
     FROM tvmaze.episode_guest_cast WHERE episode_id = ${SPOT_EPISODE};" | run_sql)
  OURS_CREW=$(printf '%s\n' \
    "SELECT string_agg(person_id::text, ',' ORDER BY sort_order)
     FROM tvmaze.episode_crew WHERE episode_id = ${SPOT_EPISODE};" | run_sql)

  EPISODE_JSON=$(curl -fsS \
    "${TVMAZE_BASE_URL}/episodes/${SPOT_EPISODE}?embed[]=guestcast&embed[]=guestcrew" \
    || echo "__FETCH_FAILED__")

  if [[ "$EPISODE_JSON" == "__FETCH_FAILED__" ]]; then
    report INVESTIGATE "order fidelity" "could not reach TV Maze for episode $SPOT_EPISODE"
  else
    UP_CAST=$(printf '%s' "$EPISODE_JSON" | python3 -c "$UPSTREAM_IDS_PY" guestcast)
    UP_CREW=$(printf '%s' "$EPISODE_JSON" | python3 -c "$UPSTREAM_IDS_PY" guestcrew)
    compare_order cast "$OURS_CAST" "$UP_CAST"
    # Crew gets its own comparison rather than riding along on cast: episode
    # crew is reachable by no other route (ADR-0003), so nothing else in this
    # script or the test suite would catch its order going wrong upstream.
    compare_order crew "$OURS_CREW" "$UP_CREW"
  fi
fi

echo "──────────────────────────────────────────────────────────────────────"

# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

echo
if (( STOPS > 0 )); then
  echo "✗ $STOPS check(s) in a STOP band. Do NOT ship NEU-964/NEU-965 until the"
  echo "  cause is understood — users would meet a wrong or partial crew list."
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
  2. Unfreeze main — merging release/v0.2.1 is safe again.
  3. Record the numbers above as a comment on NEU-961 and close it.
  4. NEU-964 and NEU-965 (the frontend crew surfaces) are now unblocked.
NEXT
fi
