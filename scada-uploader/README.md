# SCADA telemetry uploader

Local program for a CBG plant PC. It reads SCADA CSV exports from a folder, maps the columns onto the Unified GOBARdhan Portal telemetry JSON, signs each request, and can POST it. A SQLite file next to the program remembers every reading. Nothing is sent unless the operator passes `--send`.

The contract is Integration Guide V1.3 (sections 3–6 and 8). This checkout does not include a built executable.

## How it works

1. Scan `watch_folder` for `*.csv` (not recursive). A file that is still growing, or whose size or modification time changes during the stability window, is skipped until a later run. Daily filenames can change; old files can stay in the folder.
2. Map each row. `Date` + `Time` become `readingAt`, the SCADA measurement time, including rows backfilled after an outage. The clock is interpreted in `timezone` (default `Asia/Kolkata`, fixed offset `+05:30`) and written as ISO-8601, for example `2026-10-23T13:45:02+05:30`.
3. Keep the original sensor numbers. Values are not rewritten to pass validation. A row that breaks a rule in the guide is stored as `needs_attention` and is not posted.
4. On `--send`, POST each pending valid row. The default body is one JSON object (guide section 5, and the section 8 sample). `"payload_mode": "batch"` sends a JSON array instead (section 5, option B).
5. Record the result in `scada_uploader.sqlite`.

`readingAt` is not the authentication clock. `X-SCADA-Timestamp` is Unix time in seconds at the moment of the request. Section 4 rejects that header when it is more than ±5 minutes (300 seconds) from the server. The plant PC needs NTP so its clock stays inside that window.

HTTP 202 means the portal queued the batch. It does not mean government processing finished. The status `api_accepted` means the same thing: the API accepted the request into its queue. The returned `batchId` is stored. There is no status-poll API in the guide.

| Status | Meaning |
| --- | --- |
| `pending` | Valid, not yet accepted. Connection failures and HTTP 5xx stay here and are retried. |
| `api_accepted` | HTTP 202. Queued by the API. Not posted again. |
| `needs_attention` | Failed a local rule, or the API returned HTTP 400. Not posted again until a corrected export replaces it. |
| `uncertain` | The request may have left the PC (timeout, or the connection dropped after send) but no 202 came back. Not posted again until an operator requeues it. |

Rows already `api_accepted` or `uncertain` are left as they are if the same `readingAt` shows up again. A `needs_attention` row can be replaced by a later export of that same timestamp. CSV files are not deleted or renamed.

Retries inside one run are only for connection failures (the request was not sent) and HTTP 429/5xx, up to `max_attempts_per_run` (default 3). HTTP 401 is tried once per run and left `pending`, so a corrected PC clock is picked up on the next scheduled run. HTTP 400 is `needs_attention`.

## Private settings

The device id, shared secret, and portal URL are not in this repository. Put them only in `config.json` on the machine that sends data. `.gitignore` excludes `config.json`, the SQLite queue, and the log. Do not commit that file, and do not paste those values into the README or into source code.

`config.example.json` is the public template. It contains placeholders only.

| Setting | Where the value comes from |
| --- | --- |
| `device_id` | Device id registered with the portal administrator |
| `shared_secret` | Signing key issued during onboarding. This is not the Windows login password. |
| `telemetry_url` | Full POST URL, ending in `/api/v1/public/scada/telemetry` |

On a new PC, including a Windows SCADA PC:

1. Copy `config.example.json` to `config.json` in the same folder.
2. Replace `YOUR_DEVICE_ID`, `YOUR_SHARED_SECRET`, and `https://YOUR_PORTAL_HOST.example.invalid/...` with the real values.
3. Leave `config.json` on that PC.

The program refuses `--send` while `telemetry_url` is still the example placeholder.

Secret lookup order:

1. Environment variable `SCADA_SHARED_SECRET`
2. `shared_secret_file` (text file, one line, also keep that file out of Git)
3. `shared_secret` in `config.json`

The secret is not written to the log or to the dry-run summary. Restrict `config.json` to the account that runs the task (`chmod 600` on macOS or Linux; on Windows, limit the file to that user).

| Setting | Role |
| --- | --- |
| `watch_folder` | Folder of `*.csv` exports. Relative paths are from the config file's directory. |
| `device_id` | `X-SCADA-Device-Id` |
| `telemetry_url` | Full POST URL. Guide path: `/api/v1/public/scada/telemetry` |
| `timezone`, `utc_offset` | How to read the SCADA clock. `Asia/Kolkata` is always `+05:30`. |
| `payload_mode` | `single` (default) or `batch` |
| `batch_size` | Local cap used only in batch mode. The guide does not set a limit. |
| `file_stable_seconds` | How long a file must sit unchanged before it is read. |
| `database_path`, `log_path` | SQLite queue and log, next to the app by default. |

`Date` values use a dot as the decimal separator. A thousands comma (`12,548,750.75`) is accepted. A decimal comma is not. Ambiguous numeric dates such as `03/07/2026` are read day-first.

