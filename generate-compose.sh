#!/bin/bash

# Set default values for environment variables if not provided
GPU_ACCEL=${GPU_ACCEL:-amd}
WATCH_DIRECTORY=${WATCH_DIRECTORY:-/home/cookie/scaler/watch_dir}

# Detect AMD VAAPI device if AMD_DEVICE is not set
detect_amd_device() {
    if [ -z "$AMD_DEVICE" ]; then
        if [ -e /dev/dri/renderD128 ]; then
            AMD_DEVICE="/dev/dri/renderD128"
        elif [ -e /dev/dri/renderD129 ]; then
            AMD_DEVICE="/dev/dri/renderD129"
        else
            echo "No valid AMD VAAPI device found. Exiting." >&2
            exit 1
        fi
    fi
    echo "Using AMD VAAPI device: $AMD_DEVICE"
}

# Determine device mappings
if [ "$GPU_ACCEL" = "amd" ]; then
  detect_amd_device
  AMD_DEVICE_ENV="- AMD_DEVICE='/dev/dri/renderD128'" # used to passed device to the script
  DEVICES="# AMD GPU
      - $AMD_DEVICE:$AMD_DEVICE"
elif [ "$GPU_ACCEL" = "rockchip" ]; then
  DEVICES="# Rockchip VPU"$'\n'
  for dev in dri dma_heap mali0 rga mpp_service iep mpp-service vpu_service vpu-service hevc_service hevc-service rkvdec rkvenc vepu h265e; do
    if [ -e "/dev/$dev" ]; then
      DEVICES+="      - /dev/$dev:/dev/$dev"$'\n'
    fi
  done
else
  echo "Unsupported GPU_ACCEL: $GPU_ACCEL" >&2
  exit 1
fi

if [ "$QP_H264" ]; then
  QP_H264_ENV="- QP_H264='$QP_H264'"
fi
if [ "$QP_HEVC" ]; then
  QP_HEVC_ENV="- QP_HEVC='$QP_HEVC'"
fi
if [ "$CRF_H264" ]; then
  CRF_H264_ENV="- CRF_H264='$CRF_H264'"
fi
if [ "$CRF_HEVC" ]; then
  CRF_HEVC_ENV="- CRF_HEVC='$CRF_HEVC'"
fi
if [ "$CRF_AV1" ]; then
  CRF_AV1_ENV="- CRF_AV1='$CRF_AV1'"
fi

# Generate docker-compose.yml
cat <<EOF | awk 'NF' > docker-compose.yml
services:
  scalyfin:
    image: fviolence/scalyfin:latest
    container_name: scalyfin
    privileged: true
    user: 0:0
    network_mode: 'host'
    environment:
      - GPU_ACCEL=${GPU_ACCEL}
      ${AMD_DEVICE_ENV}
      ${QP_H264_ENV}
      ${QP_HEVC_ENV}
      ${CRF_H264_ENV}
      ${CRF_HEVC_ENV}
      ${CRF_AV1_ENV}
    volumes:
      - ${WATCH_DIRECTORY}:/watch_dir
    devices:
      ${DEVICES}
    restart: unless-stopped
EOF

echo "docker-compose.yml generated successfully."
