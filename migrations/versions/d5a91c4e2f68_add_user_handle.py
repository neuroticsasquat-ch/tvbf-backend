"""Add app.user.handle and app.handle_release, backfilling from display_name

NEU-1163. `display_name` has no uniqueness constraint and `UserSearchResult`
returned `{id, display_name}` and nothing else, so two users named "Tom" were
indistinguishable at the moment someone decided whether to accept a connection
request. This adds `handle` as the stable public identifier beside the free-text
label, and backfills the accounts that already exist.

Four steps in order: add the column nullable, backfill it, add `NOT NULL` and
`uq_user_handle`, then **assert the result**. The assertion is not decoration.
`ON_ERROR_STOP` catches a statement that raised, not a `CASE` a later edit
breaks into matching everyone (NEU-1195's lesson), and here the stakes are
higher than a bad local copy: a derivation that silently produces an invalid
handle leaves an account whose own validator would refuse its identifier,
discoverable only when that user next opens settings.

**The derivation is not `sql_fold.folded()`, and that is deliberate.**
`folded()` strips every punctuation character including `_`, so it cannot
preserve word boundaries — `Tom Boone` would become `tomboone` and NEU-1194's
freshly rewritten `jeanne_briggs` would become `jeannebriggs`. The expression
below collapses those runs to `_` instead. CLAUDE.md's one-fold rule is about
*comparison*: `folded()` exists so two strings compared across a seam agree.
Nothing here is compared — this is a one-way derivation whose output is a new
value, with no second string on the other side of an equals sign. What the
ticket's note actually demands is honoured: the expression runs in Postgres and
uses `immutable_unaccent`, which is the part Python cannot reproduce, so
`Lukasz` (with the stroked L) becomes `lukasz` rather than `ukasz`. If you know
the one-fold rule and not this paragraph, you will "fix" this into `folded()`
and silently change what every existing account is called.

The `[^a-z0-9_]` strip after unaccenting removes the scripts `folded()`
deliberately passes through — Cyrillic, CJK, emoji — which is why the fold alone
was never sufficient.

**Reserved words fall back to `user_<8 hex>` rather than taking a numeric
suffix**, superseding the ticket's AC 3: a suffix would turn `Admin` into
`admin2`, which is precisely the impersonation the blocklist exists to stop — a
suffix does not weaken a name, it decorates it. That same fallback covers the
empty and too-short cases, and takes the same eight id characters
`scripts/refresh_db.sh` and NEU-1195 take, so a `User 3f4a2b1c` /
`user-3f4a2b1c@anon.local` / `user_3f4a2b1c` triple reads as one account.

A genuine collision leaves the **oldest** account holding the bare stem, ordered
by `(created_at, id)` — the ordering `user_repo.list_ids` already states, for
the same reason: an ordering that is written down is one a test can assert and a
re-run can reproduce.

Idempotent by construction: the backfill only writes rows where `handle IS
NULL`, so a re-run after the column is populated is a no-op.

`app.handle_release` starts empty. It is the ledger that makes a released handle
permanently unclaimable by anyone but its original owner (§4.2), and there is
nothing to backfill — no handle has ever been released.

`downgrade` drops both. The derived values are not recoverable afterwards, which
is why §5.4 asks for them to be read out of production and recorded on the
ticket before this is applied there.

Revision ID: d5a91c4e2f68
Revises: 0c8dc6199ab9
Create Date: 2026-08-21 00:00:00.000000+00:00

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import CITEXT, UUID

revision = "d5a91c4e2f68"
down_revision = "0c8dc6199ab9"
branch_labels = None
depends_on = None

# A snapshot of `app/handles.py:RESERVED_HANDLES` **as of this revision**, not a
# copy that tracks it. No migration in this repo imports application code — 0 of
# 52, checked — and the reason is that a migration must keep meaning what it
# meant on the day it ran, while `handles.py` is free to change. This pass has
# already run everywhere it will ever run; if the module is later extended, this
# is not re-run and must not be edited to chase it. `b7d3e02c9a41` set the
# precedent with `_EMAIL_SHAPED`.
#
# The cost is honest: several hundred strings written twice, the second copy
# frozen forever. An `app.reserved_handle` table would dissolve the duplication
# and was rejected, because it forces the check out of the Pydantic alias —
# which is sync and has no session — and so out of the 422-with-a-field-`loc`
# shape NEU-1194 chose and NEU-1196 built the client half for.
_RESERVED = """
    'about', 'abuse', 'access', 'account', 'accounts', 'add', 'admin', 'administration',
    'administrator', 'ads', 'advertise', 'advertising', 'affiliate', 'affiliates', 'ajax',
    'alert', 'alerts', 'all', 'alpha', 'amp', 'analytics', 'api', 'app', 'apps', 'asc',
    'assets', 'atom', 'auth', 'authentication', 'authorize', 'autoconfig', 'autodiscover',
    'avatar', 'backup', 'banner', 'banners', 'bbs', 'beta', 'billing', 'billings',
    'bingefriend', 'blog', 'blogs', 'board', 'bookmark', 'bookmarks', 'broadcasthost',
    'business', 'buy', 'cache', 'calendar', 'campaign', 'captcha', 'careers', 'cart', 'cas',
    'categories', 'category', 'cdn', 'cgi', 'change', 'channel', 'channels', 'chart', 'chat',
    'checkout', 'clear', 'client', 'close', 'cloud', 'cms', 'com', 'comment', 'comments',
    'community', 'compare', 'compose', 'config', 'connect', 'connections', 'contact',
    'contest', 'cookies', 'copy', 'copyright', 'count', 'cpanel', 'create', 'css', 'customer',
    'customers', 'customize', 'dashboard', 'deals', 'debug', 'delete', 'desc', 'destroy',
    'dev', 'developer', 'developers', 'disconnect', 'discover', 'discuss', 'dns', 'dns0',
    'dns1', 'dns2', 'dns3', 'dns4', 'docs', 'documentation', 'domain', 'download',
    'downloads', 'downvote', 'draft', 'drop', 'edit', 'editor', 'email', 'email_change',
    'enterprise', 'episodes', 'error', 'errors', 'event', 'events', 'example', 'exception',
    'exit', 'explore', 'export', 'extensions', 'false', 'family', 'faq', 'faqs', 'features',
    'feed', 'feedback', 'feeds', 'file', 'files', 'filter', 'follow', 'follower', 'followers',
    'following', 'fonts', 'forgot', 'forgot_password', 'forgotpassword', 'form', 'forms',
    'forum', 'forums', 'friend', 'friends', 'ftp', 'get', 'git', 'graphql', 'group', 'groups',
    'guest', 'guidelines', 'guides', 'head', 'header', 'healthz', 'help', 'hide', 'home',
    'host', 'hosting', 'hostmaster', 'htpasswd', 'http', 'httpd', 'https', 'icons', 'images',
    'imap', 'img', 'import', 'index', 'info', 'insert', 'investors', 'invitations', 'invite',
    'invites', 'invoice', 'isatap', 'issues', 'jobs', 'join', 'json', 'learn', 'legal',
    'license', 'licensing', 'like', 'limit', 'live', 'load', 'local', 'localdomain',
    'localhost', 'lock', 'login', 'logout', 'mail', 'mail0', 'mail1', 'mail2', 'mail3',
    'mail4', 'mail5', 'mail6', 'mail7', 'mail8', 'mail9', 'mailerdaemon', 'map', 'marketing',
    'marketplace', 'master', 'media', 'member', 'members', 'message', 'messages', 'metrics',
    'mis', 'mobile', 'moderator', 'modify', 'more', 'mx1', 'my_shows', 'net', 'network',
    'new', 'news', 'newsletter', 'newsletters', 'next', 'nil', 'nobody', 'noc', 'none',
    'noreply', 'notification', 'notifications', 'ns0', 'ns1', 'ns2', 'ns3', 'ns4', 'ns5',
    'ns6', 'ns7', 'ns8', 'ns9', 'null', 'oauth', 'oauth2', 'offer', 'offers', 'official',
    'online', 'openapi', 'openid', 'order', 'orders', 'overview', 'owa', 'owner', 'page',
    'pages', 'partners', 'passwd', 'password', 'pay', 'payment', 'payments', 'paypal',
    'people', 'photo', 'photos', 'pixel', 'plans', 'plugins', 'policies', 'policy', 'pop',
    'pop3', 'popular', 'portal', 'portfolio', 'post', 'postfix', 'postmaster', 'poweruser',
    'preferences', 'premium', 'press', 'previous', 'pricing', 'print', 'privacy', 'private',
    'prod', 'product', 'production', 'profile', 'profiles', 'project', 'projects', 'promo',
    'public', 'purchase', 'put', 'quota', 'readyz', 'redirect', 'redoc', 'reduce', 'refund',
    'refunds', 'register', 'registration', 'remove', 'replies', 'reply', 'report', 'request',
    'reset', 'reset_password', 'response', 'return', 'returns', 'review', 'reviews', 'root',
    'rootuser', 'rss', 'rules', 'sales', 'save', 'script', 'sdk', 'search', 'secure',
    'security', 'select', 'services', 'session', 'sessions', 'settings', 'setup', 'share',
    'shift', 'shop', 'shows', 'signin', 'signup', 'site', 'sitemap', 'sites', 'smtp', 'sort',
    'source', 'sql', 'ssh', 'ssl', 'ssladmin', 'ssladministrator', 'sslwebmaster', 'staff',
    'stage', 'staging', 'stat', 'static', 'statistics', 'stats', 'status', 'store', 'style',
    'styles', 'stylesheet', 'stylesheets', 'subdomain', 'subscribe', 'sudo', 'super',
    'superuser', 'support', 'survey', 'sync', 'sysadmin', 'system', 'tablet', 'tag', 'tags',
    'team', 'telnet', 'terms', 'test', 'testimonials', 'theme', 'themes', 'today', 'tools',
    'topic', 'topics', 'tour', 'training', 'translate', 'translations', 'trending', 'trial',
    'true', 'tvbf', 'tvbingefriend', 'undefined', 'unfollow', 'unlike', 'unsubscribe',
    'upcoming', 'update', 'upgrade', 'usenet', 'user', 'username', 'users', 'uucp', 'var',
    'verify', 'verify_email', 'video', 'view', 'void', 'vote', 'vpn', 'watch_next', 'watched',
    'webmail', 'webmaster', 'website', 'widget', 'widgets', 'wiki', 'wpad', 'write', 'www',
    'www1', 'www2', 'www3', 'www4', 'you', 'yourname', 'yourusername', 'zlib'
