terraform {
  required_version = ">= 1.8.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    postgresql = {
      source  = "cyrilgdn/postgresql"
      version = "~> 1.23"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}
