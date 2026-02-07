## Terraform (OCI) — VCN + VM (+ optional Autonomous DB)

### Prereqs

- Terraform `>= 1.6`
- OCI API key auth configured (you’ll need: tenancy OCID, user OCID, fingerprint, private key path, region)

### Minimal apply

If you don’t know your current public IP yet, you can get it quickly (then append `/32`):

```bash
curl -s ifconfig.me
```

Create `terraform.tfvars`:

```hcl
tenancy_ocid       = "ocid1.tenancy.oc1..xxxx"
user_ocid          = "ocid1.user.oc1..xxxx"
fingerprint        = "aa:bb:cc:..."
private_key_path   = "/path/to/oci_api_key.pem"
region             = "eu-frankfurt-1"
compartment_ocid   = "ocid1.compartment.oc1..xxxx"
admin_cidr         = "203.0.113.10/32" # your public IP + /32
ssh_public_key     = "ssh-ed25519 AAAA..."
instance_image_ocid = "ocid1.image.oc1..xxxx" # Ubuntu image OCID in your region
```

Then:

```bash
terraform init
terraform apply
```

Outputs include the VM public IP.

### Autonomous DB (optional)

If you set `create_adb = true`, Terraform will attempt to create an Always Free Autonomous DB.
Be aware this can store the admin password in Terraform state.

