"""Pure, positive projections of independently sampled inventory sources."""
from __future__ import annotations
from dataclasses import dataclass, field
import json
import math
import re
from dataclasses import replace


@dataclass(frozen=True)
class UserAccess:
    name: str
    description: str
    protected: bool
    read_shares: int | None
    write_shares: int | None


@dataclass(frozen=True)
class UsbDevice:
    id: str
    port: str
    name: str
    vendor_id: str
    product_id: str
    speed_mbps: float | None
    is_hub: bool
    state: str
    assigned_vm: str | None = None
    remote_host: str | None = None
    assignment_source: str | None = None


@dataclass(frozen=True)
class CoralDevice:
    id: str
    name: str
    transport: str
    state: str
    temperature_c: float | None
    driver_version: str | None
    raw_status: str | None
    source: str


@dataclass(frozen=True)
class DashboardInventory:
    users: tuple[UserAccess, ...] | None = None
    usb: tuple[UsbDevice, ...] | None = None
    coral: tuple[CoralDevice, ...] | None = None
    smb_enabled: bool | None = None
    mapping_status: str = 'unsupported'
    failed: frozenset[str] = frozenset()
    sample_times: dict[str, float] = field(default_factory=dict)
    known_coral_ids: tuple[str, ...] = ()
    mapping: dict | None = None


def _users(text):
    row = json.loads(text)
    users = row['users']
    if not isinstance(users, list): raise ValueError('invalid users')
    var = row.get('var') or {}
    enabled = {'yes': True, 'no': False}.get(var.get('shareSMBEnabled'))
    rights = row.get('rights')
    valid = enabled is True and isinstance(rights, dict)
    names = set(row.get('shares') or ())
    # Disk exports are conditional; flash remains independent of array state.
    if var.get('shareDisk') == 'yes' and var.get('fsState') == 'Started':
        if row.get('disks') is None: valid = False
        names.update(row.get('disks') or ())
    elif var.get('shareDisk') not in {'yes', 'no'} or var.get('fsState') is None:
        valid = False
    names.add('flash')
    exports = []
    for name in names:
        rule = rights.get(name) if isinstance(rights, dict) else None
        if rule is None:
            if name != 'flash': valid = False
            continue
        if rule.get('export') == '-': continue
        mode = rule.get('security')
        if rule.get('export') is None or mode not in {'public', 'secure', 'private'}:
            valid = False
            continue
        readers = {v.strip() for v in rule.get('readList', '').split(',') if v.strip()}
        writers = {v.strip() for v in rule.get('writeList', '').split(',') if v.strip()}
        exports.append((mode, readers | writers, writers))
    result = []
    for user in users:
        name = user['name']
        if not isinstance(name, str) or not name: raise ValueError('invalid user name')
        read = write = None
        if valid and name != 'root':
            read = sum(mode in {'public', 'secure'} or name in readers for mode, readers, writers in exports)
            write = sum(mode == 'public' or name in writers for mode, readers, writers in exports)
        result.append(UserAccess(name, str(user.get('desc', '')), user.get('passwd') == 'yes', read, write))
    return tuple(result), enabled


def _number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def _usb(text, mapping):
    rows = json.loads(text)
    if not isinstance(rows, list): raise ValueError('invalid USB inventory')
    result = []
    valid = [row for row in rows if isinstance(row, dict) and re.fullmatch(r'(?:usb[0-9]+|[0-9]+-[0-9]+(?:\.[0-9]+)*)', str(row.get('port', '')))]
    for row in valid:
        port = row['port']
        vid, pid = str(row['idVendor']).lower(), str(row['idProduct']).lower()
        if not re.fullmatch(r'[0-9a-f]{4}', vid) or not re.fullmatch(r'[0-9a-f]{4}', pid): raise ValueError('invalid USB IDs')
        state = 'host'; vm = source = None
        active = (mapping or {}).get('active', [])
        exact = [m for m in active if str(m.get('bus', '')) == str(row.get('busnum')) and str(m.get('dev', '')) == str(row.get('devnum'))]
        pairs = [m for m in active if m.get('vendor', '').lower().removeprefix('0x') == vid and m.get('product', '').lower().removeprefix('0x') == pid and not m.get('bus')]
        if exact:
            if len(exact) == 1: vm = exact[0]['vm']; source = 'libvirt_address'; state = 'vm'
            else: state = 'unknown'
        elif pairs:
            count = sum(r.get('idVendor', '').lower() == vid and r.get('idProduct', '').lower() == pid for r in valid)
            if len(pairs) == 1 and count == 1: vm = pairs[0]['vm']; source = 'libvirt_vid_pid'; state = 'vm'
            else: state = 'unknown'
        if state == 'host':
            if str(row.get('authorized')) == '0': state = 'unauthorized'
            elif str(row.get('usbip_status')) == '2': state = 'remote'; source = 'sysfs_usbip'
            elif str(row.get('usbip_status')) == '1': state = 'usbip_bound'; source = 'sysfs_usbip'
        # Port-only plugin caches cannot bind a remote name to this device.
        speed = _number(row.get('speed'))
        result.append(UsbDevice(port, port, str(row.get('product') or row.get('manufacturer') or f'{vid}:{pid}'), vid, pid,
                                speed if speed is not None and speed > 0 else None,
                                str(row.get('bDeviceClass', '')).lower() == '09', state, vm, None, source))
    return tuple(result)


