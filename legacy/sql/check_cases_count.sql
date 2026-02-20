SELECT COUNT(*) as case_count FROM cases;
SELECT group_id, COUNT(*) as cases_per_group FROM cases GROUP BY group_id;
