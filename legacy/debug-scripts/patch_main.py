import os

file_path = "signal-bot/app/main.py"

with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

# 1. Add HTMLResponse import
if "from fastapi.responses import FileResponse, Response" in content:
    content = content.replace(
        "from fastapi.responses import FileResponse, Response",
        "from fastapi.responses import FileResponse, Response, HTMLResponse"
    )

# 2. Add new routes before healthz
new_routes = r'''
@app.get("/")
def root() -> dict:
    return {"status": "ok", "service": "SupportBot"}


@app.get("/case/{case_id}", response_class=HTMLResponse)
def view_case(case_id: str):
    case = get_case(db, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    
    evidence = get_case_evidence(db, case_id)
    
    # Simple HTML template
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
            <h1>{case.get('problem_title', 'Case ' + case_id)}</h1>
            <div class="status {case.get('status', 'open')}">{case.get('status', 'open')}</div>
            <p><strong>Problem:</strong> {case.get('problem_summary', '')}</p>
            <p><strong>Solution:</strong> {case.get('solution_summary', '')}</p>
        </div>
        
        <h2>Evidence</h2>
        <div class="evidence-list">
    """
    
    for msg in evidence:
        ts_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(msg.ts / 1000))
        html += f"""
            <div class="message">
                <div class="meta">{msg.sender_hash[:8]} at {ts_str}</div>
                <div class="content">{msg.content_text}</div>
        """
        for p in msg.image_paths:
            # Serve images via /static if possible, or just show path
            if p.startswith("/var/lib/signal/"):
                url = p.replace("/var/lib/signal/", "/static/")
                html += f'<img src="{url}" loading="lazy" />'
        html += "</div>"
        
    html += """
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html)
'''

if "@app.get(\"/healthz\")" in content:
    content = content.replace("@app.get(\"/healthz\")", new_routes + "\n\n@app.get(\"/healthz\")")
else:
    content += new_routes

with open(file_path, "w", encoding="utf-8") as f:
    f.write(content)

print("Patched main.py successfully.")
