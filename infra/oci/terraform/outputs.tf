output "vm_public_ip" {
  description = "Public IP of the compute instance"
  value       = data.oci_core_vnic.vm_vnic.public_ip_address
}

output "adb_id" {
  description = "Autonomous DB OCID (if created)"
  value       = try(oci_database_autonomous_database.adb[0].id, null)
}

