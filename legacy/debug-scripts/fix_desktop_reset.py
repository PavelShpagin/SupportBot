#!/usr/bin/env python3
"""Fix Signal Desktop startup code for QEMU/ARM64 compatibility."""

import sys

# Read the file
with open('/app/app/main.py', 'r') as f:
    lines = f.readlines()

# Find the target block and replace it
new_lines = []
skip_until_close = False
found = False

for i, line in enumerate(lines):
    if skip_until_close:
        # Skip until we find the closing part of Popen
        if 'log.info("Started Signal Desktop")' in line:
            skip_until_close = False
            new_lines.append(line)
        continue
    
    # Look for the subprocess.Popen line that starts Signal Desktop
    if 'subprocess.Popen(' in line and i > 300:
        # Check if next line has signal-desktop
        if i + 1 < len(lines) and 'signal-desktop' in lines[i+1] and '--no-sandbox' in lines[i+1]:
            found = True
            skip_until_close = True
            # Add the new code
            new_lines.append('        subprocess.Popen(\n')
            new_lines.append('            [\n')
            new_lines.append('                "signal-desktop",\n')
            new_lines.append('                "--no-sandbox",\n')
            new_lines.append('                "--disable-gpu",\n')
            new_lines.append('                "--disable-gpu-compositing",\n')
            new_lines.append('                "--disable-gpu-sandbox",\n')
            new_lines.append('                "--disable-dev-shm-usage",\n')
            new_lines.append('                "--disable-accelerated-2d-canvas",\n')
            new_lines.append('                "--disable-accelerated-video-decode",\n')
            new_lines.append('                "--single-process",\n')
            new_lines.append('            ],\n')
            new_lines.append('            env={\n')
            new_lines.append('                **os.environ,\n')
            new_lines.append('                "DISPLAY": ":99",\n')
            new_lines.append('                "LIBGL_ALWAYS_SOFTWARE": "1",\n')
            new_lines.append('                "GALLIUM_DRIVER": "llvmpipe",\n')
            new_lines.append('                "LP_NUM_THREADS": "4",\n')
            new_lines.append('                "MESA_GL_VERSION_OVERRIDE": "3.3",\n')
            new_lines.append('            },\n')
            new_lines.append('            stdout=subprocess.DEVNULL,\n')
            new_lines.append('            stderr=subprocess.DEVNULL,\n')
            new_lines.append('        )\n')
            continue
    
    new_lines.append(line)

if found:
    with open('/app/app/main.py', 'w') as f:
        f.writelines(new_lines)
    print("SUCCESS: Updated signal-desktop startup code")
else:
    print("ERROR: Could not find target code block")
    sys.exit(1)
