"""Snapshot compatibility and scheduled dashboard data ownership."""
from unraid_ssh.model import build_snapshot


def test_snapshot_binds_dashboard_and_keeps_unqueried_inventory():
    first=build_snapshot({'dash_hardware':'{"board_name":"Example Board"}'},None,sampled_at=10)
    assert first.telemetry.hardware == {'board_name':'Example Board'}
    second=build_snapshot({},first,sampled_at=40)
    assert second.telemetry.hardware == first.telemetry.hardware
    assert second.telemetry.sample_times['dash_hardware']==10
    failed=build_snapshot({'dash_requested':'dash_hardware'},second,sampled_at=310)
    assert failed.telemetry.hardware is None
    assert 'dash_hardware' in failed.telemetry.failed
    assert 'dash_hardware' not in failed.failed
    recovered=build_snapshot({'dash_hardware':'{}'},failed,sampled_at=610)
    assert recovered.telemetry.hardware == {}
