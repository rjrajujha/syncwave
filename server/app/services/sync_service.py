import time
from typing import Optional


class SyncService:
    @staticmethod
    def server_timestamp_ms() -> int:
        return int(time.time() * 1000)

    @staticmethod
    def map_play_at_to_server_time(
        *,
        play_at_ms: Optional[int],
        host_timestamp_ms: Optional[int],
        server_time_ms: int,
    ) -> Optional[int]:
        """Maps host-stamped playAt onto the signaling server clock.

        Host chunks stamp playAt using the capture device's wall clock. WAN
        listeners estimate offset against this server's clock via ping/pong,
        so relayed playAt must be expressed on the server timeline:

            playAt_server = serverTime + (playAt_host - hostTimestamp)
        """
        if play_at_ms is None or host_timestamp_ms is None:
            return play_at_ms
        try:
            lead_ms = int(play_at_ms) - int(host_timestamp_ms)
        except (TypeError, ValueError):
            return play_at_ms
        return int(server_time_ms) + lead_ms
