#!/usr/bin/env bash
# Installer / diagnostic helper for the Chione E4 temperature display.
#
# Works on any systemd distribution (built on Bazzite, verified on Debian 13);
# it detects the package manager only to print the right install hint, never to
# decide behaviour. Everything it does is reversible with `./install.sh remove`.
#
#   ./install.sh check              inspect this machine, change nothing
#   ./install.sh udev               install the udev access rule (needs sudo)
#   ./install.sh udev --group plugdev   ... granting a group instead of uaccess
#   ./install.sh service            install + start the systemd user service
#   ./install.sh all                udev, then service
#   ./install.sh print-rule         show the rule that would be installed
#   ./install.sh remove             undo both
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$REPO_DIR/chione_temp_display.py"
RULE_SRC="$REPO_DIR/udev/70-chione.rules"
RULE_DST="/etc/udev/rules.d/70-chione.rules"
UNIT_NAME="chione-temp.service"
UNIT_DST="$HOME/.config/systemd/user/$UNIT_NAME"
# $USER is not set in every environment (some containers, su -c, cron), and
# `set -u` would abort on it.
USER="${USER:-$(id -un)}"
VID="1b80"
PID="b53c"

ok()   { printf '  \033[32m+\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31mx\033[0m %s\n' "$*"; }
head_() { printf '\n\033[1m%s\033[0m\n' "$*"; }

die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# Must be global: the EXIT trap runs outside the function scope, where a
# `local` would be unset and `set -u` would abort the cleanup itself.
TMP_RULE=""
cleanup() { [ -n "${TMP_RULE:-}" ] && rm -f "$TMP_RULE"; return 0; }
trap cleanup EXIT

# Runs "$@" as root. Prefers sudo, but sudo cannot ask for a password when
# there is no terminal attached (a graphical launcher, an editor's embedded
# shell), and fails with a confusing error instead. In that case fall back to
# pkexec, which prompts through the desktop's polkit agent.
as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
        sudo "$@"                       # credentials already cached
    elif command -v sudo >/dev/null 2>&1 && [ -t 0 ] && [ -t 1 ]; then
        sudo "$@"                       # a terminal exists: sudo can prompt
    elif command -v pkexec >/dev/null 2>&1 && pgrep -x polkitd >/dev/null 2>&1; then
        warn "no terminal for sudo to prompt on; asking via pkexec instead"
        pkexec "$@"
    else
        die "root access is needed but there is no way to ask for a password here. Re-run this from a terminal."
    fi
}

# Package-manager hint only. Behaviour never branches on the distribution.
pkg_hint() {
    if   command -v apt-get >/dev/null 2>&1; then echo "sudo apt install $1"
    elif command -v dnf     >/dev/null 2>&1; then echo "sudo dnf install $1"
    elif command -v pacman  >/dev/null 2>&1; then echo "sudo pacman -S $1"
    elif command -v zypper  >/dev/null 2>&1; then echo "sudo zypper install $1"
    else echo "install the '$1' package"
    fi
}

find_node() {
    local uevent name
    for uevent in /sys/class/hidraw/hidraw*/device/uevent; do
        [ -r "$uevent" ] || continue
        if grep -qi "HID_ID=.*:0000${VID}:0000${PID}" "$uevent"; then
            name="$(basename "$(dirname "$(dirname "$uevent")")")"
            echo "/dev/$name"
            return 0
        fi
    done
    return 1
}

