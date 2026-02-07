variable "tenancy_ocid" {
  type        = string
  description = "OCI tenancy OCID"
}

variable "user_ocid" {
  type        = string
  description = "OCI user OCID (for API key auth)"
}

variable "fingerprint" {
  type        = string
  description = "OCI API key fingerprint"
}

variable "private_key_path" {
  type        = string
  description = "Path to OCI API private key on your local machine"
}

variable "region" {
  type        = string
  description = "OCI region, must match your tenancy home region for Always Free resources"
}

variable "compartment_ocid" {
  type        = string
  description = "Compartment OCID to provision resources into"
}

variable "admin_cidr" {
  type        = string
  description = "Your public IP in CIDR form (e.g. 203.0.113.10/32) for SSH and app ingress"
}

variable "ssh_public_key" {
  type        = string
  description = "SSH public key content (ssh-ed25519 ...)"
}

variable "instance_name" {
  type        = string
  description = "Compute instance display name"
  default     = "supportbot-vm"
}

variable "instance_shape" {
  type        = string
  description = "Compute shape (Always Free friendly: VM.Standard.A1.Flex)"
  default     = "VM.Standard.A1.Flex"
}

variable "instance_ocpus" {
  type        = number
  description = "OCPUs for flexible shapes"
  default     = 1
}

variable "instance_memory_gbs" {
  type        = number
  description = "Memory (GB) for flexible shapes"
  default     = 6
}

variable "instance_image_ocid" {
  type        = string
  description = "Image OCID for the instance (Ubuntu). Supply explicitly for reliability."
}

variable "create_adb" {
  type        = bool
  description = "If true, create an Autonomous Database (can store secrets in TF state)."
  default     = false
}

variable "adb_display_name" {
  type        = string
  description = "Autonomous DB display name"
  default     = "supportbot-adb"
}

variable "adb_db_name" {
  type        = string
  description = "Autonomous DB name (letters/numbers, up to OCI limits)"
  default     = "supportbot"
}

variable "adb_admin_password" {
  type        = string
  description = "Autonomous DB ADMIN password (sensitive)"
  sensitive   = true
  default     = null
}

