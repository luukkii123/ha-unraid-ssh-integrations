"""No server action in these tests is ever executed."""
from dataclasses import replace
import pytest
from unraid_ssh.model import build_snapshot
from unraid_ssh import server_actions
from unraid_ssh.parse import parse_var


def snapshot(**fields):
    var={'mdState':'STARTED','fsState':'Started','configValid':'yes','startMode':'Normal','mdResync':'0','mdResyncPos':'0','mdResyncAction':'check','mdResyncCorr':'0','mdNumMissing':'0','shareMoverActive':'no'}
    var.update(fields)
    return build_snapshot({'var':'\n'.join(f'{k}="{v}"' for k,v in var.items()),'disks':'["parity"]\nname="parity"\ndevice="fake-a"\ntype="Parity"\nstatus="DISK_OK"'},None,sampled_at=1)


@pytest.mark.parametrize('action,changes,command',[
('server_reboot',{},'/sbin/reboot'),('server_shutdown',{},'/sbin/poweroff'),
('array_start',{'mdState':'STOPPED','fsState':'Stopped'},"/usr/local/emhttp/plugins/dynamix/scripts/emcmd 'startState=STOPPED&cmdStart=Start'"),
('array_stop',{},"/usr/local/emhttp/plugins/dynamix/scripts/emcmd 'startState=STARTED&cmdStop=Stop'"),
('parity_start',{},"/usr/local/emhttp/plugins/dynamix/scripts/emcmd 'startState=STARTED&cmdCheck=Check'"),
('parity_pause',{'mdResync':'100','mdResyncPos':'10'},"/usr/local/emhttp/plugins/dynamix/scripts/emcmd 'cmdCheckPause='"),
('parity_resume',{'mdResyncPos':'10'},"/usr/local/emhttp/plugins/dynamix/scripts/emcmd 'cmdCheckResume='"),
('parity_cancel',{'mdResyncPos':'10'},"/usr/local/emhttp/plugins/dynamix/scripts/emcmd 'cmdCheckCancel='"),
('spindown_all',{},"/usr/local/emhttp/plugins/dynamix/scripts/emcmd 'cmdSpinDownAll=Apply'"),
('mover_start',{},"/usr/local/emhttp/plugins/dynamix/scripts/emcmd 'cmdStartMover=Move'"),
])
def test_allowlisted_commands(action,changes,command):
    assert server_actions.server_action_command(server_actions.ServerAction(action),snapshot(**changes))==command
    assert 'optionCorrect' not in command


@pytest.mark.parametrize('changes,state',[
({},'idle'),({'mdResync':'100','mdResyncPos':'10'},'running'),({'mdResyncPos':'10'},'paused'),
({'mdResyncAction':'recon','mdResync':'100'},'other'),({'mdResyncAction':'clear'},'other'),
({'mdResync':'bad'},'unknown'),({'mdResyncAction':'unexpected'},'unknown')])
def test_parity_states_are_explicit(changes,state):
    assert snapshot(**changes).array.parity_state==state


@pytest.mark.parametrize('action,changes',[
('array_start',{}),('array_start',{'mdState':'STOPPED','fsState':'Stopped','configValid':'no'}),
('array_start',{'mdState':'STOPPED','fsState':'Stopped','startMode':'Maintenance'}),
('array_start',{'mdState':'STOPPED','fsState':'Stopped','mdNumMissing':'1'}),
('array_start',{'mdState':'NEW_ARRAY','fsState':'Stopped'}),
('array_stop',{'mdResync':'100'}),('spindown_all',{'mdResyncPos':'10'}),
('parity_start',{'mdResyncAction':'recon'}),('parity_pause',{}),('parity_resume',{'mdResync':'100'}),
('parity_cancel',{'mdResyncAction':'clear','mdResync':'100'}),('mover_start',{'shareMoverActive':'yes'}),
('server_shutdown',{'mdState':'UNKNOWN'}),('array_stop',{'mdResyncAction':'unexpected'})])
def test_guards_refuse_unsafe_and_unknown_states(action,changes):
    with pytest.raises(server_actions.ServerActionError):
        server_actions.server_action_command(server_actions.ServerAction(action),snapshot(**changes))


def test_unknown_action_no_parity_and_unobserved_pool_members_are_refused():
    with pytest.raises(server_actions.ServerActionError): server_actions.server_action_command('server_reboot',snapshot())
    with pytest.raises(server_actions.ServerActionError): server_actions.server_action_command(server_actions.ServerAction.PARITY_START,replace(snapshot(),disks=()))
    with pytest.raises(server_actions.ServerActionError): server_actions.server_action_command(server_actions.ServerAction.ARRAY_STOP,replace(snapshot(),array=parse_var('mdState="STARTED"')))
