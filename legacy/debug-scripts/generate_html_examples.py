import time
from datetime import datetime

def generate_case_html(case_id, case_data, evidence_data):
    # Simple HTML template (matching signal-bot/app/main.py)
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Case {case_id}</title>
        <style>
            body {{ font-family: sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; }}
            .case-header {{ border-bottom: 1px solid #ccc; padding-bottom: 10px; margin-bottom: 20px; }}
            .status {{ display: inline-block; padding: 5px 10px; border-radius: 5px; background: #eee; }}
            .status.solved {{ background: #d4edda; color: #155724; }}
            .message {{ border: 1px solid #eee; padding: 10px; margin-bottom: 10px; border-radius: 5px; }}
            .meta {{ color: #666; font-size: 0.9em; margin-bottom: 5px; }}
            img {{ max-width: 100%; height: auto; margin-top: 10px; }}
        </style>
    </head>
    <body>
        <div class="case-header">
            <h1>{case_data.get('problem_title', 'Case ' + case_id)}</h1>
            <div class="status {case_data.get('status', 'open')}">{case_data.get('status', 'open')}</div>
            <p><strong>Problem:</strong> {case_data.get('problem_summary', '')}</p>
            <p><strong>Solution:</strong> {case_data.get('solution_summary', '')}</p>
        </div>
        
        <h2>Evidence</h2>
        <div class="evidence-list">
    """
    
    for msg in evidence_data:
        ts_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(msg['ts'] / 1000))
        html += f"""
            <div class="message">
                <div class="meta">{msg['sender_hash'][:8]} at {ts_str}</div>
                <div class="content">{msg['content_text']}</div>
        """
        for url in msg.get('image_urls', []):
            html += f'<img src="{url}" loading="lazy" />'
        html += "</div>"
        
    html += """
        </div>
    </body>
    </html>
    """
    return html

# Mock Data
case_1 = {
    "problem_title": "VPN Connection Failed",
    "status": "solved",
    "problem_summary": "User cannot connect to corporate VPN from home network.",
    "solution_summary": "Updated VPN client to version 5.2.1 and reset network adapter settings."
}

evidence_1 = [
    {
        "sender_hash": "user123hash",
        "ts": 1707900000000,
        "content_text": "I can't connect to the VPN. It says 'Negotiation failed'.",
        "image_urls": []
    },
    {
        "sender_hash": "support_bot",
        "ts": 1707900060000,
        "content_text": "Please try updating your client. Here is the link...",
        "image_urls": []
    },
    {
        "sender_hash": "user123hash",
        "ts": 1707900120000,
        "content_text": "That worked! Thanks.",
        "image_urls": ["https://placehold.co/600x400?text=Screenshot+of+Success"]
    }
]

# Generate
html_content = generate_case_html("101", case_1, evidence_1)

# Save
with open("example_case.html", "w", encoding="utf-8") as f:
    f.write(html_content)

print("Generated example_case.html")
