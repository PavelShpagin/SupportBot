#!/bin/bash
cd /home/pavel/dev/SupportBot
sed -i 's/\r//' commit_msg.txt
git commit -F commit_msg.txt
rm -f commit_msg.txt
git push origin main
