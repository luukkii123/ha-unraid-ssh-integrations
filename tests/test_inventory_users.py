"""Synthetic SMB permissions: set membership, never traffic counters."""
import json
from dataclasses import asdict
import pytest
from unraid_ssh.inventory import parse_inventory_sections
from unraid_ssh.collect_inventory import inventory_sections


def data():
    return dict(users=[dict(name='root',desc='Admin',passwd='yes'),dict(name='Ada',desc='<b>Text</b>',passwd='no'),dict(name='Grace',desc='',passwd='yes'),dict(name='guest',desc='',passwd='no')],var=dict(shareSMBEnabled='yes',shareDisk='yes',fsState='Started'),shares=['public','secure','private','hidden','private'],disks=['disk1'],rights={'public':dict(export='e',security='public'),'secure':dict(export='e',security='secure',writeList=' Ada '),'private':dict(export='e',security='private',readList=' Ada,Grace ',writeList='Ada'),'hidden':dict(export='-',security='public'),'disk1':dict(export='e',security='private',writeList='Grace'),'flash':dict(export='e',security='private',readList='Grace')})


def parse(row):
    return parse_inventory_sections({'inv_users':json.dumps(row)})


def test_permission_sets_disk_flash_and_positive_projection():
    result=parse(data())
    root,ada,grace,guest=result.users
    assert (root.read_shares,root.write_shares)==(None,None)
    assert (ada.read_shares,ada.write_shares)==(3,3)
    assert (grace.read_shares,grace.write_shares)==(5,2)
    assert (guest.read_shares,guest.write_shares)==(2,1)
    assert ada.description=='<b>Text</b>' and ada.protected is False
    assert set(asdict(ada))=={'name','description','protected','read_shares','write_shares'}
    assert result.smb_enabled is True
    assert not {'passwd','rights','writeList','readList'} & asdict(ada).keys()


@pytest.mark.parametrize('patch',[{'shareDisk':'no'},{'fsState':'Stopped'}])
def test_disk_not_exported_but_flash_still_counts(patch):
    row=data(); row['var'].update(patch)
    grace=parse(row).users[2]
    assert (grace.read_shares,grace.write_shares)==(4,1)


@pytest.mark.parametrize('change',['disabled','rights','mode','var'])
def test_unknown_permissions_never_become_zero(change):
    row=data()
    if change=='disabled': row['var']['shareSMBEnabled']='no'
    if change=='rights': row['rights']=None
    if change=='mode': row['rights']['public']['security']='alien'
    if change=='var': row['var']={}
    assert all(u.read_shares is None and u.write_shares is None for u in parse(row).users)


def test_no_access_is_zero_missing_users_is_failure():
    row=data(); row['shares']=['private']; row['disks']=[]; row['rights'].pop('flash')
    assert parse(row).users[3].read_shares==0
    assert parse_inventory_sections({}).users is None
    assert 'inv_users' in parse_inventory_sections({}).failed
    row['users']=[]
    assert parse(row).users==()


def test_reader_is_fixed_and_projects_before_transport():
    command=dict(inventory_sections())['inv_users']
    assert 'shadow' not in command
    assert 'parse_ini' in command
    assert 'readList' in command and 'writeList' in command
