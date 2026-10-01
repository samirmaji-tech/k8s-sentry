terraform {
  required_version = ">= 1.5.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.117"
    }
  }

  # ---- Remote state (recommended for real use) --------------------------
  # Uncomment and configure an Azure Storage backend so state is shared and
  # not stored on your laptop. Create the storage account/container first.
  #
  # backend "azurerm" {
  #   resource_group_name  = "rg-tfstate"
  #   storage_account_name = "sttfstate<unique>"
  #   container_name       = "tfstate"
  #   key                  = "k8s-sentry.terraform.tfstate"
  # }
}

provider "azurerm" {
  features {}

  # With azurerm ~> 3.x, Terraform automatically uses the subscription that
  # `az account show` reports (i.e. whatever you selected with `az account set`),
  # so no keys are needed after `az login`. To pin a specific subscription,
  # pass -var="subscription_id=..." or set the ARM_SUBSCRIPTION_ID env var.
  subscription_id = var.subscription_id != "" ? var.subscription_id : null
}
