# HEL Operations Intelligence

Airport Performance Reporting & Automation — independent portfolio project.

## Current stage

The user completed live collection, parsing and PostgreSQL loading on Windows.
Two initial Power BI pages now show traffic, source timestamps, status coverage
and basic quality checks. The 4 November 2026 refresh reconciled SQL and Power BI:
394 records, 94 without status and 0 basic quality flags. SQL-to-CSV export for
Excel is prepared and locally tested; see docs/excel_step.md for the next steps.
The Excel workbook, scheduled collection and briefing automation remain planned.
Timestamp definitions are in docs/parser_step.md.

Keep your existing working-plan Markdown file. Move it into `docs/` if convenient.

## Setup on your Windows computer

Open the existing project folder in VS Code. Keep your existing `.venv`.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

If you already configured `.env`, keep it and do not copy the example over it.
Open `.env` locally and replace only the placeholder API key with your Finavia
subscription key. The default configuration requests arrivals and departures
for HEL. The screenshot confirms the header name is `app_key`.

Run from the project folder:

```powershell
.\.venv\Scripts\python.exe scripts\fetch_finavia.py
```

Expected on success:
- HTTP 200 and a valid XML document with root `flights`.
- One XML file and one small metadata JSON file in `data/raw/`.
- A console summary of XML sections, not an invented flight count.

The metadata contains the source URL, collection time and structural information.
It does not include request headers or your key. The XML is saved as received,
not reformatted. If the API returns another schema, the script stops so we can
inspect the documented format before changing the parser.

## Project files

| Path | Purpose |
| --- | --- |
| `AGENTS.md` | Instructions for coding assistance and project boundaries. |
| `TASKS.md` | Verified progress, current task and next steps. |
| `.env.example` | Safe configuration template; contains no real key. |
| `.gitignore` | Keeps credentials, virtual environments and raw captures out of Git. |
| `requirements.txt` | Direct Python dependencies. |
| `scripts/fetch_finavia.py` | One-shot collector and XML structural summary. |
| `tests/test_fetch_finavia.py` | Offline tests with simulated HTTP responses. |
| `docs/data_source_notes.md` | What the screenshots establish and what still needs checking. |
| `data/raw/` | Local XML captures and metadata; ignored by Git. |
| `sql/`, `excel/`, `powerbi/`, `automation/` | Reserved for later working outputs. |

## Verification

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests use synthetic XML and mock HTTP responses. They do not call Finavia or
establish that a key is valid. Requirements record compatible major-version
ranges; they are not a full lockfile.

## API behaviour

- HTTPS only, fixed Finavia gateway; never downgrade to the HTTP example.
- The `app_key` is sent as a header, not a URL query parameter.
- Redirects are not followed, avoiding accidental forwarding of the key.
- A single request per run; no automatic retry or polling yet.
- Connection/read timeout of 10/30 seconds, not a strict total runtime limit.
- Invalid configuration, non-200 status, network errors, invalid XML and
  unexpected XML roots produce a concise error and non-zero exit status.
- An HTTP 200 with empty `flights` XML is saved for inspection, without claiming
  there were zero movements.
- Raw capture directories are local-only until data-use conditions are checked.

## Next milestone

Run scripts/export_excel.py and connect Excel Power Query to the fixed CSV files
using docs/excel_step.md. Build independent Excel reconciliation and a briefing
sheet. Define cross-snapshot movement identity before combined historical
performance reporting; scheduled-event semantics still need confirmation.

This is independent work using public data. It is not a Finnair or Finavia system.

