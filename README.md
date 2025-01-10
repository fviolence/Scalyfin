# Small service to silently rescale UHD to HD video.
Configured to use AMD GPU or Rockchip VPU HW acceleration if there's any, otherwise fallback to software render. Preservse metadata and aspect ratio. Tries to preserve one of 3 codecs: AV1, H.264 and HEVC, others are converted to HEVC.

### Build, push, load:
```
docker buildx build --platform linux/amd64,linux/arm64 --tag fviolence/scaler:latest --tag fviolence/scaler:v1.0 --push .
docker buildx build --platform linux/amd64 -t scaler:latest . --load
```

### Generate compose and deploy
```
GPU_ACCEL=amd WATCH_DIRECTORY=/path/to/watch OUTPUT_DIRECTORY=/path/to/output ./generate-compose.sh
docker compose up --build --force-recreate --no-deps -d
```

### Environment variables on compose to consider:
```
GPU_ACCEL - "amd" or "rockchip"
WATCH_DIRECTORY - path to directory to watch for 4k videos
OUTPUT_DIRECTORY - path to output directory with HD videos
```

NOTE: For some reason, most likely due to mapping data volume to NFS, when deploying on personal PC with AMD GPU, container unable to render anything unless root user specified.
Access to render device not an issue, passing groups or creating udev rules does not fix the issue - ffmpeg still fails with some strange errors not being able to read subtitles, even when they are disabled.
```
System.ArgumentException: Unsupported format: srt
```
