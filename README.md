## Scalyfin is a lightweight, Dockerized tool designed to scale 4K videos down to 1080p while preserving video quality and maintaining the original aspect ratio.
#### Supports hardware-accelerated encoding with ffmpeg from the [Jellyfin](https://github.com/jellyfin/jellyfin-ffmpeg) repo using AMD (VAAPI) and Rockchip (RKMPP), ensuring efficient and fast transcoding. Fallback to software rendering if rendering fails. Preserves metadata and aspect ratio. Attempts to preserve one of 3 codecs: AV1, H.264 and HEVC, all others are converted to HEVC.

### Generate compose and deploy
```
GPU_ACCEL=amd WATCH_DIRECTORY=/path/to/watch ./generate-compose.sh
docker compose up --build --force-recreate --no-deps -d
```
#### Some envaroment variable to consider
Mandatory:
> GPU_ACCEL - amd or rockchip

> WATCH_DIRECTORY - path to watch

Optional:
> AMD_DEVICE - select specific AMD device (/dev/dri/renderD128 and /dev/dri/renderD129) if both are present in the system (if both default is renderD128, otherwise the generator detects an existing self)

> QP_H264, QP_HEVC, CRF_H264, CRF_HEVC, CRF_AV1 - quality presets ('Quantization Parameter' and 'Constant Rate Factor') for corresponding codecs, values: 0-51 (lower -> better), default: H264/HEVC: 20, AV1: 25

NOTE: Compose file is gernerated with user set as root by default, adjust to your need.

### Build, push, load an image:
```
docker buildx build --platform linux/amd64,linux/arm64 --tag fviolence/scalyfin:latest --tag fviolence/scalyfin:v1.0 --push .
docker buildx build --platform linux/amd64 -t scaler:latest . --load
```