There is no config flag that turns sending on. Only the `--send` command-line flag opens a connection.

## Column map

| CSV column | JSON field |
| --- | --- |
| Date + Time | `readingAt` |
| MOISTURE SENSOR | `moisture_mg_m3` |
| CH4 | `methane_mole_percent` |
| H2S | `h2s_mg_m3` |
| H2S TOT | `totalSulphurIncludingH2s_mg_m3` |
| CO2 N2 O2 | `co2N2O2_mole_percent` |
| CO2 | `co2_mole_percent` |
| O2 | `oxygen_mole_percent` |
| FRESH SLURRY ACT FLOW | `fresh_slurry_flow_scm_hr` |
| RAW BIOGAS ACT FLOW | `raw_biogas_flow_scm_hr` |
| PURIFIED BIOGAS ACT FLOW | `purified_biogas_flow_scm_hr` |
| DISPATCH BIOGAS ACT FLOW | `dispatch_flow_kg_hr` |
| FRESH SLURRY ACT TOT | `slurry_FlowTotalizer_scm` |
| RAW BIOGAS ACT TOT | `raw_FlowTotalizer_scm` |
| PURIFIED BIOGAS ACT TOT | `purified_FlowTotalizer_scm` |
| DISPATCH BIOGAS ACT TOT | `dispatch_flowTotalizer_kg` |

Header matching ignores case and extra internal spaces. Units on the plant tags are provisional until the SCADA export's engineering units are confirmed against the guide (mg/m³, mole percent, SCM/hr, kg/hr, SCM, kg). The program does not convert units.

## Validation

Section 5 bounds, treated as inclusive:

- `moisture_mg_m3` ≤ 5.0
- `methane_mole_percent` 95.0 to 100.0
- `h2s_mg_m3` ≤ 3.7
- `totalSulphurIncludingH2s_mg_m3` ≤ 10.0
- `co2N2O2_mole_percent` ≤ 5.0
- `co2_mole_percent` ≤ 4.0
- `oxygen_mole_percent` ≤ 0.5
- Flow and totalizer fields are required numbers. The guide states no range for them.

Section 6 returns HTTP 400 when a posted value is outside those rules. The documented example is `methane_mole_percent must be >= 95.0`. A zero CH4 reading is outside 95–100, so it is stored as `needs_attention` and is not sent. Other zeros are inside the stated maxima, so they are not by themselves a reason to hold a row. `samples/SAMPLE_ONLY_two_zero_rows.csv` is fake data for local tests: two rows, every sensor at 0. Both rows are held because CH4 is 0.

## Signature

Section 4 and the section 8 Python sample:

- `Content-Type: application/json`
- `X-SCADA-Device-Id`
- `X-SCADA-Timestamp`: decimal Unix seconds, as a string
- `X-SCADA-Signature`: lowercase hex HMAC-SHA256

