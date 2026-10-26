"""Allowlisted server operations with conservative, pure state guards."""
from enum import Enum
from .model import Snapshot


class ServerAction(Enum):
    SERVER_REBOOT = 'server_reboot'
    SERVER_SHUTDOWN = 'server_shutdown'
    ARRAY_START = 'array_start'
    ARRAY_STOP = 'array_stop'
    PARITY_START = 'parity_start'
    PARITY_PAUSE = 'parity_pause'
    PARITY_RESUME = 'parity_resume'
    PARITY_CANCEL = 'parity_cancel'
    SPINDOWN_ALL = 'spindown_all'
    MOVER_START = 'mover_start'


class ServerActionError(ValueError):
    """A fixed action's state prerequisites have not been established."""


_EMCMD = '/usr/local/emhttp/plugins/dynamix/scripts/emcmd'
_PARAMS = {
    ServerAction.ARRAY_START:'startState=STOPPED&cmdStart=Start',
    ServerAction.ARRAY_STOP:'startState=STARTED&cmdStop=Stop',
    ServerAction.PARITY_START:'startState=STARTED&cmdCheck=Check',
    ServerAction.PARITY_PAUSE:'cmdCheckPause=',
    ServerAction.PARITY_RESUME:'cmdCheckResume=',
    ServerAction.PARITY_CANCEL:'cmdCheckCancel=',
    ServerAction.SPINDOWN_ALL:'cmdSpinDownAll=Apply',
    ServerAction.MOVER_START:'cmdStartMover=Move',
}


def _pool_start_safe(snapshot: Snapshot) -> bool:
    """Require coverage of every configured slot, never just returned pool rows."""
    slots = snapshot.pool_start_inventory
    if slots is None:
        return False
    if not slots:
        return not any(d.kind.lower() == 'cache' for d in snapshot.disks)
    telemetry = snapshot.telemetry
    if (not telemetry.pools or 'dash_pools' in telemetry.failed
            or telemetry.sample_times.get('dash_pools') != snapshot.sampled_at):
        return False
    groups = {}
    for slot in slots:
        if not slot.assigned or slot.status != 'DISK_OK' or not slot.mountpoint:
            return False
        groups.setdefault(slot.mountpoint, []).append(slot)
    covered = set()
    for group in groups.values():
        names = {slot.name for slot in group}
        matches = [p for p in telemetry.pools if p.get('name') in names]
        if len(matches) != 1:
            return False
        pool = matches[0]
        members = pool.get('members')
        if (pool.get('name') in covered or pool.get('state') not in {'online', 'unmounted', 'warning'}
                or not isinstance(members, (list, tuple)) or not names.issubset(members)):
            return False
        expected = [s.expected_members for s in group if s.expected_members is not None]
        if not expected or min(expected) < 1 or len(set(members)) < max(expected):
            return False
        covered.add(pool['name'])
    return len(covered) == len(telemetry.pools)


def server_action_command(action: ServerAction, snapshot: Snapshot) -> str:
    """Never interpolate external strings into a server command."""
    if not isinstance(action,ServerAction):
        raise ServerActionError('invalid_action')
    array=snapshot.array
    if array is None or 'var' in snapshot.failed or array.md_state not in {'started','stopped'} or array.parity_state in {'unknown','other'}:
        raise ServerActionError('unknown_state')
    if action in {ServerAction.SERVER_REBOOT,ServerAction.SERVER_SHUTDOWN}:
        return '/sbin/reboot' if action is ServerAction.SERVER_REBOOT else '/sbin/poweroff'
    if action is ServerAction.ARRAY_START:
        if (array.md_state!='stopped' or array.fs_state!='stopped' or array.config_valid is not True
                or array.start_mode!='normal' or array.missing_disks!=0 or array.parity_state!='idle'
                or 'disks' in snapshot.failed or any(d.status in {'missing','not_present','invalid','disabled','new','unknown'} for d in snapshot.disks)):
            raise ServerActionError('array_start_blocked')
        if not _pool_start_safe(snapshot):
            raise ServerActionError('array_start_blocked')
    else:
        if array.md_state!='started' or array.fs_state!='started':
            raise ServerActionError('array_not_started')
        state=array.parity_state
        if action is ServerAction.PARITY_PAUSE:
            if state!='running': raise ServerActionError('parity_not_running')
        elif action is ServerAction.PARITY_RESUME:
            if state!='paused': raise ServerActionError('parity_not_paused')
        elif action is ServerAction.PARITY_CANCEL:
            if state not in {'running','paused'}: raise ServerActionError('parity_not_active')
        elif state!='idle':
            raise ServerActionError('sync_active')
        if action is ServerAction.PARITY_START:
            if 'disks' in snapshot.failed or not any(d.kind.lower()=='parity' and d.status=='ok' for d in snapshot.disks):
                raise ServerActionError('parity_missing')
        if action is ServerAction.MOVER_START and (not array.mover_known or array.mover_active):
            raise ServerActionError('mover_active')
    return _EMCMD+" '"+_PARAMS[action]+"'"
