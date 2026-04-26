provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
}

# Used to create the per-app database + role on the brand-shared Postgres server.
provider "postgresql" {
  host            = data.azurerm_postgresql_flexible_server.shared.fqdn
  port            = 5432
  database        = "postgres"
  username        = var.postgres_admin_login
  password        = var.postgres_admin_password
  sslmode         = "require"
  connect_timeout = 15
  superuser       = false
}
