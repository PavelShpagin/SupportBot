## OCI (Always Free) setup — **click-by-click** (region: `eu-frankfurt-1`)

This project runs on **one OCI VM** with **three containers** (`signal-bot`, `signal-ingest`, `rag`) and an **Oracle Autonomous Database** for the relational store.

### Console navigation basics (updated UI)

- **OCI Console (base link)**: [cloud.oracle.com](https://cloud.oracle.com/)  
  If any deep link you find on the internet 404s, always start from this base console link and use the navigation menu/search.
- **Region selector**: in the top bar, switch region to **Germany Central (Frankfurt)** (region code `eu-frankfurt-1`).
- **Compartment selector**: on most list pages (VCNs, Instances, Autonomous DBs) there’s a **Compartment** dropdown near the top of the page. Always set it to `supportbot` (or your chosen compartment) before creating resources.
- **Navigation menu**: click the top-left **☰** menu; you can also use the menu’s built-in search (type “VCN”, “Instances”, “Autonomous Database”, etc.).

### Shared parameters (so we stay aligned)

- **Region**: `eu-frankfurt-1`
- **Compartment name (recommended)**: `supportbot`
- **VCN**:
  - **Name**: `supportbot-vcn`
  - **CIDR**: `10.0.0.0/16`
- **Public subnet**:
  - **Name**: `supportbot-public-subnet`
  - **CIDR**: `10.0.0.0/24`
  - **Public IPs**: enabled
- **Internet gateway**: `supportbot-igw`
- **Route table**: `supportbot-public-rt` with default route `0.0.0.0/0 → supportbot-igw`
- **Security list**: `supportbot-public-sl`
  - **Ingress**:
    - TCP 22 from `ADMIN_CIDR`
    - TCP 8000 from `ADMIN_CIDR`
  - **Egress**: all to `0.0.0.0/0`
- **Compute instance**:
  - **Name**: `supportbot-vm`
  - **Shape**: `VM.Standard.A1.Flex`
  - **OCPUs / RAM**: `1 / 6 GB`
  - **OS**: Ubuntu
- **Autonomous Database**:
  - **Display name**: `supportbot-adb`
  - **DB name**: `supportbot`
  - **Workload**: Transaction Processing (OLTP)
- **Bot API port**: `8000` (public, restricted to `ADMIN_CIDR`)
- **Chroma port**: `8001` (keep private; do not expose to the internet)

### Oracle / OCI quick links

- **Oracle Cloud Free Tier**: [Oracle Cloud Free Tier (Start for Free)](https://www.oracle.com/cloud/free/)
- **Always Free resource limits**: [Always Free Resources (OCI docs)](https://docs.oracle.com/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
- **Always Free Autonomous DB overview/limits**: [Always Free Autonomous Database (OCI docs)](https://docs.oracle.com/en-us/iaas/Content/Database/Concepts/adbfreeoverview.htm)
- **Autonomous DB network allowlist (ACL)**: [Configure network access control list (OCI docs)](https://docs.oracle.com/en/cloud/paas/autonomous-database/adbsa/network-access-control-list-configure.html)
- **OCI IPs / networking reference**: [Managing IP addresses (OCI docs)](https://docs.oracle.com/en-us/iaas/Content/Network/Tasks/managingIPaddresses.htm)

### Step 0) Create/select a compartment

**Link**: [cloud.oracle.com](https://cloud.oracle.com/)

**UI path**: ☰ Navigation menu → **Identity & Security** → **Compartments**

**Buttons to click**:
- Click **Create compartment**

- **Fill in**:
  - **Name**: `supportbot`
  - **Description**: `SupportBot resources`
  - **Parent compartment**: your root compartment (or wherever you keep projects)

### Step 1) Create networking (VCN + public subnet)

We mirror the Terraform in `infra/oci/terraform/` exactly.

**Link**: [cloud.oracle.com](https://cloud.oracle.com/)

**UI path**: ☰ Navigation menu → **Networking** → **Virtual Cloud Networks**

**Before you click anything**:
- Set **Compartment** = `supportbot`

#### 1.1 Create the VCN

**Buttons to click**:
- Click **Create VCN**
- If you’re shown a choice like **VCN Wizard / Quickstart** vs **VCN only**, choose **VCN only** (we create the IGW/route table/security list/subnet explicitly below so names match).

- **Fill in**:
  - **VCN name**: `supportbot-vcn`
  - **CIDR block**: `10.0.0.0/16`
  - Everything else: leave defaults

Click **Create VCN** to finish.

#### 1.2 Create the internet gateway

**Link**: [cloud.oracle.com](https://cloud.oracle.com/)

**UI path**: open `supportbot-vcn` → left sidebar **Resources** → **Internet Gateways**

**Buttons to click**:
- Click **Create Internet Gateway**

- **Fill in**:
  - **Name**: `supportbot-igw`
  - **Enabled**: yes

Click **Create Internet Gateway**.

#### 1.3 Create the route table

**Link**: [cloud.oracle.com](https://cloud.oracle.com/)

**UI path**: open `supportbot-vcn` → left sidebar **Resources** → **Route Tables**

**Note (important)**: OCI always creates a **Default Route Table** for every VCN. You can either:
- **Option A (recommended for staying aligned with this doc)**: create `supportbot-public-rt` and select it when creating the subnet.
- **Option B (also fine)**: click **Default Route Table for supportbot-vcn** → add the same route rule (`0.0.0.0/0 → supportbot-igw`) → and use that route table on the public subnet.

**Buttons to click**:
- Click **Create Route Table**

- **Fill in**:
  - **Name**: `supportbot-public-rt`
  - **Route rules**: add one rule:
    - **Target type**: Internet Gateway
    - **Target**: `supportbot-igw`
    - **Destination CIDR block**: `0.0.0.0/0`

Click **Create Route Table**.

#### 1.4 Create the security list (subnet firewall)

**Link**: [cloud.oracle.com](https://cloud.oracle.com/)

**UI path**: open `supportbot-vcn` → left sidebar **Resources** → **Security Lists**

**Buttons to click**:
- Click **Create Security List**

- **Fill in**:
  - **Name**: `supportbot-public-sl`
  - **Egress rules**:
    - Allow all outbound to `0.0.0.0/0` (default is usually fine)
  - **Ingress rules**: add two **stateful TCP** rules (leave source port range = “All”):
    - **Rule 1 (SSH)**:
      - **Source type**: CIDR
      - **Source CIDR**: `ADMIN_CIDR`
      - **IP protocol**: TCP
      - **Destination port range**: `22`
    - **Rule 2 (SupportBot API)**:
      - **Source type**: CIDR
      - **Source CIDR**: `ADMIN_CIDR`
      - **IP protocol**: TCP
      - **Destination port range**: `8000`

If you don’t know `ADMIN_CIDR` yet:

- **Preferred**: use OCI’s “Add my IP” button (then tighten later if needed), or run this **on your local machine** (the laptop/PC you will SSH from; you don’t need a VM yet).  
  Then use it as a CIDR like `<YOUR_PUBLIC_IP>/32` (single IP).

```bash
curl -s ifconfig.me
```

- On Windows PowerShell, use the real curl binary (not the PowerShell alias):

```powershell
curl.exe -s ifconfig.me
```

- **Temporary (unsafe)**: `0.0.0.0/0` (do this only to unblock creation; replace immediately).

Click **Create Security List**.

#### 1.5 Create the public subnet

**Link**: [cloud.oracle.com](https://cloud.oracle.com/)

**UI path**: open `supportbot-vcn` → left sidebar **Resources** → **Subnets**

**Buttons to click**:
- Click **Create Subnet**

- **Fill in**:
  - **Name**: `supportbot-public-subnet`
  - **Subnet type**: Regional
  - **CIDR block**: `10.0.0.0/24`
  - **Route table**: `supportbot-public-rt`
  - **Security lists**: include `supportbot-public-sl`
  - **Public subnet toggle / public IP setting**: ensure public IPs are allowed
    - In some UIs this is a checkbox like **Prohibit public IPv4 addresses on VNICs** → make sure it is **unchecked**.

Click **Create Subnet**.

### Step 2) Create the VM (Compute)

**Link**: [cloud.oracle.com](https://cloud.oracle.com/)

**UI path**: ☰ Navigation menu → **Compute** → **Instances**

**Before you click anything**:
- Set **Compartment** = `supportbot`

Click **Create instance** and fill:

**Buttons to click / sections** (the page is usually split into sections like “Name and placement”, “Image and shape”, “Networking”, “Add SSH keys”):

- **Name and placement**:
  - **Name**: `supportbot-vm`
  - **Availability domain**: any AD with capacity (if you hit capacity errors, retry in a different AD)
  - **Fault domain**: don’t pin this; leave as “Let Oracle choose” (pinning can reduce available capacity)
- **Image and shape**:
  - Click **Change image** → pick **Canonical Ubuntu** (latest LTS is fine)
  - Click **Change shape** → select **VM.Standard.A1.Flex**
    - **OCPUs**: `1`
    - **Memory (GB)**: `6`
- **Networking**:
  - Choose **Select existing virtual cloud network**
  - **VCN**: `supportbot-vcn`
  - **Subnet**: `supportbot-public-subnet`
  - **Assign a public IPv4 address**: enabled/checked
- **Add SSH keys**:
  - Choose **Paste public keys** and paste your public key (create one locally if needed):

```bash
ssh-keygen -t ed25519 -f ~/.ssh/supportbot_ed25519
cat ~/.ssh/supportbot_ed25519.pub
```

Click **Create** (or **Create instance**) to launch.

After creation, note the **Public IP address** shown on the instance details page (used later for ADB allowlist and SSH).

#### If you hit “Out of capacity for shape VM.Standard.A1.Flex …”

This is common on Always Free shapes.

- **Try a different Availability Domain**: switch AD-2 → AD-1 or AD-3 and retry.
- **Make sure your subnet is Regional**: open the subnet details for `supportbot-public-subnet` and confirm it’s **Regional**.  
  If you accidentally created an AD-specific subnet, the instance will be forced into that AD. Fix by deleting/recreating the subnet as **Regional** (if it’s empty) or creating an additional subnet in another AD.
- **Retry later**: capacity is dynamic; trying again later often works.

If you tried **AD-1, AD-2, AD-3** and they all fail:

- **Try a smaller A1 request first** (sometimes it fits even when bigger ones don’t):
  - Keep shape **VM.Standard.A1.Flex**
  - Set **OCPUs = 1**
  - Set **Memory = the minimum allowed by the console** (often 1–2 GB)
  - Once created, you can later **stop the instance** and try scaling memory up.
- **Try an Always Free x86 micro instance**:
  - Choose `VM.Standard.E2.1.Micro` if it shows the **Always Free-eligible** badge.
  - If the console says “not compatible with the selected image”, you likely selected an **ARM** image earlier. Fix:
    - Click **Change image** → pick an **x86_64** image (e.g., Canonical Ubuntu x86_64 / Oracle Linux x86_64)
    - Then re-open **Change shape** and select `VM.Standard.E2.1.Micro`
  - This may be tight on RAM for all 3 containers; if you go this route, start only `rag` + `signal-bot` first and bring up `signal-ingest` only when you actually need history bootstrap.
- **Upgrade tenancy to PAYG (still stay within Always Free limits)** and retry:
  - This does not guarantee capacity, but some people report better success getting A1 capacity after upgrading.
- **Last resort (if you must proceed immediately)**: use a different **Flex** shape with 1 OCPU / 6 GB (may not be Always Free) and later migrate back to A1 Flex when capacity returns.

### Step 3) Install Docker on the VM

SSH in (Ubuntu user is commonly `ubuntu`):

```bash
ssh -i ~/.ssh/supportbot_ed25519 ubuntu@<VM_PUBLIC_IP>
```

Then:

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
```

Log out/in once so group changes apply.

### Step 4) Create Autonomous Database (Always Free)

Create the VM first so you can include the VM’s public IP in the ADB allowlist.

**Link**: [cloud.oracle.com](https://cloud.oracle.com/)

**UI path**: ☰ Navigation menu → **Oracle Database** → **Autonomous Database** (or **Autonomous Databases**)

**Before you click anything**:
- Set **Compartment** = `supportbot`

Click **Create Autonomous Database** and fill:

**Fields / sections** (names vary slightly, but the structure is consistent):

- **Display name**: `supportbot-adb`
- **Database name**: `supportbot`
- **Workload type**: Transaction Processing (OLTP)
- **Deployment type**: Serverless
- **Always Free**: enabled/checked
- **Administrator credentials**:
  - Set and store an `ADMIN` password (use a password manager)
- **Network access / Access control list (ACL)**:
  - If you see an option like **Secure access from allowed IP addresses only**, choose it and add:
    - your **VM public IP** as `<VM_PUBLIC_IP>/32`
    - your **workstation public IP** as `<YOUR_PUBLIC_IP>/32`
  - If you don’t have the IPs yet, you can temporarily allow broader access just to unblock provisioning, but tighten it immediately afterward (this is security-critical).

### Step 5) Create the app user + grants

In the Autonomous DB details page, open **Database Actions** (SQL Worksheet) and run as `ADMIN`:

```sql
CREATE USER SUPPORTBOT IDENTIFIED BY "REPLACE_WITH_STRONG_PASSWORD";
GRANT CREATE SESSION TO SUPPORTBOT;
GRANT CREATE TABLE TO SUPPORTBOT;
GRANT CREATE SEQUENCE TO SUPPORTBOT;
```

### Step 6) Download the wallet and place it on the VM

- In the Autonomous DB details page, open **DB Connection** → **Download Wallet**
- Download the wallet zip and unzip it on your machine.
- Copy the wallet folder contents to the VM at `/var/lib/adb_wallet`:

```bash
sudo mkdir -p /var/lib/adb_wallet
# copy/unzip wallet files into /var/lib/adb_wallet
sudo chmod -R 0555 /var/lib/adb_wallet
```

The containers mount it read-only at `/opt/oracle/wallet`.

To find valid service names (for `ORACLE_DSN`), inspect `tnsnames.ora`:

```bash
grep -E '^[A-Za-z0-9_]+[[:space:]]*=' /var/lib/adb_wallet/tnsnames.ora | head
```

### Step 7) Deploy SupportBot (Docker Compose)

On the VM:

```bash
sudo mkdir -p /var/lib/signal/bot /var/lib/signal/ingest /var/lib/chroma /var/lib/history
git clone <YOUR_REPO_URL>
cd SupportBot
cp env.example .env
sudo docker compose up -d --build
sudo docker compose logs -f --tail=200
```

### Terraform (optional)

Terraform code is in `infra/oci/terraform/`. It provisions a VCN + subnet + VM, and can optionally create an Autonomous DB.

**Important**: creating Autonomous DB via Terraform can put DB passwords into Terraform state. Use at your discretion.

