# OBS Multi-Camera Setup and Fixer for Fedora (setup_obs_multicam.py)

## Table of Contents

*   [Introduction](#introduction)
*   [Key Features](#key-features)
*   [How to Use](#how-to-use)
*   [Troubleshooting & Important Notes](#troubleshooting--important-notes)
*   [Future Improvements](#future-improvements)
*   [License](#license)

---

## Introduction

Setting up OBS Studio for multi-camera functionality on Linux, particularly with Fedora's PipeWire architecture, can be a complex and often frustrating experience. Users frequently encounter issues such as:

*   Missing `v4l2loopback` kernel module for virtual cameras.
*   Incorrect permissions for video devices (`/dev/video*`).
*   Problems with PipeWire not properly enumerating physical or virtual cameras.
*   Conflicts between PipeWire session managers (e.g., `pipewire-media-session` vs. `wireplumber`).
*   Masked or inactive D-Bus services (like `xdg-desktop-portal` or accessibility services) that prevent applications from accessing system resources correctly.
*   A general lack of clear, consolidated troubleshooting steps.

This script, `setup_obs_multicam.py`, was created to automate the installation, configuration, and fixing of these common issues, aiming to provide a robust and streamlined solution specifically for Fedora 42 (and likely compatible with newer Fedora versions). It anticipates common pitfalls, provides active diagnostics, and offers comprehensive troubleshooting instructions.

## Key Features

The `setup_obs_multicam.py` script is a powerful and robust tool that performs the following actions to ensure a functional multi-camera setup for OBS Studio:

*   **Automatic Live Logging**: All script output is mirrored to a timestamped log file (`setup_log_YYYYMMDD_HHMMSS.txt`) while also being displayed live in your terminal. This provides an invaluable record for debugging and reviewing steps.
*   **Idempotent System Package Installation**: Installs all necessary system packages (OBS Studio, PipeWire, WirePlumber, `v4l2loopback-utils`, `xdg-desktop-portal`, `ffmpeg`, build tools, etc.) using `dnf`. It uses options to tolerate conflicts and missing packages, and critically ensures the correct `kernel-devel` package for your *current running kernel* is installed.
*   **Intelligent D-Bus Service Management**:
    *   Dynamically identifies `systemd` user or system units corresponding to critical D-Bus services like `org.freedesktop.impl.portal.PermissionStore` and `org.a11y.Bus`.
    *   Automatically unmasks, daemon-reloads, starts, and enables these services if they are found to be masked or inactive, resolving common portal and accessibility-related issues that can affect OBS.
*   **Proactive XDG Desktop Portal Setup**: Installs `xdg-desktop-portal` and `xdg-desktop-portal-gtk` and ensures the relevant user service is running. It actively monitors its status and attempts to restart it if common PipeWire connection errors or PID namespace issues are detected in its logs, aiming to stabilize desktop portal functionality.
*   **`video` Group Membership Assurance**: Verifies if the current user is in the `video` group (essential for accessing video devices) and adds them if necessary. It provides a clear and prominent warning that a **re-login or reboot is required** for this change to take full effect.
*   **Flexible `v4l2loopback` Installation**:
    *   Checks if the `v4l2loopback` kernel module is already available for the current kernel (e.g., provided by `kmod-v4l2loopback`).
    *   If not, it automatically clones the upstream `v4l2loopback` repository, builds the module from source, and installs it, ensuring virtual camera functionality is always available.
*   **Virtual Camera Creation**: Loads the `v4l2loopback` module to create a virtual video device (`/dev/video10` by default, labeled "OBS_Virtual_Cam") and waits for its appearance, then sets correct user permissions.
*   **PipeWire Service Control**: Ensures user-level PipeWire, PipeWire Pulse, and WirePlumber services are enabled and running. It performs a strategic restart of these services after other system changes to establish a clean state and re-enumerate video nodes effectively.
*   **Optional Python Dependencies**: Installs common Python packages (`requests`, `json5`, `pyyaml`) that are often useful for advanced OBS Studio scripts, using `pip --user` for user-level installation.
*   **Comprehensive Troubleshooting & Diagnostics**:
    *   Lists all physical `/dev/video*` devices detected.
    *   Provides detailed output from `v4l2-ctl --list-devices`, `lsusb`, and `pw-cli list-objects` (or `pw-dump`) for in-depth system information.
    *   Attempts to test physical video capture devices using `ffplay` or `gst-launch-1.0` to verify basic functionality outside of OBS (these may open temporary graphical windows).
    *   Detects and warns about conflicting `pipewire-media-session` (if `wireplumber` is active, which is standard on Fedora) and recommends its removal.
    *   Scans `journalctl` for recent PipeWire and WirePlumber-related errors or significant warnings in the user session, printing full log excerpts.
*   **Clear Final Guidance**: Presents a concise summary of the checks and actions taken, along with explicit, actionable troubleshooting steps and strong recommendations for re-login or reboot when necessary to activate all system changes.

## How to Use

1.  **Download the Script**:
    Save the script content to a file named `setup_obs_multicam.py` (e.g., by copying the raw content from the repository and pasting it into a new file).

2.  **Make it Executable**:
    Open a terminal and navigate to the directory where you saved the script. Then run:
    ```bash
    chmod +x setup_obs_multicam.py
    ```

3.  **Run the Script**:
    Execute the script from your terminal:
    ```bash
    ./setup_obs_multicam.py
    ```
    The script will automatically log all output to a file named `setup_log_YYYYMMDD_HHMMSS.txt` (e.g., `setup_log_20250831_145000.txt`) in the same directory, while also displaying it live in your terminal.

    **Optional: Disable File Logging**: If you prefer to only see output in the terminal and not write to a log file, you can run:
    ```bash
    ./setup_obs_multicam.py --disable-log-file
    ```

4.  **Follow Instructions Carefully**:
    Pay close attention to the script's output, especially any **WARNING** messages. It will inform you about:
    *   Packages being installed.
    *   Changes to user groups (e.g., `video` group membership) and if a **re-login/reboot is required**.
    *   Attempts to build and load the `v4l2loopback` kernel module.
    *   Status of PipeWire and XDG Desktop Portal services.
    *   Any detected issues and recommended next steps.

5.  **Re-login/Reboot if Advised (Crucial!)**:
    If the script advises you to log out and log back in, or to reboot, **it is absolutely crucial that you do so.** Many system-level changes (like new group memberships or D-Bus service reconfigurations) only take full effect after a complete session refresh. Failure to do so will likely result in OBS Studio still not detecting your cameras.

6.  **Launch OBS Studio**:
    After the script completes and any advised re-login/reboot, launch OBS Studio. Check your video capture sources; your physical cameras and the new "OBS_Virtual_Cam" (`/dev/video10`) should now be available.

## Troubleshooting & Important Notes

*   **Virtual Camera Missing (`/dev/video10` not present):**
    *   The `v4l2loopback` kernel module might not have been built correctly. Review the log file (`setup_log_*.txt`) for any build errors.
    *   Ensure your `kernel-devel-$(uname -r)` package matches your currently running kernel.
    *   You can try to manually load it: `sudo modprobe v4l2loopback devices=1 video_nr=10 card_label="OBS_Virtual_Cam" exclusive_caps=1`. Re-run the script after manual loading to verify permissions.
*   **Physical Cameras Not Detected**:
    *   If the script reports "No physical /dev/video* devices were found", it's likely a hardware or base driver issue.
    *   Check your camera cables and USB connections.
    *   Review the output from `lsusb` and `v4l2-ctl --list-devices` in the script's log. If cameras aren't listed there, the issue is outside the scope of this script.
*   **"No Capture Sources Available" in OBS / PipeWire Errors**:
    *   If OBS still doesn't see sources, even if `ffplay` or `gst-launch-1.0` tests worked, it often indicates a faulty PipeWire connection within OBS or a lingering D-Bus/portal issue.
    *   The most common fix is a **full logout and login, or a reboot**. This is critical for PipeWire and all its clients (including OBS) to establish fresh connections with the latest system configuration.
    *   Review `journalctl --user -xe | grep pipewire` and the script's log for specific "Caught PipeWire error: connection error," "unit is masked," or similar D-Bus/portal errors.
*   **Conflicting Session Managers**:
    *   The script will warn you if `pipewire-media-session` is installed alongside `wireplumber`. It's highly recommended to remove `pipewire-media-session` to avoid conflicts: `sudo dnf remove pipewire-media-session`. **Remember to reboot after removal.**
*   **Permissions**: The script attempts to set correct permissions for `/dev/video10` (owned by user:video with 0660). If you still encounter permission issues, ensure your user is indeed in the `video` group and your session has refreshed.

**Tip:** After a re-login/reboot, consider re-running this script once more. It's idempotent and will re-check PipeWire/device enumeration and apply any remaining automated fixes.

## Future Improvements

While already highly effective, a few minor refinements and user experience (UX) tweaks could make the `setup_obs_multicam.py` script even more robust and user-friendly.

### Minor Refinements in D-Bus Unit Discovery

1.  **More Robust Grep Patterns for Implicit Activation**:
    *   **Improvement**: Extend the `grep_pattern` in `find_dbus_unit_for_service` to include more general terms like `ExecStart=.*?dbus-launch` or `ExecStart=.*?--systemd-activation` within `.service` files.
    *   **Benefit**: Increases the chance of finding the correct systemd unit for D-Bus services that rely on indirect activation.

2.  **Handling Ambiguous Grep Matches**:
    *   **Improvement**: If the `grep` search for a D-Bus service name yields multiple `.service` files, the script could log all candidates, prioritize known common unit names, or perform a secondary check (`systemctl show <candidate_unit> --property=BusName`) to confirm the most relevant unit.
    *   **Benefit**: Reduces potential for incorrect unit identification in complex system configurations.

### User Experience (UX) Tweaks

1.  **`run_quiet` Output Clarity**:
    *   **Improvement**: Modify the `run_quiet` function to only print the full `combined_output` when it's genuinely informative (e.g., contains errors, warnings, or specific success data). Otherwise, a more concise message like "completed successfully" would suffice.
    *   **Benefit**: Keeps the terminal output cleaner and focuses attention on genuinely important messages.

2.  **Pre-check for `kernel-devel` during `v4l2loopback` build**:
    *   **Improvement**: Add an explicit check using `rpm -q kernel-devel-$(uname -r)` *before* starting the `v4l2loopback` build process. If it's missing or an incorrect version, issue a prominent warning/error earlier.
    *   **Benefit**: Prevents `make` failures due to missing kernel headers and provides a more direct diagnosis.

3.  **Clarity on `modinfo` vs. `modprobe`**:
    *   **Improvement**: Clarify the `modinfo` message to state that it confirms the kernel module *file exists* and is recognized, but `modprobe` is still required to *load* it into the kernel.
    *   **Benefit**: Improves user understanding of the kernel module loading process.

4.  **Explicit Warning for Graphical Test Windows**:
    *   **Improvement**: Add a more immediate warning *before* each `test_capture_device` call (e.g., "A small video window will briefly appear and close automatically for testing `/dev/videoX`.")
    *   **Benefit**: Sets user expectations, preventing confusion or premature closing of the test window.

5.  **Stronger Emphasis on `video` Group Re-login**:
    *   **Improvement**: While already warned, consider using a more visually distinct warning (e.g., using a different text color or more prominent formatting) for the re-login message. For advanced users, an optional tip about using `newgrp video` for immediate terminal-based testing could be added.
    *   **Benefit**: Ensures the critical need for a session refresh is not missed and provides potential interim testing methods.

## License

This project is open-source. Please refer to the top of the `setup_obs_multicam.py` script for specific licensing information.