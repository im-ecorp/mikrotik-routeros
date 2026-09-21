#!/usr/bin/env bash
set -euo pipefail
umask 077

# Validate private Docker bridge topology before touching disks or interfaces.
python3 /routeros/bin/runtime.py prepare
DATA_DIR=/routeros/data
SHARED_DIR=/routeros/shared
mkdir -p "$SHARED_DIR"
IMAGE_FILE=$(python3 /routeros/bin/init-disk.py "$DATA_DIR" "/routeros/$ROUTEROS_IMAGE")

ip addr flush dev eth0
ip link add qemubr0 type bridge
ip link set dev eth0 master qemubr0
ip link set dev eth0 up
ip link set dev qemubr0 up
: > /run/routeros/udhcpd.leases

ACCEL=()
if [[ -r /dev/kvm && -w /dev/kvm ]]; then
    ACCEL=(-enable-kvm)
else
    echo 'KVM unavailable; using QEMU software emulation.'
fi
# Serial and QMP stay private UNIX sockets, never Docker logs or TCP listeners.
# runtime.py is PID 1, reaps both children, and requests bounded guest poweroff.
exec python3 /routeros/bin/runtime.py supervise qemu-system-x86_64 \
    -display none -monitor none \
    -serial unix:/run/routeros/serial.sock,server=on,wait=off \
    -qmp unix:/run/routeros/qmp.sock,server=on,wait=off \
    -m 256 "${ACCEL[@]}" "$@" \
    -drive "file=$IMAGE_FILE,format=vdi,if=ide" \
    -drive "file=fat:rw:$SHARED_DIR,format=raw,id=fatdrive" \
    -nic tap,id=qemu0,script=/routeros/bin/qemu-ifup,downscript=/routeros/bin/qemu-ifdown
