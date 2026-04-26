locals {
  database_url = format(
    "postgresql+asyncpg://%s:%s@%s:5432/%s?ssl=require",
    var.tvbf_db_role,
    var.tvbf_db_password,
    data.azurerm_postgresql_flexible_server.shared.fqdn,
    var.tvbf_db_name,
  )
}

resource "azurerm_linux_web_app" "tvbf_backend" {
  name                = var.web_app_name
  resource_group_name = data.azurerm_resource_group.shared.name
  location            = data.azurerm_resource_group.shared.location
  service_plan_id     = data.azurerm_service_plan.shared.id

  https_only = true

  site_config {
    always_on           = true
    ftps_state          = "Disabled"
    http2_enabled       = true
    minimum_tls_version = "1.2"

    application_stack {
      docker_image_name   = var.container_image
      docker_registry_url = "https://ghcr.io"
    }

    health_check_path = "/healthz"
  }

  app_settings = {
    # Connection — points at the shared Postgres + per-app role.
    DATABASE_URL = local.database_url

    # Admin token used by the daily-update cron + manual ingest.
    ADMIN_TOKEN = var.admin_token

    # CORS + cookie + brute-force tunables (defaults from config.py work, listed
    # explicitly here so they're visible in the portal).
    CORS_ALLOWED_ORIGINS = var.cors_allowed_origins
    COOKIE_DOMAIN        = var.cookie_domain
    COOKIE_SAMESITE      = "lax"
    COOKIE_SECURE        = "true"
    SESSION_TTL_DAYS     = "30"

    LOGIN_LOCKOUT_THRESHOLD      = "5"
    LOGIN_LOCKOUT_WINDOW_MINUTES = "15"

    LOG_LEVEL = "INFO"

    WEBSITES_PORT                       = "8000"
    DOCKER_REGISTRY_SERVER_URL          = "https://ghcr.io"
    WEBSITES_ENABLE_APP_SERVICE_STORAGE = "false"
  }

  identity {
    type = "SystemAssigned"
  }

  logs {
    http_logs {
      file_system {
        retention_in_days = 7
        retention_in_mb   = 35
      }
    }
    application_logs {
      file_system_level = "Information"
    }
  }

  tags = var.common_tags

  lifecycle {
    # The container image tag changes every deploy via `az webapp config
    # container set` from CI. Don't let tofu fight it back to the pinned tag.
    ignore_changes = [
      site_config[0].application_stack[0].docker_image_name,
      app_settings["DOCKER_CUSTOM_IMAGE_NAME"],
    ]
  }
}

resource "azurerm_monitor_diagnostic_setting" "tvbf_backend" {
  name                       = "to-shared-log-analytics"
  target_resource_id         = azurerm_linux_web_app.tvbf_backend.id
  log_analytics_workspace_id = data.azurerm_log_analytics_workspace.shared.id

  enabled_log {
    category = "AppServiceHTTPLogs"
  }
  enabled_log {
    category = "AppServiceConsoleLogs"
  }
  enabled_log {
    category = "AppServiceAppLogs"
  }
  enabled_log {
    category = "AppServicePlatformLogs"
  }

  metric {
    category = "AllMetrics"
    enabled  = true
  }
}
