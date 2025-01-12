#!/usr/bin/env python3

import os
import tempfile
import shutil
import time
import subprocess
import threading
import logging
import json
import math
import atexit
import signal

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ====================== CONFIGURATION =======================
WATCH_DIRECTORY = os.getenv("WATCH_DIRECTORY", "/watch_dir")

# Toggle for GPU acceleration backends: "amd" or "rockchip"
GPU_ACCEL = os.getenv("GPU_ACCEL", "undef").lower()

# Device path
AMD_VAAPI_DEVICE = os.getenv("AMD_DEVICE", "") # "/dev/dri/renderD128" or "/dev/dri/renderD129" expected
# ROCKCHIP_DEVICE  = "" # not needed

# Quality parameters
QP_H264 = os.getenv("QP_H264", "20")
QP_HEVC = os.getenv("QP_HEVC", "20")
CRF_H264 = os.getenv("CRF_H264", "20")
CRF_HEVC = os.getenv("CRF_HEVC", "20")
CRF_AV1 = os.getenv("CRF_AV1", "25")

# Stability checking
STABILITY_CHECK_INTERVAL = 5       # seconds between file checks
STABILITY_REQUIRED_ROUNDS = 2      # number of consecutive stable checks required

# Logging configuration
LOG_LEVEL = logging.INFO
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"

PRESERVE_METADATA = True
SOURCE_CODEC_TO_SOFTWARE = {'h264': 'libx264', 'hevc': 'libx265', 'av1': 'libaom-av1'}

UPDATE_TIMER= 20
UPDATE_FILE = "/tmp/scalyfin_status"
TEMP_FILES = [UPDATE_FILE]
terminate = False
# =================== END OF CONFIG ==========================

def setup_logging():
    logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
    logging.info(f"GPU_ACCEL set to: {GPU_ACCEL}")
    logging.info(f"Watching directory: {WATCH_DIRECTORY}")
    logging.info(f"Quality profiles: QP_H264: {QP_H264}, QP_HEVC: {QP_HEVC}, CRF_H264: {CRF_H264}, CRF_HEVC: {CRF_HEVC}, CRF_AV1: {CRF_AV1}")


# Healthcheck handler
def update_status():
    """Periodically update the status file to indicate the script is running."""
    global terminate
    while not terminate:
        try:
            with open(UPDATE_FILE, "w") as f:
                f.write("running")
            time.sleep(UPDATE_TIMER)
        except Exception as e:
            logging.error(f"Error updating status file: {e}")
            terminate = True
            break


def cleanup_temp_files():
    """Remove all tracked temporary files."""
    for temp_file in TEMP_FILES:
        if os.path.exists(temp_file):
            try:
                os.remove(temp_file)
                logging.info(f"Removed temporary file: {temp_file}")
            except Exception as e:
                logging.error(f"Error removing file {temp_file}: {e}")
    logging.info("Shutting down.")


def signal_handler(signum, frame):
    """Set the terminate flag when a signal is received."""
    global terminate
    logging.info(f"Signal {signum} received.")
    terminate = True


class NewcomersHandler(FileSystemEventHandler):
    """
    Handles watchdog events: files created or modified.
    If a video file is found, add/update it in the pending queue for processing.
    """
    def on_created(self, event):
        if not event.is_directory:
            add_file_to_pending(event.src_path)

    def on_modified(self, event):
        if not event.is_directory and event.src_path not in pending_files:
                add_file_to_pending(event.src_path)


def add_file_to_pending(path):
    """
    Register (or re-register) a file for stability checks before processing.
    We only do so if it's really a video (ffprobe-based check).
    """
    if not is_4k_video(path):
        logging.info(f"File not a 4k video: {path}")
        return

    out_path = build_output_path(path)
    if os.path.exists(out_path):
        logging.info(f"Corresponding output ({out_path}) already exist: {path}")
        return

    with pending_files_lock:
        if path not in pending_files:
            logging.info(f"File queued for stability checks: {path}")
            pending_files[path] = FileInfo()
        else:
            logging.debug(f"File re-queued for stability checks: {path}")