"""

# `[[:punct:][:space:]]+` -> `_` first, so word boundaries survive; then
# unaccent + lowercase; then drop everything still outside the charset; then
# trim leading non-letters, cap at 30, and tidy the underscores off both ends.
_STEM = """
    btrim(
      left(
        regexp_replace(
          regexp_replace(
            immutable_unaccent(lower(
              regexp_replace(display_name, '[[:punct:][:space:]]+', '_', 'g')
            )),
            '[^a-z0-9_]', '', 'g'),
          '^[^a-z]+', ''),
        30),
      '_')
"""

_HANDLE_SHAPE = "^[a-z][a-z0-9_]{2,29}$"
_ANON_SHAPE = "^user_[0-9a-f]{8}$"


def derive_handles(source: str) -> str:
    """A `SELECT id, handle` over `source`, which must expose `(id,
    display_name, created_at)`.

    One function rather than an inlined query so the derivation has exactly one
    copy — `upgrade` runs it over `app."user"`, and
    `tests/integration/app/test_handle_backfill.py` runs the identical text over
    a `VALUES` list of the display names §5.1 tabulates. A test that restated
    the expression would be asserting against its own copy of it.
    """
    return f"""
        WITH src AS ({source}),
        stems AS (
          SELECT id,
                 {_STEM} AS stem,
                 row_number() OVER (
                   PARTITION BY {_STEM} ORDER BY created_at, id
                 ) AS rn
            FROM src
        )
        SELECT id,
               CASE
                 -- Too short, unusable, or reserved: `user_<8 hex>` from this
                 -- row's own id. **Reserved words fall back rather than taking
                 -- a numeric suffix** — `admin2` is the impersonation the
                 -- blocklist exists to stop, decorated rather than weakened.
                 WHEN length(stem) < 3 THEN 'user_' || substring(id::text, 1, 8)
                 WHEN stem = ANY(ARRAY[{_RESERVED}]) THEN
                   'user_' || substring(id::text, 1, 8)
                 -- A display name that derives to the anonymisation shape takes
                 -- the shape built from its *own* id instead. Left alone it
                 -- would hand this account an identifier keyed to some other
                 -- account's id, which is the identity inheritance the pattern
                 -- refusal in `schemas.Handle` exists to prevent.
                 WHEN stem ~ '{_ANON_SHAPE}' THEN 'user_' || substring(id::text, 1, 8)
                 -- A genuine collision leaves the **oldest** account holding the
                 -- bare stem. `left(...)` is what keeps the suffixed value
                 -- inside the 30-character ceiling.
                 WHEN rn = 1 THEN stem
                 ELSE left(stem, 30 - length(rn::text)) || rn
               END AS handle
          FROM stems
    """


def upgrade() -> None:
    op.add_column("user", sa.Column("handle", CITEXT(), nullable=True), schema="app")

    op.execute(
        f"""
        WITH derived AS (
          {derive_handles('SELECT id, display_name, created_at FROM app."user" WHERE handle IS NULL')}
        )
        UPDATE app."user" AS u
           SET handle = d.handle
          FROM derived AS d
         WHERE u.id = d.id
        """
    )

    op.alter_column("user", "handle", existing_type=CITEXT(), nullable=False, schema="app")
    op.create_unique_constraint("uq_user_handle", "user", ["handle"], schema="app")

    # The migration asserts its own result (§5.3). Three properties, one
    # statement each, all of which the `CASE` above is supposed to guarantee and
    # none of which `ON_ERROR_STOP` would notice on its own. Distinctness is
    # checked even though `uq_user_handle` is already in place, because a
    # constraint failing the *deploy* is a worse place to find out than a
    # migration raising here.
    op.execute(
        f"""
        DO $$
        DECLARE bad bigint;
        BEGIN
          SELECT count(*) INTO bad FROM app."user"
           WHERE handle !~ '{_HANDLE_SHAPE}';
          IF bad > 0 THEN
            RAISE EXCEPTION 'handle backfill produced % handle(s) of invalid shape', bad;
          END IF;

          SELECT count(*) INTO bad FROM app."user"
           WHERE handle ~ '{_ANON_SHAPE}'
             AND length({_STEM}) >= 3
             AND {_STEM} !~ '{_ANON_SHAPE}'
             AND {_STEM} <> ALL(ARRAY[{_RESERVED}])
             AND handle <> 'user_' || substring(id::text, 1, 8);
          IF bad > 0 THEN
            RAISE EXCEPTION
              'handle backfill fell back to user_<hex> for % row(s) with a usable stem', bad;
          END IF;

          SELECT count(*) INTO bad FROM (
            SELECT handle FROM app."user" GROUP BY handle HAVING count(*) > 1
          ) AS dupes;
          IF bad > 0 THEN
            RAISE EXCEPTION 'handle backfill produced % duplicate handle(s)', bad;
          END IF;
        END $$;
        """
    )

    op.create_table(
        "handle_release",
        sa.Column("handle", CITEXT(), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), nullable=True),
        sa.Column(
            "released_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app.user.id"],
            name="fk_handle_release_user",
            ondelete="SET NULL",
        ),
        schema="app",
    )
    op.execute(
        """
        CREATE INDEX ix_handle_release_user_id_released_at
            ON app.handle_release (user_id, released_at DESC)
        """
    )


def downgrade() -> None:
    op.drop_table("handle_release", schema="app")
    op.drop_constraint("uq_user_handle", "user", schema="app", type_="unique")
    op.drop_column("user", "handle", schema="app")
