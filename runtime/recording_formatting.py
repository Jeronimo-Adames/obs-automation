import csv
import ctypes
import os
import shutil
import time
from ctypes import wintypes
from datetime import datetime, timedelta
from pathlib import Path
import re

import obspython as obs

vid_total = 1
vid_count = 1
camera_id = 1
week = 0
day = 0

NS_PER_SECOND = 1_000_000_000
WINDOWS_TICK_NS = 100
WINDOWS_TICKS_TO_UNIX_EPOCH = 11644473600 * 10_000_000

FRAME_LOG_TEMP_PREFIX = "frame-log-"
FRAME_LOG_TEMP_SUFFIX = ".tmp.csv"
LEGACY_FRAME_LOG_TEMP_PREFIX = "frame-logger-"
LEGACY_FRAME_LOG_TEMP_SUFFIX = "-real.tmp.csv"
OBS_FILENAME_FORMAT_SECTION = "Output"
OBS_FILENAME_FORMAT_KEY = "FilenameFormatting"
SCRIPT_VERSION = "2026-07-21-system-time"

ISO_STAMP_RE = re.compile(
    r"(?<!\d)"
    r"(?P<year>\d{4})[-_](?P<month>\d{1,2})[-_](?P<day>\d{1,2})"
    r"T"
    r"(?P<hour>\d{1,2})[_:](?P<minute>\d{2})[_:](?P<second>\d{2})"
    r"(?:(?P<frac_sep>[_.])(?P<fraction>\d{1,6}))?"
    r"(?P<suffix>Z|-PST|-PDT|PST|PDT|-07_00|-08_00|[+-]\d{2}:?\d{2})?"
    r"(?!\d)"
)


def script_properties():
    p = obs.obs_properties_create()
    obs.obs_properties_add_int(p, "vid_total", "total video recordings", 1, 100, 1)
    obs.obs_properties_add_int(p, "vid_count", "video recording number", 1, 100, 1)
    obs.obs_properties_add_int(p, "camera_id", "Camera number", 1, 7, 1)
    obs.obs_properties_add_int(p, "week", "Week number", 0, 9999, 1)
    obs.obs_properties_add_int(p, "day", "Day number", 1, 5, 1)
    return p


def script_defaults(s):
    obs.obs_data_set_default_int(s, "vid_total", 1)
    obs.obs_data_set_default_int(s, "vid_count", 1)
    obs.obs_data_set_default_int(s, "camera_id", 1)
    obs.obs_data_set_default_int(s, "week", 0)
    obs.obs_data_set_default_int(s, "day", 1)


def script_update(s):
    global vid_total, vid_count, camera_id, week, day
    vid_total = obs.obs_data_get_int(s, "vid_total")
    vid_count = obs.obs_data_get_int(s, "vid_count")
    camera_id = obs.obs_data_get_int(s, "camera_id")
    week = obs.obs_data_get_int(s, "week")
    day = obs.obs_data_get_int(s, "day")
    set_obs_recording_filename_format()


def log_obs(level, message):
    try:
        obs.script_log(level, f"[recording_formatting] {message}")
    except Exception:
        pass


def obs_process_recording_format():
    return f"obs-{os.getpid()}-%CCYY-%MM-%DD %hh-%mm-%ss"


def set_obs_recording_filename_format():
    try:
        config = obs.obs_frontend_get_profile_config()
    except Exception as e:
        log_obs(obs.LOG_WARNING, f"Could not access OBS profile config: {e}")
        return

    if not config:
        log_obs(obs.LOG_WARNING, "OBS profile config unavailable; cannot set process-specific recording filename.")
        return

    desired = obs_process_recording_format()
    try:
        current = obs.config_get_string(config, OBS_FILENAME_FORMAT_SECTION, OBS_FILENAME_FORMAT_KEY) or ""
    except Exception:
        current = ""

    if current == desired:
        return

    try:
        obs.config_set_string(config, OBS_FILENAME_FORMAT_SECTION, OBS_FILENAME_FORMAT_KEY, desired)
        log_obs(obs.LOG_INFO, f"Set live OBS recording filename format to: {desired}")
    except Exception as e:
        log_obs(obs.LOG_WARNING, f"Could not set OBS recording filename format: {e}")


def current_system_ns():
    try:
        ft = wintypes.FILETIME()
        ctypes.windll.kernel32.GetSystemTimePreciseAsFileTime(ctypes.byref(ft))
        ticks = (ft.dwHighDateTime << 32) | ft.dwLowDateTime
        return (ticks - WINDOWS_TICKS_TO_UNIX_EPOCH) * WINDOWS_TICK_NS
    except Exception:
        return time.time_ns()


