# Shell Configuration Rule

This project uses WSL (Ubuntu) for development and deploys to an OCI ARM64 VM.

## Shell Commands

When running shell commands:
1. Always use `wsl -e bash -c '...'` wrapper for commands
2. For SSH commands to the OCI VM, use: `ssh -i ~/.ssh/supportbot_ed25519 opc@161.33.64.115`
3. Avoid complex piping through PowerShell - it strips dashes and special characters

## Known Issues

PowerShell + WSL has escaping problems:
- Dashes in command flags get stripped (e.g., `--version` becomes empty)
- Complex docker commands don't work well through the chain

## Workaround

For docker/SSH commands that don't work through the agent:
1. Provide the exact command to the user
2. User runs it in their existing WSL/SSH terminal session

## VM Details
- IP: 161.33.64.115
- User: opc
- Architecture: aarch64 (ARM64)
- SSH Key: ~/.ssh/supportbot_ed25519