def _coral(text):
    rows = json.loads(text)
    if not isinstance(rows, list): raise ValueError('invalid Coral inventory')
    result = []
    for row in rows:
        address = row.get('pci_address', '')
        if re.fullmatch(r'[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]', address):
            identifier = address.lower(); source = 'pci_address'
        else:
            identifier = row['path']; source = 'sysfs_class'
            if not re.fullmatch(r'/sys/class/apex/apex_[0-9]+', identifier): raise ValueError('invalid class identity')
        raw = row.get('status')
        state = 'ready' if raw == 'ALIVE' else 'error' if raw in {'DEAD', 'LAMED', 'DRIVER_EXITING'} else 'unknown'
        temperature = _number(row.get('temp'))
        if temperature is not None:
            temperature /= 1000
            if temperature <= -50 or temperature > 150: temperature = None
        result.append(CoralDevice(identifier, f'Coral TPU {len(result)+1}', 'pci', state, temperature, row.get('driver_version'), raw, source))
    return tuple(result)


def parse_inventory_sections(sections: dict[str, str], previous: DashboardInventory | None = None, *, sampled_at: float = 0) -> DashboardInventory:
    """Missing requested sources fail; deliberately skipped groups retain data."""
    previous = previous or DashboardInventory()
    failed = set(previous.failed)
    times = dict(previous.sample_times)
    users = previous.users
    enabled = previous.smb_enabled
    requested = set(sections.get('inv_requested', 'inv_users inv_usb inv_usb_mapping inv_coral').split())
    if 'inv_users' in requested or 'inv_users' in sections:
        times['inv_users'] = sampled_at
        try:
            users, enabled = _users(sections['inv_users'])
            failed.discard('inv_users')
        except (KeyError, ValueError, TypeError, AttributeError):
            users = None; enabled = None; failed.add('inv_users')
    def read(source, parser, previous_value):
        if source not in requested and source not in sections: return previous_value
        times[source] = sampled_at
        try:
            value = parser(sections[source]); failed.discard(source); return value
        except (KeyError, ValueError, TypeError, AttributeError):
            failed.add(source); return None
    def mapping_parser(text):
        value = json.loads(text)
        if not isinstance(value, dict) or not isinstance(value.get('active'), list) or type(value.get('supported')) is not bool:
            raise ValueError('invalid USB mapping')
        # Positive projection also protects retained internal state from caches.
        return {'supported': value['supported'], 'active': [{k: row[k] for k in ('vm', 'bus', 'dev', 'vendor', 'product') if k in row} for row in value['active']]}
    mapping = read('inv_usb_mapping', mapping_parser, previous.mapping)
    usb = read('inv_usb', lambda text: _usb(text, mapping), previous.usb)
    coral = read('inv_coral', _coral, previous.coral)
    if coral is not None:
        coral = tuple(row for row in coral if row.transport == 'pci')
        coral += tuple(CoralDevice('usb:' + row.id, 'Coral USB TPU', 'usb', 'unknown', None, None, None, 'usb_vid_pid')
                       for row in usb or () if (row.vendor_id, row.product_id) in {('18d1', '9302'), ('1a6e', '089a')})
    known = previous.known_coral_ids if coral is None else tuple(row.id for row in coral if row.transport == 'pci')
    status = 'partial' if mapping is None else 'ok' if mapping['supported'] else 'unsupported'
    return DashboardInventory(users=users, usb=usb, coral=coral, smb_enabled=enabled, mapping_status=status,
                              failed=frozenset(failed), sample_times=times, mapping=mapping, known_coral_ids=known)
