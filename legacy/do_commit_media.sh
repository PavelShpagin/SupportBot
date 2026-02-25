#!/bin/bash
cd /home/pavel/dev/SupportBot
git add signal-bot/app/main.py Caddyfile test_media_ingest.py
git commit -m "feat(media): full media support for case pages, test script, debug endpoints"
git log --oneline -3
git push origin main
