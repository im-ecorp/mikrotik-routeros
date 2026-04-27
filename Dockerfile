FROM alpine:3.19

LABEL org.opencontainers.image.authors="Hossein Sepiol <mhsaeidi81@gmail.com>"
LABEL description="MikroTik RouterOS CHR running inside Docker using QEMU"

ARG ROUTEROS_VERSION=7.21.4

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
    unzip \
    md5sum

RUN mkdir /routeros && \
    wget "${ROUTEROS_URL}" -O /routeros/image.zip && \
    wget "${ROUTEROS_URL}.md5" -O /routeros/image.zip.md5 && \
    cd /routeros && md5sum -c image.zip.md5 && \
    unzip image.zip -d /routeros && \
    rm -f image.zip image.zip.md5

WORKDIR /routeros

COPY bin /routeros/bin/

RUN chmod +x /routeros/bin/*

RUN mkdir -p /routeros/data /routeros/shared

ENTRYPOINT ["/routeros/bin/entrypoint.sh"]
