import subprocess

def ssh(cmd, timeout=20):
    r = subprocess.run(
        ['ssh', '-i', '/home/pavel/.ssh/supportbot_ed25519', '-o', 'StrictHostKeyChecking=no',
         'opc@161.33.64.115', cmd],
        capture_output=True, text=True, timeout=timeout
    )
    return r.stdout.strip()

# Find domain from env file or compose
print("=== Domain ===")
print(ssh('cat /home/opc/supportbot/.env 2>/dev/null | grep -i domain'))
print(ssh('head -3 /home/opc/supportbot/Caddyfile 2>/dev/null'))

# Status counts
print("\n=== Status counts ===")
print(ssh('docker exec supportbot-db-1 mysql -u supportbot -psupportbot supportbot -e "SELECT status, count(*) n FROM cases GROUP BY status"'))

# All non-archived cases with ID and title
print("\n=== Current cases ===")
rows = ssh('docker exec supportbot-db-1 mysql -u supportbot -psupportbot supportbot -e "SELECT case_id, status, problem_title FROM cases WHERE status IN (\'solved\',\'open\') ORDER BY created_at DESC"')
print(rows)
