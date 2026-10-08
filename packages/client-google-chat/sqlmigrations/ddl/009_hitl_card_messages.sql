-- rambler up
-- Which posted approval card answers which pending call. A click updates its own card,
-- but a TYPED answer reaches the orchestrator without touching it, so the card kept
-- live buttons. When the server reports how the words were read (hitl-decision
-- extension), the client looks the card up here by call id and settles it. The app
-- cannot list messages, so the name is remembered when the card is posted.
create table hitl_card_message (
    project_id   text        not null,
    call_id      text        not null,
    message_name text        not null,
    expires_at   timestamptz not null,
    primary key (project_id, call_id)
);

create index idx_hitl_card_message_expires_at on hitl_card_message (expires_at);

-- rambler down
drop table if exists hitl_card_message;