cmd_check() {
    local rc=0 node

    head_ "System"
    if [ -r /etc/os-release ]; then
        # shellcheck disable=SC1091
        ok "$(. /etc/os-release && echo "$PRETTY_NAME")  kernel $(uname -r)"
    fi
    if [ -d /run/systemd/system ]; then ok "systemd is running"
    else warn "systemd not detected: the service step will not apply"; fi

    head_ "Python"
    if command -v python3 >/dev/null 2>&1; then
        ok "$(python3 --version) at $(command -v python3)"
    else
        bad "python3 not found: $(pkg_hint python3)"; rc=1
    fi
    [ -f "$SCRIPT" ] && ok "driver script: $SCRIPT" || { bad "missing $SCRIPT"; rc=1; }

    head_ "Cooler (USB $VID:$PID)"
    if node="$(find_node)"; then
        ok "found at $node"
        if [ -r "$node" ] && [ -w "$node" ]; then
            ok "read/write access granted"
        else
            bad "no access to $node yet: run './install.sh udev', then replug"
            rc=1
        fi
    else
        bad "not found. Is it plugged in? List USB devices with 'lsusb' ($(pkg_hint usbutils))"
        rc=1
    fi

    head_ "CPU temperature sensor"
    if command -v python3 >/dev/null 2>&1 && [ -f "$SCRIPT" ]; then
        local sensors
        sensors="$(python3 "$SCRIPT" --list-hwmon 2>/dev/null | grep 'default$' || true)"
        if [ -n "$sensors" ]; then
            printf '%s\n' "$sensors" | while IFS=$'\t' read -r drv file val _; do
                ok "$drv $file = $val"
            done
        else
            bad "no CPU hwmon driver found (k10temp / coretemp / zenpower)"
            warn "Intel: sudo modprobe coretemp   |   AMD: sudo modprobe k10temp"
            warn "make it permanent: echo coretemp | sudo tee /etc/modules-load.d/coretemp.conf"
            rc=1
        fi
    fi

    head_ "Device access method"
    if [ -f "$RULE_DST" ]; then ok "udev rule installed at $RULE_DST"
    else warn "udev rule not installed (run './install.sh udev')"; fi
    if command -v loginctl >/dev/null 2>&1 &&
       loginctl show-user "$USER" -p State 2>/dev/null | grep -q 'State=active'; then
        ok "active logind session: the default 'uaccess' rule applies"
    else
        warn "no active logind session: prefer './install.sh udev --group plugdev'"
    fi

    head_ "Service"
    if [ -f "$UNIT_DST" ]; then
        ok "unit installed at $UNIT_DST"
        systemctl --user is-active --quiet "$UNIT_NAME" \
            && ok "service is running" \
            || warn "service is not running (systemctl --user status $UNIT_NAME)"
    else
        warn "service not installed (run './install.sh service')"
    fi

    if [ "$rc" -eq 0 ]; then
        head_ "Ready. Run it with: python3 $SCRIPT"
    else
        head_ "Fix the items marked x above, then re-run './install.sh check'."
    fi
    return "$rc"
}

# Parses the shared --group option. Sets RULE_GROUP.
RULE_GROUP=""
parse_rule_opts() {
    RULE_GROUP=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --group) RULE_GROUP="${2:-}"; [ -n "$RULE_GROUP" ] || die "--group needs a name"; shift 2
                # The name is spliced into a sed expression and a udev rule.
                [[ "$RULE_GROUP" =~ ^[a-z_][a-z0-9_-]*$ ]] || die "invalid group name: $RULE_GROUP" ;;
            *) die "unknown option: $1" ;;
        esac
    done
}

# Writes the rule to install into $1. Pure text work, no privileges, so the
# result can be inspected with `./install.sh print-rule` before anything is
# touched on the system.
build_rule() {
    local out="$1"
    [ -f "$RULE_SRC" ] || die "missing $RULE_SRC"
    if [ -n "$RULE_GROUP" ]; then
        # Swap the active line: uaccess out, the group variant in.
        sed -e '/TAG+="uaccess"/s/^/#/' \
            -e "s|^#\(SUBSYSTEM==\"hidraw\".*GROUP=\)\"plugdev\"|\1\"$RULE_GROUP\"|" \
            "$RULE_SRC" > "$out"
        grep -q "^SUBSYSTEM.*GROUP=\"$RULE_GROUP\"" "$out" \
            || die "failed to build the group rule variant"
    else
        cp "$RULE_SRC" "$out"
    fi
}

cmd_print_rule() {
    parse_rule_opts "$@"
    TMP_RULE="$(mktemp)"
    build_rule "$TMP_RULE"
    grep -v '^\s*#' "$TMP_RULE" | grep -v '^\s*$'
}

cmd_udev() {
    parse_rule_opts "$@"

    if [ -n "$RULE_GROUP" ]; then
        getent group "$RULE_GROUP" >/dev/null 2>&1 \
            || die "group '$RULE_GROUP' does not exist. Create it with: sudo groupadd -f $RULE_GROUP && sudo usermod -aG $RULE_GROUP $USER"
        id -nG "$USER" | tr ' ' '\n' | grep -qx "$RULE_GROUP" \
            || warn "you are not in group '$RULE_GROUP' yet: sudo usermod -aG $RULE_GROUP $USER (then log out and back in)"
        echo "Installing rule granting group '$RULE_GROUP'."
    else
        echo "Installing rule granting the active desktop user (uaccess)."
    fi

    TMP_RULE="$(mktemp)"
    build_rule "$TMP_RULE"

    # One elevation for all three steps, so pkexec prompts once rather than
    # three times. Paths are passed as arguments, never interpolated into the
    # shell text.
    as_root sh -c '
        set -e
        install -m 0644 -o root -g root "$1" "$2"
        udevadm control --reload-rules
        udevadm trigger --subsystem-match=hidraw
    ' _ "$TMP_RULE" "$RULE_DST" || die "failed to install the rule"
    ok "installed $RULE_DST"

    local node
    if node="$(find_node)"; then
        if [ -r "$node" ] && [ -w "$node" ]; then
            ok "access to $node is active"
        else
            warn "no access to $node yet. Replug the cooler (or reboot); a rule"
            warn "cannot retroactively change an already-open device node."
        fi
    fi
}

