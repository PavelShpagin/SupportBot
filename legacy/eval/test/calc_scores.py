import json
import os

def calculate_accuracy(filepath, name):
    if not os.path.exists(filepath):
        return None

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        if isinstance(data, list):
            results = data
        else:
            results = data.get('results', data)
        
        scores = []
        for r in results:
            if isinstance(r, dict):
                s = r.get('judge_score', r.get('score', 0))
                scores.append(s)
        
        if not scores:
            return None
        
        n = len(scores)
        avg = sum(scores) / n
        acc7 = sum(1 for s in scores if s >= 7) / n * 100
        acc8 = sum(1 for s in scores if s >= 8) / n * 100
        
        return {'name': name, 'n': n, 'avg': round(avg, 2), 'acc7': round(acc7, 1), 'acc8': round(acc8, 1)}
        
    except Exception as e:
        return {'name': name, 'error': str(e)}


if __name__ == "__main__":
    files = [
        ('large_eval_results.json', 'DocsOnly'),
        ('chat_search_eval_results.json', 'ChatRAG'),
        ('unified_eval_results.json', 'DocsChat'),
        ('ultimate_eval_results.json', 'SupportBot'),
        ('clean_eval_results.json', 'CleanAgent'),
    ]
    
    print('=' * 60)
    print('ABLATION STUDY RESULTS')
    print('=' * 60)
    
    for path, name in files:
        result = calculate_accuracy(path, name)
        if result and 'error' not in result:
            print(f"{result['name']:12} N={result['n']:4} Avg={result['avg']:5.2f} Acc>=7={result['acc7']:5.1f}% Acc>=8={result['acc8']:5.1f}%")
        elif result:
            print(f"{name}: {result['error']}")
    
    print('=' * 60)
