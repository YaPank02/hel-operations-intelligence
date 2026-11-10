-- Reporting selects the latest source generation for each direction.
-- Historical observations remain available in hel.flight_observation.
CREATE OR REPLACE VIEW hel.v_latest_flights AS
WITH newest_section AS (
    SELECT DISTINCT ON (s.airport_scope, s.request_scope, ss.direction)
        ss.snapshot_id, ss.direction, ss.source_generated_at
    FROM hel.snapshot s
    JOIN hel.snapshot_section ss USING (snapshot_id)
    ORDER BY s.airport_scope, s.request_scope, ss.direction,
        ss.source_generated_at DESC,
        s.source_captured_at DESC NULLS LAST, s.source_sha256 DESC
)
SELECT f.*, n.source_generated_at, s.source_captured_at, s.loaded_at,
    s.source_file,
    (f.scheduled_at AT TIME ZONE 'Europe/Helsinki')::date
        AS scheduled_date_helsinki
FROM newest_section n
JOIN hel.flight_observation f
    ON f.snapshot_id = n.snapshot_id AND f.direction = n.direction
JOIN hel.snapshot s ON s.snapshot_id = f.snapshot_id;

CREATE OR REPLACE VIEW hel.v_status_summary AS
SELECT direction, COALESCE(NULLIF(status_code, ''), '(not reported)') AS status_code,
    COUNT(*) AS record_count
FROM hel.v_latest_flights
GROUP BY direction, COALESCE(NULLIF(status_code, ''), '(not reported)');

CREATE OR REPLACE VIEW hel.v_traffic_hourly AS
SELECT direction,
    date_trunc('hour', scheduled_at, 'UTC') AS scheduled_hour_utc,
    date_trunc('hour', scheduled_at, 'UTC') AT TIME ZONE 'Europe/Helsinki'
        AS scheduled_hour_helsinki,
    COUNT(*) AS record_count
FROM hel.v_latest_flights
WHERE scheduled_at IS NOT NULL
GROUP BY direction, date_trunc('hour', scheduled_at, 'UTC');
-- UTC is retained as the unique time-band key when a local hour repeats in autumn.

CREATE OR REPLACE VIEW hel.v_snapshot_quality AS
SELECT ss.snapshot_id, s.source_file, ss.direction,
    ss.source_generated_at, ss.expected_record_count,
    COUNT(f.source_row_number) AS stored_record_count,
    COUNT(f.source_row_number) FILTER (
        WHERE cardinality(f.quality_flags) > 0) AS rows_with_basic_quality_flags,
    COUNT(f.source_row_number) FILTER (
        WHERE f.actual_event_at IS NOT NULL) AS rows_with_actual_event,
    COUNT(f.source_row_number) FILTER (
        WHERE f.block_event_at IS NOT NULL) AS rows_with_block_event
FROM hel.snapshot_section ss
JOIN hel.snapshot s USING (snapshot_id)
LEFT JOIN hel.flight_observation f
    ON f.snapshot_id = ss.snapshot_id AND f.direction = ss.direction
GROUP BY ss.snapshot_id, s.source_file, ss.direction,
    ss.source_generated_at, ss.expected_record_count;
