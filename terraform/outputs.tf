output "resource_group_name" {
  description = "Resource group containing the AKS cluster."
  value       = azurerm_resource_group.this.name
}

output "cluster_name" {
  description = "AKS cluster name."
  value       = azurerm_kubernetes_cluster.this.name
}

output "location" {
  description = "Azure region."
  value       = azurerm_resource_group.this.location
}

output "kubernetes_version" {
  description = "Kubernetes version AKS provisioned."
  value       = azurerm_kubernetes_cluster.this.kubernetes_version
}

output "cluster_host" {
  description = "AKS API server endpoint."
  value       = azurerm_kubernetes_cluster.this.kube_config[0].host
  sensitive   = true
}

output "configure_kubectl" {
  description = "Run this to point kubectl at the new cluster."
  value = format(
    "az aks get-credentials --resource-group %s --name %s",
    azurerm_resource_group.this.name,
    azurerm_kubernetes_cluster.this.name,
  )
}
