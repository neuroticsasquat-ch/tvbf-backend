variable "subscription_id" {
  type = string
}

# These four match the names exported by sasquatch-infra/. Keep in sync if the
# shared layer's variables change.
variable "shared_resource_group_name" {
  type    = string
  default = "sasquatch-shared-rg"
}

variable "shared_app_service_plan_name" {
  type    = string
  default = "sasquatch-asp"
}

variable "shared_log_analytics_name" {
  type    = string
  default = "sasquatch-logs"
}

variable "shared_postgres_server_name" {
  type    = string
  default = "sasquatch-pg"
}

variable "postgres_admin_login" {
  type    = string
  default = "pgadmin"
}

variable "postgres_admin_password" {
  description = "Admin password for the shared Postgres server. Used only to provision the tvbf database + role; the Web App connects as tvbf_user."
  type        = string
  sensitive   = true
}

# Identity for the per-app database role.
variable "tvbf_db_name" {
  type    = string
  default = "tvbf"
}

variable "tvbf_db_role" {
  type    = string
  default = "tvbf_user"
}

variable "tvbf_db_password" {
  description = "Password for tvbf_user. Generate a long random and rotate as needed."
  type        = string
  sensitive   = true
}

variable "tvbf_db_connection_limit" {
  description = "Per-role connection limit; protects other apps from a runaway tvbf pool."
  type        = number
  default     = 20
}

# Backend Web App.
variable "web_app_name" {
  type    = string
  default = "tvbf-backend"
}

variable "container_image" {
  description = "Initial GHCR image to deploy. CI passes ghcr.io/<owner>/tvbf-backend:main via TF_VAR_container_image; deploy.yml updates the running image out-of-band, and lifecycle.ignore_changes keeps tofu from fighting it back. Override locally via tfvars if you need to apply from your laptop."
  type        = string
  default     = "ghcr.io/REPLACE_ME/tvbf-backend:main"
}

variable "admin_token" {
  description = "ADMIN_TOKEN env var for the API. Used by daily-update cron + manual ingest."
  type        = string
  sensitive   = true
}

variable "cors_allowed_origins" {
  type    = string
  default = "https://app.tvbingefriend.com"
}

variable "cookie_domain" {
  type    = string
  default = ".tvbingefriend.com"
}

variable "common_tags" {
  type = map(string)
  default = {
    brand     = "neuroticsasquatch"
    app       = "tvbf"
    component = "backend"
    managedBy = "opentofu"
    repo      = "tvbf-backend"
  }
}
