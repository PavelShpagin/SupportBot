import requests
import re

url = "https://docs.google.com/document/d/1kWE7IBlQ2YbMBNFPl-8uV8KdI23SNcxFqMOf_E6Xq9Y/export?format=html"
try:
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    print(f"Status: {response.status_code}")
    print(f"Content start: {response.text[:500]}")
    
    # Check for images
    images = re.findall(r'<img[^>]+src="([^"]+)"', response.text)
    print(f"Found {len(images)} images")
    if images:
        print(f"First image src: {images[0]}")
except Exception as e:
    print(f"Error: {e}")
