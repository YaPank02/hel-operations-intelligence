// Replace the source query for Flights in the existing report.
// Keep the server and database values from your current query if they differ.
let
    Database = PostgreSQL.Database("127.0.0.1:5432", "hel_operations"),
    Sql = Text.Combine({
        "SELECT snapshot_id, source_row_number, direction, home_airport, flight_number,",
        "    scheduled_date_helsinki,",
        "    scheduled_at AT TIME ZONE 'Europe/Helsinki' AS scheduled_time_helsinki,",
        "    block_event_at AT TIME ZONE 'Europe/Helsinki' AS block_time_helsinki,",
        "    status_code, status_text, route_1, route_2, gate, stand,",
        "    array_to_string(codeshares, ', ') AS codeshares,",
        "    cardinality(quality_flags) AS quality_issue_count,",
        "    array_to_string(quality_flags, ' | ') AS quality_flags,",
        "    is_upcoming, forecast_hour_utc, forecast_hour_helsinki, weather_window_key,",
        "    forecast_retrieved_helsinki, temperature_c, wind_speed_ms, precipitation_mm_1h,",
        "    weather_attention, weather_condition, wind_gust_ms, visibility_m,",
        "    weather_level, weather_reason, snow_flag,",
        "    CASE weather_level",
        "        WHEN 'Routine' THEN '#E3F2E7'",
        "        WHEN 'Monitor' THEN '#FFF1CB'",
        "        WHEN 'Priority review' THEN '#FCE2E2'",
        "        WHEN 'Unknown' THEN '#ECEFF1'",
        "        ELSE '#FFFFFF'",
        "    END AS weather_background,",
        "    CASE WHEN snow_flag THEN 'Snow: supplier check' END AS snow_badge,",
        "    CASE WHEN snow_flag THEN '#EDDEF7' ELSE '#FFFFFF' END AS snow_background",
        "FROM hel.v_latest_flights_weather"
    }, "#(lf)"),
    Source = Value.NativeQuery(Database, Sql, null, [EnableFolding = true])
in
    Source
