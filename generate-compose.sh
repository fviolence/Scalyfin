#!/bin/bash

# Set default values for environment variables if not provided
GPU_ACCEL=${GPU_ACCEL:-amd}
WATCH_DIRECTORY=${WATCH_DIRECTORY:-/home/cookie/scaler/watch_dir}
OUTPUT_DIRECTORY=${OUTPUT_DIRECTORY:-/home/cookie/scaler/output_dir}

# Determine device mappings
if [ "$GPU_ACCEL" = "amd" ]; then
  DEVICES="# AMD GPU
      - /dev/dri/renderD128:/dev/dri/renderD128"
elif [ "$GPU_ACCEL" = "rockchip" ]; then
  DEVICES="# Rockchip VPU
      - /dev/dri:/dev/dri
      - /dev/dma_heap:/dev/dma_heap
      - /dev/mali0:/dev/mali0
      - /dev/rga:/dev/rga
      - /dev/mpp_service:/dev/mpp_service"
else
  echo "Unsupported GPU_ACCEL: $GPU_ACCEL" >&2
  exit 1
fi

# Export variables for envsubst
export GPU_ACCEL DEVICES WATCH_DIRECTORY OUTPUT_DIRECTORY

# Generate docker-compose.yml
envsubst < docker-compose.template.yml > docker-compose.yml
echo "docker-compose.yml generated successfully."
