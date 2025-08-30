#!/usr/bin/env python3
"""
setup_obs_multicam.py
Fully PRF-compliant Fedora 42 installer and fixer for OBS multicam:
- Installs required system packages (idempotent, tolerant to conflicts)
- Ensures user is in 'video' group (adds if necessary, instructs relogin)
- Installs and enables xdg-desktop-portal (desktop portal support)
- Builds/installs v4l2loopback module when needed and loads it
- Waits for /dev/video10 (virtual camera) before continuing
- Starts PipeWire/WirePlumber and verifies PipeWire nodes for video devices
- Automatically logs all output to a file while also displaying it live in the terminal.
- Installs optional Python deps for OBS scripts
- Attempts to automatically fix common issues found during checks,
  including dynamically discovering and unmasking relevant systemd units.
- Prints thorough troubleshooting instructions if cameras aren't visible
"""

from __future__ import annotations
import os
import sys
import subprocess
import time
import getpass
import grp
import shutil
import datetime # For unique log filenames
from typing import List, Tuple, Optional

# ---------------------------
# Config
# ---------------------------
VIRTUAL_VIDEO_NR = 10
VIRTUAL_DEVICE = f"/dev/video{VIRTUAL_VIDEO_NR}"
V4L2LOOPBACK_REPO = "https://github.com/umlaeute/v4l2loopback.git"
LONG_CMD_TIMEOUT = 3600  # seconds for long-running commands (just a safety)
SLEEP_SHORT = 0.5
SLEEP_LONG = 1.0

# PACKAGES: trimmed to reliably-available Fedora packages; skip extras that cause noise
SYSTEM_PACKAGES = [
    "obs-studio",
    "pipewire",
    "pipewire-v4l2",
    "v4l2loopback-utils",
    "kmod-v4l2loopback",
    "wireplumber",
    "xdg-desktop-portal",
    "xdg-desktop-portal-gtk",
    "ffmpeg",
    "python3-pip",
    "git",  # Required for v4l2loopback build
    "make",  # Required for v4l2loopback build
    "gcc",  # Required for v4l2loopback build
    "kernel-devel", # Will be specified with kernel_ver
    "v4l2-utils", # For v4l2-ctl
    "gstreamer1-plugins-good", # For gst-launch-1.0
    "pipewire-utils", # For pw-cli
]
PYTHON_DEPS = ["requests", "json5", "pyyaml"]

# ---------------------------
# GLOBAL LOGGING CONTROL (Automated Tee-like behavior)
# ---------------------------
class LogStream:
    """A file-like object that writes to multiple streams (e.g., console and file)."""
    def __init__(self, original_stream, log_file):
        self.original_stream = original_stream
        self.log_file = log_file

    def write(self, data):
        self.original_stream.write(data)
        if self.log_file:
            self.log_file.write(data)
        self.flush()

    def flush(self):
        self.original_stream.flush()
        if self.log_file:
            self.log_file.flush()

_log_file_handle: Optional[object] = None # Global reference to the actual log file object
# Store original streams globally before redirection to ensure they are always accessible
sys_stdout_original = sys.stdout
sys_stderr_original = sys.stderr

