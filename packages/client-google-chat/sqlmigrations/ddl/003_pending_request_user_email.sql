-- rambler up
alter table pending_requests
    add column user_email text;

-- rambler down
alter table pending_requests
    drop column if exists user_email;
