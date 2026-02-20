#!/usr/bin/env python3
"""Quick cost estimation for 800/200 evaluation."""

def estimate_costs(n_messages: int, n_eval_scenarios: int) -> dict:
    """Estimate API costs."""
    
    # Mining phase (800 messages)
    n_chunks = 8
    mining_input_tokens = n_chunks * 15000
    mining_output_tokens = n_chunks * 3000
    
    # gemini-3-pro-preview pricing
    mining_cost = (
        (mining_input_tokens / 1_000_000) * 1.25 +
        (mining_output_tokens / 1_000_000) * 5.00
    )
    
    # Evaluation phase
    eval_tokens_per_scenario = 4600
    eval_total_tokens = n_eval_scenarios * eval_tokens_per_scenario
    
    # 40% fast models, 60% quality models
    fast_tokens = int(eval_total_tokens * 0.4)
    quality_tokens = int(eval_total_tokens * 0.6)
    
    # 70% input, 30% output
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
    
    return {
        "mining_cost": round(mining_cost, 3),
        "eval_cost": round(eval_cost, 3),
        "total_cost": round(mining_cost + eval_cost, 3),
        "time_minutes": round((n_chunks * 2 + n_eval_scenarios * 0.5), 1),
    }

if __name__ == "__main__":
    est = estimate_costs(800, 22)
    print(f"Mining cost: ${est['mining_cost']}")
    print(f"Eval cost: ${est['eval_cost']}")
    print(f"Total cost: ${est['total_cost']} USD")
    print(f"Time estimate: {est['time_minutes']} minutes")
