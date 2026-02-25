#!/bin/bash
docker exec supportbot-db-1 mysql -u supportbot -psupportbot supportbot -e "
SELECT case_id, status, problem_title, LEFT(solution_summary,60) as sol
FROM cases ORDER BY created_at DESC;
"

echo "=== STATUS COUNTS ==="
docker exec supportbot-db-1 mysql -u supportbot -psupportbot supportbot -e "
SELECT status, count(*) as n FROM cases GROUP BY status;
"

echo "=== DUPLICATE TITLES ==="
docker exec supportbot-db-1 mysql -u supportbot -psupportbot supportbot -e "
SELECT problem_title, count(*) as n FROM cases GROUP BY problem_title HAVING n > 1;
"
