"""Constants for the Unraid SSH integration.

One config entry = one Unraid server reached over SSH with a key pair that
the config flow generates itself. Nothing here depends on Unraid Connect.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "unraid_ssh"

# --- config entry keys -------------------------------------------------------
CONF_HOST: Final = "host"
CONF_PORT: Final = "port"
CONF_NAME: Final = "name"
CONF_PRIVATE_KEY: Final = "private_key"   # PEM, generated in the config flow
CONF_PUBLIC_KEY: Final = "public_key"     # OpenSSH line shown to the user
CONF_HOST_KEY: Final = "host_key"         # server host key, trust on first use
CONF_SCAN_INTERVAL: Final = "scan_interval"        # seconds
CONF_UPDATE_INTERVAL: Final = "update_interval"    # hours

DEFAULT_PORT: Final = 22
DEFAULT_NAME: Final = "Unraid SSH"
DEFAULT_SCAN_INTERVAL: Final = 30
MIN_SCAN_INTERVAL: Final = 10
MAX_SCAN_INTERVAL: Final = 300
DEFAULT_UPDATE_INTERVAL: Final = 6
MIN_UPDATE_INTERVAL: Final = 1
MAX_UPDATE_INTERVAL: Final = 24

KEY_COMMENT: Final = "unraid_ssh@homeassistant"
PUBKEYS_FILE: Final = "/boot/config/ssh/root.pubkeys"
AUTHORIZED_KEYS_FILE: Final = "/root/.ssh/authorized_keys"

# --- timeouts (seconds) -------------------------------------------------------
CONNECT_TIMEOUT: Final = 10
POLL_TIMEOUT: Final = 20
CONTAINER_ACTION_TIMEOUT: Final = 60
STACK_ACTION_TIMEOUT: Final = 300
VM_ACTION_TIMEOUT: Final = 30
UPDATE_TIMEOUT: Final = 600
REMOTE_DIGEST_TIMEOUT_PER_IMAGE: Final = 30

# --- paths on the Unraid server ----------------------------------------------
EMHTTP_DIR: Final = "/var/local/emhttp"
COMPOSE_PROJECTS_DIR: Final = "/boot/config/plugins/compose.manager/projects"
COMPOSE_SH: Final = "/usr/local/emhttp/plugins/compose.manager/scripts/compose.sh"
UPDATE_CONTAINER_SH: Final = (
    "/usr/local/emhttp/plugins/dynamix.docker.manager/scripts/update_container"
)

# --- orphan cleanup ------------------------------------------------------------
#: How long a unique id has to stay absent from successful polls before its
#: entity is removed. `compose up` and Unraid's own `update_container` tear a
#: container down and recreate it within seconds; without this window the user
#: would lose that entity's area, name and labels for a restart.
STALE_GRACE: Final = timedelta(minutes=5)

# --- output markers ------------------------------------------------------------
SECTION_MARKER: Final = "@@@ "
END_SECTION: Final = "end"

# --- enum options (sensor device_class ENUM needs the closed list) ------------
ARRAY_STATES: Final = ("started", "stopped", "starting", "stopping", "unknown")
DISK_STATUSES: Final = (
    "ok", "new", "disabled", "invalid", "missing", "not_present", "unknown"
)
VM_STATES: Final = (
    "running", "shut_off", "paused", "in_shutdown", "crashed", "pmsuspended",
    "idle", "unknown"
)

#: MDI icons only for entities without a device_class (see arrstack/entity.py).
ENTITY_ICONS: Final = {
    "cpu_percent": "mdi:cpu-64-bit",
    "ram_percent": "mdi:memory",
    "load_1": "mdi:chart-line",
    "array_state": "mdi:harddisk-plus",
    "parity_progress": "mdi:progress-check",
    "parity_errors": "mdi:alert-circle-outline",
    "parity_running": "mdi:sync",
    "mover_active": "mdi:truck-fast",
    "check_updates": "mdi:package-down",
    "gpu_util": "mdi:expansion-card",
    "gpu_vram": "mdi:memory",
    "gpu_fan": "mdi:fan",
    "fan_rpm": "mdi:fan",
    "fan_percent": "mdi:fan",
    "disk_status": "mdi:harddisk",
    "disk_spundown": "mdi:sleep",
    "share_used": "mdi:folder-network",
    "share_free": "mdi:folder-network-outline",
    "stack": "mdi:layers",
    "container": "mdi:docker",
    "vm": "mdi:monitor",
    "vm_state": "mdi:monitor-dashboard",
}
