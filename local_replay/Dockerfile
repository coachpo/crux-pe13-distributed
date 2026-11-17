FROM ubuntu:22.04@sha256:9abcaffc92a5279fff3a6a41635c0800ea33e528425fb389658806476e7defde
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    python3 sudo libncurses5 flex bison libssl-dev libelf-dev zstd ccache \
    build-essential libxml2-utils zip unzip cpio lz4 xz-utils bc rsync ca-certificates \
    u-boot-tools device-tree-compiler e2fsprogs android-sdk-libsparse-utils gcc-aarch64-linux-gnu libc6-dev-arm64-cross \
    && useradd --create-home --uid 1001 --shell /bin/bash runner \
    && printf 'runner ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/runner \
    && chmod 0440 /etc/sudoers.d/runner
USER runner
WORKDIR /home/runner
