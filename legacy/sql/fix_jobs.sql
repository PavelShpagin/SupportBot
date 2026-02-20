ALTER TABLE jobs DROP CONSTRAINT jobs_status_chk;
ALTER TABLE jobs ADD CONSTRAINT jobs_status_chk CHECK (status IN ('pending', 'in_progress', 'done', 'failed', 'cancelled'));
