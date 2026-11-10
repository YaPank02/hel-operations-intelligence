-- One row per UTC hour in the latest successfully loaded HEL forecast.
CREATE TABLE IF NOT EXISTS hel.fmi_hourly_forecast (
    forecast_hour_utc timestamptz PRIMARY KEY,
    smart_symbol smallint,
    temperature_c double precision,
    wind_speed_ms double precision,
    precipitation_mm_1h double precision,
    retrieved_at timestamptz NOT NULL,
    CHECK (forecast_hour_utc = date_trunc('hour', forecast_hour_utc, 'UTC'))
);

-- Upgrade the already installed table in place. Earlier forecasts have NULL
-- gust and visibility values until the HARMONIE forecast replaces them.
ALTER TABLE hel.fmi_hourly_forecast
    ADD COLUMN IF NOT EXISTS wind_gust_ms double precision,
    ADD COLUMN IF NOT EXISTS visibility_m double precision;

-- Classify the forecast once per airport hour, before matching flight rows.
CREATE OR REPLACE VIEW hel.v_fmi_hourly_attention AS
WITH forecast AS (
    SELECT w.*,
        smart_symbol % 100 AS day_symbol,
        extract(month FROM forecast_hour_utc AT TIME ZONE 'Europe/Helsinki') BETWEEN 5 AND 10 AS summer
    FROM hel.fmi_hourly_forecast w
), benchmarks AS (
    SELECT f.*,
        CASE WHEN summer THEN 15 ELSE 20 END AS gust_monitor,
        CASE WHEN summer THEN 20 ELSE 25 END AS gust_priority,
        day_symbol IN (21, 24, 27) OR day_symbol BETWEEN 31 AND 39 AS is_rain,
        COALESCE(day_symbol BETWEEN 51 AND 59, false) AS snow_flag
    FROM forecast f
), signals AS (
    SELECT b.*,
        CASE
            WHEN wind_gust_ms >= gust_priority THEN 2
            WHEN wind_gust_ms >= gust_monitor THEN 1
            ELSE 0
        END AS gust_signal,
        CASE
            WHEN is_rain AND precipitation_mm_1h >= 30 THEN 2
            WHEN is_rain AND precipitation_mm_1h >= 20 THEN 1
            ELSE 0
        END AS rain_signal,
        CASE
            WHEN day_symbol IN (14, 17, 71, 74, 77) THEN 2
            WHEN day_symbol = 9 OR snow_flag THEN 1
            ELSE 0
        END AS symbol_signal,
        NULLIF(concat_ws(', ',
            CASE WHEN day_symbol IS NULL THEN 'weather symbol' END,
            CASE WHEN wind_gust_ms IS NULL THEN 'gusts' END,
            CASE WHEN is_rain AND precipitation_mm_1h IS NULL THEN 'hourly rain' END
        ), '') AS missing_checks,
        CASE
            WHEN day_symbol BETWEEN 21 AND 27 THEN 'Rain showers'
            WHEN day_symbol BETWEEN 31 AND 39 THEN 'Rain'
            WHEN day_symbol BETWEEN 41 AND 49 THEN 'Sleet'
            WHEN day_symbol BETWEEN 51 AND 56 THEN 'Snow showers'
            WHEN day_symbol BETWEEN 57 AND 59 THEN 'Snowfall'
            WHEN day_symbol BETWEEN 61 AND 67 THEN 'Hail showers'
            WHEN day_symbol BETWEEN 71 AND 77 THEN 'Thundershowers'
            ELSE CASE day_symbol
                WHEN 1 THEN 'Clear'
                WHEN 2 THEN 'Mostly clear'
                WHEN 4 THEN 'Partly cloudy'
                WHEN 6 THEN 'Mostly cloudy'
                WHEN 7 THEN 'Overcast'
                WHEN 9 THEN 'Fog'
                WHEN 11 THEN 'Drizzle'
                WHEN 14 THEN 'Freezing drizzle'
                WHEN 17 THEN 'Freezing rain'
            END
        END AS weather_condition
    FROM benchmarks b
)
SELECT forecast_hour_utc, smart_symbol, temperature_c, wind_speed_ms, precipitation_mm_1h,
    retrieved_at, wind_gust_ms, visibility_m, weather_condition, snow_flag,
    CASE greatest(gust_signal, rain_signal, symbol_signal)
        WHEN 2 THEN 'Priority review'
        WHEN 1 THEN 'Monitor'
        ELSE CASE WHEN missing_checks IS NULL THEN 'Routine' ELSE 'Unknown' END
    END AS weather_level,
    COALESCE(NULLIF(concat_ws(' · ',
        CASE WHEN gust_signal > 0 THEN 'Gusts ' || wind_gust_ms::text || ' m/s (benchmark '
            || (CASE WHEN gust_signal = 2 THEN gust_priority ELSE gust_monitor END)::text || ' m/s)' END,
        CASE WHEN rain_signal > 0 THEN 'Rain ' || precipitation_mm_1h::text || ' mm/h (benchmark '
            || (CASE WHEN rain_signal = 2 THEN '30' ELSE '20' END) || ' mm/h)' END,
        CASE WHEN symbol_signal > 0 THEN weather_condition || ' forecast' END,
        CASE WHEN missing_checks IS NOT NULL THEN 'Missing ' || missing_checks END
    ), ''), 'No monitored trigger') AS weather_reason
