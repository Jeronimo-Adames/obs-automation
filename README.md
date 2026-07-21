# OBS Automation Scripts

This repo contains the OBS Python script half of the classroom recording workflow. It works with the native Frame Logger OBS plugin from `frame-logger-for-obs`.

Both pieces are required:

- `frame-logger-for-obs.dll` creates the precise per-frame CSV.
- `Python Scripts/recording_formatting.py` renames and moves the MP4 and matching CSV after recording stops.

Do not also load old Python frame-logging scripts. The C++ plugin is the frame logger now; this Python OBS script is the organizer/formatter.

## Active OBS Runtime Script

Load this file in OBS:

```text
Python Scripts\recording_formatting.py
```

In OBS:

```text
Tools > Scripts > + > recording_formatting.py
```

The script exposes fields for:

```text
total video recordings
video recording number
Camera number
Week number
Day number
```

Week and day values are output with two digits:

```text
W09
D03
```

## How It Works With Frame Logger

The native plugin writes a temporary CSV next to the active OBS recording:

```text
frame-logger-<OBS_PROCESS_ID>-real.tmp.csv
```

The Python script looks only for the temp CSV belonging to its own OBS process. This lets six simultaneous OBS instances record into the same output folder without fighting over the same temp log.

The script reads the first `ISO_timestamp` from that CSV, normalizes it to exactly three millisecond digits, then uses that timestamp for the final MP4 and CSV names.

## Time And DST Behavior

The C++ plugin is the source of truth for frame timestamps. It calculates:

```text
PTP-disciplined Windows time + SEEMA_TIME
```

Current SEEMA time:

```text
1783354049
```

The Python script has a fallback timestamp path for cases where the C++ log is missing. That fallback also adds `SEEMA_TIME` first, then uses local Windows timezone rules on the real SEEMA-adjusted date. It does not decide `PDT` or `PST` from the fake 1970-ish system date.

In summer California time, expected suffix is:

```text
-PDT
```

In winter:

```text
-PST
```

## Final Output Layout

The script sets OBS's live recording filename format to include the current OBS process ID:

```text
obs-<pid>-YYYY-MM-DD HH-MM-SS.mp4
```

That raw MP4 name is only temporary. After recording stops, the script moves the MP4 and matching CSV into:

```text
<OBS output folder>/
  W09/
    D03/
      VIDEO_W09D03_YYYY_MM_DD/
        C4_W09D03_REC7-13_YYYY-MM-DDTHH_MM_SS_mmm-PDT.mp4
        VIDEO_LOG_W09D03_YYYY_MM_DD/
          L4_W09D03_REC7-13_YYYY-MM-DDTHH_MM_SS_mmm-PDT.csv
```

The CSV still contains:

```csv
frame,timestamp_ms,ISO_timestamp
```

`timestamp_ms` is relative frame time from recording start. `ISO_timestamp` is absolute SEEMA-adjusted time, rounded to exactly three millisecond digits.

If a final MP4 or CSV path already exists, the script appends the OBS process ID and then a numeric suffix if needed:

```text
_OBS15816
_OBS15816_2
```

## Finding The Script OBS Is Actually Using

OBS aggressively remembers script paths. If OBS keeps loading an old script:

```text
Tools > Scripts
```

Select the script and check the exact file path shown there. Remove old copies, add the intended file again, and restart OBS.

Also check:

```text
Help > Log Files > View Current Log
```

Search for:

```text
[recording_formatting]
Loaded version
```

The script logs its version and path on load.

## Plugin Install Location

The companion plugin belongs in ProgramData as a third-party OBS plugin package:

```text
C:\ProgramData\obs-studio\plugins\frame-logger-for-obs\bin\64bit\frame-logger-for-obs.dll
```

`C:\ProgramData` is often hidden. Type this directly into File Explorer:

```text
C:\ProgramData\obs-studio\plugins
```

If `ProgramData` is not visible on a managed computer, it may simply be hidden or restricted by policy. The path can still exist and still be the correct install location.

## Repair Tools

The folder includes one-off repair CLIs for already-recorded logs. These are not OBS runtime scripts.

### `fix_logs_from_filename_anchor_batch.py`

Use when the log filename contains the correct recording start timestamp and the CSV contents need to be rebuilt from `timestamp_ms`.

Drop the script into the folder with bad logs and run:

```powershell
python .\fix_logs_from_filename_anchor_batch.py
```

It moves originals into `wrong_logs`, writes corrected logs back into the same folder, and writes `filename_anchor_fix_manifest.csv`.

### `round_log_timestamps_to_milliseconds.py`

Use when existing logs contain six fractional timestamp digits, for example:

```text
2026-07-10T09_12_34_567532-PDT
```

Run:

```powershell
python .\round_log_timestamps_to_milliseconds.py
```

It rounds to exactly three millisecond digits:

```text
_567532 -> _568
_567499 -> _567
_999500 -> next second _000
```

It also moves originals into `wrong_logs` and writes a manifest beside the script.

### `shift_logs_back_one_hour.py` and `shift_logs_forward_one_hour.py`

Use when the entire log filename and all CSV timestamps need a simple one-hour correction.

```powershell
python .\shift_logs_back_one_hour.py
python .\shift_logs_forward_one_hour.py
```

Both scripts archive originals into `wrong_logs`, write corrected logs back into the same folder, and generate a manifest.

### `fix_2025_logs_to_2026_batch.py`

Use for the old-SEEMA-to-new-SEEMA batch correction. It hardcodes:

```text
old SEEMA = 1752160362
new SEEMA = 1783354049
```

It writes corrected copies into a `fixed_seema_logs` folder.

### `subtract_old_seema_from_logs.py`

Use when logs were recorded on computers already set to real-world time but the old SEEMA offset was added again. It subtracts:

```text
1752160362
```

from absolute timestamp columns and leaves relative frame timing untouched.

### `correct_seema_logs.py`

Use for explicit anchor-based repairs when a known fake timestamp maps to a known real timestamp.

## USB Package Layout

The current USB package is organized under:

```text
D:\OBS_Tools
```

Useful folders:

```text
D:\OBS_Tools\01_frame_logger_plugin_programdata_install
D:\OBS_Tools\02_recording_formatter_obs_python
D:\OBS_Tools\03_log_repair_tools
D:\OBS_Tools\04_source_reference_current_code
```

The active OBS Python script on the USB is:

```text
D:\OBS_Tools\02_recording_formatter_obs_python\recording_formatting.py
```
