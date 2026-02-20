#!/bin/bash
docker exec supportbot-db mysql -u root -prootpassword supportbot -e "SELECT id, type, status, created_at FROM job_queue ORDER BY id DESC LIMIT 10;"
