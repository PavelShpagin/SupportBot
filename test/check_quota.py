#!/usr/bin/env python3
"""Check if API quota is available"""

import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
api_key = os.getenv('GOOGLE_API_KEY')

if not api_key:
    print("❌ No API key found in .env")
    exit(1)

print(f"Using API key: {api_key[:15]}...{api_key[-5:]}")
print()

client = OpenAI(
    api_key=api_key,
    base_url='https://generativelanguage.googleapis.com/v1beta/'
)

try:
    print("Testing API quota with simple call (gemini-2.5-flash-lite)...")
    resp = client.chat.completions.create(
        model='gemini-2.5-flash-lite',
        messages=[{'role': 'user', 'content': 'Say "test"'}],
        max_tokens=10
    )
    print("✅ API QUOTA IS AVAILABLE!")
    print(f"Response: {resp.choices[0].message.content}")
    print()
    print("You can rerun the evaluation now.")
    
except Exception as e:
    error_str = str(e)
    if '429' in error_str or 'quota' in error_str.lower() or 'RESOURCE_EXHAUSTED' in error_str:
        print("❌ API QUOTA STILL EXCEEDED")
        print()
        print("Error details:")
        print(error_str[:500])
        print()
        print("How to fix:")
        print("1. Wait 24 hours for quota reset (resets daily)")
        print("2. OR: Get a new API key from https://aistudio.google.com/apikey")
        print("3. OR: Upgrade to paid plan at https://ai.google.dev/pricing")
    else:
        print(f"⚠️ Other error occurred:")
        print(error_str[:500])
