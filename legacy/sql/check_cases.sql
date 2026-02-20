SELECT case_id, group_id, LEFT(problem_title, 60) as title, created_at FROM cases ORDER BY created_at DESC LIMIT 15;
