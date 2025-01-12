# syntax=docker/dockerfile:1
FROM debian:bookworm-slim
ARG TARGETARCH

# Verify TARGETARCH
RUN echo "Building for TARGETARCH=${TARGETARCH}"

# Install necessary tools and jellyfin-ffmpeg
ARG MALI_PKG_VER="1.9-1_arm64"
ARG MALI_PKG_TAG="v1.9-1-55611b0"
ARG MALI_PKG_CFG="valhall-g610-g13p0-gbm"

ARG GMMLIB_VER=22.5.4
ARG IGC2_VER=2.2.3
ARG IGC2_BUILD=18220
ARG NEO_VER=24.48.31907.7
ARG IGC1_LEGACY_VER=1.0.17537.20
ARG NEO_LEGACY_VER=24.35.30872.22

# Common environment variables
ENV DOTNET_SYSTEM_GLOBALIZATION_INVARIANT="1" \
    LC_ALL="en_US.UTF-8" \
    LANG="en_US.UTF-8" \
    LANGUAGE="en_US:en" \
    JELLYFIN_FFMPEG_PATH="/usr/lib/jellyfin-ffmpeg"

# Install system dependencies
RUN apt-get update \
    && apt-get install --no-install-recommends --no-install-suggests -y \
       ocl-icd-libopencl1 wget ca-certificates gnupg curl apt-transport-https \
    && curl -fsSL https://repo.jellyfin.org/jellyfin_team.gpg.key | gpg --dearmor -o /etc/apt/trusted.gpg.d/debian-jellyfin.gpg \
    && echo "deb [arch=${TARGETARCH}] https://repo.jellyfin.org/master/debian bookworm main" > /etc/apt/sources.list.d/jellyfin.list \
    && apt-get update \
    && apt-get upgrade -y

# ARM64-specific setup
RUN if [ "$TARGETARCH" = "arm64" ]; then \
    mkdir libmali-rockchip \
    && cd libmali-rockchip \
    && wget https://github.com/tsukumijima/libmali-rockchip/releases/download/${MALI_PKG_TAG}/libmali-${MALI_PKG_CFG}_${MALI_PKG_VER}.deb \
    && apt-get install --no-install-recommends --no-install-suggests -y ./*.deb \
    && cd .. \
    && rm -rf libmali-rockchip; \
fi

# AMD64-specific setup
RUN if [ "$TARGETARCH" = "amd64" ]; then \
    mkdir intel-compute-runtime \
    && cd intel-compute-runtime \
    && wget https://github.com/intel/compute-runtime/releases/download/${NEO_VER}/libigdgmm12_${GMMLIB_VER}_amd64.deb \
    && wget https://github.com/intel/intel-graphics-compiler/releases/download/v${IGC2_VER}/intel-igc-core-2_${IGC2_VER}+${IGC2_BUILD}_amd64.deb \
    && wget https://github.com/intel/intel-graphics-compiler/releases/download/v${IGC2_VER}/intel-igc-opencl-2_${IGC2_VER}+${IGC2_BUILD}_amd64.deb \
    && wget https://github.com/intel/compute-runtime/releases/download/${NEO_VER}/intel-opencl-icd_${NEO_VER}_amd64.deb \
    && wget https://github.com/intel/intel-graphics-compiler/releases/download/igc-${IGC1_LEGACY_VER}/intel-igc-core_${IGC1_LEGACY_VER}_amd64.deb \
    && wget https://github.com/intel/intel-graphics-compiler/releases/download/igc-${IGC1_LEGACY_VER}/intel-igc-opencl_${IGC1_LEGACY_VER}_amd64.deb \
    && wget https://github.com/intel/compute-runtime/releases/download/${NEO_LEGACY_VER}/intel-opencl-icd-legacy1_${NEO_LEGACY_VER}_amd64.deb \
    && dpkg -i *.deb \
    && cd .. \
    && rm -rf intel-compute-runtime; \
fi

# FFMPEG and python setup
RUN apt-get install --no-install-recommends --no-install-suggests -y jellyfin-ffmpeg7 \
       openssl locales libfontconfig1 libfreetype6 python3 python3-pip python3-watchdog lsof \
    && sed -i -e 's/# en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen && locale-gen

# Cleanup
RUN apt-get clean autoclean -y \
    && apt-get autoremove -y

# Add FFmpeg to PATH
ENV PATH="$JELLYFIN_FFMPEG_PATH:$PATH"

# Final runtime setup
WORKDIR /app

# Copy scripts
COPY healthcheck.sh /app/healthcheck.sh
COPY scalyfin.py /app/scalyfin.py
CMD ["python3", "/app/scalyfin.py"]

# Add healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 CMD /app/healthcheck.sh