cmd_service() {
    # A user service belongs to the desktop user: under sudo the unit lands in
    # /root/.config and systemctl --user has no bus, so nothing gets installed.
    [ "$(id -u)" -ne 0 ] || die "run 'service' without sudo (it is a per-user service)"
    [ -d /run/systemd/system ] || die "systemd is not running on this machine"
    command -v python3 >/dev/null 2>&1 || die "python3 not found: $(pkg_hint python3)"
    [ -f "$SCRIPT" ] || die "missing $SCRIPT"

    mkdir -p "$(dirname "$UNIT_DST")"
    # Generated rather than copied so ExecStart carries this checkout's real
    # path: the shipped unit assumes ~/chione-display-linux, which is
    # rarely true.
    # systemd reads % as a specifier (and $ as a variable in ExecStart), so
    # escape them in paths that go into the unit.
    local script_u="${SCRIPT//%/%%}"
    local repo_u="${REPO_DIR//%/%%}"
    local py_u
    py_u="$(command -v python3)"
    py_u="${py_u//%/%%}"; py_u="${py_u//\$/\$\$}"
    local script_x="${SCRIPT//%/%%}"; script_x="${script_x//\$/\$\$}"
    cat > "$UNIT_DST" <<EOF
[Unit]
Description=Gamdias Chione E4 temperature display (real CPU temp via hwmon)
# Generated by install.sh from $REPO_DIR on $(date -Is).
# Re-run './install.sh service' after moving the repository.
#
# If the script isn't there (a repo on a removable or late-mounting disk),
# skip the start quietly instead of failing. Without this the interpreter
# exits 2 for "no such file", which RestartPreventExitStatus=2 below reads as
# permanent misconfiguration and the service stays dead until reset-failed.
ConditionPathExists=$script_u

[Service]
# Quoted so a checkout path containing spaces stays a single argument.
ExecStart="$py_u" "$script_x"
WorkingDirectory=$repo_u
Restart=on-failure
RestartSec=5
# Stop retrying on permanent misconfiguration (exit code 2): no matching
# hwmon sensor. (A missing device permission is retried, not fatal.)
RestartPreventExitStatus=2

[Install]
WantedBy=default.target
EOF
    ok "wrote $UNIT_DST"
    case "$REPO_DIR" in
        "$HOME"/*) ;;
        *) warn "the repo lives outside your home ($REPO_DIR)."
           warn "if that path is a removable or late-mounting disk, the service"
           warn "will skip starting until it is mounted. Moving the repo under"
           warn "\$HOME and re-running './install.sh service' avoids that." ;;
    esac
    systemctl --user daemon-reload
    systemctl --user enable "$UNIT_NAME"
    # restart, not "enable --now": on a re-run the service is already active
    # and --now would leave the old unit's process in place, silently ignoring
    # the file just written. restart also starts it when it is stopped.
    systemctl --user restart "$UNIT_NAME"
    sleep 1
    if systemctl --user is-active --quiet "$UNIT_NAME"; then
        ok "service started"
    else
        warn "service did not start cleanly:"
        systemctl --user status "$UNIT_NAME" --no-pager --lines=15 || true
    fi
    echo
    echo "Logs:   journalctl --user -u $UNIT_NAME -f"
    echo "Stop:   systemctl --user disable --now $UNIT_NAME"
    echo
    echo "To keep it running without a graphical login, enable lingering:"
    echo "  sudo loginctl enable-linger $USER"
    echo "In that case install the udev rule with --group (uaccess needs an"
    echo "active session): ./install.sh udev --group plugdev"
}

cmd_remove() {
    if [ -f "$UNIT_DST" ]; then
        systemctl --user disable --now "$UNIT_NAME" 2>/dev/null || true
        # disable fails silently without a user bus, so drop the enable symlink by hand
        rm -f "$(dirname "$UNIT_DST")/default.target.wants/$UNIT_NAME"
        rm -f "$UNIT_DST"
        systemctl --user daemon-reload 2>/dev/null \
            || warn "systemctl --user daemon-reload failed (no user bus?)"
        ok "removed $UNIT_DST"
    else
        warn "no unit at $UNIT_DST"
    fi
    if [ -f "$RULE_DST" ]; then
        as_root sh -c 'rm -f "$1" && udevadm control --reload-rules && udevadm trigger --subsystem-match=hidraw' _ "$RULE_DST"
        ok "removed $RULE_DST"
    else
        warn "no rule at $RULE_DST"
    fi
}

case "${1:-check}" in
    check)      cmd_check ;;
    udev)       shift; cmd_udev "$@" ;;
    print-rule) shift; cmd_print_rule "$@" ;;
    service)    cmd_service ;;
    all)        shift; cmd_udev "$@"; cmd_service ;;
    remove)     cmd_remove ;;
    -h|--help|help)
        sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ;;
    *) die "unknown command '${1}'. Try: check | udev | service | all | remove" ;;
esac
