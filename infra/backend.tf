terraform {
  backend "azurerm" {
    resource_group_name  = "sasquatch-tfstate-rg"
    storage_account_name = "sasquatchtfstatesa"
    container_name       = "tfstate"
    key                  = "tvbf-backend.tfstate"
    use_azuread_auth     = true
  }
}
