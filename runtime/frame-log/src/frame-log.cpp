// plugin-main.cpp

#include <obs-module.h>
#include <obs-frontend-api.h>
#include <util/platform.h>

#include <filesystem>
#include <fstream>
#include <chrono>
#include <string>
#include <sstream>
#include <iomanip>
#include <cstdint>
#include <ctime>
#include <system_error>
#include <atomic>
#include <condition_variable>
#include <deque>
#include <mutex>
#include <thread>

#if defined(_WIN32)
#include <windows.h>
#endif

namespace fs = std::filesystem;

OBS_DECLARE_MODULE();

const char *obs_module_name()
{
	return "Frame Log";
}

const char *obs_module_description()
{
	return "Per-frame CSV logger using the GPS/PTP-disciplined Windows system clock.";
}

struct LogRow {
	uint64_t frame_index;
	uint64_t timestamp_us;
	int64_t system_ns;
};

static std::ofstream g_csv;
static std::atomic_bool g_recording = false;
static std::atomic_bool g_writer_should_stop = false;
static std::thread g_writer_thread;
static std::mutex g_queue_mutex;
static std::condition_variable g_queue_cv;
static std::deque<LogRow> g_log_queue;
static uint64_t g_frame_index = 0;
static uint64_t g_t0_ns = 0;
static int64_t g_t0_system_ns = 0;

// Process-specific temp CSV in the OBS recording folder. Multiple OBS instances may record
// into the same folder, so the PID prevents temp log collisions. The Python
// recording_formatting script owns final naming and moves this file with the finished MP4.
static fs::path g_csv_path;

static int64_t current_system_ns()
{
#if defined(_WIN32)
	static constexpr uint64_t WINDOWS_TICK = 100ULL;
	static constexpr uint64_t SEC_TO_UNIX_EPOCH = 11644473600ULL;
	static constexpr uint64_t WINDOWS_TICKS_TO_UNIX_EPOCH = SEC_TO_UNIX_EPOCH * 10000000ULL;

	// The GPS/PTP software disciplines this clock, making it the absolute time source.
	FILETIME ft{};
	GetSystemTimePreciseAsFileTime(&ft);

	ULARGE_INTEGER ticks{};
	ticks.LowPart = ft.dwLowDateTime;
	ticks.HighPart = ft.dwHighDateTime;

	return static_cast<int64_t>((ticks.QuadPart - WINDOWS_TICKS_TO_UNIX_EPOCH) * WINDOWS_TICK);
#else
	using namespace std::chrono;
	return duration_cast<nanoseconds>(system_clock::now().time_since_epoch()).count();
#endif
}

static std::string local_stamp(int64_t system_ns)
{
	static constexpr int64_t NS_PER_SECOND = 1000000000LL;
	static constexpr int64_t NS_PER_MILLISECOND = 1000000LL;

	const int64_t rounded_ns =
		((system_ns + (NS_PER_MILLISECOND / 2LL)) / NS_PER_MILLISECOND) * NS_PER_MILLISECOND;
	const time_t sec = static_cast<time_t>(rounded_ns / NS_PER_SECOND);
	const int ms = static_cast<int>((rounded_ns % NS_PER_SECOND) / NS_PER_MILLISECOND);

	std::tm tm_local{};
#if defined(_WIN32)
	localtime_s(&tm_local, &sec);
#else
	localtime_r(&sec, &tm_local);
#endif

	std::ostringstream oss;
	oss << std::put_time(&tm_local, "%Y-%m-%dT%H_%M_%S") << '_' << std::setw(3) << std::setfill('0') << ms
	    << (tm_local.tm_isdst > 0 ? "-PDT" : "-PST");
	return oss.str();
}

static void write_log_row(const LogRow &row)
{
	const uint64_t ms_int = row.timestamp_us / 1000ULL;
	const uint32_t ms_frac = static_cast<uint32_t>(row.timestamp_us % 1000ULL);

	g_csv << row.frame_index << ',' << ms_int << '.' << std::setw(3) << std::setfill('0') << ms_frac << ','
	      << local_stamp(row.system_ns) << '\n';
}

static void writer_loop()
{
#if defined(_WIN32)
	SYSTEM_INFO system_info{};
	GetSystemInfo(&system_info);
	if (system_info.dwNumberOfProcessors > 0)
		SetThreadIdealProcessor(GetCurrentThread(), GetCurrentProcessId() % system_info.dwNumberOfProcessors);
	SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_BELOW_NORMAL);
	SetThreadPriority(GetCurrentThread(), THREAD_MODE_BACKGROUND_BEGIN);
