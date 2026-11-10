-- Run only in the hel_operations database. Safe to reapply for this initial schema.
CREATE SCHEMA IF NOT EXISTS hel;

CREATE TABLE IF NOT EXISTS hel.snapshot (
    snapshot_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_sha256 text NOT NULL UNIQUE CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    source_file text NOT NULL,
    source_bytes bigint NOT NULL CHECK (source_bytes > 0),
    airport_scope text NOT NULL CHECK (airport_scope = 'HEL'),
    request_scope text NOT NULL CHECK (request_scope = 'all'),
    source_captured_at timestamptz,
    loaded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expected_record_count integer NOT NULL CHECK (expected_record_count > 0)
);

CREATE TABLE IF NOT EXISTS hel.snapshot_section (
    snapshot_id bigint NOT NULL REFERENCES hel.snapshot(snapshot_id),
    direction text NOT NULL CHECK (direction IN ('dep', 'arr')),
    source_generated_at timestamptz NOT NULL,
    expected_record_count integer NOT NULL CHECK (expected_record_count >= 0),
    PRIMARY KEY (snapshot_id, direction)
);

CREATE TABLE IF NOT EXISTS hel.flight_observation (
    snapshot_id bigint NOT NULL,
    direction text NOT NULL,
    source_row_number integer NOT NULL CHECK (source_row_number > 0),
    home_airport text,
    flight_number text,
    source_flight_date text,
    scheduled_at timestamptz,
    estimated_at timestamptz,
    previous_estimated_at timestamptz,
    actual_event_at timestamptz,
    block_event_at timestamptz,
    status_code text,
    status_text text,
    aircraft_registration text,
    aircraft_type text,
    route_1 text,
    route_2 text,
    route_3 text,
    route_4 text,
    gate text,
    stand text,
    codeshares text[] NOT NULL,
    quality_flags text[] NOT NULL,
    source_fields jsonb NOT NULL CHECK (jsonb_typeof(source_fields) = 'object'),
    PRIMARY KEY (snapshot_id, direction, source_row_number),
    FOREIGN KEY (snapshot_id, direction)
        REFERENCES hel.snapshot_section(snapshot_id, direction)
);
CREATE INDEX IF NOT EXISTS flight_observation_scheduled_idx
    ON hel.flight_observation(scheduled_at);

COMMENT ON TABLE hel.snapshot IS
    'One distinct XML payload, identified by SHA-256. Identical content is loaded once.';
COMMENT ON TABLE hel.flight_observation IS
    'One source flight row in one snapshot. No stable cross-snapshot movement ID is assumed.';
COMMENT ON COLUMN hel.flight_observation.actual_event_at IS
    'Source act_d: generic actual event time, not assumed to be takeoff or landing.';
COMMENT ON COLUMN hel.flight_observation.block_event_at IS
    'Source ablk_d: off-block for departures; on-block for arrivals.';
