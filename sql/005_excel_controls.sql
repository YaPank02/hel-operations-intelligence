-- Independent SQL aggregates for reconciliation against Excel calculations.
-- NULL scheduled dates remain a separate group, rather than being discarded.
SELECT scheduled_date_helsinki, direction,
    COUNT(*) AS flight_records,
    COUNT(*) FILTER (WHERE cardinality(quality_flags) > 0) AS flagged_records,
    COUNT(*) FILTER (
        WHERE COALESCE(TRIM(status_text), '') = ''
    ) AS records_without_status
FROM hel.v_latest_flights
GROUP BY scheduled_date_helsinki, direction
ORDER BY scheduled_date_helsinki NULLS LAST, direction;
