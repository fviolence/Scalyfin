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

if [ "$MAX_BITRATE" ]; then
  MAX_BITRATE_ENV="- MAX_BITRATE=$MAX_BITRATE"
fi
if [ "$DELETE_ORIGINAL_FILE" ]; then
  DELETE_ORIGINAL_FILE_ENV="- DELETE_ORIGINAL_FILE='$DELETE_ORIGINAL_FILE'"
fi

# Function to convert cpu.weight to cpu.shares
convert_cpu_weight_to_shares() {
  local cpu_weight=$1
  # Ensure cpu.weight is within valid range [1, 10000]
  if ((cpu_weight < 1 || cpu_weight > 10000)); then
    echo "Error: cpu.weight must be between 1 and 10000"
    exit 1
  fi
  # Apply the correct conversion formula
  CPU_SHARES=$((2 + ((262142 * cpu_weight) - 1) / 9999))
}

if [ "$CPU_WEIGHT" ]; then
  convert_cpu_weight_to_shares "$CPU_WEIGHT"
fi

if [ "$CPU_SHARES" ]; then
  cpu_shares=$((CPU_SHARES))
  if ((cpu_shares < 2 || cpu_shares > 262144)); then
    echo "Error: cpu_shares must be between 2 and 262144"
    exit 1
  fi
  CPU_SHARES_SET="cpu_shares: $CPU_SHARES"
  if [ "$CPU_WEIGHT" ]; then
    CPU_SHARES_SET="$CPU_SHARES_SET # cpu.weight = $CPU_WEIGHT"
  fi
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
      ${MAX_BITRATE_ENV}
      ${DELETE_ORIGINAL_FILE_ENV}
    volumes:
      - ${WATCH_DIRECTORY}:/watch_dir
    devices:
      ${DEVICES}
    ${CPU_SHARES_SET}
    restart: unless-stopped
EOF

echo "docker-compose.yml generated successfully."
