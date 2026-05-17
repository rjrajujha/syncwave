from __future__ import annotations

from typing import Any

from ..models.events import EventEnvelope
from ..models.peer import Peer
from .room_service import RoomError, RoomService
from .sync_service import SyncService

SYNC_TARGET_BUFFER_MS = 680


class SignalingService:
    def __init__(self, room_service: RoomService, sync_service: SyncService):
        self._room_service = room_service
        self._sync_service = sync_service
        self._stream_meta_by_room: dict[str, dict[str, Any]] = {}
        self._stream_host_peer_by_room: dict[str, str] = {}

    def handle(self, event: EventEnvelope) -> tuple[EventEnvelope, list[EventEnvelope]]:
        if event.type == 'room.create':
            return self._handle_room_create(event)
        if event.type == 'room.join':
            return self._handle_room_join(event)
        if event.type == 'room.leave':
            return self._handle_room_leave(event)
        if event.type == 'sync.ping':
            return self._handle_sync_ping(event)
        if event.type == 'stream.host_start':
            return self._handle_stream_host_start(event)
        if event.type == 'stream.host_stop':
            return self._handle_stream_host_stop(event)
        if event.type == 'stream.listener_join':
            return self._handle_stream_listener_join(event)
        if event.type == 'stream.audio_chunk':
            return self._handle_stream_audio_chunk(event)
        if event.type == 'stream.ping':
            return self._handle_stream_ping(event)

        return (
            EventEnvelope(
                type='error',
                requestId=event.request_id,
                roomId=event.room_id,
                payload={'message': f'Unsupported event type: {event.type}'},
            ),
            [],
        )

    def _handle_room_create(
        self, event: EventEnvelope
    ) -> tuple[EventEnvelope, list[EventEnvelope]]:
        payload = event.payload

        room_name = str(payload.get('roomName', 'SyncWave Room')).strip() or 'SyncWave Room'
        host_peer_id = str(event.peer_id or payload.get('hostPeerId') or '').strip()
        if not host_peer_id:
            raise RoomError('host peerId is required')

        host_device_name = str(payload.get('deviceName') or 'Unknown Host').strip()
        host_platform = str(payload.get('platform') or 'unknown').strip().lower()
        normalized_pin = RoomService.normalize_pin(
            payload.get('pin') if isinstance(payload.get('pin'), str) else None
        )
        raw_pin_protected = payload.get('pinProtected')
        pin_protected = (
            bool(raw_pin_protected) if raw_pin_protected is not None else None
        )

        room = self._room_service.create_room(
            room_name=room_name,
            host_peer_id=host_peer_id,
            host_device_name=host_device_name,
            host_platform=host_platform,
            pin=normalized_pin,
            room_id=payload.get('roomId')
            if isinstance(payload.get('roomId'), str)
            else None,
            pin_protected=pin_protected,
        )

        response = EventEnvelope(
            type='room.created',
            requestId=event.request_id,
            roomId=room.room_id,
            peerId=host_peer_id,
            payload={'room': self._public_room_payload(room)},
        )
        return response, []

    def _handle_room_join(self, event: EventEnvelope) -> tuple[EventEnvelope, list[EventEnvelope]]:
        if event.room_id is None:
            raise RoomError('roomId is required for room.join')
        if event.peer_id is None:
            raise RoomError('peerId is required for room.join')

        payload = event.payload
        existing_room = self._room_service.get_room(event.room_id)
        already_joined = (
            existing_room is not None
            and any(peer.peer_id == event.peer_id for peer in existing_room.participants)
        )
        listener = Peer(
            peerId=event.peer_id,
            deviceName=str(payload.get('deviceName') or 'Unknown Listener'),
            platform=(str(payload.get('platform') or 'unknown')).lower(),
            role='listener',
        )

        room = self._room_service.join_room(
            room_id=event.room_id,
            peer=listener,
            pin=RoomService.normalize_pin(
                payload.get('pin') if isinstance(payload.get('pin'), str) else None
            ),
        )

        joined_response = EventEnvelope(
            type='room.joined',
            requestId=event.request_id,
            roomId=room.room_id,
            peerId=event.peer_id,
            payload={
                'room': self._public_room_payload(room),
            },
        )

        broadcast_events: list[EventEnvelope] = []
        if not already_joined:
            broadcast_events = [
                EventEnvelope(
                    type='participant.joined',
                    roomId=room.room_id,
                    peerId=event.peer_id,
                    payload={'participant': listener.model_dump(by_alias=True, mode='json')},
                ),
                EventEnvelope(
                    type='stream.listener_count',
                    roomId=room.room_id,
                    payload={'count': self._listener_count(room)},
                ),
            ]

        return joined_response, broadcast_events

    def _handle_room_leave(self, event: EventEnvelope) -> tuple[EventEnvelope, list[EventEnvelope]]:
        if event.room_id is None or event.peer_id is None:
            raise RoomError('roomId and peerId are required for room.leave')

        room = self._room_service.leave_room(room_id=event.room_id, peer_id=event.peer_id)

        response_payload: dict[str, Any] = {
            'status': 'left',
        }
        if room is not None:
            response_payload['roomStatus'] = room.status

        left_response = EventEnvelope(
            type='room.left',
            requestId=event.request_id,
            roomId=event.room_id,
            peerId=event.peer_id,
            payload=response_payload,
        )

        broadcasts = [
            EventEnvelope(
                type='participant.left',
                roomId=event.room_id,
                peerId=event.peer_id,
                payload={'peerId': event.peer_id},
            ),
            EventEnvelope(
                type='stream.listener_count',
                roomId=event.room_id,
                payload={'count': self._listener_count(room) if room is not None else 0},
            ),
        ]

        if room is not None and room.status == 'closed':
            self._stream_meta_by_room.pop(event.room_id, None)
            self._stream_host_peer_by_room.pop(event.room_id, None)
            broadcasts.append(
                EventEnvelope(
                    type='room.closed',
                    roomId=event.room_id,
                    payload={'reason': 'host_left_or_empty_room'},
                )
            )

        return left_response, broadcasts

    def _handle_sync_ping(self, event: EventEnvelope) -> tuple[EventEnvelope, list[EventEnvelope]]:
        return (
            EventEnvelope(
                type='sync.pong',
                requestId=event.request_id,
                roomId=event.room_id,
                peerId=event.peer_id,
                payload={
                    'serverTimestamp': self._sync_service.server_timestamp_ms(),
                },
            ),
            [],
        )

    def _handle_stream_host_start(
        self, event: EventEnvelope
    ) -> tuple[EventEnvelope, list[EventEnvelope]]:
        if event.room_id is None:
            raise RoomError('roomId is required for stream.host_start')

        room = self._room_service.get_room(event.room_id)
        if room is None or room.status != 'active':
            raise RoomError('Room is not active for stream.host_start')

        meta_payload = {
            'roomId': event.room_id,
            'streamStartedAt': event.payload.get('streamStartedAt'),
            'targetBufferMs': event.payload.get('targetBufferMs', SYNC_TARGET_BUFFER_MS),
            'serverTime': self._sync_service.server_timestamp_ms(),
        }
        self._stream_meta_by_room[event.room_id] = meta_payload
        if event.peer_id is not None:
            self._stream_host_peer_by_room[event.room_id] = event.peer_id

        return (
            EventEnvelope(
                type='stream.ready',
                requestId=event.request_id,
                roomId=event.room_id,
                peerId=event.peer_id,
                payload={
                    'roomId': event.room_id,
                    'serverTimestamp': self._sync_service.server_timestamp_ms(),
                    'streamStartedAt': meta_payload.get('streamStartedAt'),
                    'targetBufferMs': meta_payload.get('targetBufferMs'),
                },
            ),
            [
                EventEnvelope(
                    type='stream.meta',
                    roomId=event.room_id,
                    peerId=event.peer_id,
                    payload=meta_payload,
                )
            ],
        )

    def _handle_stream_host_stop(
        self, event: EventEnvelope
    ) -> tuple[EventEnvelope, list[EventEnvelope]]:
        if event.room_id is None:
            raise RoomError('roomId is required for stream.host_stop')

        room = self._room_service.get_room(event.room_id)
        if room is not None and room.status == 'active':
            self._room_service.close_room(room_id=event.room_id)
        self._stream_meta_by_room.pop(event.room_id, None)
        self._stream_host_peer_by_room.pop(event.room_id, None)

        return (
            EventEnvelope(
                type='stream.host_stopped',
                requestId=event.request_id,
                roomId=event.room_id,
                peerId=event.peer_id,
                payload={'roomId': event.room_id},
            ),
            [
                EventEnvelope(
                    type='stream.host_stopped',
                    roomId=event.room_id,
                    peerId=event.peer_id,
                    payload={'roomId': event.room_id},
                ),
                EventEnvelope(
                    type='stream.listener_count',
                    roomId=event.room_id,
                    payload={'count': 0},
                ),
                EventEnvelope(
                    type='room.closed',
                    roomId=event.room_id,
                    payload={'reason': 'host_stopped'},
                )
            ],
        )

    def _handle_stream_listener_join(
        self, event: EventEnvelope
    ) -> tuple[EventEnvelope, list[EventEnvelope]]:
        if event.room_id is None:
            raise RoomError('roomId is required for stream.listener_join')
        if event.peer_id is None:
            raise RoomError('peerId is required for stream.listener_join')

        room = self._room_service.get_room(event.room_id)
        if room is None or room.status != 'active':
            raise RoomError('Room is not active for stream.listener_join')

        if not any(peer.peer_id == event.peer_id for peer in room.participants):
            raise RoomError('room.join is required before stream.listener_join')

        payload = event.payload
        self._room_service.validate_room_pin(
            room_id=event.room_id,
            pin=RoomService.normalize_pin(
                payload.get('pin') if isinstance(payload.get('pin'), str) else None
            ),
        )

        meta_payload = {
            'roomId': event.room_id,
            **self._stream_meta_by_room.get(event.room_id, {}),
        }
        if 'serverTime' not in meta_payload:
            meta_payload['serverTime'] = self._sync_service.server_timestamp_ms()

        return (
            EventEnvelope(
                type='stream.listener_joined',
                requestId=event.request_id,
                roomId=event.room_id,
                peerId=event.peer_id,
                payload=meta_payload,
            ),
            [],
        )

    def _handle_stream_audio_chunk(
        self, event: EventEnvelope
    ) -> tuple[EventEnvelope, list[EventEnvelope]]:
        if event.room_id is None:
            raise RoomError('roomId is required for stream.audio_chunk')

        room = self._room_service.get_room(event.room_id)
        if room is None or room.status != 'active':
            raise RoomError('Room is not active for stream.audio_chunk')

        chunk_payload = dict(event.payload)
        server_time = self._sync_service.server_timestamp_ms()
        chunk_payload['serverTime'] = server_time
        if 'roomId' not in chunk_payload:
            chunk_payload['roomId'] = event.room_id

        mapped_play_at = self._sync_service.map_play_at_to_server_time(
            play_at_ms=chunk_payload.get('playAt'),
            host_timestamp_ms=chunk_payload.get('hostTimestamp'),
            server_time_ms=server_time,
        )
        if mapped_play_at is not None:
            chunk_payload['playAt'] = mapped_play_at
        self._stream_meta_by_room[event.room_id] = {
            **self._stream_meta_by_room.get(event.room_id, {}),
            'roomId': event.room_id,
            'sampleRate': chunk_payload.get('sampleRate'),
            'channelCount': chunk_payload.get('channelCount'),
            'format': chunk_payload.get('format'),
            'durationMs': chunk_payload.get('durationMs'),
            'targetBufferMs': self._stream_meta_by_room.get(event.room_id, {}).get(
                'targetBufferMs',
                SYNC_TARGET_BUFFER_MS,
            ),
            'streamStartedAt': chunk_payload.get(
                'streamStartedAt',
                self._stream_meta_by_room.get(event.room_id, {}).get('streamStartedAt'),
            ),
            'serverTime': chunk_payload['serverTime'],
        }

        return (
            EventEnvelope(
                type='stream.audio_accepted',
                requestId=event.request_id,
                roomId=event.room_id,
                peerId=event.peer_id,
                payload={
                    'roomId': event.room_id,
                    'sequence': chunk_payload.get('sequence'),
                },
            ),
            [
                EventEnvelope(
                    type='stream.audio_chunk',
                    roomId=event.room_id,
                    peerId=event.peer_id,
                    payload=chunk_payload,
                )
            ],
        )

    def _handle_stream_ping(
        self, event: EventEnvelope
    ) -> tuple[EventEnvelope, list[EventEnvelope]]:
        return (
            EventEnvelope(
                type='stream.pong',
                requestId=event.request_id,
                roomId=event.room_id,
                peerId=event.peer_id,
                payload={
                    'serverTime': self._sync_service.server_timestamp_ms(),
                    'clientTime': event.payload.get('clientTime'),
                },
            ),
            [],
        )

    def handle_disconnect(self, *, room_id: str, peer_id: str) -> list[EventEnvelope]:
        is_stream_host = self._stream_host_peer_by_room.get(room_id) == peer_id
        if is_stream_host:
            room = self._room_service.get_room(room_id)
            if room is not None and room.status == 'active':
                room = self._room_service.close_room(room_id=room_id)
            self._stream_meta_by_room.pop(room_id, None)
            self._stream_host_peer_by_room.pop(room_id, None)
        else:
            room = self._room_service.leave_room(room_id=room_id, peer_id=peer_id)

        events = [
            EventEnvelope(
                type='participant.left',
                roomId=room_id,
                peerId=peer_id,
                payload={'peerId': peer_id},
            ),
            EventEnvelope(
                type='stream.listener_count',
                roomId=room_id,
                payload={'count': self._listener_count(room) if room is not None else 0},
            ),
        ]
        if room is not None and room.status == 'closed':
            self._stream_meta_by_room.pop(room_id, None)
            self._stream_host_peer_by_room.pop(room_id, None)
            events.append(
                EventEnvelope(
                    type='room.closed',
                    roomId=room_id,
                    payload={
                        'reason': 'host_relay_disconnected'
                        if is_stream_host
                        else 'host_left_or_empty_room'
                    },
                )
            )
        return events

    def _public_room_payload(self, room) -> dict[str, Any]:
        payload = room.model_dump(by_alias=True, mode='json')
        payload.pop('pinHash', None)
        return payload

    def _listener_count(self, room) -> int:
        return sum(1 for peer in room.participants if peer.role == 'listener')
