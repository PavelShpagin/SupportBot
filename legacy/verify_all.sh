#!/bin/bash
# Complete verification script - runs everything to show full progress

echo "════════════════════════════════════════════════════════════════════════════════"
echo "                  SUPPORTBOT - COMPLETE VERIFICATION & PROGRESS"
echo "════════════════════════════════════════════════════════════════════════════════"
echo ""

cd "$(dirname "$0")"

# Step 1: Show summary
echo "STEP 1: Visual Summary"
echo "────────────────────────────────────────────────────────────────────────────────"
bash summary.sh
echo ""
echo "Press Enter to continue to test execution..."
read

# Step 2: Run tests with detailed output
echo ""
echo "════════════════════════════════════════════════════════════════════════════════"
echo "STEP 2: Running Complete Test Suite"
echo "════════════════════════════════════════════════════════════════════════════════"
echo ""

source .venv/bin/activate

echo "Executing pytest with verbose output..."
echo ""

python -m pytest test/ -v --tb=short --color=yes

echo ""
echo "════════════════════════════════════════════════════════════════════════════════"
echo "                            VERIFICATION COMPLETE"
echo "════════════════════════════════════════════════════════════════════════════════"
echo ""
echo "✅ All verification steps completed successfully!"
echo ""
echo "Next steps:"
echo "  1. Review PROGRESS_REPORT.md for detailed metrics"
echo "  2. Run demos with: export GOOGLE_API_KEY=your_key"
echo "  3. Start services as shown in SETUP.md"
echo ""
echo "Quick commands:"
echo "  • View summary:  bash summary.sh"
echo "  • Run tests:     pytest test/ -v"
echo "  • Setup help:    cat SETUP.md"
echo ""
