-- Run after the Python loader commits, in the hel_operations Query Tool.
SELECT
    (SELECT COUNT(*) FROM hel.snapshot) AS stored_snapshots,
    (SELECT COUNT(*) FROM hel.flight_observation) AS stored_observations,
    (SELECT COUNT(*) FROM hel.v_latest_flights) AS latest_source_records;

SELECT direction, COUNT(*) AS latest_source_records
FROM hel.v_latest_flights GROUP BY direction ORDER BY direction;

SELECT * FROM hel.v_snapshot_quality ORDER BY snapshot_id, direction;

SELECT flight_number, direction,
    scheduled_at AT TIME ZONE 'Europe/Helsinki' AS scheduled_helsinki,
    actual_event_at AT TIME ZONE 'Europe/Helsinki' AS actual_event_helsinki,
    block_event_at AT TIME ZONE 'Europe/Helsinki' AS block_event_helsinki,
    status_code, route_1, gate, codeshares
FROM hel.v_latest_flights
ORDER BY scheduled_at, direction, flight_number LIMIT 20;

-- Repeated candidate keys in a single snapshot are review items, not automatic merges.
SELECT snapshot_id, direction, home_airport, flight_number, scheduled_at, COUNT(*)
FROM hel.flight_observation
GROUP BY snapshot_id, direction, home_airport, flight_number, scheduled_at
HAVING COUNT(*) > 1;
