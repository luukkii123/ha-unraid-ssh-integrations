"""Actual managed worker timeout/cancellation using synthetic subprocesses."""
import os
import shlex
import subprocess
import time
import pytest
from unraid_ssh import collect
from test_dashboard_review_fixes import _running


@pytest.mark.parametrize('cancel',[False,True])
def test_inventory_worker_kills_nested_foreground_children(monkeypatch,tmp_path,cancel):
    pidfile=tmp_path/'pid'; outputs=tmp_path/'outputs'; outputs.mkdir()
    command="timeout --foreground 10 bash -c "+shlex.quote("trap '' TERM; sleep 30 & echo $! > "+shlex.quote(str(pidfile))+"; wait")
    monkeypatch.setattr(collect,'SECTIONS',(('stat','printf baseline'),))
    monkeypatch.setattr(collect,'dashboard_sections',lambda **kw:(('dash_requested','printf marker'),))
    monkeypatch.setattr(collect,'inventory_sections',lambda:(('inv_usb_mapping',command),('inv_usb','printf usb'),('inv_coral','printf coral')))
    script=collect.build_state_command(include_devices=True,include_usb_mapping=True).replace('mktemp -d','mktemp -d -p '+shlex.quote(str(outputs)))
    process=subprocess.Popen(['bash','-c',script],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    started=time.monotonic()
    try:
        while not pidfile.exists() and time.monotonic()-started<3: time.sleep(.01)
        assert pidfile.exists()
        if cancel: process.terminate()
        stdout,stderr=process.communicate(timeout=4)
        assert not _running(int(pidfile.read_text()))
        assert not list(outputs.iterdir())
        if not cancel:
            sections=collect.split_output(stdout)
            assert sections['stat']=='baseline' and sections['inv_usb']=='usb' and sections['inv_coral']=='coral'
            assert 'inv_usb_mapping' not in sections
            assert time.monotonic()-started<2.5
    finally:
        if process.poll() is None: process.kill(); process.communicate()


def test_all_three_group_budgets_finish_inside_outer_poll_timeout(monkeypatch,tmp_path):
    # More hanging jobs than fit the group prove both queue deadlines, while
    # the inventory contributes its own bounded allowance without displacement.
    monkeypatch.setattr(collect,'SECTIONS',tuple(('fast'+str(i),'sleep 30') for i in range(40)))
    monkeypatch.setattr(collect,'dashboard_sections',lambda **kw:(('dash_requested','printf marker'),)+tuple((name,'sleep 30') for name in ('dash_hardware','dash_smart','dash_pools','dash_shares','dash_gpu','dash_streams')))
    monkeypatch.setattr(collect,'inventory_sections',lambda:(('inv_usb_mapping','sleep 30'),('inv_usb','sleep 30'),('inv_coral','sleep 30')))
    started=time.monotonic()
    result=subprocess.run(['bash','-c',collect.build_state_command(include_devices=True,include_usb_mapping=True)],capture_output=True,text=True,timeout=20)
    elapsed=time.monotonic()-started
    assert result.returncode==0 and result.stdout.rstrip().endswith('@@@ end')
    assert elapsed<19
    # Existing groups remain eight seconds each, inventory gets two seconds.
    command=collect.build_state_command(include_devices=True)
    assert command.count('dashboard_now+800')==2 and command.count('dashboard_now+200')==1
