#!/usr/bin/env python3

import os
import re
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

from pysubparser import parser, writer

# ====================== CONFIGURATION =======================
WATCH_DIR = os.path.abspath(os.getenv("WATCH_DIR", "/watch_dir"))
OUTPUT_DIR = os.path.abspath(os.getenv("OUTPUT_DIR", "/output_dir"))
SCAN_INTERVAL = 60                 # Scan interval in seconds

# Toggle for GPU acceleration backends: "amd" or "rockchip"
GPU_ACCEL = os.getenv("GPU_ACCEL", "undef").lower()

# Device path
# "/dev/dri/renderD128" or "/dev/dri/renderD129" expected
AMD_VAAPI_DEVICE = os.getenv("AMD_DEVICE", "")

# Max bit-rates map
MAX_BITRATES_MAP = {
    '30fps': {
        '1080p': 12000000,
        '4k': 49000000,
    },
    '60fps': {
        '1080p': 18000000,
        '4k': 75000000,
    },
}

# Toggle to remove/leave original file after conversion (default: yes)
DELETE_ORIGINAL_FILE = os.getenv("DELETE_ORIGINAL_FILE", "yes").lower() in ("true", "1", "yes")
RENAME_ONLY = os.getenv("RENAME_ONLY", "no").lower() in ("true", "1", "yes")

# Stability checking
STABILITY_CHECK_INTERVAL = 5       # seconds between file checks
STABILITY_REQUIRED_ROUNDS = 4      # number of consecutive stable checks required

# Logging configuration
LOG_LEVEL = logging.INFO
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"

# Mappings
SUBS_CODEC_TO_EXTENSION = {
    'subrip': '.srt',
    'ass': '.ass',
    'ssa': '.ssa',
    'microdvd': '.sub',
    'subviewer': '.txt'}

# Healthcheck configuration
UPDATE_TIMER = 20
UPDATE_FILE = "/tmp/scalyfin_status"
TEMP_FILES = [UPDATE_FILE]
terminate = False
# =================== END OF CONFIG ==========================


def setup_logging():
    logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
    logging.info(f"GPU_ACCEL set to: {GPU_ACCEL}")
    logging.info(f"Watching directory: {WATCH_DIR}")


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


def update_files_map(files_map, path):
    if path in files_map:
        info = files_map[path]
        size_last = info.size_history[-1]
        size_now = os.path.getsize(path)
        mod_time_now = os.path.getmtime(path)
        if size_last != size_now or info.mod_time != mod_time_now:
            files_map.pop(path, None)


def is_new_file(path):
    update_files_map(processed_files, path)
    update_files_map(skippable_files, path)
    return path not in pending_files and path not in processed_files and path not in skippable_files


class NewcomersHandler(FileSystemEventHandler):
    """
    Handles watchdog events: files created or modified.
    If a video file is found, add/update it in the pending queue for processing.
    """
    def on_created(self, event):
        if not event.is_directory and is_new_file(event.src_path):
            logging.info(
                f"File system event found new file on create: "
                f"{event.src_path}")
            add_file_to_pending(event.src_path)

    def on_modified(self, event):
        if not event.is_directory and is_new_file(event.src_path):
            logging.info(
                f"File system event found new file on modify: "
                f"{event.src_path}")
            add_file_to_pending(event.src_path)


def scan_directory():
    """Periodically scan the directory for new files."""
    while True:
        for root, _, files in os.walk(WATCH_DIR):
            for file in files:
                file_path = os.path.join(root, file)
                if is_new_file(file_path):
                    logging.info(f"Scanner found new file: {file_path}")
                    add_file_to_pending(file_path)
        time.sleep(SCAN_INTERVAL)


def convert_subtitle(input_subs, output_subs):
    """
    Convert a subtitle file from one format to another.
    :param input_subs: path of the input subtitle file
    :param output_subs: path of the output subtitle file
    """
    subtitles = parser.parse(input_subs)
    writer.write(subtitles, output_subs)
    logging.info(f"Converted {input_subs} to {output_subs}")


def add_file_to_pending(path):
    """
    Register (or re-register) a file for stability checks before processing.
    We only do so if it's really a video (ffprobe-based check).
    """
    with pending_files_lock:
        if path not in pending_files:
            logging.info(f"File queued for stability checks: {path}")
            pending_files[path] = FileInfo()
        else:
            logging.debug(f"File re-queued for stability checks: {path}")


