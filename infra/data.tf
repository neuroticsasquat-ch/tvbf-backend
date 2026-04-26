# Look up brand-shared resources by name. These were created by sasquatch-infra/.

data "azurerm_resource_group" "shared" {
  name = var.shared_resource_group_name
}

data "azurerm_service_plan" "shared" {
  name                = var.shared_app_service_plan_name
  resource_group_name = data.azurerm_resource_group.shared.name
}

data "azurerm_log_analytics_workspace" "shared" {
  name                = var.shared_log_analytics_name
  resource_group_name = data.azurerm_resource_group.shared.name
}

data "azurerm_postgresql_flexible_server" "shared" {
  name                = var.shared_postgres_server_name
  resource_group_name = data.azurerm_resource_group.shared.name
}
