# OBS Automation Scripts

This repo contains the OBS Python script half of the HECD classroom recording workflow. It works with the native Frame Logger OBS plugin from `frame-logger-for-obs`.

Both pieces are required:

- `frame-logger-for-obs.dll` creates the precise per-frame CSV.
- `Python Scripts/recording_formatting.py` renames/moves the MP4 and matching CSV after recording stops.

Do not also load old Python frame-logging scripts. The C++ plugin is the frame logger now; the Python OBS script is the organizer/formatter.

## Active OBS Script

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

Week numbers are output with at least two digits:

```text
W08
W13
```

## How It Works With Frame Logger

The native plugin writes a temporary CSV next to the active OBS recording:

```text
frame-logger-<OBS_PROCESS_ID>-real.tmp.csv
```

The Python script looks for the temp CSV belonging to its own OBS process, reads the first `ISO_timestamp`, and uses that timestamp for the final MP4 and CSV names. This prevents six simultaneous OBS instances from fighting over the same temp file.

Expected final output:

```text
<OBS output folder>/
  W08/
    D3/
      Video_W08D3_YYYY_MM_DD/
        C1_W08D3_REC1-6_YYYY-MM-DDTHH_MM_SS_mmm-PST.mp4
        video_log/
          L1_W08D3_REC1-6_YYYY-MM-DDTHH_MM_SS_mmm-PST.csv
```

The script also sets OBS's live recording filename format to include the current OBS process ID:

```text
obs-<pid>-YYYY-MM-DD HH-MM-SS.mp4
```

That reduces raw MP4 filename collisions before final organization happens.

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

If `ProgramData` is not visible on a managed UCSD computer, it may simply be hidden or restricted by policy. The path can still exist and still be the correct install location.

## Repair Tools

The folder also includes repair CLIs for already-recorded logs:

```text
Python Scripts\recalibrate_logs_from_filename.py
Python Scripts\subtract_old_seema_from_logs.py
Python Scripts\correct_seema_logs.py
```

Use `recalibrate_logs_from_filename.py` when the CSV filename contains the best available start timestamp and the CSV contents need to be rebuilt from `timestamp_ms`, `frame_ms`, or `relative_ms`.

Use `subtract_old_seema_from_logs.py` when logs were recorded on computers already set to real-world time but the old SEEMA offset was added again. It subtracts:

```text
1752160362
```

from absolute timestamp columns and leaves relative frame timing untouched.

Use `correct_seema_logs.py` for explicit anchor-based repairs when a known fake timestamp maps to a known real timestamp.

All repair tools write corrected copies by default when used with `--out`; avoid `--in-place` unless you intentionally want backups plus replacement.

## Current SEEMA Values

Current SEEMA time:

```text
1783354049
```

Old SEEMA time, for repair only:

```text
1752160362
```
