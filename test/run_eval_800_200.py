#!/usr/bin/env python3
"""
Large-scale evaluation on 800/200 split with rate limiting.

This script:
1. Mines cases from 800 messages (max 200 cases)
2. Runs quality evaluation with rate limiting to avoid API limits
3. Provides cost estimation before running

Usage:
  source .venv/bin/activate
  python test/run_eval_800_200.py
"""

import json
import os
import time
from pathlib import Path


def estimate_costs(n_messages: int, n_eval_scenarios: int) -> dict:
    """
    Estimate API costs for the evaluation.
    
    Mining phase (800 messages -> structured cases):
    - Chunk into ~6-8 chunks of history
    - Each chunk: ~15K tokens in + 3K tokens out = 18K tokens
    - Model: gemini-3-pro-preview (extraction) + gemini-2.5-flash-lite (embeddings)
    
    Evaluation phase (20-22 scenarios):
    - Per scenario:
      - decide_consider: ~500 tokens (gemini-2.5-flash-lite, fast)
      - embed: ~100 tokens (gemini-embedding-001, fast)
      - decide_and_respond: ~2500 tokens (gemini-3-pro-preview, quality)
      - judge: ~1500 tokens (gemini-2.5-flash-lite, fast)
    - Total per scenario: ~4600 tokens
    
    Gemini pricing (Feb 2026):
    - gemini-2.5-flash-lite: $0.075/1M input, $0.30/1M output
    - gemini-3-pro-preview: $1.25/1M input, $5.00/1M output
    - gemini-embedding-001: $0.00001/1M tokens (negligible)
    """
    
    # Mining phase
    n_chunks = 8  # Approximate for 800 messages
    mining_tokens_per_chunk = 18000
    mining_total_tokens = n_chunks * mining_tokens_per_chunk
    mining_input_tokens = n_chunks * 15000
    mining_output_tokens = n_chunks * 3000
    
    # Use gemini-3-pro-preview for extraction (high quality needed)
    mining_cost_input = (mining_input_tokens / 1_000_000) * 1.25
    mining_cost_output = (mining_output_tokens / 1_000_000) * 5.00
    mining_cost = mining_cost_input + mining_cost_output
    
    # Evaluation phase
    eval_tokens_per_scenario = 4600
    eval_total_tokens = n_eval_scenarios * eval_tokens_per_scenario
    
    # Mix of models in eval
    # Fast operations (decide, judge): gemini-2.5-flash-lite (~40% of tokens)
    # Quality operations (respond): gemini-3-pro-preview (~60% of tokens)
    fast_tokens = int(eval_total_tokens * 0.4)
    quality_tokens = int(eval_total_tokens * 0.6)
    
    # Approximate input/output split (70% input, 30% output)
    fast_input = int(fast_tokens * 0.7)
    fast_output = int(fast_tokens * 0.3)
    quality_input = int(quality_tokens * 0.7)
    quality_output = int(quality_tokens * 0.3)
    
    eval_cost = (
        (fast_input / 1_000_000) * 0.075 +
        (fast_output / 1_000_000) * 0.30 +
        (quality_input / 1_000_000) * 1.25 +
        (quality_output / 1_000_000) * 5.00
    )
    
    total_cost = mining_cost + eval_cost
    
    return {
        "mining": {
            "chunks": n_chunks,
            "tokens": mining_total_tokens,
            "cost_usd": round(mining_cost, 3),
        },
        "evaluation": {
            "scenarios": n_eval_scenarios,
            "tokens": eval_total_tokens,
            "cost_usd": round(eval_cost, 3),
        },
        "total": {
            "tokens": mining_total_tokens + eval_total_tokens,
            "cost_usd": round(total_cost, 3),
            "time_estimate_minutes": round((n_chunks * 20 + n_eval_scenarios * 15) / 60, 1),
        },
    }


