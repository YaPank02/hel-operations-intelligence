"""Fetch FMI's hourly HEL forecast, then load it into the existing database."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import math
from pathlib import Path
import sys
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[1]
FORECAST_QUERY = 'fmi::forecast::harmonie::surface::point::simple'
PARAMETERS = ('Temperature', 'WindSpeedMS', 'WindGust', 'Visibility', 'Precipitation1h', 'SmartSymbol')
FMI_URL = 'https://opendata.fmi.fi/wfs?' + urlencode({
    'service': 'WFS',
    'version': '2.0.0',
    'request': 'GetFeature',
    'storedquery_id': FORECAST_QUERY,
    'latlon': '60.3172,24.9633',
    'timestep': 60,
    'parameters': ','.join(PARAMETERS),
})
WFS_ROOT = '{http://www.opengis.net/wfs/2.0}FeatureCollection'
FMI_FEATURE = '{http://xml.fmi.fi/schema/wfs/2.0}'
MAX_BYTES = 2 * 1024 * 1024

# FMI's SmartSymbol v3 codes, including the corresponding night symbols (+100).
# https://en.ilmatieteenlaitos.fi/weather-symbols
DAY_SYMBOLS = {
    1, 2, 4, 6, 7, 9, 11, 14, 17, 21, 24, 27,
    *range(31, 40), *range(41, 50), *range(51, 60), 61, 64, 67, 71, 74, 77,
}
KNOWN_SYMBOLS = DAY_SYMBOLS | {code + 100 for code in DAY_SYMBOLS}


class WeatherError(ValueError):
    pass


def finite_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def parse_forecast(payload: bytes):
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise WeatherError('FMI did not return valid forecast XML.') from exc
    if root.tag != WFS_ROOT:
        raise WeatherError('FMI did not return a WFS forecast collection.')

    hours = {}
    for entry in root.iter(FMI_FEATURE + 'BsWfsElement'):
        parameter = entry.findtext(FMI_FEATURE + 'ParameterName')
        if parameter not in PARAMETERS:
            continue
        try:
            timestamp = datetime.fromisoformat(entry.findtext(FMI_FEATURE + 'Time') or '')
        except ValueError as exc:
            raise WeatherError('FMI returned an invalid forecast timestamp.') from exc
        if timestamp.tzinfo is None:
            raise WeatherError('FMI forecast timestamps must specify a time zone.')
        hour = timestamp.astimezone(timezone.utc)
        if hour.minute or hour.second or hour.microsecond:
            raise WeatherError('FMI returned a non-hourly forecast timestamp.')
        values = hours.setdefault(hour, {})
        if parameter in values:
            raise WeatherError(f'FMI returned duplicate values for {parameter} at {hour.isoformat()}.')
        number = finite_number(entry.findtext(FMI_FEATURE + 'ParameterValue'))
        if parameter != 'Temperature' and number is not None and number < 0:
            number = None
        values[parameter] = number

    rows = []
    for hour, values in sorted(hours.items()):
        symbol = values.get('SmartSymbol')
        rows.append((
            hour,
            int(symbol) if symbol in KNOWN_SYMBOLS else None,
            values.get('Temperature'),
            values.get('WindSpeedMS'),
            values.get('Precipitation1h'),
            values.get('WindGust'),
            values.get('Visibility'),
        ))
    return rows


def fetch_forecast():
    request = Request(FMI_URL, headers={'User-Agent': 'HEL-Operations-Intelligence/1.0'})
    with urlopen(request, timeout=20) as response:
        payload = response.read(MAX_BYTES + 1)
    if len(payload) > MAX_BYTES:
        raise WeatherError('FMI response exceeded the forecast size limit.')
    retrieved_at = datetime.now(timezone.utc)
    rows = parse_forecast(payload)
    current_hour = retrieved_at.replace(minute=0, second=0, microsecond=0)
    if not any(hour >= current_hour and symbol is not None for hour, symbol, *_ in rows):
        raise WeatherError('FMI returned no current or future hours with a usable weather symbol.')
    return payload, rows, retrieved_at


def save_forecast(conn, rows, retrieved_at):
    """Replace the full HEL forecast in one transaction after validation."""
    with conn.transaction():
        # Serialize weather loads, including initial table/view creation.
        conn.execute('SELECT pg_advisory_xact_lock(1279872358, 8)')
        conn.execute((ROOT / 'sql' / '008_fmi_weather.sql').read_text(encoding='utf-8'), prepare=False)
        latest = conn.execute('SELECT max(retrieved_at) FROM hel.fmi_hourly_forecast').fetchone()[0]
        if latest is not None and latest >= retrieved_at:
            return False
        conn.execute('DELETE FROM hel.fmi_hourly_forecast')
        with conn.cursor() as cursor:
            cursor.executemany('''
                INSERT INTO hel.fmi_hourly_forecast
                    (forecast_hour_utc, smart_symbol, temperature_c, wind_speed_ms,
                     precipitation_mm_1h, wind_gust_ms, visibility_m, retrieved_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ''', ((*row, retrieved_at) for row in rows))
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='Check the free FMI feed without connecting to PostgreSQL.')
    mode.add_argument('--export', action='store_true', help='Load the forecast and refresh the existing Excel export.')
    args = parser.parse_args()

    try:
        payload, rows, retrieved_at = fetch_forecast()
    except (WeatherError, URLError, TimeoutError, OSError) as exc:
        print(f'FMI weather fetch failed. Previous data was kept. {exc}', file=sys.stderr)
        return 1
    symbols = sum(symbol is not None for _, symbol, *_ in rows)
    gusts = sum(gust is not None for *_, gust, _visibility in rows)
    visibility_hours = sum(visibility is not None for *_, visibility in rows)
    print(f'FMI HARMONIE HEL forecast: {len(rows)} hours, {symbols} with a usable weather symbol.')
    print(f'Forecast coverage: {gusts} hours with gusts and {visibility_hours} hours with visibility.')
    print(f'Forecast span (UTC): {rows[0][0].isoformat()} to {rows[-1][0].isoformat()}.')
    if args.check:
        return 0

    try:
        import psycopg
        from dotenv import load_dotenv
        from load_postgres import DatabaseSettings, LoadError
        from export_excel import ExportError, export_bundle, verify_bundle
    except ImportError as exc:
        print(f'Could not load the existing PostgreSQL environment. {exc}', file=sys.stderr)
        return 1

    try:
        load_dotenv(ROOT / '.env', override=False)
        archive = ROOT / 'data' / 'raw' / 'weather'
        archive.mkdir(parents=True, exist_ok=True)
        stamp = retrieved_at.strftime('%Y%m%dT%H%M%S_%fZ')
        (archive / f'fmi_HEL_harmonie_{stamp}.xml').write_bytes(payload)
        settings = DatabaseSettings.from_environment()
        with settings.connect() as conn:
            saved = save_forecast(conn, rows, retrieved_at)
            print('Weather saved in PostgreSQL.' if saved else 'A newer weather forecast is already in PostgreSQL.')
            statuses = conn.execute('''
                SELECT weather_level, count(*), count(*) FILTER (WHERE snow_flag)
                FROM hel.v_latest_flights_weather
                WHERE is_upcoming
                GROUP BY weather_level
                ORDER BY weather_level
            ''').fetchall()
            if statuses:
                for status, count, _ in statuses:
                    print(f'{status}: {count} upcoming flight records')
                print(f'Snow badge: {sum(snow for _, _, snow in statuses)} upcoming flight records')
            else:
                print('Upcoming flight records: 0. Refresh Finavia and load the new snapshot.')
            if args.export:
                destination = ROOT / 'data' / 'processed' / 'excel'
                export_bundle(conn, destination)
                result = verify_bundle(destination)
                print(f"Excel export verified: {result['files']['flights_current.csv']['rows']} flight records.")
    except psycopg.OperationalError:
        print('Could not connect to PostgreSQL. Use your existing project database settings.', file=sys.stderr)
        return 1
    except psycopg.Error as exc:
        print(f'PostgreSQL weather or export step failed (SQLSTATE {exc.sqlstate}).', file=sys.stderr)
        return 1
    except (LoadError, ExportError, OSError) as exc:
        print(f'Weather load or Excel export failed. {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