class FileInfo:
    """
    Tracks info about a file to see if it remains stable (size unchanged, not in use).
    """
    def __init__(self):
        self.size_history = []
        self.rounds_stable = 0


def stability_checker():
    """
    Thread function: periodically checks each pending file.
    If stable for enough rounds and not locked, we process it.
    """
    while True:
        time.sleep(STABILITY_CHECK_INTERVAL)
        check_pending_files()


def check_pending_files():
    with pending_files_lock:
        to_remove = []

        for path, info in pending_files.items():
            if not os.path.exists(path):
                logging.warning(f"File disappeared: {path}")
                to_remove.append(path)
                continue

            # Check if file is in use by another process
            if is_file_in_use(path):
                logging.debug(f"File is in use by another process, skipping: {path}")
                info.rounds_stable = 0
                continue

            # Check size stability
            size_now = os.path.getsize(path)
            if info.size_history and size_now == info.size_history[-1]:
                # Size unchanged vs. last check
                info.rounds_stable += 1
            else:
                # Size changed or first time check
                info.rounds_stable = 0

            info.size_history.append(size_now)
            if len(info.size_history) > 5:
                info.size_history.pop(0)

            # If stable for STABILITY_REQUIRED_ROUNDS intervals, process
            if info.rounds_stable >= STABILITY_REQUIRED_ROUNDS:
                logging.info(f"File is stable, processing: {path}")
                process_file(path)
                to_remove.append(path)

        # Remove processed or disappeared files
        for r in to_remove:
            pending_files.pop(r, None)


def is_file_in_use(path):
    """
    Check via `lsof` if the file is used by another process.
    If returncode == 0 AND there's output, it's open. Otherwise, not in use.
    """
    try:
        result = subprocess.run(["lsof", "--", path],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                text=True)
        if result.returncode == 0 and result.stdout.strip():
            return True
        return False
    except Exception as e:
        logging.exception(f"Error checking file usage for {path}: {e}")
        # If we fail, assume not in use
        return False


def get_video_resolution(input_file):
    """
    Retrieve the resolution of the video using ffprobe.
    """
    try:
        cmd = [
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "json",
            input_file
        ]
        logging.info(f"[CMD] get_video_resolution: {' '.join(cmd)}")
        output = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode()
        data = json.loads(output)
    except Exception as e:
        logging.debug(f"Error in get_video_resolution for {input_file}: {e}")
        return 0, 0

    if "streams" not in data or not data["streams"]:
        logging.info(f"No 'streams' in ffprobe response for file: {input_file}")
        return 0, 0

    try:
        stream = data["streams"][0]
        width = int(stream.get("width", 0))
        height = int(stream.get("height", 0))
        logging.info(f"Width {width} and height {height} for file: {input_file}")
        return width, height
    except Exception as e:
        logging.error(f"Failed to get resolution for {input_file}: {e}")
        return 0, 0


