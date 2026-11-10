CREATE TABLE ledger_events (
    is_settled BOOLEAN NOT NULL,
    account_id BIGINT NOT NULL,
    region_id SMALLINT NOT NULL,
    settled_at TIMESTAMPTZ NOT NULL,
    fee_tier BOOLEAN NOT NULL,
    amount_cents BIGINT NOT NULL,
    currency_code SMALLINT NOT NULL,
    tx_uuid UUID NOT NULL,
    audit_seq BIGINT NOT NULL
);