def main():
    repo = Path(__file__).parent.parent
    
    print("=" * 70)
    print("EVALUATION: 800 messages / 200 max cases")
    print("=" * 70)
    
    # Cost estimation
    est = estimate_costs(n_messages=800, n_eval_scenarios=22)
    
    print("\n📊 COST ESTIMATION:")
    print(f"\n1. Mining Phase (800 messages -> structured cases):")
    print(f"   - Chunks to process: {est['mining']['chunks']}")
    print(f"   - Tokens: ~{est['mining']['tokens']:,}")
    print(f"   - Cost: ${est['mining']['cost_usd']}")
    
    print(f"\n2. Evaluation Phase (~20-22 scenarios):")
    print(f"   - Scenarios: {est['evaluation']['scenarios']}")
    print(f"   - Tokens: ~{est['evaluation']['tokens']:,}")
    print(f"   - Cost: ${est['evaluation']['cost_usd']}")
    
    print(f"\n3. TOTAL:")
    print(f"   - Tokens: ~{est['total']['tokens']:,}")
    print(f"   - Cost: ${est['total']['cost_usd']} USD")
    print(f"   - Time: ~{est['total']['time_estimate_minutes']} minutes")
    
    print("\n" + "=" * 70)
    if not os.environ.get("SKIP_CONFIRM"):
        response = input("\nProceed with evaluation? (yes/no): ")
        if response.lower() not in ["yes", "y"]:
            print("Aborted.")
            return
    else:
        print("\nSKIP_CONFIRM=1, proceeding automatically...")
    
    # Set environment variables for 800/200 scale
    os.environ["REAL_LAST_N_MESSAGES"] = "800"
    os.environ["REAL_MAX_CASES"] = "200"
    os.environ["REAL_EVAL_N"] = "20"  # Will be more if we find more cases
    
    print("\n" + "=" * 70)
    print("PHASE 1: Mining cases from 800 messages")
    print("=" * 70)
    
    # Import and run mining
    import sys
    sys.path.insert(0, str(repo / "test"))
    
    print("\n⏳ Starting case mining (this will take ~10-15 minutes)...")
    print("   Rate limiting: 2-3 seconds between API calls")
    
    start_time = time.time()
    
    # Run mining script
    from mine_real_cases import main as mine_main
    mine_main()
    
    mining_time = time.time() - start_time
    print(f"\n✅ Mining complete in {mining_time/60:.1f} minutes")
    
    # Check mined cases
    cases_path = repo / "test/data/signal_cases_structured.json"
    data = json.loads(cases_path.read_text())
    n_cases = len(data.get("cases", []))
    print(f"   Structured cases extracted: {n_cases}")
    
    print("\n" + "=" * 70)
    print("PHASE 2: Running quality evaluation")
    print("=" * 70)
    
    print("\n⏳ Starting evaluation (this will take ~10-15 minutes)...")
    print("   Rate limiting: 1-2 seconds between scenarios")
    
    eval_start = time.time()
    
    # Run evaluation script
    from run_real_quality_eval import main as eval_main
    eval_main()
    
    eval_time = time.time() - eval_start
    total_time = time.time() - start_time
    
    print("\n" + "=" * 70)
    print("EVALUATION COMPLETE!")
    print("=" * 70)
    print(f"\nMining time: {mining_time/60:.1f} minutes")
    print(f"Eval time: {eval_time/60:.1f} minutes")
    print(f"Total time: {total_time/60:.1f} minutes")
    
    # Show results
    results_path = repo / "test/data/real_quality_eval.json"
    if results_path.exists():
        results = json.loads(results_path.read_text())
        summary = results.get("summary", {})
        
        print("\n📊 RESULTS:")
        for cat, stats in summary.get("by_category", {}).items():
            if stats.get("n", 0) > 0:
                print(f"\n{cat}:")
                print(f"  Pass rate: {stats['pass_rate']*100:.1f}% ({stats['passed']}/{stats['n']})")
                print(f"  Avg score: {stats['avg_score']:.2f}/10")
        
        print("\n✅ Results saved to: test/data/real_quality_eval.json")


if __name__ == "__main__":
    main()
