import requests
import json
import time
import os

# Configuration
REPO_OWNER = "signalapp"
REPO_NAME = "Signal-Android"
OUTPUT_FILE = "test/signal_open_source_data.json"
MAX_ISSUES = 10  # Very small batch to try to bypass rate limits
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")

def get_headers():
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "SupportBot-Research" # Sometimes UA helps
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"
    return headers

def fetch_issues():
    all_issues = []
    page = 1
    per_page = 10 
    
    print(f"Fetching issues from {REPO_OWNER}/{REPO_NAME}...")
    
    while len(all_issues) < MAX_ISSUES:
        url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/issues"
        params = {
            "state": "closed",
            "sort": "comments",
            "direction": "desc",
            "per_page": per_page,
            "page": page
        }
        
        print(f"Fetching page {page}...")
        response = requests.get(url, params=params, headers=get_headers())
        
        if response.status_code != 200:
            print(f"Error fetching issues: {response.status_code} - {response.text}")
            break
            
        issues = response.json()
        if not issues:
            break
            
        all_issues.extend(issues)
        page += 1
        
        # Respect rate limits
        time.sleep(5)
        
    return all_issues[:MAX_ISSUES]

def fetch_comments(comments_url):
    response = requests.get(comments_url, headers=get_headers())
    if response.status_code != 200:
        print(f"Error fetching comments: {response.status_code}")
        return []
    return response.json()

import re

def process_issues(issues):
    dataset = []
    
    print(f"Processing {len(issues)} issues...")
    
    for i, issue in enumerate(issues):
        if "pull_request" in issue:
            continue  # Skip PRs
            
        print(f"Processing issue #{issue['number']} ({i+1}/{len(issues)})...")
        
        question = f"{issue['title']}\n\n{issue['body']}"
        
        # Check for images and emojis in question
        has_images = bool(re.search(r'!\[.*\]\(.*\)|<img.*>', question))
        has_emojis = bool(re.search(r':[a-z_]+:', question)) # Simple emoji check (GitHub style)
        
        # Fetch comments to find an answer
        comments = fetch_comments(issue['comments_url'])
        
        if not comments:
            continue
            
        # Strategy: 
        # 1. Look for comments from repo owners/collaborators (hard to know without auth/metadata, but we can guess)
        # 2. Or just take the last comment as the "resolution" or "status update"
        # 3. Or combine all comments into a "discussion"
        
        # For this dataset, let's take the last comment as the "Answer" or "Resolution"
        # and maybe the most reacted comment if available (but that requires more parsing)
        
        answer = comments[-1]['body']
        
        # Check for images and emojis in answer
        if not has_images:
            has_images = bool(re.search(r'!\[.*\]\(.*\)|<img.*>', answer))
        if not has_emojis:
            has_emojis = bool(re.search(r':[a-z_]+:', answer))
        
        # Clean up
        if not question or not answer:
            continue
            
        entry = {
            "id": issue['number'],
            "url": issue['html_url'],
            "question": question,
            "answer": answer,
            "source": "Signal-Android GitHub Issues",
            "has_images": has_images,
            "has_emojis": has_emojis
        }
        
        dataset.append(entry)
        
        # Sleep to be nice to API
        time.sleep(0.5)
        
    return dataset

def main():
    issues = fetch_issues()
    if not issues:
        print("No issues found or error occurred.")
        return

    data = process_issues(issues)
    
    print(f"Collected {len(data)} Q&A pairs.")
    
    # Save to file
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        
    print(f"Saved to {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
