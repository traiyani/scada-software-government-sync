# SCADA telemetry uploader

Local program for a CBG plant PC. It reads SCADA CSV exports from a folder, maps the columns onto the Unified GOBARdhan Portal telemetry JSON, signs each request, and can POST it. A SQLite file next to the program remembers every reading. Nothing is sent unless the operator passes `--send`.

The contract is Integration Guide V1.3 (sections 3–6 and 8). This checkout does not include a built executable.

## How it works

1. Scan `watch_folder` for `*.csv` (not recursive). A file that is still growing, or whose size or modification time changes during the stability window, is skipped until a later run. Daily filenames can change; old files can stay in the folder. After a file is read in full, its size and modification time are stored in `scada_uploader.sqlite`. A later run skips that file at once while both are unchanged, so old daily files cost almost nothing and only a growing or re-exported file is read again.
2. Map each row. `Date` + `Time` become `readingAt`, the SCADA measurement time, including rows backfilled after an outage. The clock is interpreted in `timezone` (default `Asia/Kolkata`, fixed offset `+05:30`) and written as ISO-8601, for example `2026-10-23T13:45:02+05:30`.
3. Keep the original sensor numbers. Values are not rewritten to pass validation. A row that breaks a rule in the guide is stored as `needs_attention` and is not posted.
4. On `--send`, POST each pending valid row. The default body is one JSON object (guide section 5, and the section 8 sample). `"payload_mode": "batch"` sends a JSON array instead (section 5, option B).
5. Record the result, and the size and modification time of each file read in full, in `scada_uploader.sqlite`.

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
| `validate_ranges` | `true` (default) holds rows outside the section 5 bounds. `false` sends them as measured and lets the portal decide. See Validation. |
| `batch_size` | Local cap used only in batch mode. The guide does not set a limit. |
| `file_stable_seconds` | How long a file must sit unchanged before it is read. |
| `database_path`, `log_path` | SQLite queue and log, next to the app by default. |
| `ca_bundle_file` | Optional PEM file of extra trusted CA certificates, used together with the Windows store. Empty by default. See HTTPS certificates. |

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

`"validate_ranges": false` turns off only the bounds above. Each run then logs `Range checks are OFF`, and rows held only for a bound go back to `pending` on the next run, without deleting the database. A row is still held when a column is missing, a cell is empty or not a number, or Date/Time cannot be read, because it cannot be sent as a JSON number. Rows the portal rejected with HTTP 400 stay `needs_attention`, as do `api_accepted` and `uncertain` rows. Section 6 says the portal returns HTTP 400 for out-of-range values, so with the checks off those rows are posted once and then become `needs_attention`. Setting it back to `true` holds pending out-of-range rows again before they are sent.

## Signature

Section 4 and the section 8 Python sample:

- `Content-Type: application/json`
- `X-SCADA-Device-Id`
- `X-SCADA-Timestamp`: decimal Unix seconds, as a string
- `X-SCADA-Signature`: lowercase hex HMAC-SHA256

