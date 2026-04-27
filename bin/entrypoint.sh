#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="/routeros/data"
SHARED_DIR="/routeros/shared"
IMAGE_FILE="$DATA_DIR/$ROUTEROS_IMAGE"

mkdir -p "$DATA_DIR"
mkdir -p "$SHARED_DIR"

if [ ! -f "$IMAGE_FILE" ]; then
    echo "Creating persistent hard drive for MikroTik..."
    cp "/routeros/$ROUTEROS_IMAGE" "$IMAGE_FILE"
fi

QEMU_BRIDGE='qemubr0'
DUMMY_DHCPD_IP='10.0.0.254'
QEMU_IFUP='/routeros/bin/qemu-ifup'
QEMU_IFDOWN='/routeros/bin/qemu-ifdown'
DHCPD_CONF_FILE='/routeros/dhcpd.conf'

function default_intf() {
    ip -json route show | jq -r '.[] | select(.dst == "default") | .dev'
}

/routeros/bin/generate-dhcpd-conf.py $QEMU_BRIDGE > $DHCPD_CONF_FILE
default_dev=$(default_intf)

ip addr flush dev "$default_dev"

if ! ip link show "$QEMU_BRIDGE" &>/dev/null; then
    ip link add "$QEMU_BRIDGE" type bridge
fi

ip link set dev "$default_dev" master "$QEMU_BRIDGE"
ip link set dev "$default_dev" up
ip link set dev "$QEMU_BRIDGE" up

touch /var/lib/udhcpd/udhcpd.leases
udhcpd -I $DUMMY_DHCPD_IP -f $DHCPD_CONF_FILE &

# Detect KVM availability
KVM_FLAG=""
if [ -e /dev/kvm ]; then
    echo "KVM detected — enabling hardware acceleration."
    KVM_FLAG="-enable-kvm"
else
    echo "WARNING: KVM not available — running in software emulation mode (slower)."
fi

# Graceful shutdown handler
QEMU_PID=""
cleanup() {
    echo "Shutting down MikroTik RouterOS..."
    if [ -n "$QEMU_PID" ]; then
        kill -TERM "$QEMU_PID" 2>/dev/null || true
        wait "$QEMU_PID" 2>/dev/null || true
    fi
    echo "MikroTik stopped."
}
trap cleanup SIGTERM SIGINT

echo "Starting MikroTik RouterOS..."

qemu-system-x86_64 \
    -nographic -serial mon:stdio \
    -m 256 \
    $KVM_FLAG \
    "$@" \
    -hda "$IMAGE_FILE" \
    -drive file=fat:rw:"$SHARED_DIR",format=raw,id=fatdrive \
    -nic tap,id=qemu0,script=$QEMU_IFUP,downscript=$QEMU_IFDOWN &

QEMU_PID=$!
wait $QEMU_PID
