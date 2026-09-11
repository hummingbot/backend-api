#!/bin/bash
# Who, if anyone, already owns this host's tailnet interface?
#
# Sourced by setup.sh (to pick a default mode) and by the Makefile (to enforce
# one at deploy time). Prints exactly one word:
#
#   sidecar  our own hummingbot-tailscale container is running
#   native   a tailscaled is running on the host itself
#   none     nothing here owns a tailnet interface
#
# The predicate is deliberately about the RESOURCE, not about which product
# installed it. "Is Condor here?" is the wrong question: the most common real
# conflict is a VPS that already runs Tailscale for the admin's own SSH
# access, and no product check sees that. tailscale0 is the thing actually
# contended, it is path-independent, and on a host where the other component
# lives elsewhere there is simply nothing local to find.
#
# Why it matters: two kernel-mode tailscaled in one network namespace is
# fatal and silent. The second dies with
#   tstun.New("tailscale0"): device or resource busy
# while its container stays "running", so restart:unless-stopped never fires
# and `docker ps` shows it healthy.

# Is our own sidecar container running?
tailnet_sidecar_running() {
    command -v docker >/dev/null 2>&1 &&
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx 'hummingbot-tailscale'
}

# Is a tailscaled running on the HOST ITSELF -- something other than our
# sidecar, and therefore something the sidecar would contend with?
#
# This is the question that actually decides userspace mode, and it cannot be
# answered by "is a sidecar running": both can be true at once, which is the
# normal state on a co-located Condor install after the first deploy.
#
# A bare `pgrep -x tailscaled` cannot tell a host daemon from the sidecar's
# own: on a native-Linux host the PID namespace is the parent of every
# container's, so container processes DO appear in host process lists (under
# Docker Desktop / WSL2 they do not). The cgroup is the portable
# discriminator, and reading it needs no docker permissions -- which matters
# when the installing user cannot run `docker ps` at all.
# procps is not guaranteed on a minimal host, and its absence must not read
# as "no daemon running" -- that is the permissive answer, and it would skip
# the userspace downgrade. /proc is always there on Linux and needs no tools.
_tailnet_tailscaled_pids() {
    if command -v pgrep >/dev/null 2>&1; then
        pgrep -x tailscaled 2>/dev/null
        return
    fi
    local d
    for d in /proc/[0-9]*; do
        [ -r "$d/comm" ] || continue
        if [ "$(cat "$d/comm" 2>/dev/null)" = tailscaled ]; then
            printf '%s\n' "${d#/proc/}"
        fi
    done
}

# Does the tailnet device exist on this host?
#
# /sys/class/net is present on every Linux kernel and reading it needs no
# iproute2, so a host without `ip` still gets a real answer rather than the
# permissive one -- previously a missing `ip` read as "device free" and sent
# a caller straight into the collision this is meant to detect.
#
# No /sys and no tools means this is not Linux, and a network_mode: host
# container cannot hold the host's device there in the first place, so
# "absent" is the correct answer rather than a guess.
tailnet_device_present() {
    [ -e /sys/class/net/tailscale0 ] && return 0
    command -v ip >/dev/null 2>&1 && ip link show tailscale0 >/dev/null 2>&1 && return 0
    command -v ifconfig >/dev/null 2>&1 && ifconfig tailscale0 >/dev/null 2>&1 && return 0
    return 1
}

_tailnet_in_container() {
    [ -r "/proc/$1/cgroup" ] &&
    grep -qE '(docker|containerd|libpod|kubepods)' "/proc/$1/cgroup" 2>/dev/null
}

# A tailscaled that is NOT inside a container. No /proc (macOS) means we
# cannot say it is containerised, and there a container's processes are not
# visible here anyway -- so treating it as a host daemon is right.
_tailnet_host_tailscaled() {
    local pid
    for pid in $(_tailnet_tailscaled_pids); do
        _tailnet_in_container "$pid" && continue
        return 0
    done
    return 1
}

# A containerised tailscaled that is NOT our own sidecar -- someone else's
# host-networked Tailscale container, which contends for tailscale0 exactly
# like a host daemon would. Identified by container id in the cgroup path,
# since the sidecar's tailscaled is a child of containerboot and so does not
# match the container's own main PID.
_tailnet_foreign_container_tailscaled() {
    local ours pid
    ours="$(docker inspect -f '{{.Id}}' hummingbot-tailscale 2>/dev/null || true)"
    for pid in $(_tailnet_tailscaled_pids); do
        _tailnet_in_container "$pid" || continue
        if [ -n "$ours" ] && grep -q "$ours" "/proc/$pid/cgroup" 2>/dev/null; then
            continue
        fi
        return 0
    done
    return 1
}

