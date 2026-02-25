#!/bin/bash
set -e
cd /home/pavel/dev/SupportBot
git commit -F /tmp/commit_msg.txt && git push origin main
