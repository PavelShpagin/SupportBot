#!/usr/bin/env bash
set -e
cd /home/pavel/dev/SupportBot
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:$PATH"
git status --short docs/ALGORITHM_FLOW.md
git add docs/ALGORITHM_FLOW.md
git diff --cached --stat docs/ALGORITHM_FLOW.md
git commit -m "docs: rewrite ALGORITHM_FLOW.md to reflect current production architecture" || true
git push origin HEAD
