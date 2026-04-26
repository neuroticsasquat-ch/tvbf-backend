# Create the tvbf database + a non-superuser role for the API. The role's
# CONNECTION LIMIT keeps a runaway pool in tvbf from starving other apps that
# share the same Postgres server.

resource "postgresql_database" "tvbf" {
  name             = var.tvbf_db_name
  owner            = var.postgres_admin_login
  encoding         = "UTF8"
  lc_collate       = "en_US.utf8"
  lc_ctype         = "en_US.utf8"
  connection_limit = -1
}

resource "postgresql_role" "tvbf_user" {
  name             = var.tvbf_db_role
  login            = true
  password         = var.tvbf_db_password
  connection_limit = var.tvbf_db_connection_limit

  # Inherit only what the Web App needs at runtime — the role doesn't own DDL
  # privileges. Alembic migrations run as the admin login from CI.
  superuser       = false
  create_role     = false
  create_database = false
}

resource "postgresql_grant" "tvbf_user_db_connect" {
  database    = postgresql_database.tvbf.name
  role        = postgresql_role.tvbf_user.name
  object_type = "database"
  privileges  = ["CONNECT", "TEMPORARY"]
}

# Schemas (`tvmaze`, `app`) are created by Alembic during migrations — no need
# to pre-create them here. We grant USAGE + table privileges on whatever the
# admin login creates.
resource "postgresql_default_privileges" "tvbf_user_tables" {
  role        = postgresql_role.tvbf_user.name
  database    = postgresql_database.tvbf.name
  schema      = "public" # placeholder; the actual app schemas need their own grants
  owner       = var.postgres_admin_login
  object_type = "table"
  privileges  = ["SELECT", "INSERT", "UPDATE", "DELETE"]
}

# After Alembic creates the tvmaze + app schemas, grant the role usage. Run this
# once manually or via a deploy hook:
#
#   GRANT USAGE ON SCHEMA tvmaze, app TO tvbf_user;
#   GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA tvmaze, app TO tvbf_user;
#   ALTER DEFAULT PRIVILEGES IN SCHEMA tvmaze, app
#     GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO tvbf_user;
#
# This is left out of tofu because the schemas don't exist until first migration runs.