The HMAC key is the shared secret. The message is `X-SCADA-Timestamp + "." + body`, UTF-8. `body` is the exact compact JSON that is posted (`json.dumps` separators `(',', ':')` in the guide's sample: no spaces or newlines). The signature is computed over those bytes, not over a second pretty-printed copy.

Header names are sent with that exact spelling. Python 3.9's `urllib.request` would rewrite them with `str.title()` (`X-Scada-Device-Id`), so this program uses `http.client` from the standard library instead. Redirects are not followed.

## Dry run and a real send

From this folder, with Python 3.9+:

```bash
python3 -m unittest discover -s tests -t .
python3 -m scada_uploader --config config.json
```

The second command reads the watch folder, updates SQLite, prints the request it would make, and does not open a network connection. Copy `samples/SAMPLE_ONLY_two_zero_rows.csv` into `incoming/` if you want to see the zero-CH4 rows held back.

When the portal team is ready:

```bash
python3 -m scada_uploader --config config.json --send
```

Exit 0 on a dry run, or on a send run that queued at least one row and had no uncertain delivery. Exit 1 when a send run had a file error, any uncertain row, or at least one request and no 202. Rows still `pending` after a partial success stay for the next run. Exit 2 when `--send` and `--dry-run` are combined, or when `--send` is pointed at the placeholder URL in the example config.

If a timeout left rows `uncertain` and you have confirmed with the portal that those `batchId`s were not queued, put them back and send on a later command:

```bash
python3 -m scada_uploader --config config.json --requeue-uncertain
python3 -m scada_uploader --config config.json --send
```

`--requeue-uncertain` alone does not POST.

## Windows SCADA PC

There is no status window. The scheduled task runs in the background. After each run, read `scada_uploader.log` and the queue statuses in `scada_uploader.sqlite`.

An exe has not been built in this checkout. `build_windows.bat` must be run on a 64-bit Windows machine. Python is needed only on that build machine. The plant PC can then run `scada-uploader.exe` without Python.

`watch_folder`, `database_path`, and `log_path` are resolved from the folder that contains `config.json`, not from the process working directory. Set `watch_folder` to the full path of the SCADA CSV export folder. Leave `database_path` as `scada_uploader.sqlite` and `log_path` as `scada_uploader.log` so both sit next to `config.json`.

### Build the exe

On the Windows PC, in this folder:

```bat
build_windows.bat
```

The script stops if Python 3.9 or newer, 64-bit, is not on PATH. It installs PyInstaller if that machine does not already have it, then writes `dist\scada-uploader.exe`. The exe is windowless so a console does not flash every 15 minutes. `Asia/Kolkata` uses a fixed `+05:30` offset and does not need the `tzdata` package.

### Copy the program

Copy these into one folder, for example `C:\SCADA\uploader`:

- `dist\scada-uploader.exe`
- `config.example.json`
- `register_scheduled_task.ps1`

### Create config.json

In that install folder, copy the example and fill in the three private values. Do not commit this file.

```bat
cd /d C:\SCADA\uploader
copy config.example.json config.json
notepad config.json
```

Set:

- `device_id` to the registered device id
- `shared_secret` to the portal signing key
- `telemetry_url` to the full portal URL, ending in `/api/v1/public/scada/telemetry`
- `watch_folder` to the full path of the SCADA CSV export folder, such as `D:\Plant\SCADA\Export`

Use a local disk path or a UNC path for `watch_folder`. A mapped drive letter often disappears when nobody is logged on. Do not put the Windows account password in `config.json`.

Optional check that does not POST:

```bat
scada-uploader.exe --config config.json
```

The live task adds `--send`.

### Register the 15-minute task

```bat
powershell -ExecutionPolicy Bypass -File .\register_scheduled_task.ps1 -InstallDir "C:\SCADA\uploader"
```

`-InstallDir` defaults to the folder that contains the script. The task name is `SCADA Portal Upload`. It runs:

```bat
scada-uploader.exe --config config.json --send
```

with that folder as the working directory. The first run is on the `:02`, `:17`, `:32`, or `:47` minute, then every 15 minutes. That 15-minute interval matches the sample export cadence. Change it with `-IntervalMinutes` or in Task Scheduler. The guide itself does not set an interval.

The task does not start a second copy if the previous run is still going (`IgnoreNew`), and each run is limited to 10 minutes. The program also keeps `scada_uploader.lock`. The task does not wake the PC from sleep. The PC must stay powered on. If a run was missed because the PC was off, `StartWhenAvailable` starts one when the PC is back.

By default the task runs only while the current user is logged on, so registration does not ask for a Windows password. To run when nobody is logged in: open Task Scheduler, open `SCADA Portal Upload`, Properties, General, select **Run whether user is logged on or not**, click OK, and enter the Windows password when prompted. Do not put that password in a file. This prompts for the same password without storing it in the script:

```bat
schtasks /Change /TN "SCADA Portal Upload" /RU "COMPUTER\User" /RP *
```

### How to see status

There is no status window. Next to `config.json`:

- `scada_uploader.log` records skipped files, validation holds, and each HTTP result.
- `scada_uploader.sqlite` is the queue. Status values are `pending`, `api_accepted`, `needs_attention`, and `uncertain`. `api_accepted` means the API queued the reading (HTTP 202), not that government processing finished.

### Remove the task

```bat
powershell -ExecutionPolicy Bypass -File .\register_scheduled_task.ps1 -Unregister
```

## Still needs confirmation from the portal

- Push interval. It is not in the guide.
- Whether production accepts section 5 option B (a JSON array). The opening sentence of section 5 and the section 8 sample are a single object, which is this program's default. Batch mode is implemented.
- Maximum array length, if batch mode is used. `batch_size` (default 20) is a local cap, not a figure from the guide.
- Duplicate and backfill behavior. The guide does not say what happens if the same `readingAt` is submitted twice (retry, crash after the server queued the body but before SQLite was updated, or a corrected backfill).
- No API is documented for checking that a `batchId` from HTTP 202 was eventually stored. `api_accepted` means queued only.
- Section 5 calls `readingAt` a "UTC Timestamp", but the example is an offset timestamp (`2026-07-03T11:00:00+05:30`). The section 7 Postman script replaces `readingAt` with `new Date().toISOString()` (current UTC, with a `Z`). This program follows the section 5 example and keeps the SCADA measurement time. It does not copy the Postman script.
- Bounds are applied as inclusive. The guide says "95.0 to 100.0" and "Max: N", and the 400 example only shows methane below 95.
- The table text for `slurry_FlowTotalizer_scm` says "Gas volume", which looks copied from the gas totalizers. The field name used here is the one in the JSON example.
- Engineering units on the SCADA tags versus the units named in the guide.
- HTTP status used when the timestamp is outside ±5 minutes. Section 4 describes the window. The 401 example in section 6 is "Invalid SCADA request signature", which may or may not be the same rejection.
