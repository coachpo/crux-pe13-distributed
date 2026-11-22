ARG REPLAY_IMAGE=crux-local-replay:jammy
FROM ${REPLAY_IMAGE}
USER root
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ninja-build squashfs-tools \
    && rm -r /var/lib/apt/lists
USER runner
