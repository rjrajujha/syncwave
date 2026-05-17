from app.services.sync_service import SyncService


def test_map_play_at_to_server_time_preserves_lead() -> None:
    server_time = 5_000_000
    host_timestamp = 4_999_200
    play_at = 4_999_880
    mapped = SyncService.map_play_at_to_server_time(
        play_at_ms=play_at,
        host_timestamp_ms=host_timestamp,
        server_time_ms=server_time,
    )
    assert mapped == server_time + (play_at - host_timestamp)


def test_map_play_at_to_server_time_passthrough_when_missing_host_timestamp() -> None:
    play_at = 123456
    mapped = SyncService.map_play_at_to_server_time(
        play_at_ms=play_at,
        host_timestamp_ms=None,
        server_time_ms=999,
    )
    assert mapped == play_at
