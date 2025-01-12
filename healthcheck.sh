#!/bin/bash

STATUS_FILE="/tmp/scalyfin_status"

# Check if the status file exists and was updated in the last 30 seconds
if [ -f "$STATUS_FILE" ] && [ "$(find "$STATUS_FILE" -mmin -0.5 2>/dev/null)" ]; then
    exit 0  # Healthy
else
    exit 1  # Unhealthy
fi