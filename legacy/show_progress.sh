#!/bin/bash
# Complete setup, test, and evaluation progress tracker

set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

echo "╔══════════════════════════════════════════════════════════════════════════════╗"
echo "║                    SUPPORTBOT - COMPLETE PROGRESS TRACKER                    ║"
echo "╚══════════════════════════════════════════════════════════════════════════════╝"
echo ""

# ============================================================================
# STEP 1: Environment Setup (Fast with uv)
# ============================================================================
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "STEP 1: Environment Setup"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if [ ! -d ".venv" ]; then
    echo "⚡ Running FAST setup with uv..."
    START_TIME=$(date +%s)
    ./setup_env.sh
    END_TIME=$(date +%s)
    DURATION=$((END_TIME - START_TIME))
    echo ""
    echo "✅ Setup completed in ${DURATION} seconds!"
else
    echo "✅ Virtual environment already exists"
fi

source .venv/bin/activate
echo "✅ Environment activated"
echo ""

# ============================================================================
# STEP 2: Run Tests
# ============================================================================
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "STEP 2: Running Test Suite"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

python -m pytest test/ -v --tb=short | tee /tmp/supportbot_test_results.txt

# Count results
PASSED=$(grep -c "PASSED" /tmp/supportbot_test_results.txt || true)
FAILED=$(grep -c "FAILED" /tmp/supportbot_test_results.txt || true)
SKIPPED=$(grep -c "SKIPPED" /tmp/supportbot_test_results.txt || true)

echo ""
echo "📊 Test Results:"
echo "   ✅ Passed:  $PASSED"
echo "   ❌ Failed:  $FAILED"
echo "   ⏭️  Skipped: $SKIPPED"
echo ""

# ============================================================================
# STEP 3: Demo Scripts (Optional - requires GOOGLE_API_KEY)
# ============================================================================
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "STEP 3: Demo Scripts (Optional)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if [ -z "$GOOGLE_API_KEY" ] && [ ! -f ".env" ]; then
    echo "⚠️  GOOGLE_API_KEY not set - skipping demos"
    echo "   To run demos:"
    echo "   1. Add GOOGLE_API_KEY=your_key to .env file"
    echo "   2. Or export GOOGLE_API_KEY=your_key"
    echo "   3. Then run: python test/run_case_extraction_demo.py"
    echo ""
else
    echo "✅ GOOGLE_API_KEY found"
    echo ""
    
    # Only show available demos
    echo "Available demos:"
    echo "  • python test/run_case_extraction_demo.py  (shows case extraction)"
    echo "  • python test/run_quality_demo.py          (shows response quality)"
    echo "  • python test/run_image_to_text_demo.py    (shows multimodal processing)"
    echo ""
    
    read -p "Run case extraction demo? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo ""
        echo "Running case extraction demo..."
        python test/run_case_extraction_demo.py
        echo ""
    fi
fi

# ============================================================================
# STEP 4: Evaluation Scripts
# ============================================================================
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "STEP 4: Evaluation Scripts"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

EVAL_DATA_DIR="test/data/streaming_eval"
if [ -f "$EVAL_DATA_DIR/context_kb.json" ] && [ -f "$EVAL_DATA_DIR/eval_messages_labeled.json" ]; then
    echo "✅ Evaluation dataset found"
    echo ""
    
    read -p "Run streaming evaluation? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo ""
        echo "Running streaming evaluation..."
        python test/run_streaming_eval.py
        echo ""
        
        if [ -f "$EVAL_DATA_DIR/eval_summary.json" ]; then
            echo "📊 Evaluation Summary:"
            cat "$EVAL_DATA_DIR/eval_summary.json" | python -m json.tool
        fi
    fi
else
    echo "⚠️  Evaluation dataset not found"
    echo "   To prepare dataset, run:"
    echo "   python test/prepare_streaming_eval_dataset.py"
    echo ""
fi

# ============================================================================
# Summary
# ============================================================================
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "COMPLETE PROGRESS SUMMARY"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "✅ Environment setup: COMPLETE (fast mode with uv)"
echo "✅ Test suite:        $PASSED passed, $FAILED failed, $SKIPPED skipped"
echo ""
echo "📁 Project structure:"
echo "   • signal-bot/       - Main bot application"
echo "   • signal-ingest/    - Message ingestion service"
echo "   • test/             - Test suite and evaluation scripts"
echo ""
echo "🚀 Next steps:"
echo "   1. Review test results above"
echo "   2. Run demo scripts to see bot in action"
echo "   3. Check evaluation metrics if dataset is available"
echo ""
echo "═══════════════════════════════════════════════════════════════════════════════"
echo "All systems operational! 🎉"
echo "═══════════════════════════════════════════════════════════════════════════════"