#endif

	std::deque<LogRow> batch;
	uint64_t rows_since_flush = 0;

	while (true) {
		{
			std::unique_lock<std::mutex> lock(g_queue_mutex);
			g_queue_cv.wait_for(lock, std::chrono::milliseconds(250), [] {
				return g_writer_should_stop.load(std::memory_order_acquire) || !g_log_queue.empty();
			});
			batch.swap(g_log_queue);
		}

		for (const LogRow &row : batch) {
			write_log_row(row);
			++rows_since_flush;
		}
		batch.clear();

		if (rows_since_flush >= 64) {
			g_csv.flush();
			rows_since_flush = 0;
		}

		if (g_writer_should_stop.load(std::memory_order_acquire)) {
			std::lock_guard<std::mutex> lock(g_queue_mutex);
			if (g_log_queue.empty())
				break;
		}
	}

	g_csv.flush();

#if defined(_WIN32)
	SetThreadPriority(GetCurrentThread(), THREAD_MODE_BACKGROUND_END);
#endif
}

// Try to resolve the configured recording root folder.
// Falls back to current working directory.
static fs::path get_recording_root_resolved()
{
	obs_output_t *out = obs_frontend_get_recording_output();
	if (out) {
		fs::path result;
		obs_data_t *s = obs_output_get_settings(out);
		if (s) {
			const char *keys[] = {"path", "directory", "RecFilePath", "rec_file_path"};
			for (const char *k : keys) {
				const char *v = obs_data_get_string(s, k);
				if (v && *v) {
					result = fs::u8path(v);
					break;
				}
			}
			obs_data_release(s);
		}
		obs_output_release(out);

		if (!result.empty()) {
			if (result.has_extension() || result.filename().has_extension())
				result = result.parent_path();
			return result;
		}
	}
	return fs::current_path();
}

static void close_csv()
{
	if (g_writer_thread.joinable()) {
		{
			std::lock_guard<std::mutex> lock(g_queue_mutex);
			g_writer_should_stop.store(true, std::memory_order_release);
		}
		g_queue_cv.notify_all();
		g_writer_thread.join();
	}

	if (g_csv.is_open()) {
		g_csv.flush();
		g_csv.close();
	}
}

static void clear_log_queue()
{
	std::lock_guard<std::mutex> lock(g_queue_mutex);
	g_log_queue.clear();
}

static void start_writer()
{
	g_writer_should_stop.store(false, std::memory_order_release);
	g_writer_thread = std::thread(writer_loop);
}

static std::string frame_log_temp_filename()
{
#if defined(_WIN32)
	return "frame-log-" + std::to_string(GetCurrentProcessId()) + ".tmp.csv";
#else
	return "frame-log.tmp.csv";
#endif
}

static void on_tick(void *, float)
{
	if (!g_recording.load(std::memory_order_acquire))
		return;

	const uint64_t now_ns = os_gettime_ns();
	const uint64_t us = (now_ns - g_t0_ns) / 1000ULL;
	const int64_t system_ns = g_t0_system_ns + static_cast<int64_t>(now_ns - g_t0_ns);

	++g_frame_index;

	bool should_notify = false;
	{
		std::lock_guard<std::mutex> lock(g_queue_mutex);
		if (!g_recording.load(std::memory_order_acquire))
			return;
		g_log_queue.push_back({g_frame_index, us, system_ns});
		should_notify = g_log_queue.size() >= 64;
	}
	if (should_notify)
		g_queue_cv.notify_one();
}

static void on_frontend_event(enum obs_frontend_event ev, void *)
{
	switch (ev) {
	case OBS_FRONTEND_EVENT_RECORDING_STARTED: {
		const fs::path root = get_recording_root_resolved();

		std::error_code ec;
		fs::create_directories(root, ec);

		close_csv();
		clear_log_queue();

		g_t0_ns = os_gettime_ns();
		g_t0_system_ns = current_system_ns();
		g_csv_path = root / frame_log_temp_filename();

		g_csv.open(g_csv_path, std::ios::out | std::ios::trunc);
		if (!g_csv.is_open()) {
			blog(LOG_ERROR, "[FrameLog] Failed to open: %s", g_csv_path.string().c_str());
			return;
		}

		g_csv << "frame,timestamp_ms,ISO_timestamp\n";

		g_frame_index = 0;
		start_writer();
		g_recording.store(true, std::memory_order_release);

		blog(LOG_INFO, "[FrameLog] Logging to: %s", g_csv_path.string().c_str());
		break;
	}

	case OBS_FRONTEND_EVENT_RECORDING_STOPPING: {
		if (!g_recording.load(std::memory_order_acquire))
			break;
		g_recording.store(false, std::memory_order_release);
		close_csv();
		break;
	}

	case OBS_FRONTEND_EVENT_RECORDING_STOPPED: {
		if (g_recording.exchange(false, std::memory_order_acq_rel))
			close_csv();
		break;
	}

	default:
		break;
	}
}

bool obs_module_load()
{
	obs_add_tick_callback(on_tick, nullptr);
	obs_frontend_add_event_callback(on_frontend_event, nullptr);
	blog(LOG_INFO, "[FrameLog] Loaded.");
	return true;
}

void obs_module_unload()
{
	obs_remove_tick_callback(on_tick, nullptr);
	g_recording.store(false, std::memory_order_release);
	close_csv();
	blog(LOG_INFO, "[FrameLog] Unloaded.");
}
