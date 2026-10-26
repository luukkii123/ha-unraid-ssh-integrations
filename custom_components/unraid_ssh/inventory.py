"""Pure, positive projections of independently sampled inventory sources."""
from __future__ import annotations
from dataclasses import dataclass, field
import json


@dataclass(frozen=True)
class UserAccess:
    name: str
    description: str
    protected: bool
    read_shares: int | None
    write_shares: int | None


@dataclass(frozen=True)
class DashboardInventory:
    users: tuple[UserAccess, ...] | None = None
    usb: tuple | None = None
    coral: tuple | None = None
    smb_enabled: bool | None = None
    mapping_status: str = 'unsupported'
    failed: frozenset[str] = frozenset()
    sample_times: dict[str, float] = field(default_factory=dict)
    known_coral_ids: tuple[str, ...] = ()
    mapping: tuple | None = None


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
    return DashboardInventory(users=users, smb_enabled=enabled, failed=frozenset(failed), sample_times=times)