tailnet_has_native() {
    # 1. The host's own tailscaled socket. Definitive: a sidecar keeps its
    #    socket inside its container and cannot answer here.
    if command -v tailscale >/dev/null 2>&1 && tailscale status >/dev/null 2>&1; then
        return 0
    fi
    # 2. A tailscaled process outside any container. Also definitive, and it
    #    still works when the host has no tailscale CLI installed.
    _tailnet_host_tailscaled && return 0
    # 3. tailscale0 exists. A kernel-mode sidecar runs with network_mode:
    #    host and creates that device in the host's own namespace, so OUR
    #    sidecar is the one owner that is not a conflict. Anything else --
    #    another host-networked Tailscale container, or an owner that cannot
    #    be identified -- contends just as a host daemon would.
    #
    #    The asymmetry decides the unknown case: answering "native" when it is
    #    only our own sidecar costs an unnecessary userspace downgrade, and
    #    userspace serves port 8000 perfectly well. Answering "not native"
    #    when something else owns the device costs a wedged sidecar and an
    #    unreachable API. So anything unproven resolves toward native.
    if tailnet_device_present; then
        if tailnet_sidecar_running && ! _tailnet_foreign_container_tailscaled; then
            return 1
        fi
        return 0
    fi
    return 1
}

# Native is reported FIRST when both are present. Ownership of the device is
# the fact that matters, and the sidecar is the thing that has to yield to it;
# reporting "sidecar" there would hide the contention it is meant to reveal.
tailnet_state() {
    tailnet_has_native && { echo native; return; }
    tailnet_sidecar_running && { echo sidecar; return; }
    echo none
}

# Resolve the effective mode from .env plus what is actually on the host.
#
# TAILSCALE_MODE is authoritative when set. TAILSCALE_ENABLED remains the
# historical switch and keeps working: enabled-but-unspecified means "put the
# API on the tailnet, you pick how", and how depends on whether this host
# already has a daemon.
#
# Echoes: none | host | sidecar
tailnet_mode() {
    local declared="${TAILSCALE_MODE:-}"
    local enabled="${TAILSCALE_ENABLED:-false}"
    local state; state="$(tailnet_state)"

    case "$declared" in
        none|host|sidecar) echo "$declared"; return ;;
        "") : ;;
        *) echo "[ERROR] TAILSCALE_MODE must be none, host or sidecar (got: $declared)" >&2; return 1 ;;
    esac

    case "$enabled" in
        [Tt]rue|[Yy]es|1) : ;;
        *) echo none; return ;;
    esac

    # Enabled, mode unspecified. A host that already runs its own tailscaled
    # gets `host`: joining the tailnet a second time from the same machine
    # would contend for the same device for no gain.
    if [ "$state" = native ]; then echo host; else echo sidecar; fi
}

# Should the sidecar run in userspace (netstack) mode?
#
# This is the safety net that makes correctness independent of detection being
# right. Netstack creates no TUN device and touches no host routes or
# iptables, so it coexists with a kernel-mode daemon unconditionally --
# verified on a live tailnet: a userspace node registered alongside a
# kernel-mode one in the same netns and served a loopback-bound port to the
# other node, with a single tailscale0 between them.
#
# The cost is real but small: a userspace node cannot route for the host, only
# accept inbound. Serving port 8000 is exactly that, so nothing is lost here.
tailnet_needs_userspace() {
    # Deliberately NOT `tailnet_state = native`: when a native daemon and our
    # sidecar are both up, that is precisely when the downgrade is required,
    # and a composite state that reported "sidecar" there would skip it.
    tailnet_has_native
}

# The name this node is ACTUALLY registered under, or empty if it cannot be
# asked.
#
# Tailscale suffixes a hostname that is already taken: ask for
# "hummingbot-api" on a tailnet that has one, and the node comes up as
# "hummingbot-api-1". TAILSCALE_HOSTNAME therefore records what was
# REQUESTED, and is the wrong thing to print in a URL -- on a shared team
# tailnet it names somebody else's machine, which may well answer, so the
# mistake surfaces as an authentication error rather than a name error.
#
# `--self=true --peers=false` prints exactly one line, whose second field is
# the DNS label the control plane assigned. No JSON parser needed, which
# matters in a sidecar image that has neither python nor jq.
#
# Pass the command that reaches the right daemon:
#   tailnet_node_name                                    # this host
#   tailnet_node_name docker exec hummingbot-tailscale tailscale   # sidecar
tailnet_node_name() {
    local line
    if [ "$#" -eq 0 ]; then
        set -- tailscale
    fi
    line="$("$@" status --self=true --peers=false 2>/dev/null | grep -v '^[[:space:]]*$' | head -1)" || return 1
    [ -n "$line" ] || return 1
    # 100.74.198.21  hummingbot-api-2  michaeld@  linux  -
    printf '%s' "$line" | awk '{print $2}'
}