# wrapper around ffmpeg call returning success status
def render_file(input_path, temp_output_path, width, height, source_codec):
    ffmpeg_cmd = build_ffmpeg_command(input_path, temp_output_path, width, height, source_codec, PRESERVE_METADATA)

    try:
        subprocess.run(ffmpeg_cmd, shell=True, check=True, text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return True
    except subprocess.CalledProcessError as e:
        logging.error(f"[ERROR] {input_path}: Return code {e.returncode}\n{e.stderr}")

    if GPU_ACCEL == ["amd", "rockchip"]:
        logging.warning(f"Falling back to software.")
        target_codec = SOURCE_CODEC_TO_SOFTWARE.get(source_codec, "libx265")
        ffmpeg_cmd = build_ffmpeg_command_software(input_path, temp_output_path, width, height, target_codec, PRESERVE_METADATA)

        try:
            subprocess.run(ffmpeg_cmd, shell=True, check=True, text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return True
        except subprocess.CalledProcessError as e:
            logging.error(f"[ERROR] Software render attempt failed.")
            logging.error(f"[ERROR] {input_path}: Return code {e.returncode}\n{e.stderr}")

    return False


def process_file(input_path):
    """
    1. Rename the original file.
    2. Generate the output file path.
    3. Transcode the video while preserving metadata and aspect ratio.
    """
    temp_output_path = build_temp_path(input_path)
    final_output_path = build_output_path(input_path)

    if os.path.exists(final_output_path):
        logging.info(f"Output already exists, skipping: {final_output_path}")
        return

    source_codec = get_video_codec(input_path)
    width, height = get_video_resolution(input_path)
    if not width or not height:
        logging.error(f"Skipping {input_path}: Unable to determine resolution.")
        return

    logging.info(f"[PROCESS] {input_path} -> {temp_output_path}")
    if render_file(input_path, temp_output_path, width, height, source_codec):
        logging.info(f"[DONE] {temp_output_path}")
        # Move to final output path
        logging.info(f"[MOVE] {temp_output_path} -> {final_output_path}")
        shutil.move(temp_output_path, final_output_path)
        rename_original_file(input_path)

    # Ensure temp file is cleaned if something went wrong
    if os.path.exists(temp_output_path):
        os.remove(temp_output_path)
        logging.info(f"Cleaned up temporary file: {temp_output_path}")


def rename_original_file(input_path):
    """
    Rename the original file by appending ' - 4k' to its name.
    """
    dir_path, filename = os.path.split(input_path)
    base, ext = os.path.splitext(filename)

    # Check if the base name ends with ' - 4k' to avoid collisions
    if base.endswith(" - 4k"):
        base = base[:-5]

    new_name = f"{base.strip()} - 4k{ext}"
    new_path = os.path.join(dir_path, new_name)

    if new_path != input_path:
        logging.info(f"Renaming original file: {input_path} -> {new_path}")
        shutil.move(input_path, new_path)
    return new_path


def build_temp_path(input_path):
    """
    Build the output path by appending ' - 1080p' to the original filename.
    """
    dir_path, filename = os.path.split(input_path)
    base, ext = os.path.splitext(filename)
    temp_file = tempfile.NamedTemporaryFile(prefix="scaler_", suffix=ext, delete=False)
    output_path = temp_file.name
    TEMP_FILES.append(output_path)
    temp_file.close()
    return output_path


def build_output_path(input_path):
    """
    Build the output path by appending ' - 1080p' to the original filename.
    """
    dir_path, filename = os.path.split(input_path)
    base, ext = os.path.splitext(filename)

    # Check if the base name ends with ' - 4k' to avoid collisions
    if base.endswith(" - 4k"):
        base = base[:-5]

    output_name = f"{base.strip()} - 1080p{ext}"
    return os.path.join(dir_path, output_name)


def is_4k_video(path):
    """
    Checks via ffprobe:
        1) Codec type video
        2) Duration more than 1 sec (to avoid images)
        3) Resolution 4k and above
    """
    if not os.path.isfile(path):
        return False

    data = None
    try:
        cmd = [
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_type:format=duration",
            "-of", "json",
            path
        ]
        logging.info(f"[CMD] is_4k_video: {' '.join(cmd)}")
        output = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode()
        data = json.loads(output)
    except Exception as e:
        logging.debug(f"Error in is_4k_video for {path}: {e}")
        return False

    if "streams" not in data or not data["streams"]:
        logging.info(f"No 'streams' in ffprobe response for file: {path}")
        return False
    if "format" not in data or not data["format"]:
        logging.info(f"No 'format' in ffprobe response for file: {path}")
        return False

    stream = data["streams"][0]
    if stream.get("codec_type", "") != "video":
        logging.info(f"Codec type not video for file: {path}")
        return False
    duration = float(data.get("format", {}).get("duration", 0.0))
    if duration <= 1:
        logging.info(f"Duration less then 1s for file: {path}")
        return False

    width, height = get_video_resolution(path)
    return (width >= 3840 or height >= 2160)


def get_video_codec(input_file):
    """
    Identify the codec (h264, hevc, av1, etc.) via ffprobe.
    """
    try:
        cmd = [
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name",
            "-of", "csv=p=0",
            input_file
        ]
        logging.info(f"[CMD] get_video_codec: {' '.join(cmd)}")
        codec = subprocess.check_output(cmd).decode().strip().lower()
        return codec
    except:
        return "other"


def calculate_scaled_resolution(width, height, target_width=1920):
    """
    Calculate the scaled resolution while preserving the aspect ratio.
    Adds padding if necessary to maintain the target height.
    """
    scaled_height = math.ceil(target_width * height / width)
    return target_width, scaled_height


# ----------------------------------------------------------------
#  FFmpeg command-building logic for AMD vs. Rockchip + fallback
# ----------------------------------------------------------------

def build_ffmpeg_command(input_file, output_file, width, height, source_codec, preserve_metadata):
    """
    Decide how to encode based on GPU_ACCEL.
    """
    if GPU_ACCEL == "amd":
        return build_ffmpeg_command_amd(input_file, output_file, width, height, source_codec, preserve_metadata)
    elif GPU_ACCEL == "rockchip":
        return build_ffmpeg_command_rockchip(input_file, output_file, width, height, source_codec, preserve_metadata)
    else:
        logging.warning(f"Unknown GPU_ACCEL={GPU_ACCEL}. Falling back to software.")
        target_codec = SOURCE_CODEC_TO_SOFTWARE.get(source_codec, "libx265")
        return build_ffmpeg_command_software(input_file, output_file, width, height, target_codec, preserve_metadata)


def build_ffmpeg_command_amd(input_file, output_file, width, height, source_codec, preserve_metadata):
    """
    AMD VAAPI: can do h264_vaapi, hevc_vaapi, av1_vaapi, etc.
    """
    if source_codec == "h264":
        encoder = f"h264_vaapi -qp {QP_H264}"
    elif source_codec == "av1":
        encoder = f"av1_vaapi -crf {CRF_AV1} -b:v 0"
    else:
        # Default to HEVC VAAPI
        encoder = f"hevc_vaapi -qp {QP_HEVC}"

    # Hardware scaling with VAAPI
    scaled_width, scaled_height = calculate_scaled_resolution(width, height)
    scale_filter = f"format=nv12,hwupload,scale_vaapi=w={scaled_width}:h={scaled_height}"

    # Metadata preservation
    metadata_opts = ""
    if preserve_metadata:
        metadata_opts = "-map 0 -map_metadata 0 -c:a copy -c:s copy"

    cmd = (
        f"ffmpeg -y "
        f"-hwaccel vaapi -vaapi_device {AMD_VAAPI_DEVICE} "
        f"-i '{input_file}' "
        f"-progress pipe:1 -nostats "
        f"{metadata_opts} "
        f"-vf '{scale_filter}' "
        f"-c:v {encoder} "
        f"'{output_file}'"
    )
    logging.info(f"[CMD] build_ffmpeg_command_amd: {cmd}")
    return cmd


def build_ffmpeg_command_rockchip(input_file, output_file, width, height, source_codec, preserve_metadata):
    """
    Rockchip RKMPP: can do h264_rkmpp, hevc_rkmpp.
    AV1 encoding not supported (decode-only), so fallback to software if AV1 is desired.
    """
    if source_codec == "h264":
        encoder = f"h264_rkmpp -qp {QP_H264}"
    elif source_codec == "av1":
        # Rockchip can't encode AV1 => software fallback
        logging.info("Rockchip: Falling back to software libaom-av1 for AV1 encoding.")
        return build_ffmpeg_command_software(input_file, output_file, width, height, "libaom-av1", preserve_metadata)
    else:
        # Default to HEVC rkmpp
        encoder = f"hevc_rkmpp -qp {QP_HEVC}"

    # We'll assume software scaling for Rockchip (some SoCs might allow MPP-based scaling)
    scaled_width, scaled_height = calculate_scaled_resolution(width, height)
    scale_filter = f"scale={scaled_width}:{scaled_height}"

    metadata_opts = ""
    if preserve_metadata:
        metadata_opts = "-map 0 -map_metadata 0 -c:a copy -c:s copy"

    cmd = (
        f"ffmpeg -y "
        f"-i '{input_file}' "
        f"-progress pipe:1 -nostats "
        f"{metadata_opts} "
        f"-vf '{scale_filter}' "
        f"-c:v {encoder} "
        f"'{output_file}'"
    )
    logging.info(f"[CMD] build_ffmpeg_command_rockchip: {cmd}")
    return cmd


def build_ffmpeg_command_software(input_file, output_file, width, height, target_codec, preserve_metadata):
    """
    Fallback software encoding (libx264, libx265, libaom-av1, etc.),
    preserving metadata/streams if requested.
    """
    # Example CRF/preset choices
    if target_codec == "libx264":
        video_quality = f"-crf {CRF_H264} -preset medium"
    elif target_codec == "libaom-av1":
        # Example: use CRF, no bitrate, moderate speed
        video_quality = f"-crf {CRF_AV1} -b:v 0 -cpu-used 4"
    else:
        # Default to libx265
        video_quality = f"-crf {CRF_HEVC} -preset medium"

    scaled_width, scaled_height = calculate_scaled_resolution(width, height)
    scale_filter = f"scale={scaled_width}:{scaled_height}"

    metadata_opts = ""
    if preserve_metadata:
        metadata_opts = "-map 0 -map_metadata 0 -c:a copy -c:s copy"

    cmd = (
        f"ffmpeg -y "
        f"-i '{input_file}' "
        f"-progress pipe:1 -nostats "
        f"{metadata_opts} "
        f"-vf '{scale_filter}' "
        f"-c:v {target_codec} {video_quality} "
        f"'{output_file}'"
    )
    logging.info(f"[CMD] build_ffmpeg_command_software: {cmd}")
    return cmd


# -----------------------------------------------------------
# INITIAL BULK PROCESS + MAIN (Watchdog + Stability Thread)
# -----------------------------------------------------------

def process_all_existing_files():
    """
    Scans the watch directory at startup; queues any video for stability checks
    (unless its 1080p output already exists).
    """
    for root, dirs, files in os.walk(WATCH_DIRECTORY):
        for f in files:
            path = os.path.join(root, f)
            add_file_to_pending(path)


def main():
    global terminate

    setup_logging()
    logging.info(f"Starting with GPU_ACCEL={GPU_ACCEL}")

    # Setup temporary files cleanup on exit
    atexit.register(cleanup_temp_files)
    # Attach signal handlers to set the terminate flag
    signal.signal(signal.SIGTERM, signal_handler) # docker stop sends SIGTERM
    signal.signal(signal.SIGHUP, signal_handler)  # docker kill --signal=SIGHUP sends SIGHUP

    # 1) Bulk process existing files
    process_all_existing_files()

    # 2) Start watchdog
    event_handler = NewcomersHandler()
    observer = Observer()
    observer.schedule(event_handler, WATCH_DIRECTORY, recursive=True)
    observer.start()

    # 3) Start stability checker thread
    checker_thread = threading.Thread(target=stability_checker, daemon=True)
    checker_thread.start()

    # 4) Start status updater thread
    status_thread = threading.Thread(target=update_status, daemon=True)
    status_thread.start()

    logging.info(f"Monitoring directory (recursive): {WATCH_DIRECTORY}. Press Ctrl+C to stop.")
    try:
        while not terminate:
            time.sleep(1)
    except KeyboardInterrupt:
        logging.info("Keyboard interrupt received.")

    logging.info("Stopping observer.")
    observer.stop()
    observer.join()
    # since checker_thread and status_thread are daemon threads
    # they will automatically terminate when the main program exits


# Global thread-safe dictionary for pending files
pending_files = {}
pending_files_lock = threading.Lock()

if __name__ == "__main__":
    main()