def iso_stamp_from_system_ns(system_ns):
    rounded_ns = ((system_ns + 500_000) // 1_000_000) * 1_000_000
    sec, ns = divmod(rounded_ns, NS_PER_SECOND)
    local = time.localtime(sec)
    ms = ns // 1_000_000
    suffix = "-PDT" if local.tm_isdst > 0 else "-PST"
    iso = f"{time.strftime('%Y-%m-%dT%H_%M_%S', local)}_{ms:03d}{suffix}"
    date_str = time.strftime("%Y_%m_%d", local)
    return iso, date_str


def fallback_iso_stamp():
    return iso_stamp_from_system_ns(current_system_ns())


def round_to_millisecond(dt):
    ms = (dt.microsecond + 500) // 1000
    if ms >= 1000:
        dt += timedelta(seconds=1)
        ms = 0
    return dt.replace(microsecond=ms * 1000)


def normalize_iso_stamp(iso):
    value = (iso or "").strip()
    match = ISO_STAMP_RE.search(value)
    if not match:
        return value

    fraction = match.group("fraction") or "0"
    microsecond = int(fraction.ljust(6, "0")[:6])
    dt = datetime(
        int(match.group("year")),
        int(match.group("month")),
        int(match.group("day")),
        int(match.group("hour")),
        int(match.group("minute")),
        int(match.group("second")),
        microsecond,
    )
    dt = round_to_millisecond(dt)
    suffix = match.group("suffix") or ""
    normalized = f"{dt.strftime('%Y-%m-%dT%H_%M_%S')}_{dt.microsecond // 1000:03d}{suffix}"
    return value[: match.start()] + normalized + value[match.end() :]


def date_str_from_iso(iso):
    try:
        return normalize_iso_stamp(iso).split("T", 1)[0].replace("-", "_")
    except Exception:
        return fallback_iso_stamp()[1]


def week_label():
    return f"{week:02d}"


def day_label():
    return f"{day:02d}"


def frame_log_temp_name_for_this_obs():
    return f"{FRAME_LOG_TEMP_PREFIX}{os.getpid()}{FRAME_LOG_TEMP_SUFFIX}"


def frame_log_candidates(base_dir, original_video_base):
    return [
        base_dir / frame_log_temp_name_for_this_obs(),
        base_dir / f"{LEGACY_FRAME_LOG_TEMP_PREFIX}{os.getpid()}{LEGACY_FRAME_LOG_TEMP_SUFFIX}",
    ]


def wait_for_any_stable_file(paths, timeout=30, settle=1.0):
    start = time.time()
    last_sizes = {}
    stable_since = {}

    while time.time() - start < timeout:
        for path in paths:
            if not path.exists():
                continue

            try:
                size = path.stat().st_size
            except OSError:
                continue

            if last_sizes.get(path) == size:
                if stable_since.get(path) is None:
                    stable_since[path] = time.time()
                elif time.time() - stable_since[path] >= settle:
                    return path
            else:
                last_sizes[path] = size
                stable_since[path] = None

        time.sleep(0.25)

    for path in paths:
        if path.exists():
            return path
    return None


def read_frame_log_iso(frame_log):
    if not frame_log or not frame_log.exists():
        return None

    try:
        with frame_log.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                for key in ("ISO_timestamp", "ISO_stamp", "ISO_2025"):
                    value = (row.get(key) or "").strip()
                    if value:
                        return normalize_iso_stamp(value)
    except Exception as e:
        log_obs(obs.LOG_WARNING, f"Could not read timestamp from {frame_log}: {e}")
    return None


def move_with_retry(src, dst, timeout=30):
    start = time.time()
    last_error = None
    while time.time() - start < timeout:
        try:
            shutil.move(str(src), str(dst))
            return True
        except Exception as e:
            last_error = e
            time.sleep(0.25)

    try:
        shutil.copy2(str(src), str(dst))
        src.unlink()
        return True
    except Exception as e:
        log_obs(obs.LOG_WARNING, f"Could not move {src} to {dst}: {e}; last move error: {last_error}")
        return False


def collision_safe_path(path):
    if not path.exists():
        return path

    pid_path = path.with_name(f"{path.stem}_OBS{os.getpid()}{path.suffix}")
    if not pid_path.exists():
        return pid_path

    for i in range(2, 1000):
        candidate = path.with_name(f"{path.stem}_OBS{os.getpid()}_{i}{path.suffix}")
        if not candidate.exists():
            return candidate

    raise RuntimeError(f"Could not find non-colliding destination for {path}")


def on_recording_stop(event):
    if event != obs.OBS_FRONTEND_EVENT_RECORDING_STOPPED:
        return

    recording = obs.obs_frontend_get_last_recording()
    if not recording:
        return

    mp4 = Path(recording)
    base_dir = mp4.parent
    original_video_base = mp4.stem

    candidates = frame_log_candidates(base_dir, original_video_base)
    log_obs(obs.LOG_INFO, f"Looking for this OBS process frame log: {candidates[0]}")
    frame_log = wait_for_any_stable_file(candidates)
    if frame_log:
        log_obs(obs.LOG_INFO, f"Using frame log: {frame_log}")

    iso = read_frame_log_iso(frame_log)
    if iso:
        date_str = date_str_from_iso(iso)
    else:
        iso, date_str = fallback_iso_stamp()
        log_obs(obs.LOG_WARNING, "Frame log timestamp unavailable; using current system time for filename.")

    week_id = week_label()
    day_id = day_label()

    target = base_dir / f"W{week_id}" / f"D{day_id}" / f"VIDEO_W{week_id}D{day_id}_{date_str}"
    target.mkdir(parents=True, exist_ok=True)
    logs = target / f"VIDEO_LOG_W{week_id}D{day_id}_{date_str}"
    logs.mkdir(parents=True, exist_ok=True)

    new_mp4 = collision_safe_path(target / f"C{camera_id}_W{week_id}D{day_id}_REC{vid_count}-{vid_total}_{iso}.mp4")
    move_with_retry(mp4, new_mp4)

    if frame_log and frame_log.exists():
        new_csv = collision_safe_path(logs / f"L{camera_id}_W{week_id}D{day_id}_REC{vid_count}-{vid_total}_{iso}.csv")
        move_with_retry(frame_log, new_csv)
    else:
        log_obs(obs.LOG_WARNING, "No frame log CSV found to move with the recording.")


def script_load(s):
    log_obs(obs.LOG_INFO, f"Loaded version {SCRIPT_VERSION} from {__file__}; pid={os.getpid()}")
    set_obs_recording_filename_format()
    obs.obs_frontend_add_event_callback(on_recording_stop)


def script_unload():
    pass
