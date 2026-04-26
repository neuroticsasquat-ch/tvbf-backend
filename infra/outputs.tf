output "web_app_name" {
  value = azurerm_linux_web_app.tvbf_backend.name
}

output "web_app_default_hostname" {
  value = azurerm_linux_web_app.tvbf_backend.default_hostname
}

output "web_app_principal_id" {
  description = "System-assigned managed identity principal ID — useful for granting the app access to other Azure resources later."
  value       = azurerm_linux_web_app.tvbf_backend.identity[0].principal_id
}

output "database_url_template" {
  description = "Connection string shape (password redacted) — for documentation, not consumption."
  value       = format("postgresql+asyncpg://%s:<password>@%s:5432/%s?ssl=require", var.tvbf_db_role, data.azurerm_postgresql_flexible_server.shared.fqdn, var.tvbf_db_name)
}
