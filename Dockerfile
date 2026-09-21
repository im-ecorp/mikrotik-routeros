FROM alpine:3.24.2@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6

LABEL org.opencontainers.image.authors="Hossein Sepiol <mhsaeidi81@gmail.com>"
LABEL description="MikroTik RouterOS CHR running inside Docker using QEMU"

# Seed selection initializes new disks; it never upgrades an existing guest.
ARG ROUTEROS_VERSION=7.21.4
# Vendor checksum of chr-7.21.4.vdi.zip; other seeds must override both args.
ARG ROUTEROS_SHA256=b4c5036d24a1ae4490758843aae0c8af2b41237dd69832c770621ee72341db0f
ARG WRAPPER_VERSION=dev
ARG SOURCE_REVISION=unknown
ARG SOURCE_URL=https://github.com/im-ecorp/mikrotik-routeros

LABEL org.opencontainers.image.version=$WRAPPER_VERSION \
      org.opencontainers.image.revision=$SOURCE_REVISION \
      org.opencontainers.image.source=$SOURCE_URL \
      io.mikrotik-routeros.seed.version=$ROUTEROS_VERSION \
      io.mikrotik-routeros.seed.sha256=$ROUTEROS_SHA256

ENV ROUTEROS_VERSION=$ROUTEROS_VERSION
ENV ROUTEROS_IMAGE=chr-$ROUTEROS_VERSION.vdi
ENV ROUTEROS_URL=https://download.mikrotik.com/routeros/$ROUTEROS_VERSION/chr-$ROUTEROS_VERSION.vdi.zip

RUN apk add --no-cache \
    wget \
    netcat-openbsd \
    qemu-system-x86_64 \
    busybox-extras \
    iproute2 \
    iputils \
    bridge-utils \
    iptables \
    jq \
    bash \
    python3 \
    unzip

RUN mkdir /routeros && \
    wget --timeout=60 --tries=3 "${ROUTEROS_URL}" -O /routeros/image.zip && \
    printf '%s  /routeros/image.zip\n' "$ROUTEROS_SHA256" | sha256sum -c - && \
    unzip /routeros/image.zip -d /routeros && \
    rm -f /routeros/image.zip

WORKDIR /routeros

COPY bin /routeros/bin/

RUN chmod +x /routeros/bin/*

RUN mkdir -p /routeros/data /routeros/shared

ENTRYPOINT ["/routeros/bin/entrypoint.sh"]
