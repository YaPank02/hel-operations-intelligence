-- Preserve the original flight columns and append the shared forecast fields.
SELECT snapshot_id, source_row_number, direction, home_airport, flight_number,
    scheduled_date_helsinki,
    scheduled_at AT TIME ZONE 'Europe/Helsinki' AS scheduled_time_helsinki,
    block_event_at AT TIME ZONE 'Europe/Helsinki' AS block_time_helsinki,
    status_code, status_text, route_1, route_2, gate, stand,
    array_to_string(codeshares, ', ') AS codeshares,
    cardinality(quality_flags) AS quality_issue_count,
    array_to_string(quality_flags, ' | ') AS quality_flags,
    is_upcoming, forecast_hour_utc, forecast_hour_helsinki, weather_window_key,
    forecast_retrieved_helsinki, temperature_c, wind_speed_ms, precipitation_mm_1h,
    weather_attention, weather_condition
FROM hel.v_latest_flights_weather
ORDER BY scheduled_at NULLS LAST, direction, flight_number,
    snapshot_id, source_row_number;