class FileInfo:
    """
    Tracks info about a file to see if it remains stable
    """
    def __init__(self):
        self.size_history = []
        self.mod_time = 0
        self.rounds_stable = 0


def stability_checker():
    """
    Thread function: periodically checks each pending file.
    If stable for enough rounds and not locked, we process it.
    """
    while True:
        time.sleep(STABILITY_CHECK_INTERVAL)
        check_pending_files()


def split_file_name(name):
    """
    Splits a file name into its directory path, base name (without tag),
    and extension.
    """
    dir_path, filename = os.path.split(name)
    base, ext = os.path.splitext(filename)
    # Regex pattern to match " - {Tag}" only at the end of the string
    pattern = r" - [^()]+$"
    # Remove the optional tag and " - " if present
    base = re.sub(pattern, "", base)
    return dir_path, base, ext


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
                logging.debug(
                    f"File is in use by another process, skipping: {path}")
                info.rounds_stable = 0
                continue
            # Check files last modification time
            mod_time = os.path.getmtime(path)
            if info.mod_time != mod_time:
                info.mod_time = mod_time
                info.rounds_stable = 0
            # Check size stability
            size_now = os.path.getsize(path)
            if info.size_history and size_now == info.size_history[-1]:
                # Size unchanged vs. last check
                info.rounds_stable += 1
            else:
                # Size changed or first time check
                info.rounds_stable = 0
            # Add size to history and trim list to max 5 last checks
            info.size_history.append(size_now)
            if len(info.size_history) > 5:
                info.size_history.pop(0)
            # If stable for STABILITY_REQUIRED_ROUNDS intervals, process
            if info.rounds_stable >= STABILITY_REQUIRED_ROUNDS:
                logging.info(f"File is stable, processing: {path}")
                if not is_video(path):
                    logging.debug(f"File not a video: {path}")
                    skippable_files[path] = info
                else:
                    process_file(path)
                    if os.path.exists(path):
                        processed_files[path] = info
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
        result = subprocess.run(["lsof", "--", path], check=False, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode == 0 and result.stdout.strip():
            return True
        return False
    except Exception as e:
        logging.exception(f"Error checking file usage for {path}: {e}")
        # If we fail, assume not in use
        return False


def get_video_resolution(video_path):
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
            video_path
        ]
        logging.info(f"[CMD] get_video_resolution: {' '.join(cmd)}")
        result = subprocess.run(cmd, check=True, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stream = json.loads(result.stdout).get("streams", [ dict() ])[0]
        width = int(stream.get("width", 0))
        height = int(stream.get("height", 0))
        logging.info(
            f"Width {width} and height {height} for file: {video_path}")
        return width, height
    except Exception as e:
        logging.error(f"Failed to get resolution for {video_path}: {e}")
        return 0, 0


# wrapper around ffmpeg call returning success status
def render_file(input_path, output_path, params):
    ffmpeg_cmd = build_ffmpeg_command(input_path, output_path, params)

    try:
        subprocess.run(ffmpeg_cmd, check=True, text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return True
    except subprocess.CalledProcessError as e:
        logging.error(
            f"[ERROR] {input_path}: Return code "
            f"{e.returncode}\n"
            f"{e.stderr}")

    if GPU_ACCEL == ["amd", "rockchip"]:
        logging.warning(f"Falling back to software.")
        ffmpeg_cmd = build_ffmpeg_command_software(
            input_path, output_path, params)

        try:
            subprocess.run(ffmpeg_cmd, check=True, text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return True
        except subprocess.CalledProcessError as e:
            logging.error(f"[ERROR] Software render attempt failed.")
            logging.error(
                f"[ERROR] {input_path}: Return code "
                f"{e.returncode}\n"
                f"{e.stderr}")

    return False


def get_streams_info(video_path, stream_type="s"):
    """Extract subtitle streams info using ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", f"{stream_type}", "-show_entries",
        "stream=index,codec_name,codec_type:stream_tags=title,language",
        "-of", "json", video_path
    ]
    logging.info(f"[CMD] get_stream_info: {' '.join(cmd)}")
    result = subprocess.run(cmd, check=True, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return json.loads(result.stdout).get("streams", [])


def build_temp_path(prefix, suffix):
    """Build a name for temporary file."""
    temp_file = tempfile.NamedTemporaryFile(
        prefix=prefix, suffix=suffix, delete=False)
    output_path = temp_file.name
    TEMP_FILES.append(output_path)
    temp_file.close()
    return output_path


def transcode_through_temp(input_path, output_path, ext, params):
    temp_path = build_temp_path("scaler_", ext)
    logging.info(f"[PROCESS] {input_path} -> {temp_path}")
    if render_file(input_path, temp_path, params):
        logging.info(f"[DONE] {temp_path}")
        # Move to final output path
        logging.info(f"[MOVE] {temp_path} -> {output_path}")
        shutil.move(temp_path, output_path)

    # Ensure temp file is cleaned if something went wrong
    if os.path.exists(temp_path):
        os.remove(temp_path)
        logging.info(f"Cleaned up temporary file: {temp_path}")


def extract_subtitles(video_path, streams):
    """Extract subtitle streams using ffmpeg."""
    cmd = ["ffmpeg", "-y", "-i", video_path]
    for stream in streams:
        index = stream['index']
        output_path = stream['orig_subs']
        cmd += ["-map", f"0:s:{index}", output_path]
    logging.info(f"[CMD] extract_subtitles: {' '.join(cmd)}")
    subprocess.run(cmd, check=True, text=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def process_subtitles(input_path):
    """
    Process some subtitles' codecs,
    as Jellyfin-ffmpeg is unable to transcode them.
    """
    subtitle_maps = []
    streams_to_extract = []
    subtitle_streams = get_streams_info(input_path)
    for stream in subtitle_streams:
        index = subtitle_streams.index(stream)
        codec = stream["codec_name"]
        if codec in ["ass", "ssa"]:  # Advanced SubStation Alpha
            # extract subtitles and convert them
            orig_subs = build_temp_path(
                f"sub_{index}_", SUBS_CODEC_TO_EXTENSION[codec])
            conv_subs = build_temp_path(f"sub_{index}_", ".srt")
            streams_to_extract.append(
                {'index': index, 'orig_subs': orig_subs, 'conv_subs': conv_subs})
            # maps for ffmpeg
            file_index = len(streams_to_extract)  # counts from 1
            lang = stream['tags']['language']
            title = stream['tags']['title']
            subtitle_maps.append([
                f"-map", f"{file_index}",
                f"-c:s:{index}", "srt",
                f"-metadata:s:s:{index}", f"language={lang}",
                f"-metadata:s:s:{index}", f"title='{title}'",
            ])
        else:
            # codec copied as is
            subtitle_maps.append(["-map", f"0:s:{index}", f"-c:s:{index}", "copy"])

    if len(streams_to_extract) == 0:
        if subtitle_maps:
            logging.info(
                f"Found no subtitle streams to convert. "
                f"Coping all subtitles as is")
            return {'files': [], 'maps': [["-map", "0:s", "-c:s", "copy"]]}
        else:
            logging.info("Found no subtitle streams found at all")
            return {'files': [], 'maps': []}

    # extract subtitles to temp files
    extract_subtitles(input_path, streams_to_extract)
    # convert subtitles to srt format
    for stream in streams_to_extract:
        convert_subtitle(stream['orig_subs'], stream['conv_subs'])

    # remove original subtitles files
    for stream in streams_to_extract:
        os.remove(stream['orig_subs'])
    logging.info("Removed original subtitles files")

    return {'files': [stream['conv_subs']
                      for stream in streams_to_extract], 'maps': subtitle_maps}


def process_file(input_path):
    """
    Transcode the video while preserving metadata and aspect ratio.
    """
    width, height = get_video_resolution(input_path)
    if not width or not height:
        logging.error(f"Skipping {input_path}: Unable to determine resolution")
        return
    is_4k = (width >= 3840) or (height >= 2160)

    # Using dir_path and then replacing the root directory to preserve subdirectory structure
    dir_path, base, ext = split_file_name(input_path)
    output_dir_path = dir_path.replace(WATCH_DIR, OUTPUT_DIR)
    os.makedirs(output_dir_path, exist_ok=True)
    modify_permissions(output_dir_path)
    output_path_4k = os.path.join(output_dir_path, f"{base} - 4k{ext}")
    output_path_1080p = os.path.join(output_dir_path, f"{base} - 1080p{ext}")

    default_path, scaled_path = (
        output_path_4k, output_path_1080p) if is_4k else (
        output_path_1080p, "")
    do_transcoding = not os.path.exists(default_path)
    do_scaled_transcoding = is_4k and not os.path.exists(scaled_path)

    # process subtitles first to WA Jellyfin-ffmpeg issue
    subs = process_subtitles(input_path)
    # video frame-rate
    video_fps = get_video_fps(input_path)
    logging.info(f"Video fps: {video_fps}")
    # max bitrate of FPS and resolution
    fps_key = '60fps' if video_fps >= 35.0 else '30fps'
    resol_key = '4k' if is_4k else '1080p'
    max_bitrate = MAX_BITRATES_MAP[fps_key][resol_key]
    # original video bit-rate
    orig_bitrate = get_video_bitrate(input_path, max_bitrate)
    logging.info(f"Video bitrate: {orig_bitrate}")
    # target non-scale bit-rate
    bitrate = max_bitrate if orig_bitrate > max_bitrate else orig_bitrate
    logging.info(f"Target non-scale bitrate: {bitrate}")
    source_codec = get_video_codec(input_path)
    logging.info(f"Source codec: {source_codec}")
    params = {'subs': subs, 'bitrate': bitrate, 'source_codec': source_codec}

    if not do_transcoding and not do_scaled_transcoding:
        logging.info(f"Skipping {input_path}: Already processed")
        return

    if do_transcoding:
        # only rename original file if nothing to be changed
        if RENAME_ONLY and source_codec in ['h264', 'hevc', 'av1'] and len(subs['files']) == 0 and bitrate == orig_bitrate:
            logging.info(f"Transcoding without rescale is excessive")
            if input_path != default_path:
                logging.info(f"[MOVE] {input_path} -> {default_path}")
                shutil.move(input_path, default_path)
                input_path = default_path
        else:
            logging.info("Transcoding without rescale")
            transcode_through_temp(input_path, default_path, ext, params)
        modify_permissions(default_path)

    if do_scaled_transcoding:
        logging.info("Transcoding with rescale")
        scaled_width, scaled_height = calculate_scaled_resolution(
            width, height)
        logging.info(f"Scaled resolution: {scaled_width}x{scaled_height}")
        # recaclculate bitrate based on scaled resolution
        scaled_bitrate = math.ceil(
            bitrate * scaled_width * scaled_height / (width * height))
        logging.info(f"Scaled bitrate: {scaled_bitrate}")
        params['bitrate'] = scaled_bitrate
        params['resolution'] = [scaled_width, scaled_height]
        transcode_through_temp(input_path, scaled_path, ext, params)
        modify_permissions(scaled_path)

    if DELETE_ORIGINAL_FILE and input_path != default_path:
        # remove original video file
        os.remove(input_path)
        logging.info(f"Cleaned up original file: {input_path}")
        # delete parent directories if empty
        parent_path = os.path.dirname(input_path)
        while os.path.normpath(parent_path) != os.path.normpath(WATCH_DIR):
            if not os.path.isdir(parent_path):
                break
            if len(os.listdir(parent_path)) == 0:
                try:
                    os.rmdir(parent_path)
                except Exception as e:
                    logging.debug(f"Error removing directory '{parent_path}': {e}")
                    break
            else:
                break
            parent_path = os.path.dirname(parent_path)

    # Delete all subtitle files
    if subs['files']:
        for sub in subs['files']:
            os.remove(sub)
        logging.info("Cleaned up subtitles")


def modify_permissions(file_path):
    permissions = 0o755 if os.path.isdir(file_path) else 0o644
    os.chmod(file_path, permissions)
    uid = 1000
    gid = 1000
    os.chown(file_path, uid, gid)


def is_video(path):
    """Checks via mediainfo: frame count should be greater then zero."""
    try:
        # Run the mediainfo command
        cmd = ["mediainfo", "--Output=Video;%FrameCount%", path]
        logging.info(f"[CMD] is_video: {' '.join(cmd)}")
        result = subprocess.run(cmd, check=True, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        # Retrieve the frame count from the output
        frame_count = result.stdout.strip()

        # If frame count is a valid integer and greater than 0, it's a video
        if frame_count.isdigit() and int(frame_count) > 0:
            return True
    except Exception as e:
        logging.debug(f"Error in is_video for {path}: {e}")

    return False


def get_video_fps(video_path):
    """
    Get overall FPS via mediainfo
    """
    try:
        # Run the mediainfo command
        cmd = ["mediainfo", "--Output=General;%FrameRate%", video_path]
        logging.info(f"[CMD] get_video_fps: {' '.join(cmd)}")
        result = subprocess.run(cmd, check=True, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # Retrieve the frame-rate from the output
        fps_str = result.stdout.strip()
        fps = float(fps_str)
        return fps
    except Exception as e:
        logging.debug(f"Error in get_video_fps for {video_path}: {e}")

    logging.debug(f"Could not get FPS for {video_path}, using 60.0 by default")
    return 60.0


def get_video_bitrate(video_path, max_bitrate):
    """
    Get overall bitrate via mediainfo
    """
    try:
        # Run the mediainfo command
        cmd = ["mediainfo", "--Output=General;%OverallBitRate%", video_path]
        logging.info(f"[CMD] get_video_bitrate: {' '.join(cmd)}")
        result = subprocess.run(cmd, check=True, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # Retrieve the bit-rate from the output
        bitrate = result.stdout.strip()

        if bitrate.isdigit():
            return int(bitrate)
    except Exception as e:
        logging.debug(f"Error in get_video_bitrate for {video_path}: {e}")

    logging.debug(
        f"Could not get bit-rate for {video_path}, "
        f"using MAX_BITRATE: {max_bitrate}"
    )
    return max_bitrate


def get_video_codec(video_path):
    """
    Identify the codec (h264, hevc, av1, etc.) via ffprobe.
    """
    try:
        cmd = [
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name",
            "-of", "json",
            video_path
        ]
        logging.info(f"[CMD] get_video_codec: {' '.join(cmd)}")
        result = subprocess.run(cmd, check=True, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stream = json.loads(result.stdout).get("streams", [ dict() ])[0]
        return stream.get("codec_name", "other")
    except Exception as e:
        logging.debug(f"Error in get_video_codec for {video_path}: {e}")
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

def build_ffmpeg_command(input_file, output_file, params):
    """
    Decide how to encode based on GPU_ACCEL.
    """
    if GPU_ACCEL == "amd":
        return build_ffmpeg_command_amd(input_file, output_file, params)
    elif GPU_ACCEL == "rockchip":
        return build_ffmpeg_command_rockchip(input_file, output_file, params)
    else:
        logging.warning(
            f"Unknown GPU_ACCEL={GPU_ACCEL}. Falling back to software.")
        return build_ffmpeg_command_software(input_file, output_file, params)


def build_ffmpeg_command_amd(input_file, output_file, params):
    """
    AMD VAAPI: can do h264_vaapi, hevc_vaapi, av1_vaapi, etc.
    """
    bitrate = str(params.get('bitrate'))
    subs = params.get('subs')
    resolution = params.get('resolution')
    source_codec = params.get('source_codec')

    encoder = ["-map", "0:v:0", "-c:v"]
    if source_codec == "h264":
        encoder += ["h264_vaapi", "-b:v:0", bitrate]
    elif source_codec == "av1":
        encoder += ["av1_vaapi", "-b:v:0", bitrate]
    else:
        # Default to HEVC VAAPI
        encoder += ["hevc_vaapi", "-b:v:0", bitrate]

    # Hardware scaling with VAAPI
    vf = "format=nv12,hwupload"
    if resolution:
        width, height = resolution
        vf += f",scale_vaapi=w={width}:h={height}"

    # Construct ffmpeg command line with VAAPI and scaling.
    cmd = [
        "ffmpeg", "-y", "-fix_sub_duration",
        "-hwaccel", "vaapi", "-vaapi_device", AMD_VAAPI_DEVICE,
        "-i", input_file,
    ]
    for sub_input in subs['files']:
        cmd += ["-i", sub_input]
    cmd += ["-vf", vf, "-map_metadata", "0"]
    cmd += encoder
    cmd += ["-map", "0:a", "-c:a", "copy"]
    for sub_map in subs['maps']:
        cmd += sub_map
    cmd += ["-movflags", "+faststart", output_file]

    logging.info(f"[CMD] build_ffmpeg_command_amd: {' '.join(cmd)}")
    return cmd


def build_ffmpeg_command_rockchip(input_file, output_file, params):
    """
    Rockchip RKMPP: can do h264_rkmpp, hevc_rkmpp.
    AV1 encoding not supported (decode-only),
    so fallback to software if AV1 is desired.
    """
    bitrate = str(params.get('bitrate'))
    subs = params.get('subs')
    resolution = params.get('resolution')
    source_codec = params.get('source_codec')

    encoder = ["-map", "0:v:0", "-c:v"]
    if source_codec == "h264":
        encoder += ["h264_rkmpp", "-b:v:0", bitrate]
    elif source_codec == "av1":
        # Rockchip can't encode AV1 => software fallback
        logging.info(
            "Rockchip: Falling back to software libaom-av1 for AV1 encoding.")
        return build_ffmpeg_command_software(
            input_file, output_file, "libaom-av1", params)
    else:
        # Default to HEVC rkmpp
        encoder += ["hevc_rkmpp", "-b:v:0", bitrate]

    # Construct ffmpeg command line with RKMPP hardware acceleration.
    cmd = [
        "ffmpeg", "-y", "-fix_sub_duration",
        "-hwaccel", "rkmpp",
        "-i", input_file,
    ]
    for sub_input in subs['files']:
        cmd += ["-i", sub_input]
    if resolution:
        width, height = resolution
        cmd += ["-vf", f"scale={width}:{height}"]
    cmd += ["-map_metadata", "0"]
    cmd += encoder
    cmd += ["-map", "0:a", "-c:a", "copy"]
    for sub_map in subs['maps']:
        cmd += sub_map
    cmd += ["-movflags", "+faststart", output_file]

    logging.info(f"[CMD] build_ffmpeg_command_rockchip: {' '.join(cmd)}")
    return cmd


def build_ffmpeg_command_software(input_file, output_file, params):
    """
    Fallback software encoding (libx264, libx265, libaom-av1, etc.),
    preserving metadata/streams if requested.
    """
    bitrate = str(params.get('bitrate'))
    subs = params.get('subs')
    resolution = params.get('resolution')
    source_codec = params.get('source_codec')

    # translate source codec to SW codec
    SOURCE_CODEC_TO_SOFTWARE = {
        'h264': 'libx264',
        'hevc': 'libx265',
        'av1': 'libaom-av1'}
    target_codec = SOURCE_CODEC_TO_SOFTWARE.get(source_codec, "undef")
    video_quality = ["-map", "0:v:0", "-c:v", target_codec, "-b:v:0", bitrate]

    # Construct the command line.
    cmd = [
        "ffmpeg", "-y", "-fix_sub_duration",
        "-i", input_file,
    ]
    for sub_input in subs['files']:
        cmd += ["-i", sub_input]
    if resolution:
        width, height = resolution
        cmd += ["-vf", f"scale={width}:{height}"]
    cmd += ["-map_metadata", "0"]
    cmd += video_quality
    cmd += ["-map", "0:a", "-c:a", "copy"]
    for sub_map in subs['maps']:
        cmd += sub_map
    cmd += ["-movflags", "+faststart", output_file]

    logging.info(f"[CMD] build_ffmpeg_command_software: {' '.join(cmd)}")
    return cmd


# -----------------------------------------------------------
# INITIAL BULK PROCESS + MAIN (Watchdog + Stability Thread)
# -----------------------------------------------------------

def process_all_existing_files():
    """
    Scans the watch directory at startup; queues any video for stability checks
    (unless its 1080p output already exists).
    """
    for root, dirs, files in os.walk(WATCH_DIR):
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
    signal.signal(signal.SIGTERM, signal_handler)  # docker stop sends SIGTERM
    # docker kill --signal=SIGHUP sends SIGHUP
    signal.signal(signal.SIGHUP, signal_handler)

    # 1) Bulk process existing files
    process_all_existing_files()

    # 2) Start watchdog
    event_handler = NewcomersHandler()
    observer = Observer()
    observer.schedule(event_handler, WATCH_DIR, recursive=True)
    observer.start()

    # 3) Start stability checker thread
    checker_thread = threading.Thread(target=stability_checker, daemon=True)
    checker_thread.start()

    # 4) Start status updater thread
    status_thread = threading.Thread(target=update_status, daemon=True)
    status_thread.start()

    # 5) Start the periodic scanner thread
    scanner_thread = threading.Thread(target=scan_directory, daemon=True)
    scanner_thread.start()

    logging.info(
        f"Monitoring directory (recursive): {WATCH_DIR}. "
        f"Press Ctrl+C to stop.")
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
processed_files = {}
skippable_files = {}
pending_files_lock = threading.Lock()

if __name__ == "__main__":
    main()
