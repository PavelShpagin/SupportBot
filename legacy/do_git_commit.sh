#!/bin/bash
cd /home/pavel/dev/SupportBot
git commit -m "Add multimodal image support and improve question detection

Improvements:
- Implement image-to-text extraction in case mining
- Add P_DECISION_SYSTEM statement detection
- Enhance stage 1 filter logic
- Filter open cases from KB

Results on 400/16: 85% pass rate, 93.75% on real cases"
git push origin main