The HMAC key is the shared secret. The message is `X-SCADA-Timestamp + "." + body`, UTF-8. `body` is the exact compact JSON that is posted (`json.dumps` separators `(',', ':')` in the guide's sample: no spaces or newlines). The signature is computed over those bytes, not over a second pretty-printed copy.

Header names are sent with that exact spelling. Python 3.9's `urllib.request` would rewrite them with `str.title()` (`X-Scada-Device-Id`), so this program uses `http.client` from the standard library instead. Redirects are not followed.

## HTTPS certificates

The certificate is always verified. Python trusts only the root certificates already in the Windows store. Browsers and Windows can download a missing root on demand, so a site can open in a browser while this program fails with `CERTIFICATE_VERIFY_FAILED ... unable to get local issuer certificate`. Those rows stay `pending`.

The portal's chain ends at `GlobalSign Root R46`. Windows downloads that root only when something first needs it, so a new plant PC may not have it. This folder includes it as `portal_ca.pem`. Copy that file next to `config.json` and set `"ca_bundle_file": "portal_ca.pem"`. The path is resolved from the config folder. The program trusts the file in addition to the Windows store, not instead of it, and does not change the Windows store. The program refuses to start if the file is missing or is not a PEM certificate. Check the SHA-256 fingerprint of a downloaded root before trusting it. For GlobalSign Root R46 it is `4F:A3:12:6D:8D:3A:11:D1:C4:85:5A:4F:80:7C:BA:D6:CF:91:9D:3A:5A:88:B0:3B:EA:2C:63:72:D9:3C:40:C9`.

If antivirus or a proxy inspects HTTPS, it presents its own root instead. Export that root from the Windows certificate store as Base-64 (PEM) and use it as `ca_bundle_file`.

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
- `portal_ca.pem`, the public `GlobalSign Root R46` certificate for the portal's HTTPS chain. It is not a secret. See [HTTPS certificates](#https-certificates).

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

- `ca_bundle_file` to `portal_ca.pem` if a run reports `CERTIFICATE_VERIFY_FAILED`

Use a local disk path or a UNC path for `watch_folder`. A mapped drive letter often disappears when nobody is logged on. Do not put the Windows account password in `config.json`.

`config.json` must be valid JSON. In Windows paths, write each backslash twice or use forward slashes: `"D:\\Plant\\SCADA\\Export"` or `"D:/Plant/SCADA/Export"`. A single backslash breaks the file. Do not put a comma after the last setting. A broken `config.json` makes the exe exit at once with code 1. Nothing is written to the log, because logging starts only after the config is read. See [Run it by hand](#run-it-by-hand) for how to see the message.

### Run it by hand

Always change to the install folder first. Running from another folder uses a different `config.json`, database, and log.

```bat
cd /d C:\SCADA\uploader
scada-uploader.exe --config config.json
scada-uploader.exe --config config.json --send
```

The first command is a dry run and does not POST. The second sends pending rows. The exe has no window, and Command Prompt does not wait for a windowless program, so `%ERRORLEVEL%` right after it is not the program's result. To wait, and to capture the summary or a config error:

```bat
cmd /c "scada-uploader.exe --config config.json > out.txt 2>&1"
echo %ERRORLEVEL%
type out.txt
```

`0` is a successful dry run, or a send run where at least one row was accepted and none were uncertain. `1` is a config error, a file error, an uncertain row, or a send run in which nothing was accepted. `2` is a refused option: `--send` with `--dry-run`, or `--send` with the placeholder URL. The full rules are under [Dry run and a real send](#dry-run-and-a-real-send).

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

To start a run now instead of waiting for the next 15-minute slot:

```bat
schtasks /Run /TN "SCADA Portal Upload"
```

### How to see status

Command Prompt does not know PowerShell commands such as `Get-ScheduledTask`. Use `schtasks`:

```bat
schtasks /Query /TN "SCADA Portal Upload" /V /FO LIST
```

Read `Status`, `Last Run Time`, `Next Run Time`, and `Last Result`. `Last Result` is the exit code of the last run: `0`, `1`, or `2` as described above. `267009` means it is running now, `267011` means it has never run, and `267014` means it was stopped, for example at the 10-minute limit. In PowerShell, or from Command Prompt:

```bat
powershell -Command "Get-ScheduledTaskInfo -TaskName 'SCADA Portal Upload'"
```

The last lines of the log, from the install folder:

```bat
powershell -Command "Get-Content scada_uploader.log -Tail 20"
```

Each run ends with a summary. `Previous run:` gives the start, finish, mode, and exit code of the run before it. `Database:` gives the row count for each status. `Requests:` (send runs only) gives the number of POSTs, rows accepted with HTTP 202 in this run, and rows marked uncertain.

Next to `config.json`:

- `scada_uploader.log` records skipped files, validation holds, and each HTTP result.
- `scada_uploader.sqlite` is the queue. Status values are `pending`, `api_accepted`, `needs_attention`, and `uncertain`. `api_accepted` means the API queued the reading (HTTP 202), not that government processing finished.
- The `runs` table in `scada_uploader.sqlite` has one row per run: start and finish time, `dry_run` or `send`, file and row counts, and the exit code. A run that crashed has no `finished_at`.
- Each run's log summary includes a `Previous run:` line with the last run's start, finish, mode, and exit code, or says it did not finish.

If the newest log summary, or the newest `runs` row, is more than about 15 minutes old, the scheduled task has stopped running. Check Task Scheduler.

### Remove the task

```bat
powershell -ExecutionPolicy Bypass -File .\register_scheduled_task.ps1 -Unregister
```

### Optional dry-run test task

To watch the schedule before anything is sent, create a separate task without `--send`. It uses the same `config.json`, database, and log, and it never POSTs.

```bat
schtasks /Create /TN "SCADA Uploader Dry Run" /SC MINUTE /MO 15 /TR "\"C:\SCADA\uploader\scada-uploader.exe\" --config \"C:\SCADA\uploader\config.json\""
schtasks /Delete /TN "SCADA Uploader Dry Run" /F
```

### Troubleshooting

- **Task `Last Result` is 1 and the log has no new summary.** `config.json` is probably not valid JSON: a single backslash in a path, or a comma after the last setting. Run the `cmd /c` command under [Run it by hand](#run-it-by-hand) and read `out.txt`, or paste the file into a JSON validator.
- **`CERTIFICATE_VERIFY_FAILED ... unable to get local issuer certificate`.** Copy `portal_ca.pem` next to `config.json` and set `"ca_bundle_file": "portal_ca.pem"`. See [HTTPS certificates](#https-certificates).
- **HTTP 401.** Check `device_id`, `shared_secret`, and the PC clock. The timestamp must be within 5 minutes of the server, so keep NTP on. Rows stay `pending` and are retried on the next run.
- **HTTP 400.** The portal rejected the values, usually out of range. The row becomes `needs_attention` and is not sent again unless a corrected export replaces it.
- **Rows stay `pending` with a connection error** (no network, DNS, TLS). Nothing was sent. The next run retries them.

## Still needs confirmation from the portal

- Push interval. It is not in the guide.
- Whether production accepts section 5 option B (a JSON array). The opening sentence of section 5 and the section 8 sample are a single object, which is this program's default. Batch mode is implemented.
- Maximum array length, if batch mode is used. `batch_size` (default 20) is a local cap, not a figure from the guide.
- Duplicate and backfill behavior. The guide does not say what happens if the same `readingAt` is submitted twice (retry, crash after the server queued the body but before SQLite was updated, or a corrected backfill).
- No API is documented for checking that a `batchId` from HTTP 202 was eventually stored. `api_accepted` means queued only.
- Section 5 calls `readingAt` a "UTC Timestamp", but the example is an offset timestamp (`2026-07-03T11:00:00+05:30`). The section 7 Postman script replaces `readingAt` with `new Date().toISOString()` (current UTC, with a `Z`). This program follows the section 5 example and keeps the SCADA measurement time. It does not copy the Postman script.
- Bounds are applied as inclusive. The guide says "95.0 to 100.0" and "Max: N", and the 400 example only shows methane below 95.
- What the first live send showed (24-Oct-2026, `validate_ranges` false): the portal returned HTTP 400 for every row outside the section 5 bounds, as section 6 says, and those rows are now `needs_attention`. It returned HTTP 202 for rows with negative moisture (about −42 to −49 mg/m³), so it appears to have no lower bound on moisture. Whether negative moisture is acceptable data still needs confirmation.
- The table text for `slurry_FlowTotalizer_scm` says "Gas volume", which looks copied from the gas totalizers. The field name used here is the one in the JSON example.
- Engineering units on the SCADA tags versus the units named in the guide.
- HTTP status used when the timestamp is outside ±5 minutes. Section 4 describes the window. The 401 example in section 6 is "Invalid SCADA request signature", which may or may not be the same rejection.
