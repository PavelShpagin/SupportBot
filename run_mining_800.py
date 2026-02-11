#!/usr/bin/env python3
"""Run mining on 800 messages."""
import os
import subprocess
import sys
from pathlib import Path

repo = Path(__file__).parent
os.chdir(repo)

# Set environment variables
os.environ["REAL_LAST_N_MESSAGES"] = "800"
os.environ["REAL_MAX_CASES"] = "200"
os.environ["REAL_EVAL_N"] = "20"

print("=" * 70)
print("PHASE 1: Mining cases from 800 messages")
print("=" * 70)
print()

# Run mining
sys.path.insert(0, str(repo / "test"))
from mine_real_cases import main as mine_main

mine_main()
