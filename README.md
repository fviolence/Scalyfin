## Scalyfin is a lightweight, dockerized Linux tool designed to scale 4K videos down to 1080p while preserving video quality and maintaining the original aspect ratio.
#### Supports hardware-accelerated encoding with ffmpeg from the [Jellyfin](https://github.com/jellyfin/jellyfin-ffmpeg) repo using AMD (VAAPI) and Rockchip (RKMPP), ensuring efficient and fast transcoding. Fallback to software rendering if rendering fails. Preserves metadata and aspect ratio. Attempts to preserve one of 3 codecs: AV1, H.264 and HEVC, all others are converted to HEVC.

#### Features:
 * Automatic directory monitoring for new 4K videos.
 * Hardware-accelerated video transcoding for AMD and Rockchip.
 * Preserves metadata, converts Advanced SubStation Alpha subtitles to SubRip, limits bitrate if set value is exceeded.

## Examples of Docker Compose files.
##### [AMD](https://jellyfin.org/docs/general/administration/hardware-acceleration/amd/)
```yaml
services:
  scalyfin:
    image: fviolence/scalyfin
    container_name: scalyfin
    privileged: true
    network_mode: 'host'
    environment:
      - GPU_ACCEL=amd
      - AMD_DEVICE='/dev/dri/renderD128'
    volumes:
      - /path/to/watch:/watch_dir
    devices:
      # AMD GPU
      - /dev/dri/renderD128:/dev/dri/renderD128
    restart: unless-stopped
```
Device is one of the following:
```bash
/dev/dri/renderD128 or /dev/dri/renderD129
```

##### [Rockchip](https://jellyfin.org/docs/general/administration/hardware-acceleration/rockchip/)
```yaml
services:
  scalyfin:
    image: fviolence/scalyfin:latest
    container_name: scalyfin
    privileged: true
    network_mode: 'host'
    environment:
      - GPU_ACCEL=rockchip
    volumes:
      - /path/to/watch:/watch_dir
    devices:
      # Rockchip VPU
      - /dev/dri:/dev/dri
      - /dev/dma_heap:/dev/dma_heap
      - /dev/mali0:/dev/mali0
      - /dev/rga:/dev/rga
      - /dev/mpp_service:/dev/mpp_service
    restart: unless-stopped
```
List of devices:
```bash
for dev in dri dma_heap mali0 rga mpp_service iep mpp-service vpu_service vpu-service hevc_service hevc-service rkvdec rkvenc vepu h265e ; do [ -e "/dev/$dev" ] && echo "/dev/$dev"; done
```

## Generate compose and deploy
```
GPU_ACCEL=amd WATCH_DIR=/path/to/watch OUTPUT_DIR=/path/to/output ./generate-compose.sh
docker compose up --build --force-recreate --no-deps -d
```
##### NOTE: Compose file is gernerated with user set as root by default, adjust to your need.

## Some envaroment variable to consider
Mandatory:
| **Variable**           | **Description**                                                                                   |
|------------------------|---------------------------------------------------------------------------------------------------|
| `GPU_ACCEL`            | Specifies the GPU backend to use. Supported values: `amd` (for AMD GPUs) or `rockchip` (for Rockchip devices). |
| `WATCH_DIR`            | Directory to monitor for the videos.                                                              |
| `OUTPUT_DIR`           | Directory to output the videos.                                                                   |

Optional:
| **Variable**           | **Description**                                                                                   | **Default Value**                       |
|------------------------|---------------------------------------------------------------------------------------------------|-----------------------------------------|
| `AMD_DEVICE`           | Path to the AMD VAAPI device (e.g., `/dev/dri/renderD128` or `/dev/dri/renderD129`).              | Auto-detected with `/dev/dri/renderD128` as default if both present. |
| `DELETE_ORIGINAL_FILE` | Boolean flag to specify if original video should be deleted after being processed.                | True      |


#### Build, push, load an image:
```
docker buildx build --platform linux/amd64,linux/arm64 --tag fviolence/scalyfin:latest --tag fviolence/scalyfin:v1.0 --push .
docker buildx build --platform linux/amd64 -t scalyfin:latest --load .
```
