# OBS Recording Tools

This repository contains the two runtime components used together on every recording computer:

1. `runtime/frame-log/bin/frame-log.dll` logs one timestamped CSV row per rendered OBS frame.
2. `runtime/recording_formatting.py` gives the finished MP4 and CSV their camera, week, day, recording, and start-time names and moves them into the final folders.

The C++ plugin owns frame capture and timestamps. The Python script owns final naming and file organization. Do not load a separate Python frame logger; it would duplicate the plugin's work and create competing CSV files.

## Repository Layout

```text
runtime/
  recording_formatting.py       OBS Python formatter/organizer
  frame-log/
    src/frame-log.cpp           Native OBS frame logger source
    bin/frame-log.dll           Ready-to-install Windows x64 plugin
    CMakeLists.txt               Plugin build entry point
    CMakePresets.json            Windows x64 build preset
    buildspec.json               Plugin name, version, and OBS dependencies
    cmake/                       OBS plugin build support
tools/
  log-repair/                    Offline tools for already-recorded CSV files
```

Only the two files under `runtime` are needed for normal OBS recording. The tools under `tools/log-repair` are for historical data repair and are never loaded into OBS.

## Install On Windows

### 1. Install The Native Plugin

Completely close every OBS instance and confirm no `obs64.exe` remains in Task Manager.

Create this folder if needed:

```text
C:\ProgramData\obs-studio\plugins\frame-log\bin\64bit
```

Copy:

```text
runtime\frame-log\bin\frame-log.dll
```

to:

```text
C:\ProgramData\obs-studio\plugins\frame-log\bin\64bit\frame-log.dll
```

`C:\ProgramData` is hidden by default on many Windows and managed UCSD computers. Type the path directly into File Explorer or enable **View > Show > Hidden items**.

Remove old copies of `frame-logger-for-obs.dll` or `frame-log.dll` from other OBS plugin locations. OBS can scan several locations, including:

```text
C:\Program Files\obs-studio\obs-plugins\64bit
C:\ProgramData\obs-studio\plugins
C:\Users\<user>\AppData\Roaming\obs-studio\plugins
C:\Users\<user>\AppData\Local\obs-studio\plugins
```

There should be one active copy of the plugin per computer.

### 2. Load The Python Formatter

Open OBS and go to:

```text
Tools > Scripts > +
```

Select:

```text
runtime\recording_formatting.py
```

If OBS still lists `Python Scripts\recording_formatting.py`, remove that old entry and add the new `runtime` path. OBS remembers the exact script path.

Configure the script's recording count, recording number, camera number, week, and day. Week and day labels are always padded to two digits, such as `W09D03`.

## How The Two Components Work Together

When recording starts:

1. OBS begins an MP4 whose temporary filename contains that OBS process ID.
2. `frame-log.dll` creates `frame-log-<PID>.tmp.csv` beside the MP4.
3. The plugin captures precise Windows system time once with `GetSystemTimePreciseAsFileTime()`.
4. Each frame advances that absolute-time anchor using OBS's monotonic `os_gettime_ns()` clock.
5. CSV writing happens on a background writer thread so disk work does not block frame capture.

When recording stops:

1. `recording_formatting.py` waits for the CSV belonging to its own OBS process to finish writing.
2. It reads the first `ISO_timestamp` from that CSV as the recording start time.
3. It rounds any fractional timestamp to exactly three millisecond digits.
4. It creates the final week/day/video/log folders.
5. It moves and renames the MP4 and CSV together.

The OBS process ID keeps six simultaneous OBS instances from sharing temporary files. Final-name collisions add `_OBS<PID>` and then a numeric suffix instead of overwriting existing recordings.

## Time Model

The GPS/PTP software must discipline the Windows system clock to the correct current date and time. Both runtime components use Windows system time directly:

```text
absolute timestamp = GPS/PTP-disciplined Windows system time
```

No SEEMA value or fixed epoch offset is added. Historical SEEMA constants exist only inside offline repair tools for old recordings.

The plugin records the absolute anchor once and uses monotonic elapsed time for every frame. This prevents a mid-recording system-clock step from making frame timestamps jump backward while retaining the precision of the GPS/PTP-disciplined start time.

Windows converts the real system date into local California time. Its installed timezone rules choose `PDT` during daylight saving time and `PST` otherwise. Internet access is not required, but Windows must be configured for the correct Pacific timezone and automatic daylight-saving adjustment.

## CSV And Output Names

The plugin writes:

```csv
frame,timestamp_ms,ISO_timestamp
1,2.486,2026-07-10T09_12_34_568-PDT
2,19.153,2026-07-10T09_12_34_584-PDT
```

`timestamp_ms` is elapsed time from recording start. `ISO_timestamp` is local absolute system time rounded to exactly three millisecond digits.

Expected final layout:

```text
<OBS output folder>/
  W09/
    D03/
      VIDEO_W09D03_2026_07_10/
        C4_W09D03_REC7-13_2026-07-10T09_12_34_568-PDT.mp4
        VIDEO_LOG_W09D03_2026_07_10/
          L4_W09D03_REC7-13_2026-07-10T09_12_34_568-PDT.csv
```

## Verify The Installation

In OBS, open:

```text
Help > Log Files > View Current Log
```

Search for:

```text
[FrameLog] Loaded.
[recording_formatting] Loaded version
```

During a recording, the output folder should contain one `frame-log-<PID>.tmp.csv` per recording OBS process. Duplicate timestamp-named temporary CSV files indicate that an old plugin or Python frame logger is still loaded.

## Build The Plugin

Requirements:

- Windows x64
- Visual Studio 2022 Build Tools with the C++ workload
- CMake 3.28 or newer
- Windows SDK 10.0.22621 or compatible

From the repository root:

```powershell
cd .\runtime\frame-log
cmake --preset windows-x64
cmake --build --preset windows-x64
```

The development DLL is produced at:

```text
runtime\frame-log\build_x64\RelWithDebInfo\frame-log.dll
```

The checked-in install-ready build is:

```text
runtime\frame-log\bin\frame-log.dll
```

## Offline Log Repair Tools

Run these only on copies or folders of historical logs. They are independent CLI programs and are not part of the live OBS workflow.

- `fix_logs_from_filename_anchor_batch.py`: anchors every CSV to the correct start time in its filename.
- `round_log_timestamps_to_milliseconds.py`: rounds six-digit fractions to exactly three millisecond digits.
- `shift_logs_back_one_hour.py`: moves filenames and CSV timestamps back one hour.
- `shift_logs_forward_one_hour.py`: moves filenames and CSV timestamps forward one hour.
- `fix_2025_logs_to_2026_batch.py`: repairs the known old-SEEMA/new-SEEMA batch error.
- `subtract_old_seema_from_logs.py`: removes an old SEEMA offset that was added to an already-correct clock.
- `correct_seema_logs.py`: applies an explicit known fake-time to real-time anchor.
- `recalibrate_logs_from_filename.py`: recalibrates a selected log from its filename start time.

Each repair script documents its own output folders and manifest behavior in its command help or source header.