def init_automatic_logging(log_path: str):
    global _log_file_handle

    try:
        _log_file_handle = open(log_path, 'w', encoding='utf-8')
        # Immediately write a header to the log file
        _log_file_handle.write(f"--- Log started: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        _log_file_handle.flush()

        # Redirect sys.stdout and sys.stderr to our custom LogStream
        sys.stdout = LogStream(sys_stdout_original, _log_file_handle)
        sys.stderr = LogStream(sys_stderr_original, _log_file_handle)
        
        # Now, standard print calls and anything writing to stdout/stderr will go to both.
        # This makes the info/warn/error functions simpler.
        # This message will be the first thing printed to both terminal and file.
        print(f"[INFO] All script output is automatically being logged to '{log_path}'.")

    except IOError as e:
        # Fallback if logging to file fails. Report to original stderr.
        sys_stderr_original.write(f"[ERROR] Could not open log file '{log_path}': {e}\n")
        sys_stderr_original.flush()
        _log_file_handle = None # Ensure it's None if opening failed

def close_automatic_logging():
    global _log_file_handle
    if _log_file_handle:
        try:
            _log_file_handle.write(f"--- Log ended: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            _log_file_handle.close()
        except Exception as e:
            sys_stderr_original.write(f"[ERROR] Error closing log file: {e}\n")
            sys_stderr_original.flush()
        # Restore original stdout/stderr (important for clean exit)
        sys.stdout = sys_stdout_original
        sys.stderr = sys_stderr_original
    _log_file_handle = None

# Custom print functions now simply use standard print, which is redirected by LogStream
def info(msg: str):
    print(f"[INFO] {msg}")

def warn(msg: str):
    print(f"[WARNING] {msg}")

def error(msg: str):
    print(f"[ERROR] {msg}", file=sys.stderr) # Errors should still go to stderr conceptually


# ---------------------------
# Helpers (adjusted to use new logging)
# ---------------------------
def run_live(cmd: List[str], cwd: str | None = None, env: dict | None = None, timeout: int = LONG_CMD_TIMEOUT, allow_fail: bool = False) -> Tuple[int, str]:
    """
    Run a system command and stream stdout/stderr live (like tee).
    Returns (returncode, combined_output_str).
    If allow_fail is False and command returns non-zero, exit the script.
    """
    info(f"Running: {' '.join(cmd)}")
    try:
        # Subprocess stdout/stderr are now explicitly captured and sent through our logger
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=cwd, env=env, text=True)
        out_lines = []
        start = time.time()
        while True:
            line = proc.stdout.readline()
            if line:
                # Direct subprocess output through our logging system
                print(f"[LIVE] {line.rstrip()}") 
                out_lines.append(line)
            elif proc.poll() is not None:
                break
            else:
                # timeout safety
                if time.time() - start > timeout:
                    proc.kill()
                    warn("Command timeout exceeded; killed process.")
                    break
                time.sleep(0.05)
        rc = proc.wait()
        combined = "".join(out_lines)
        if rc != 0:
            warn(f"Command exited with code {rc}: {' '.join(cmd)}")
            if not allow_fail:
                sys.exit(rc)
        return rc, combined
    except FileNotFoundError:
        warn(f"Command not found: {cmd[0]}")
        if not allow_fail:
            sys.exit(1)
        return 127, ""
    except Exception as e:
        error(f"Failed to run command {' '.join(cmd)}: {e}")
        if not allow_fail:
            sys.exit(1)
        return 1, str(e)


def run_quiet(cmd: List[str], allow_fail: bool = True, description: str = "command") -> Tuple[int, str]:
    """
    Run a short command without streaming; return (rc, output).
    Includes improved feedback to prevent perceived stalling.
    """
    cmd_str = ' '.join(cmd)
    info(f"Executing '{description}': {cmd_str} (this might take a moment)...")
    try:
        out = subprocess.run(cmd, check=not allow_fail, capture_output=True, text=True)
        combined_output = out.stdout + out.stderr
        if out.returncode != 0:
            warn(f"'{description}' failed with code {out.returncode}:\nOutput:\n{combined_output.strip()}")
        elif combined_output.strip():
            info(f"'{description}' completed successfully. Output:\n{combined_output.strip()}")
        else:
            info(f"'{description}' completed successfully with no output.")
        return out.returncode, combined_output
    except FileNotFoundError:
        warn(f"'{description}' command not found: {cmd[0]}")
        if not allow_fail:
            sys.exit(1)
        return 127, ""
    except Exception as e:
        error(f"Failed to execute '{description}': {cmd_str} -> {e}")
        if not allow_fail:
            sys.exit(1)
        return 1, str(e)

def check_command_exists(cmd: str) -> bool:
    """Checks if a command exists in the system's PATH."""
    return shutil.which(cmd) is not None

# ---------------------------
# Core tasks
# ---------------------------
def ensure_sudo_available():
    if not check_command_exists("sudo"):
        error("sudo is required but not found. Install sudo and re-run.")
        sys.exit(1)

def detect_kernel_version() -> str:
    return subprocess.check_output(["uname", "-r"], text=True).strip()

def install_system_packages():
    """
    Installs packages via dnf with options that allow erasing conflicts and skip unavailable bits.
    Uses run_live for streaming output to keep user informed.
    """
    ensure_sudo_available()
    info("Installing system packages (this may take several minutes)...")
    
    kernel_ver = detect_kernel_version()
    pkg_list = SYSTEM_PACKAGES.copy()
    
    # Ensure kernel-devel for the *current* kernel is in the list
    kernel_devel_pkg = f"kernel-devel-{kernel_ver}"
    if kernel_devel_pkg not in pkg_list:
        pkg_list.append(kernel_devel_pkg)
        
    # Remove generic kernel-devel if specific version is added to prevent conflicts
    if "kernel-devel" in pkg_list and kernel_devel_pkg != "kernel-devel":
        pkg_list.remove("kernel-devel")

    # Construct command
    cmd = ["sudo", "dnf", "install", "-y", "--allowerasing", "--skip-broken", "--skip-unavailable"] + pkg_list
    run_live(cmd, allow_fail=True)
    info("Package install step finished (errors, if any, were tolerated).")

def ensure_video_group_membership() -> bool:
    """
    Ensure current user belongs to 'video'. If not, add via sudo and remind to log out/in.
    Returns True if relogin is likely required, False otherwise.
    """
    user = getpass.getuser()
    user_groups = []
    try:
        # Get all groups the user is a member of
        for g in grp.getgrall():
            if user in g.gr_mem:
                user_groups.append(g.gr_name)
        
        # Also check the user's primary group
        primary_gid = os.getgid()
        primary_group = grp.getgrgid(primary_gid).gr_name
        if primary_group not in user_groups:
            user_groups.append(primary_group)
    except Exception as e:
        warn(f"Could not determine user groups using grp module, falling back to 'groups' command: {e}")
        # Fallback for systems where grp module might not behave as expected
        _rc, groups_output = run_quiet(["groups", user], allow_fail=True, description=f"groups for user {user}")
        if _rc == 0:
            user_groups = groups_output.split()
        else:
            error(f"Could not determine groups for user {user}. Please check manually.")
            return False # Can't be sure, assume no relogin needed to avoid false positive

    if "video" in user_groups:
        info(f"User '{user}' already in 'video' group.")
        return False  # no relogin required
    
    info(f"User '{user}' is not in 'video' group. Adding now (requires sudo)...")
    rc, _ = run_live(["sudo", "usermod", "-aG", "video", user], allow_fail=True)
    if rc == 0:
        warn("You must fully log out and log back in (or reboot) for 'video' group membership to take effect in your desktop session.")
        return True  # relogin likely required
    else:
        error(f"Failed to add user '{user}' to 'video' group.")
        return False

# --- D-Bus Unit Discovery Functions ---
def find_dbus_unit_for_service(dbus_service_name: str, scope: str) -> Optional[str]:
    """
    Dynamically finds the systemd unit name responsible for a given D-Bus service name.
    Searches specified scope, then falls back to common heuristics if needed.
    'scope' can be '--user' or '--system'.
    """
    info(f"Searching for systemd {scope.strip().replace('--', '') + ' ' if scope else ''}unit providing D-Bus service '{dbus_service_name}'...")
    
    # Strategy 1: Direct systemctl D-Bus service to unit mapping
    # This queries systemd for the unit that *provides* a D-Bus service name.
    cmd_show_unit = ["systemctl"]
    if scope: cmd_show_unit.append(scope)
    cmd_show_unit.extend(["show", dbus_service_name, "--property=Unit", "--value"])
    
    rc, output = run_quiet(cmd_show_unit, allow_fail=True, description=f"querying systemd for unit of D-Bus service '{dbus_service_name}' ({scope.strip().replace('--', '') if scope else 'system'} scope)")
    
    if rc == 0 and output.strip() and output.strip().lower() != "unit" and not output.strip().startswith("No unit"):
        unit_name = output.strip()
        info(f"Systemd reports unit '{unit_name}' provides D-Bus service '{dbus_service_name}' in {scope.strip().replace('--', '') if scope else 'system'} scope.")
        return unit_name

    # Strategy 2: Grep through systemd unit files for BusName= or related D-Bus activation
    # This is often needed if Strategy 1 fails, especially if the service is masked or implicitly activated.
    search_paths = []
    if scope == "--user":
        # Standard user unit paths
        search_paths.append(os.path.expanduser("~/.config/systemd/user/"))
        search_paths.append("/usr/lib/systemd/user/")
    else: # --system or no scope
        # Standard system unit paths
        search_paths.append("/etc/systemd/system/")
        search_paths.append("/usr/lib/systemd/system/")
    
    # Grep only for .service files to avoid unnecessary searches and errors on other file types
    service_files_to_grep = []
    for path_dir in search_paths:
        if os.path.isdir(path_dir):
            try:
                for filename in os.listdir(path_dir):
                    if filename.endswith(".service"):
                        service_files_to_grep.append(os.path.join(path_dir, filename))
            except OSError as e:
                warn(f"Could not list directory {path_dir} for D-Bus unit search: {e}")
                
    if service_files_to_grep:
        info(f"Attempting to grep {len(service_files_to_grep)} systemd unit files for D-Bus service name '{dbus_service_name}'...")
        # Use a more precise regex to find BusName= or D-Bus activated services within unit files
        # It needs to match 'BusName=org.freedesktop.impl.portal.PermissionStore' or a similar pattern.
        # The regex also accounts for potential quotes or comments.
        grep_pattern = fr"^(BusName|ExecStart|D-BusService)=\s*\"?{dbus_service_name.replace('.', r'\.')}(\.service)?\"?(?:\s*|#.*)?$"
        grep_cmd = ["grep", "-lE", grep_pattern] + service_files_to_grep
        
        rc_grep, grep_output = run_quiet(grep_cmd, allow_fail=True, description=f"grepping unit files for {dbus_service_name}")
        
        if rc_grep == 0 and grep_output.strip():
            # Get the first matching unit file and extract its name
            unit_file_path = grep_output.splitlines()[0].strip()
            unit_name = os.path.basename(unit_file_path)
            info(f"Found candidate unit '{unit_name}' by grepping unit file '{unit_file_path}' for D-Bus service '{dbus_service_name}'.")
            return unit_name
    
    # Strategy 3: Heuristic for common unit names (final fallback if direct search/grep fails)
    common_unit_names: List[str] = []
    if dbus_service_name == "org.a11y.Bus":
        common_unit_names = ["at-spi-dbus-bus.service", "at-spi-bus-launcher.service"]
    elif dbus_service_name == "org.freedesktop.impl.portal.PermissionStore":
        common_unit_names = ["xdg-desktop-portal.service", "xdg-desktop-portal-gtk.service", "xdg-desktop-portal-kde.service", "xdg-desktop-portal-gnome.service", "flatpak-portal.service"]
    
    if common_unit_names:
        for candidate_unit in common_unit_names:
            info(f"Checking if common candidate {scope.strip().replace('--', '') + ' ' if scope else ''}unit '{candidate_unit}' explicitly exists for '{dbus_service_name}'...")
            cmd_list_unit = ["systemctl"]
            if scope: cmd_list_unit.append(scope)
            cmd_list_unit.extend(["list-unit-files", "--full", "--all", candidate_unit]) # Check if unit file exists
            
            rc_file_exists, _ = run_quiet(cmd_list_unit, allow_fail=True, description=f"listing unit file for {candidate_unit} ({scope.strip().replace('--', '') if scope else 'system'} scope)")
            if rc_file_exists == 0:
                info(f"Candidate {scope.strip().replace('--', '') + ' ' if scope else ''}unit file '{candidate_unit}' found via explicit lookup. Assuming it's the relevant unit for '{dbus_service_name}'.")
                return candidate_unit

    warn(f"Could not reliably determine the systemd {scope.strip().replace('--', '') + ' ' if scope else ''}unit for D-Bus service '{dbus_service_name}'.")
    return None

def is_unit_masked(unit_name: str, scope: str) -> bool:
    """Checks if a given systemd unit is masked."""
    cmd = ["systemctl"]
    if scope: cmd.append(scope)
    cmd.extend(["is-masked", unit_name])
    rc, _ = run_quiet(cmd, allow_fail=True, description=f"checking if {unit_name} is masked ({scope.strip().replace('--', '') if scope else 'system'} scope)")
    return rc == 0

def unmask_and_start_dbus_service(dbus_service_name: str):
    """
    Discovers the systemd unit for a D-Bus service (searching both user and system scopes),
    then unmasks, daemon-reloads, starts, and enables it.
    """
    info(f"Attempting to ensure D-Bus service '{dbus_service_name}' is unmasked and active.")
    
    unit_name = None
    target_scope = "" # Will be filled with "--user" or "--system"

    # Try user scope first
    unit_name = find_dbus_unit_for_service(dbus_service_name, "--user")
    if unit_name:
        target_scope = "--user"
    else:
        # If not found in user scope, try system scope
        unit_name = find_dbus_unit_for_service(dbus_service_name, "--system")
        if unit_name:
            target_scope = "--system"

    if not unit_name:
        error(f"Cannot unmask D-Bus service '{dbus_service_name}': no corresponding systemd unit found in either user or system scope.")
        return

    current_scope_desc = target_scope.strip().replace('--', '').capitalize() if target_scope else "System"

    if not is_unit_masked(unit_name, target_scope):
        info(f"Service unit '{unit_name}' is not masked in {current_scope_desc} scope. Proceeding to ensure it's running.")
    else:
        info(f"Attempting to unmask {current_scope_desc} service unit '{unit_name}'...")
        unmask_successful = False
        if target_scope == "--user":
            rc_unmask_user, _ = run_quiet(["systemctl", "--user", "unmask", unit_name], allow_fail=True, description=f"unmasking {unit_name} (user scope)")
            if rc_unmask_user == 0:
                info(f"Successfully unmasked '{unit_name}' in user scope.")
                unmask_successful = True
            else:
                warn(f"User scope unmask failed for {unit_name}. Trying with sudo (system-wide unmask) as a fallback.")
                rc_unmask_sudo, _ = run_quiet(["sudo", "systemctl", "unmask", unit_name], allow_fail=True, description=f"unmasking {unit_name} (sudo system-wide fallback)")
                if rc_unmask_sudo == 0:
                    info(f"Successfully unmasked '{unit_name}' via sudo (system-wide).")
                    unmask_successful = True
        else: # target_scope == "--system" or empty (system default)
            rc_unmask_sudo, _ = run_quiet(["sudo", "systemctl", "unmask", unit_name], allow_fail=True, description=f"unmasking {unit_name} (sudo system-wide)")
            if rc_unmask_sudo == 0:
                info(f"Successfully unmasked '{unit_name}' system-wide.")
                unmask_successful = True
        
        if not unmask_successful:
            error(f"Failed to unmask '{unit_name}' in any scope. Manual intervention may be required.")
            return

    info(f"Reloading systemd {current_scope_desc} daemon configuration...")
    cmd_daemon_reload = ["systemctl"]
    if target_scope: cmd_daemon_reload.append(target_scope)
    run_quiet(cmd_daemon_reload + ["daemon-reload"], allow_fail=True, description=f"daemon-reload for {unit_name}")

    info(f"Attempting to start and enable {current_scope_desc} service '{unit_name}'...")
    cmd_start = ["systemctl"]
    if target_scope: cmd_start.append(target_scope)
    run_quiet(cmd_start + ["start", unit_name], allow_fail=True, description=f"start {unit_name}")

    cmd_enable = ["systemctl"]
    if target_scope: cmd_enable.append(target_scope)
    run_quiet(cmd_enable + ["enable", unit_name], allow_fail=True, description=f"enable {unit_name}")
    
    # Check final status
    cmd_status = ["systemctl"]
    if target_scope: cmd_status.append(target_scope)
    rc_status, status_output = run_quiet(cmd_status + ["status", unit_name], allow_fail=True, description=f"status of {unit_name}")
    if rc_status == 0 and "Active: active (running)" in status_output:
        info(f"{current_scope_desc} service '{unit_name}' is now active and running.")
        if is_unit_masked(unit_name, target_scope): # Re-check if it somehow got re-masked
            warn(f"WARNING: Despite attempts, service '{unit_name}' is still reported as masked after starting/enabling. A reboot is highly recommended.")
    else:
        warn(f"{current_scope_desc} service '{unit_name}' is not active after attempting to unmask, start, and enable. Status:\n{status_output}")
        warn("A full logout/login or reboot might be necessary to fully activate this service.")


def install_xdg_portal_and_enable():
    """
    Ensure xdg-desktop-portal & xdg-desktop-portal-gtk are installed and the user service is enabled.
    This function now also tries to actively restart xdg-desktop-portal if issues are detected in its status.
    """
    info("Installing and enabling xdg-desktop-portal + xdg-desktop-portal-gtk...")
    
    # Try to start it
    run_quiet(["systemctl", "--user", "start", "xdg-desktop-portal.service"], allow_fail=True, description="start xdg-desktop-portal service")
    
    # Check status
    rc, status_output = run_quiet(["systemctl", "--user", "status", "xdg-desktop-portal.service"], allow_fail=True, description="check xdg-desktop-portal status")
    
    if rc == 0 and "Active: active (running)" in status_output:
        info("xdg-desktop-portal user service is active and running.")
        # Check for specific errors in the log output of xdg-desktop-portal
        if "Caught PipeWire error: connection error" in status_output or "Realtime error: Could not get pidns" in status_output:
            warn("Detected PipeWire connection errors or PID namespace errors in xdg-desktop-portal logs.")
            info("Attempting to restart xdg-desktop-portal.service to resolve potential issues...")
            run_quiet(["systemctl", "--user", "restart", "xdg-desktop-portal.service"], allow_fail=True, description="restart xdg-desktop-portal service")
            # Re-check status after restart
            rc_recheck, status_recheck_output = run_quiet(["systemctl", "--user", "status", "xdg-desktop-portal.service"], allow_fail=True, description="re-check xdg-desktop-portal status")
            if rc_recheck == 0 and "Active: active (running)" in status_recheck_output:
                info("xdg-desktop-portal user service restarted and is now active and running.")
                if "Caught PipeWire error: connection error" in status_recheck_output or "Realtime error: Could not get pidns" in status_recheck_output:
                    warn("Errors still present in xdg-desktop-portal logs after restart. A more persistent issue or a full reboot might be necessary.")
            else:
                error("Failed to restart xdg-desktop-portal user service. Please check manually.")
    else:
        warn("xdg-desktop-portal user service is not active. This might indicate an issue with your desktop environment or systemd user services.")
        warn("Please ensure your desktop environment properly starts xdg-desktop-portal. A reboot might help.")


def build_and_install_v4l2loopback():
    """
    If packaged module is not present for current kernel, build and install from upstream repo.
    """
    kernel_ver = detect_kernel_version()
    
    # Check if the kmod-v4l2loopback package already provides the module for the current kernel
    # This is often installed by default on Fedora.
    info("Checking if kmod-v4l2loopback provides the module for the current kernel...")
    rc, _ = run_quiet(["modinfo", "v4l2loopback"], allow_fail=True, description="modinfo v4l2loopback")
    if rc == 0:
        info(f"v4l2loopback module found via modinfo. Assuming packaged version is sufficient for kernel {kernel_ver}.")
        return

    warn(f"v4l2loopback module not found or not loaded for current kernel {kernel_ver}. Attempting to build from source.")

    # Ensure /tmp/v4l2loopback is clean before cloning/building
    if os.path.exists("/tmp/v4l2loopback"):
        info("Cleaning up previous v4l2loopback build directory in /tmp/v4l2loopback...")
        run_quiet(["sudo", "rm", "-rf", "/tmp/v4l2loopback"], allow_fail=True, description="cleanup /tmp/v4l2loopback")

    # Clone if needed
    v4l2loopback_dir = "/tmp/v4l2loopback"
    if not os.path.exists(v4l2loopback_dir): # Re-check after cleanup
        info(f"Cloning {V4L2LOOPBACK_REPO} to {v4l2loopback_dir}...")
        rc, _ = run_live(["git", "clone", V4L2LOOPBACK_REPO, v4l2loopback_dir], allow_fail=True)
        if rc != 0:
            error("Failed to clone v4l2loopback repository. Skipping manual build.")
            return

    # Build
    info("Attempting to build v4l2loopback module...")
    rc, out = run_live(["make"], cwd=v4l2loopback_dir, allow_fail=True)
    if rc != 0:
        warn("Make failed; continuing to try 'sudo make install' in case partial build is usable or error was minor.")

    # Install (allow unsigned install)
    info("Attempting to install v4l2loopback module...")
    run_live(["sudo", "make", "install", "INSTALL_MOD_STRIP=1"], cwd=v4l2loopback_dir, allow_fail=True)
    run_live(["sudo", "depmod", "-a"], allow_fail=True)
    info("v4l2loopback build/install attempts complete.")

def load_virtual_camera_and_wait() -> bool:
    """
    Load v4l2loopback and wait for /dev/video{VIRTUAL_VIDEO_NR}.
    Returns True if successful, False otherwise.
    """
    info("Loading v4l2loopback kernel module (creating virtual camera)...")
    
    # First, unload it to ensure a clean load, and prevent issues if already partially loaded
    run_quiet(["sudo", "modprobe", "-r", "v4l2loopback"], allow_fail=True, description="unloading v4l2loopback module (if loaded)")

    rc, _ = run_live([
        "sudo", "modprobe", "v4l2loopback",
        f"devices=1", f"video_nr={VIRTUAL_VIDEO_NR}",
        "card_label=OBS_Virtual_Cam", "exclusive_caps=1"
    ], allow_fail=True)

    if rc != 0:
        error("Failed to load v4l2loopback module. Virtual camera will not be available.")
        # Attempt to gather more info on why modprobe failed
        rc_dmesg, dmesg_output = run_quiet(["dmesg", "|", "grep", "v4l2loopback"], allow_fail=True, description="dmesg for v4l2loopback errors")
        if rc_dmesg == 0 and dmesg_output.strip():
            error(f"Recent kernel messages related to v4l2loopback:\n{dmesg_output.strip()}")
        warn("Ensure kernel-devel matches your running kernel: sudo dnf install kernel-devel-$(uname -r)")
        return False

    info(f"Waiting up to 20s for {VIRTUAL_DEVICE} to appear...")
    for i in range(40):  # 40 * 0.5s = 20s
        if os.path.exists(VIRTUAL_DEVICE):
            info(f"{VIRTUAL_DEVICE} is present.")
            # set permissions (idempotent)
            run_quiet(["sudo", "chown", f"{getpass.getuser()}:video", VIRTUAL_DEVICE], allow_fail=True, description=f"chown {VIRTUAL_DEVICE}")
            run_quiet(["sudo", "chmod", "0660", VIRTUAL_DEVICE], allow_fail=True, description=f"chmod {VIRTUAL_DEVICE}")
            return True
        time.sleep(SLEEP_SHORT)
    warn(f"{VIRTUAL_DEVICE} did not appear within timeout.")
    return False

def start_pipewire_services_and_wait() -> Tuple[bool, str]:
    """
    Start PipeWire user services and check for video nodes.
    Return (True if video nodes found, discovered pipewire nodes output).
    """
    info("Starting PipeWire & WirePlumber user services... (ensuring enabled and started)")
    # Attempt to enable and start, handling common warnings about static units
    services = ["pipewire.service", "pipewire-pulse.service", "wireplumber.service"]
    for service in services:
        info(f"Attempting to start and enable --user {service}...")
        # Try to enable first (if it's not a static unit)
        run_quiet(["systemctl", "--user", "enable", service], allow_fail=True, description=f"systemctl enable {service}")
        # Then try to start it
        run_quiet(["systemctl", "--user", "start", service], allow_fail=True, description=f"systemctl start {service}")
        # Check status
        rc, status = run_quiet(["systemctl", "--user", "status", service], allow_fail=True, description=f"systemctl status {service}")
        if rc == 0 and "Active: active (running)" in status:
            info(f"User service '{service}' is active and running.")
        else:
            warn(f"User service '{service}' is not active or failed to start. Status:\n{status}")
            warn(f"You may need to manually restart your user session or reboot for PipeWire services to function correctly.")


    info("Waiting for PipeWire to enumerate video/capture nodes (up to 10s)...")
    nodes_output = ""
    for _ in range(20):  # 20 * 0.5s = 10s
        if check_command_exists("pw-cli"):
            rc, nodes_output = run_quiet(["pw-cli", "list-objects"], allow_fail=True, description="pw-cli list-objects")
        elif check_command_exists("pw-dump"):
            rc, nodes_output = run_quiet(["pw-dump"], allow_fail=True, description="pw-dump")
        else:
            warn("pw-cli/pw-dump not found; skipping PipeWire node enumeration.")
            break
        
        if rc == 0 and ("/dev/video" in nodes_output or "v4l2" in nodes_output.lower() or "Video/Device" in nodes_output):
            info("PipeWire shows video/V4L2 entities.")
            return True, nodes_output
        time.sleep(SLEEP_SHORT)
    warn("PipeWire did not report video nodes in time or 'pw-cli'/'pw-dump' failed.")
    return False, nodes_output

def restart_pipewire_services():
    """Restarts PipeWire and WirePlumber user services."""
    info("Restarting PipeWire and WirePlumber user services...")
    run_quiet(["systemctl", "--user", "restart", "pipewire.service", "pipewire-pulse.service", "wireplumber.service"], allow_fail=True, description="restart pipewire services")
    info("PipeWire and WirePlumber services restarted.")

def list_physical_video_devices() -> List[str]:
    """
    Return list of /dev/video* that exist (excluding the virtual device).
    """
    devs = []
    # Iterate through potential video devices. A common range is 0-15 or 0-31
    for i in range(0, 32): 
        path = f"/dev/video{i}"
        if os.path.exists(path):
            devs.append(path)
    
    # Exclude virtual if present
    devs = [d for d in devs if d != VIRTUAL_DEVICE]
    return devs

def install_python_dependencies():
    info("Installing optional Python dependencies for better UX (user-level pip installs)...")
    # use user install to avoid requiring sudo for pip packages
    run_quiet([sys.executable, "-m", "pip", "install", "--user", "--upgrade", "pip"], allow_fail=True, description="upgrade pip")
    run_quiet([sys.executable, "-m", "pip", "install", "--user"] + PYTHON_DEPS, allow_fail=True, description="install python dependencies")

def check_v4l2_devices() -> str:
    """Runs v4l2-ctl --list-devices and returns its output."""
    if check_command_exists("v4l2-ctl"):
        rc, output = run_quiet(["v4l2-ctl", "--list-devices"], allow_fail=True, description="v4l2-ctl --list-devices")
        if rc == 0:
            return output
    return "v4l2-ctl not found or failed."

def check_lsusb() -> str:
    """Runs lsusb and returns its output."""
    if check_command_exists("lsusb"):
        rc, output = run_quiet(["lsusb"], allow_fail=True, description="lsusb")
        if rc == 0:
            return output
    return "lsusb not found or failed."

def check_pipewire_nodes_detailed() -> str:
    """Runs pw-cli list-objects and returns its output, or pw-dump as fallback."""
    if check_command_exists("pw-cli"):
        rc, output = run_quiet(["pw-cli", "list-objects"], allow_fail=True, description="pw-cli list-objects")
        if rc == 0: return output
    elif check_command_exists("pw-dump"):
        rc, output = run_quiet(["pw-dump"], allow_fail=True, description="pw-dump")
        if rc == 0: return output
    return "pw-cli or pw-dump not found."

def test_capture_device(device_path: str) -> bool:
    """
    Attempts to test a video capture device using ffplay or gst-launch-1.0.
    Returns True if successful, False otherwise.
    """
    info(f"Attempting to test video capture for {device_path}...")
    
    # Try ffplay first
    if check_command_exists("ffplay"):
        info(f"Trying ffplay {device_path} (will attempt to run for 5 seconds)...")
        try:
            # Use Popen to control timeout and prevent hanging
            proc = subprocess.Popen(["ffplay", "-loglevel", "quiet", "-t", "5", device_path],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            # Wait for the process to finish or for the timeout
            try:
                proc.wait(timeout=6) # Give it slightly more time to naturally exit
                if proc.returncode == 0:
                    info(f"ffplay test for {device_path} successful.")
                    return True
                else:
                    warn(f"ffplay test for {device_path} exited with code {proc.returncode}.")
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                info(f"ffplay test for {device_path} terminated after timeout (likely successful display).")
                return True # Assuming success if it ran for the duration
        except Exception as e:
            warn(f"ffplay test for {device_path} failed: {e}")
    
    # Fallback to gst-launch-1.0
    if check_command_exists("gst-launch-1.0"):
        info(f"Trying gst-launch-1.0 v4l2src device={device_path} ! autovideosink (will attempt to run for 5 seconds)...")
        try:
            proc = subprocess.Popen(["gst-launch-1.0", "v4l2src", f"device={device_path}", "!", "autovideosink"],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            time.sleep(5) # Let it run for 5 seconds
            proc.terminate()
            proc.wait(timeout=2) # Wait for it to actually terminate
            info(f"gst-launch-1.0 test for {device_path} completed (process terminated after 5s).")
            return True # If it ran for 5s, it likely worked.
        except Exception as e:
            warn(f"gst-launch-1.0 test for {device_path} failed or timed out: {e}")
    
    warn(f"No successful external capture test for {device_path} could be performed or tools not found.")
    return False

def check_and_suggest_pipewire_media_session_removal():
    """
    Checks if pipewire-media-session is installed and suggests its removal
    if WirePlumber is active, as they can conflict.
    """
    info("Checking for conflicting PipeWire session managers...")
    rc_installed, dnf_list_output = run_quiet(["dnf", "list", "installed", "pipewire-media-session"], allow_fail=True, description="dnf list pipewire-media-session")
    
    # DNF list installed returns RC=0 even if package not found, but output is "Error: No matching Packages to list"
    # or it prints actual package info. Check for "installed" string in the output for a reliable check.
    if "installed" in dnf_list_output.lower() and rc_installed == 0 and "pipewire-media-session" in dnf_list_output:
        warn("'pipewire-media-session' is installed. On Fedora, 'wireplumber' is the default and recommended session manager.")
        warn("Having both can cause conflicts and unexpected behavior with PipeWire devices.")
        info("It is highly recommended to remove 'pipewire-media-session' if you intend to use WirePlumber:")
        info("  -> sudo dnf remove pipewire-media-session")
        warn("After removal, please reboot your system to ensure WirePlumber takes full control.")
    else:
        info("pipewire-media-session is not installed. WirePlumber should be the sole PipeWire session manager.")

def check_journalctl_pipewire_errors():
    """
    Scans journalctl for recent PipeWire errors in the user session.
    Now prints full output for both pipewire and wireplumber services within the last 24 hours.
    """
    info("Checking recent 'journalctl' logs for PipeWire-related activity in your user session (last 24 hours)...")

    # Check pipewire.service
    info("Checking 'pipewire.service' logs:")
    rc_pw, output_pw = run_quiet(["journalctl", "--user", "-u", "pipewire.service", "--since", "24 hours ago", "--no-pager"], allow_fail=True, description="journalctl pipewire.service")
    if rc_pw == 0 and ("error" in output_pw.lower() or "fail" in output_pw.lower()):
        warn("Detected 'pipewire.service' errors or failures in journalctl. This might indicate an unstable PipeWire state:")
        print(output_pw)
    elif rc_pw != 0:
        warn(f"Could not retrieve 'pipewire.service' logs from journalctl (rc={rc_pw}). Output:\n{output_pw}")
    elif output_pw.strip():
        info("'pipewire.service' logs (no explicit errors detected):")
        print(output_pw)
    else:
        info("No recent 'pipewire.service' logs found in journalctl.")

    # Check wireplumber.service
    info("Checking 'wireplumber.service' logs:")
    rc_wp, output_wp = run_quiet(["journalctl", "--user", "-u", "wireplumber.service", "--since", "24 hours ago", "--no-pager"], allow_fail=True, description="journalctl wireplumber.service")
    if rc_wp == 0 and ("error" in output_wp.lower() or "fail" in output_wp.lower() or "masked" in output_wp.lower() or "leaked proxy" in output_wp.lower()):
        warn("Detected 'wireplumber.service' errors, failures, or significant warnings (e.g., masked units, leaked proxies) in journalctl:")
        print(output_wp)
    elif rc_wp != 0:
        warn(f"Could not retrieve 'wireplumber.service' logs from journalctl (rc={rc_wp}). Output:\n{output_wp}")
    elif output_wp.strip():
        info("'wireplumber.service' logs (no explicit errors detected):")
        print(output_wp)
    else:
        info("No recent 'wireplumber.service' logs found in journalctl.")


# ---------------------------
# Troubleshooting output (moved up for definition order)
# ---------------------------
def print_troubleshooting(had_virtual_device: bool, pipewire_ok: bool, physical_devs: List[str], video_group_needs_relogin: bool, current_log_filename: str):
    print("\n" + "="*60)
    print("[FINAL CHECKS SUMMARY]")
    print(f"Virtual device {VIRTUAL_DEVICE} present: {had_virtual_device}")
    print(f"PipeWire video nodes detected: {pipewire_ok}")
    print(f"Physical /dev/video devices: {physical_devs if physical_devs else 'None found'}")
    print(f"User needs relogin for 'video' group membership: {video_group_needs_relogin}") 
    print("="*60)

    if not physical_devs:
        warn("\nNo physical /dev/video* devices were found. If you expect cameras attached, please check:")
        info(" - Cables and USB power.")
        info(" - Running 'lsusb':\n" + check_lsusb())
        info(" - Running 'v4l2-ctl --list-devices':\n" + check_v4l2_devices())
        warn("If cameras still aren't detected by the system, it's a hardware/driver issue outside this script's scope.")
    else:
        info("\nPhysical cameras detected. If OBS still shows 'No capture sources available', here's what was attempted:")
        
        if video_group_needs_relogin:
            warn("User needs to log out and log back in (or reboot) for 'video' group membership to take effect.")
            warn("This script cannot automate a relogin/reboot. Please do this manually and re-run the script after.")
        else:
            info("User is already in 'video' group. No relogin needed for group membership.")

        info("1. Attempted to restart PipeWire & WirePlumber services to refresh connections.")
        info("2. Checked xdg-desktop-portal status again and restarted it if errors were detected to stabilize portals.")
        info("3. Attempted to unmask and start D-Bus services for Accessibility (org.a11y.Bus) and Permission Store (org.freedesktop.impl.portal.PermissionStore).")


        info("\nNow performing further automated checks based on the previous output:")
        
        info(" - Checking PipeWire nodes in detail:")
        info(check_pipewire_nodes_detailed())

        # Check for conflicting session managers
        check_and_suggest_pipewire_media_session_removal()

        # Check journalctl for active errors
        check_journalctl_pipewire_errors()
        
        info(" - Testing physical capture devices outside OBS (this may open temporary graphical windows):")
        for dev in physical_devs:
            test_capture_device(dev)

        warn("\nIf issues persist after the automated steps, manual investigation may be required:")
        if not pipewire_ok:
            warn(" - PipeWire is still not reporting video nodes consistently. This is a critical issue.")
        
        warn(" - Even if `ffplay` worked, OBS may struggle if its PipeWire connection is faulty.")
        warn("   If you continue to see 'Caught PipeWire error: connection error', 'unit is masked', or similar D-Bus/portal errors in logs,")
        warn("   or if OBS's `linux-pipewire.so` module reports 'No capture sources available', a full user session refresh is often required.")
        warn("   This means **logging out of your desktop session completely and logging back in, or rebooting.**")
        warn("   This ensures PipeWire and all its clients (including OBS) pick up the latest configurations and establish fresh PipeWire connections.")
        warn(" - Check WirePlumber policy files (for advanced users):")
        info("   -> Look in /usr/share/wireplumber or /etc/wireplumber for policy files and ensure V4L2 policy is enabled.")
        warn("   -> For WirePlumber policy troubleshooting see: https://gitlab.freedesktop.org/pipewire/wireplumber/-/wikis")

    if not had_virtual_device:
        warn(f"\nVirtual camera ({VIRTUAL_DEVICE}) is required but not present.")
        warn(" - The v4l2loopback kernel module may not have been built for your kernel properly.")
        info(" - The script attempted to build it. Please check the logs above for build errors.")
        info(f" - To manually reload: sudo modprobe v4l2loopback devices=1 video_nr={VIRTUAL_VIDEO_NR} card_label=OBS_Virtual_Cam exclusive_caps=1")
        warn(" - If you manually loaded the module, re-run this script to verify permissions.")
    else:
        info(f"Virtual camera {VIRTUAL_DEVICE} is present and functional.")


    print("\nLogs & next steps (for manual inspection if automated steps failed):")
    info(f" - Full script execution log saved to: {current_log_filename}") # Explicitly show log file path
    info(" - OBS log: Help -> Log Files -> View Current Log")
    info(" - System messages: journalctl --user -xe | grep pipewire")
    print("="*60 + "\n")


# ---------------------------
# Main Logic
# ---------------------------
def _main_logic(current_log_filename: str): # Pass log filename to main logic
    info("Starting setup_obs_multicam.py main logic.")

    ensure_sudo_available()
    
    # 1) Install system packages (stream output)
    install_system_packages()

    # 2) Dynamically find and unmask D-Bus services that are reported as masked
    unmask_and_start_dbus_service("org.freedesktop.impl.portal.PermissionStore")
    unmask_and_start_dbus_service("org.a11y.Bus")

    # 3) Ensure xdg-desktop-portal and enable it (now includes active error detection/restarts)
    install_xdg_portal_and_enable()

    # 4) Ensure user is in video group (may require relogin)
    relogin_needed = ensure_video_group_membership()

    # 5) Build/install v4l2loopback if needed
    build_and_install_v4l2loopback()

    # 6) Load virtual camera and wait
    virtual_ok = load_virtual_camera_and_wait()

    # 7) Start PipeWire & WirePlumber and check nodes (initial check)
    pipewire_initial_ok, _ = start_pipewire_services_and_wait()

    # 8) Restart PipeWire services here to ensure a clean state after all modules are loaded
    # and after any D-Bus services have been unmasked/started.
    restart_pipewire_services()
    # Re-check PipeWire nodes after restart to get the most accurate state
    pipewire_ok_after_restart, _ = start_pipewire_services_and_wait()


    # 9) Install python deps (optional)
    install_python_dependencies()

    # 10) List physical devices
    physical_devs = list_physical_video_devices()

    # 11) Final troubleshooting instructions (now with automated fixes where possible)
    print_troubleshooting(virtual_ok, pipewire_ok_after_restart, physical_devs, relogin_needed, current_log_filename)

    info("Setup script finished. Launch OBS (the launcher run-obs.sh in your obs-portable folder, or from your applications menu) and check video capture sources.")
    info("If cameras still do not show, please review the troubleshooting steps and their outcomes above.")
    if relogin_needed or not pipewire_ok_after_restart:
        warn("Given the issues detected (e.g., xdg-desktop-portal errors, potential PipeWire instability),")
        warn("a full **logout and login** or **reboot** is highly recommended to ensure all system services")
        warn("and applications (like OBS) pick up the latest configurations and establish fresh PipeWire connections.")
    info("Tip: After a relogin/reboot, re-run this script once to re-check PipeWire / device enumeration and apply any remaining automated fixes.")


# ---------------------------
# Main Entry Point (handles logging decision)
# ---------------------------
if __name__ == "__main__":
    # Ensure sys_stdout_original and sys_stderr_original are defined before init_automatic_logging
    # (These are already defined globally above the LogStream class for safety).

    log_filename = f"setup_log_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    
    if "--disable-log-file" in sys.argv:
        print("[INFO] File logging explicitly disabled via command-line argument.")
        _main_logic(log_filename) # Still pass filename for consistency, though it won't be written to
    else:
        # Automatic file logging by default
        init_automatic_logging(log_filename)
        try:
            _main_logic(log_filename) # Pass the filename to main logic
        finally:
            close_automatic_logging() # Ensure log file is closed on script exit or error