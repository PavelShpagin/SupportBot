import json

def calculate_accuracy(filepath):
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            results = json.load(f)
        
        total = len(results)
        if total == 0:
            return 0
            
        # Assuming score >= 7 is "accurate" (or maybe the paper uses a different metric?)
        # The paper says "85% accuracy". Let's see what the average score is.
        total_score = sum(r.get('judge_score', 0) for r in results)
        average_score = total_score / total
        
        # Let's also count "success" as score >= 7
        success_count = sum(1 for r in results if r.get('judge_score', 0) >= 7)
        accuracy = (success_count / total) * 100
        
        print(f"File: {filepath}")
        print(f"Total Cases: {total}")
        print(f"Average Score: {average_score:.2f}/10")
        print(f"Accuracy (Score >= 7): {accuracy:.1f}%")
        print("-" * 20)
        
    except Exception as e:
        print(f"Error reading {filepath}: {e}")

calculate_accuracy('test/unified_eval_results.json')
# calculate_accuracy('test/ultimate_eval_results.json') # This file was too big to read fully, but let's try if it works
