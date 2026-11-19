# ng-2026-11-05 is the first tag that can sign in after Apple's October 2026 changes (it provisions its
# own anisette identity through a /v3/provisioning_session server). Older tags fail with -22411,
# -80045 or -80046. Both patches below apply cleanly to it.
ARG ALTSERVER_TAG=ng-2026-11-05

# AltServer is built from source with the patches in altserver/. Stock AltServer removes every
# free provisioning profile on the device before installing, which makes iOS forget that the user
# trusted the developer, so each refresh brought back "Untrusted Developer". It also loads each file
# byte by byte and sends it as one packet, so large app binaries time out over Wi-Fi.
FROM ghcr.io/nyamisty/altserver_builder_alpine_aarch64 AS altserver-arm64
FROM ghcr.io/nyamisty/altserver_builder_alpine_amd64 AS altserver-amd64
FROM altserver-${TARGETARCH} AS altserver
ARG ALTSERVER_TAG
COPY altserver/*.patch /tmp/patches/
RUN git clone -q --recursive --depth 1 --shallow-submodules -b "$ALTSERVER_TAG" \
      https://github.com/jaakkopalvaila/AltServer-Linux /src \
 && cd /src && git apply /tmp/patches/*.patch \
 && mkdir build && cd build && make -f ../Makefile -j"$(nproc)" \
 && cp AltServer-* /AltServer

FROM debian:trixie-slim

ARG NETMUXD_TAG=v0.4.3

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libimobiledevice-utils usbmuxd openssl python3 curl ca-certificates coreutils tzdata \
 && rm -rf /var/lib/apt/lists/*

RUN arch="$(uname -m)" \
 && case "$arch" in aarch64|arm64) arch=aarch64 ;; x86_64) ;; *) echo "unsupported arch $arch"; exit 1 ;; esac \
 && curl -fsSL "https://github.com/jkcoxson/netmuxd/releases/download/${NETMUXD_TAG}/netmuxd-${arch}-unknown-linux-gnu.tar.gz" \
      | tar xz -C /usr/local/bin \
 && chmod +x /usr/local/bin/netmuxd

# plumesign (MIT, github.com/bitxeno/PlumeImpactor) pairs, signs and installs for an Apple TV, which has no
# usbmuxd. The release is pinned and checked against its SHA-256. If the download or the check fails, the image
# is still built, only without Apple TV support (the web page then says the helper isn't installed), so
# iPhones and iPads are never affected.
ARG PLUMESIGN_TAG=v2.7.1-patch.11
ARG PLUMESIGN_SHA256_X86_64=91c82918449f4810bff96bc43be746b86db3f5236b505102d2ed7daa0f78f5b8
ARG PLUMESIGN_SHA256_AARCH64=baa4fe218aced4021f51470c00ac95c8e4ebfd2bdfa13f22c2a8ffa310288f59
RUN arch="$(uname -m)"; \
    case "$arch" in aarch64|arm64) arch=aarch64; sum="$PLUMESIGN_SHA256_AARCH64" ;; *) arch=x86_64; sum="$PLUMESIGN_SHA256_X86_64" ;; esac; \
    if curl -fsSL -o /tmp/plumesign.tgz "https://github.com/bitxeno/PlumeImpactor/releases/download/${PLUMESIGN_TAG}/plumesign-linux-${arch}.tar.gz" \
       && echo "${sum}  /tmp/plumesign.tgz" | sha256sum -c - \
       && tar xzf /tmp/plumesign.tgz -C /tmp \
       && install -m 0755 "/tmp/plumesign-linux-${arch}" /usr/local/bin/plumesign; \
    then echo "plumesign ${PLUMESIGN_TAG} installed"; \
    else echo "WARNING: plumesign could not be installed, Apple TV support is off"; fi; \
    rm -f /tmp/plumesign.tgz "/tmp/plumesign-linux-${arch}"

COPY --from=altserver /AltServer /usr/local/bin/AltServer
COPY scripts/ /usr/local/bin/
COPY resigner/ /opt/resigner/

ENV DATA_DIR=/data \
    PYTHONPATH=/opt \
    PYTHONUNBUFFERED=1
WORKDIR /data
EXPOSE 8080
CMD ["python3", "-m", "resigner"]
