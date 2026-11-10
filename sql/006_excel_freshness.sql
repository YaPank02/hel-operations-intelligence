-- Same section selection as v_latest_flights; includes a zero-row section.
SELECT DISTINCT ON (s.airport_scope, s.request_scope, ss.direction)
    ss.direction, ss.snapshot_id,
    ss.source_generated_at AT TIME ZONE 'Europe/Helsinki'
        AS source_updated_helsinki,
    s.source_captured_at AT TIME ZONE 'Europe/Helsinki'
        AS collected_helsinki,
    s.loaded_at AT TIME ZONE 'Europe/Helsinki'
        AS database_loaded_helsinki,
    ss.expected_record_count AS source_records
FROM hel.snapshot s
JOIN hel.snapshot_section ss USING (snapshot_id)
WHERE s.airport_scope = 'HEL' AND s.request_scope = 'all'
ORDER BY s.airport_scope, s.request_scope, ss.direction,
    ss.source_generated_at DESC,
    s.source_captured_at DESC NULLS LAST, s.source_sha256 DESC;
