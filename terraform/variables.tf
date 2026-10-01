variable "subscription_id" {
  description = "Azure subscription ID. Leave empty to use the az CLI's current subscription."
  type        = string
  default     = ""
}

variable "location" {
  description = "Azure region to deploy into."
  type        = string
  default     = "centralindia"
}

variable "environment" {
  description = "Environment tag."
  type        = string
  default     = "lab"
}

variable "resource_group_name" {
  description = "Name of the resource group to create for the cluster."
  type        = string
  default     = "rg-k8s-sentry"
}

variable "cluster_name" {
  description = "Name of the AKS cluster."
  type        = string
  default     = "aks-sentry-cluster"
}

variable "kubernetes_version" {
  description = "AKS Kubernetes version. Leave empty to let AKS pick its default for the region."
  type        = string
  default     = ""
}

variable "node_count" {
  description = "Number of nodes in the default node pool."
  type        = number
  default     = 2
}

variable "node_vm_size" {
  description = "VM size for the default node pool (Standard_B2s is a cheap lab default)."
  type        = string
  default     = "Standard_B2s_v2"
}