FROM signals;

-- Keep every latest Finavia source row. The forecast key is unique, so the
-- left join cannot increase the flight counts. Only upcoming HEL rows can flag.
CREATE OR REPLACE VIEW hel.v_latest_flights_weather AS
SELECT f.*,
    COALESCE(f.scheduled_at >= CURRENT_TIMESTAMP, false) AS is_upcoming,
    w.forecast_hour_utc AT TIME ZONE 'UTC' AS forecast_hour_utc,
    w.forecast_hour_utc AT TIME ZONE 'Europe/Helsinki' AS forecast_hour_helsinki,
    to_char(w.forecast_hour_utc AT TIME ZONE 'Europe/Helsinki', 'YYYY-MM-DD HH24:MI')
        || ' Helsinki (' || to_char(w.forecast_hour_utc AT TIME ZONE 'UTC', 'HH24:MI')
        || ' UTC)' AS weather_window_key,
    w.retrieved_at AT TIME ZONE 'Europe/Helsinki' AS forecast_retrieved_helsinki,
    w.temperature_c,
    w.wind_speed_ms,
    w.precipitation_mm_1h,
    CASE
        WHEN f.scheduled_at IS NULL THEN 'Schedule unavailable'
        WHEN f.scheduled_at < CURRENT_TIMESTAMP THEN 'Past flight'
        WHEN w.smart_symbol IS NULL THEN 'Forecast unavailable'
        WHEN w.snow_flag THEN 'Snow forecast'
        ELSE 'No snow flag'
    END AS weather_attention,
    w.weather_condition,
    w.wind_gust_ms,
    w.visibility_m,
    CASE WHEN f.scheduled_at >= CURRENT_TIMESTAMP
        THEN COALESCE(w.weather_level, 'Unknown') END AS weather_level,
    CASE WHEN f.scheduled_at >= CURRENT_TIMESTAMP
        THEN COALESCE(w.weather_reason, 'Forecast unavailable for this hour') END AS weather_reason,
    COALESCE(w.snow_flag, false) AS snow_flag
FROM hel.v_latest_flights f
LEFT JOIN hel.v_fmi_hourly_attention w
    ON f.home_airport = 'HEL'
    AND f.scheduled_at >= CURRENT_TIMESTAMP
    AND w.forecast_hour_utc = date_trunc('hour', f.scheduled_at, 'UTC')
    -- Three hours is a project freshness rule, not an FMI forecast limit.
    AND w.retrieved_at >= CURRENT_TIMESTAMP - interval '3 hours';
