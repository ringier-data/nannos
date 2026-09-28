-- rambler up
-- The token broker returns a binding secret when a sign-in is redeemed and requires it on
-- every token request for that user (console-backend ADR-0011 amendment 1). It lives with
-- the broker row it belongs to: a sign-in again replaces both.
alter table user_auth add column broker_binding_secret text;

comment on column user_auth.broker_binding_secret is
    'broker rows: the binding secret from the broker''s /redeem, sent on every /token call; null for rows signed in before it existed';

-- rambler down
alter table user_auth drop column if exists broker_binding_secret;
