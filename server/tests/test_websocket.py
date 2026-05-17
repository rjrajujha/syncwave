from __future__ import annotations

import os
import re
from contextlib import contextmanager
from typing import Iterator, Optional

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app
from app.models.events import EventEnvelope
from app.services.room_service import RoomError, RoomService
from app.services.signaling_service import SignalingService
from app.services.sync_service import SyncService


@contextmanager
def make_client(env: Optional[dict[str, str]] = None) -> Iterator[TestClient]:
    original: dict[str, str | None] = {}
    if env:
        for key, value in env.items():
            original[key] = os.environ.get(key)
            os.environ[key] = value

    get_settings.cache_clear()
    app = create_app()

    try:
        with TestClient(app) as client:
            yield client
    finally:
        get_settings.cache_clear()
        for key, previous in original.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous


def send_hello(
    websocket,
    *,
    protocol_version: str = '1',
    pin: Optional[str] = None,
    client_role: Optional[str] = None,
    listener_only: bool = False,
) -> dict:
    payload = {
        'appName': 'SyncWave App',
        'appVersion': '1.1.5',
        'protocolVersion': protocol_version,
        'clientPlatform': 'android',
    }
    if pin is not None:
        payload['serverConnectionPin'] = pin
    if client_role is not None:
        payload['clientRole'] = client_role
    if listener_only:
        payload['listenerOnly'] = True

    websocket.send_json(
        {
            'type': 'server.hello',
            'requestId': 'hello-1',
            'payload': payload,
        }
    )
    return websocket.receive_json()


def create_room(
    websocket,
    *,
    request_id: str = 'create-1',
    pin: Optional[str] = None,
) -> str:
    payload = {
        'roomName': 'Test Room',
        'deviceName': 'Host Device',
        'platform': 'android',
    }
    if pin is not None:
        payload['pin'] = pin

    websocket.send_json(
        {
            'type': 'room.create',
            'requestId': request_id,
            'payload': payload,
        }
    )
    response = websocket.receive_json()
    assert response['type'] == 'room.created'
    assert re.match(r'^WAN-[A-Z0-9]{5}$', response['roomId'])
    return response['roomId']


def make_signaling_service() -> SignalingService:
    return SignalingService(
        room_service=RoomService(
            ttl_seconds=3600,
            max_participants=20,
            pin_hash_secret='test-secret',
        ),
        sync_service=SyncService(),
    )


def test_websocket_connect_and_server_hello_success() -> None:
    with make_client() as client:
        with client.websocket_connect('/ws?peerId=peer_test') as websocket:
            ready = websocket.receive_json()
            assert ready['type'] == 'connection.ready'
            hello_response = send_hello(websocket)

    assert hello_response['type'] == 'server.ready'
    assert hello_response['payload']['serverVersion'] == '1.1.5'


def test_server_hello_requires_auth_when_enabled() -> None:
    env = {
        'REQUIRE_SERVER_CONNECTION_PIN': 'true',
        'SERVER_CONNECTION_PIN': '12345678',
    }
    with make_client(env) as client:
        with client.websocket_connect('/ws?peerId=peer_auth') as websocket:
            websocket.receive_json()
            response = send_hello(websocket)

    assert response['type'] == 'server.auth_required'


def test_host_handshake_requires_server_pin_when_auth_enabled() -> None:
    env = {
        'REQUIRE_SERVER_CONNECTION_PIN': 'true',
        'SERVER_CONNECTION_PIN': '12345678',
    }
    with make_client(env) as client:
        with client.websocket_connect('/ws?peerId=host_auth') as websocket:
            websocket.receive_json()
            response = send_hello(websocket)

    assert response['type'] == 'server.auth_required'
    assert response['payload']['code'] == 'server_connection_pin_required'


def test_listener_handshake_succeeds_without_server_pin() -> None:
    env = {
        'REQUIRE_SERVER_CONNECTION_PIN': 'true',
        'SERVER_CONNECTION_PIN': '12345678',
        'ALLOW_LISTENER_ONLY_WITHOUT_SERVER_PIN': 'false',
    }
    with make_client(env) as client:
        with client.websocket_connect('/ws?peerId=browser_listener_public') as websocket:
            websocket.receive_json()
            response = send_hello(
                websocket,
                client_role='listener',
                listener_only=True,
            )

    assert response['type'] == 'server.ready'
    assert response['payload']['listenerOnly'] is True
    assert response['payload']['authenticated'] is False
    assert response['payload']['authenticationRequired'] is False


def test_server_hello_auth_failed_with_wrong_pin() -> None:
    env = {
        'REQUIRE_SERVER_CONNECTION_PIN': 'true',
        'SERVER_CONNECTION_PIN': '12345678',
    }
    with make_client(env) as client:
        with client.websocket_connect('/ws?peerId=peer_auth_fail') as websocket:
            websocket.receive_json()
            response = send_hello(websocket, pin='87654321')

    assert response['type'] == 'server.auth_failed'


def test_server_hello_rejects_malformed_server_pin() -> None:
    env = {
        'REQUIRE_SERVER_CONNECTION_PIN': 'true',
        'SERVER_CONNECTION_PIN': '12345678',
    }
    with make_client(env) as client:
        with client.websocket_connect('/ws?peerId=peer_auth_malformed') as websocket:
            websocket.receive_json()
            response = send_hello(websocket, pin='123456789')

    assert response['type'] == 'server.auth_failed'
    assert response['payload']['code'] == 'server_connection_pin_invalid'


def test_listener_only_peer_cannot_send_host_relay_events() -> None:
    env = {
        'REQUIRE_SERVER_CONNECTION_PIN': 'true',
        'SERVER_CONNECTION_PIN': '12345678',
    }
    with make_client(env) as client:
        with client.websocket_connect('/ws?peerId=listener_only_relay') as websocket:
            websocket.receive_json()
            assert (
                send_hello(
                    websocket,
                    client_role='listener',
                    listener_only=True,
                )['type']
                == 'server.ready'
            )
            websocket.send_json(
                {
                    'type': 'stream.audio_chunk',
                    'requestId': 'audio-denied-1',
                    'roomId': 'WAN-RM01P',
                    'payload': {'sequence': 1},
                }
            )
            response = websocket.receive_json()

    assert response['type'] == 'stream.failed'
    assert response['payload']['code'] == 'server_connection_pin_required'


def test_server_hello_unsupported_protocol_version() -> None:
    with make_client() as client:
        with client.websocket_connect('/ws?peerId=peer_version') as websocket:
            websocket.receive_json()
            response = send_hello(websocket, protocol_version='2')

    assert response['type'] == 'server.unsupported_version'


def test_room_create_join_leave_and_sync_success() -> None:
    with make_client() as client:
        with client.websocket_connect('/ws?peerId=host_a') as host:
            host.receive_json()
            assert send_hello(host)['type'] == 'server.ready'
            room_id = create_room(host)

            with client.websocket_connect('/ws?peerId=listener_a') as listener:
                listener.receive_json()
                assert send_hello(listener)['type'] == 'server.ready'

                listener.send_json(
                    {
                        'type': 'room.join',
                        'requestId': 'join-1',
                        'roomId': room_id,
                        'payload': {
                            'deviceName': 'Listener Device',
                            'platform': 'ios',
                        },
                    }
                )
                joined = listener.receive_json()
                assert joined['type'] == 'room.joined'

                participant_joined = host.receive_json()
                assert participant_joined['type'] == 'participant.joined'

                listener.send_json(
                    {
                        'type': 'sync.ping',
                        'requestId': 'sync-1',
                        'roomId': room_id,
                        'payload': {},
                    }
                )
                sync_response = listener.receive_json()
                assert sync_response['type'] == 'sync.pong'

                listener.send_json(
                    {
                        'type': 'room.leave',
                        'requestId': 'leave-1',
                        'roomId': room_id,
                        'payload': {},
                    }
                )
                left = listener.receive_json()
    assert left['type'] == 'room.left'


def test_duplicate_room_join_does_not_emit_duplicate_listener_broadcasts() -> None:
    signaling = make_signaling_service()
    created, _ = signaling.handle(
        EventEnvelope(
            type='room.create',
            peerId='host_direct',
            payload={
                'roomName': 'Direct Test',
                'deviceName': 'Host',
                'platform': 'android',
            },
        )
    )
    room_id = created.room_id
    assert room_id is not None

    join_event = EventEnvelope(
        type='room.join',
        requestId='join-direct-1',
        roomId=room_id,
        peerId='listener_direct',
        payload={'deviceName': 'Web Listener', 'platform': 'web'},
    )
    joined, first_broadcasts = signaling.handle(join_event)
    joined_again, duplicate_broadcasts = signaling.handle(
        join_event.model_copy(update={'request_id': 'join-direct-2'})
    )

    assert joined.type == 'room.joined'
    assert joined_again.type == 'room.joined'
    assert [event.type for event in first_broadcasts] == [
        'participant.joined',
        'stream.listener_count',
    ]
    assert duplicate_broadcasts == []
    participants = joined_again.payload['room']['participants']
    assert [peer['peerId'] for peer in participants].count('listener_direct') == 1


def test_late_stream_listener_receives_cached_audio_metadata() -> None:
    signaling = make_signaling_service()
    created, _ = signaling.handle(
        EventEnvelope(
            type='room.create',
            peerId='host_meta',
            payload={
                'roomName': 'Metadata Test',
                'deviceName': 'Host',
                'platform': 'android',
            },
        )
    )
    room_id = created.room_id
    assert room_id is not None

    ready, _ = signaling.handle(
        EventEnvelope(
            type='stream.host_start',
            roomId=room_id,
            peerId='host_meta',
            payload={'streamStartedAt': 111, 'targetBufferMs': 680},
        )
    )
    assert ready.type == 'stream.ready'

    signaling.handle(
        EventEnvelope(
            type='stream.audio_chunk',
            roomId=room_id,
            peerId='host_meta',
            payload={
                'sequence': 7,
                'sampleRate': 48000,
                'channelCount': 1,
                'durationMs': 40,
                'format': 'pcm16',
                'payload': 'AAAA',
                'hostTimestamp': 1000,
                'playAt': 1680,
            },
        )
    )

    signaling.handle(
        EventEnvelope(
            type='room.join',
            requestId='join-meta-late',
            roomId=room_id,
            peerId='late_listener',
            payload={'deviceName': 'Late Listener', 'platform': 'web'},
        )
    )

    response, broadcasts = signaling.handle(
        EventEnvelope(
            type='stream.listener_join',
            requestId='listener-meta-1',
            roomId=room_id,
            peerId='late_listener',
            payload={'roomId': room_id},
        )
    )

    assert broadcasts == []
    assert response.type == 'stream.listener_joined'
    assert response.payload['targetBufferMs'] == 680
    assert response.payload['sampleRate'] == 48000
    assert response.payload['channelCount'] == 1
    assert response.payload['durationMs'] == 40


def test_stream_listener_join_requires_active_room() -> None:
    signaling = make_signaling_service()

    with pytest.raises(RoomError, match='Room is not active'):
        signaling.handle(
            EventEnvelope(
                type='stream.listener_join',
                requestId='listener-meta-missing',
                roomId='WAN-NONE1',
                peerId='late_listener',
                payload={'roomId': 'WAN-NONE1'},
            )
        )


def test_web_listener_platform_can_join_wan_room() -> None:
    with make_client() as client:
        with client.websocket_connect('/ws?peerId=host_web_listener') as host:
            host.receive_json()
            assert send_hello(host)['type'] == 'server.ready'
            room_id = create_room(host)

            with client.websocket_connect('/ws?peerId=browser_listener') as listener:
                listener.receive_json()
                assert send_hello(listener)['type'] == 'server.ready'
                listener.send_json(
                    {
                        'type': 'room.join',
                        'requestId': 'join-web-1',
                        'roomId': room_id,
                        'payload': {
                            'deviceName': 'Web Listener',
                            'platform': 'web',
                        },
                    }
                )
                joined = listener.receive_json()

    assert joined['type'] == 'room.joined'
    assert 'pinHash' not in joined['payload']['room']
    participants = joined['payload']['room']['participants']
    web_participant = next(
        participant for participant in participants if participant['peerId'] == 'browser_listener'
    )
    assert web_participant['platform'] == 'web'


def test_protected_server_allows_browser_listener_join_with_room_pin_only() -> None:
    env = {
        'REQUIRE_SERVER_CONNECTION_PIN': 'true',
        'SERVER_CONNECTION_PIN': '12345678',
    }
    with make_client(env) as client:
        with client.websocket_connect('/ws?peerId=host_protected') as host:
            host.receive_json()
            assert send_hello(host, pin='12345678')['type'] == 'server.ready'
            room_id = create_room(host, pin='654321')

            with client.websocket_connect('/ws?peerId=browser_protected') as listener:
                listener.receive_json()
                assert (
                    send_hello(
                        listener,
                        pin='12345678',
                        client_role='listener',
                        listener_only=True,
                    )['type']
                    == 'server.ready'
                )
                listener.send_json(
                    {
                        'type': 'room.join',
                        'requestId': 'join-protected-listener',
                        'roomId': room_id,
                        'payload': {
                            'deviceName': 'Web Listener',
                            'platform': 'web',
                            'pin': '654321',
                        },
                    }
                )
                joined = listener.receive_json()

    assert joined['type'] == 'room.joined'
    assert joined['payload']['room']['pinProtected'] is True
    assert 'pinHash' not in joined['payload']['room']


def test_invalid_event_returns_typed_error() -> None:
    with make_client() as client:
        with client.websocket_connect('/ws?peerId=peer_invalid') as websocket:
            websocket.receive_json()
            websocket.send_json({'payload': {'x': 1}})
            response = websocket.receive_json()

    assert response['type'] == 'error'
    assert response['payload']['code'] == 'invalid_event_schema'


def test_room_leave_failure_uses_typed_error_event() -> None:
    with make_client() as client:
        with client.websocket_connect('/ws?peerId=peer_leave_fail') as websocket:
            websocket.receive_json()
            assert send_hello(websocket)['type'] == 'server.ready'
            websocket.send_json(
                {
                    'type': 'room.leave',
                    'requestId': 'leave-fail-1',
                    'payload': {},
                }
            )
            response = websocket.receive_json()

    assert response['type'] == 'room.leave_failed'


def test_disconnect_cleanup_broadcasts_participant_left_and_room_closed() -> None:
    with make_client() as client:
        with client.websocket_connect('/ws?peerId=host_disconnect') as host:
            host.receive_json()
            assert send_hello(host)['type'] == 'server.ready'
            room_id = create_room(host)

            with client.websocket_connect('/ws?peerId=listener_disconnect') as listener:
                listener.receive_json()
                assert send_hello(listener)['type'] == 'server.ready'

                listener.send_json(
                    {
                        'type': 'room.join',
                        'requestId': 'join-disconnect-1',
                        'roomId': room_id,
                        'payload': {
                            'deviceName': 'Listener',
                            'platform': 'android',
                        },
                    }
                )
                joined = listener.receive_json()
                assert joined['type'] == 'room.joined'

                host.receive_json()  # participant.joined

                host.close()

                event_one = listener.receive_json()
                event_two = listener.receive_json()
                event_three = listener.receive_json()
                event_types = {event_one['type'], event_two['type'], event_three['type']}

    assert 'participant.left' in event_types
    assert 'stream.listener_count' in event_types
    assert 'room.closed' in event_types


def test_stream_ping_returns_server_time() -> None:
    with make_client() as client:
        with client.websocket_connect('/ws?peerId=peer_stream_ping') as websocket:
            websocket.receive_json()
            assert send_hello(websocket)['type'] == 'server.ready'
            websocket.send_json(
                {
                    'type': 'stream.ping',
                    'requestId': 'stream-ping-1',
                    'payload': {'clientTime': 12345},
                }
            )
            response = websocket.receive_json()

    assert response['type'] == 'stream.pong'
    assert 'serverTime' in response['payload']
    assert response['payload']['clientTime'] == 12345


def test_protected_room_join_without_pin_rejected() -> None:
    with make_client() as client:
        with client.websocket_connect('/ws?peerId=host_pin_gate') as host:
            host.receive_json()
            assert send_hello(host)['type'] == 'server.ready'
            room_id = create_room(host, pin='112233')

            with client.websocket_connect('/ws?peerId=listener_pin_gate') as listener:
                listener.receive_json()
                assert send_hello(listener)['type'] == 'server.ready'
                listener.send_json(
                    {
                        'type': 'room.join',
                        'requestId': 'join-pin-gate-1',
                        'roomId': room_id,
                        'payload': {
                            'deviceName': 'Listener',
                            'platform': 'web',
                        },
                    }
                )
                response = listener.receive_json()

    assert response['type'] == 'room.join_failed'
    assert 'PIN' in response['payload']['message']


def test_stream_listener_join_without_pin_on_protected_room_rejected() -> None:
    with make_client() as client:
        with client.websocket_connect('/ws?peerId=host_lj_pin') as host:
            host.receive_json()
            assert send_hello(host)['type'] == 'server.ready'
            room_id = create_room(host, pin='445566')

            with client.websocket_connect('/ws?peerId=listener_lj_pin') as listener:
                listener.receive_json()
                assert send_hello(listener)['type'] == 'server.ready'
                listener.send_json(
                    {
                        'type': 'room.join',
                        'requestId': 'join-lj-pin-1',
                        'roomId': room_id,
                        'payload': {
                            'deviceName': 'Listener',
                            'platform': 'web',
                            'pin': '445566',
                        },
                    }
                )
                assert listener.receive_json()['type'] == 'room.joined'
                host.receive_json()  # participant.joined
                host.receive_json()  # stream.listener_count

                listener.send_json(
                    {
                        'type': 'stream.listener_join',
                        'requestId': 'listener-join-no-pin',
                        'roomId': room_id,
                        'payload': {'roomId': room_id},
                    }
                )
                response = listener.receive_json()

    assert response['type'] == 'stream.failed'
    assert 'PIN' in response['payload']['message']


def test_room_create_pin_protected_without_pin_rejected() -> None:
    with make_client() as client:
        with client.websocket_connect('/ws?peerId=host_create_pin') as host:
            host.receive_json()
            assert send_hello(host)['type'] == 'server.ready'
            host.send_json(
                {
                    'type': 'room.create',
                    'requestId': 'create-pin-protected',
                    'payload': {
                        'roomName': 'Protected',
                        'deviceName': 'Host',
                        'platform': 'android',
                        'pinProtected': True,
                    },
                }
            )
            response = host.receive_json()

    assert response['type'] == 'room.create_failed'
    assert 'PIN' in response['payload']['message']


def test_stream_listener_join_without_room_join_rejected() -> None:
    with make_client() as client:
        with client.websocket_connect('/ws?peerId=host_skip_join') as host:
            host.receive_json()
            assert send_hello(host)['type'] == 'server.ready'
            room_id = create_room(host)

            with client.websocket_connect('/ws?peerId=listener_skip_join') as listener:
                listener.receive_json()
                assert send_hello(listener, client_role='listener', listener_only=True)[
                    'type'
                ] == 'server.ready'
                listener.send_json(
                    {
                        'type': 'stream.listener_join',
                        'requestId': 'listener-skip-join-1',
                        'roomId': room_id,
                        'payload': {'roomId': room_id},
                    }
                )
                response = listener.receive_json()

    assert response['type'] == 'stream.failed'
    assert 'room.join' in response['payload']['message']


def test_stream_audio_chunk_routes_to_room_listeners() -> None:
    with make_client() as client:
        with client.websocket_connect('/ws?peerId=host_stream') as host:
            host.receive_json()
            assert send_hello(host)['type'] == 'server.ready'
            room_id = create_room(host)

            with client.websocket_connect('/ws?peerId=listener_stream') as listener:
                listener.receive_json()
                assert send_hello(listener)['type'] == 'server.ready'

                listener.send_json(
                    {
                        'type': 'room.join',
                        'requestId': 'join-stream-1',
                        'roomId': room_id,
                        'payload': {'deviceName': 'Listener', 'platform': 'ios'},
                    }
                )
                joined = listener.receive_json()
                assert joined['type'] == 'room.joined'

                host.receive_json()  # participant.joined
                host.receive_json()  # stream.listener_count

                host_timestamp = int(__import__('time').time() * 1000) - 10_000
                play_at = host_timestamp + 680
                host.send_json(
                    {
                        'type': 'stream.audio_chunk',
                        'requestId': 'audio-1',
                        'roomId': room_id,
                        'payload': {
                            'sequence': 1,
                            'sampleRate': 48000,
                            'channelCount': 1,
                            'durationMs': 40,
                            'format': 'pcm16',
                            'payload': 'AAAB',
                            'hostTimestamp': host_timestamp,
                            'playAt': play_at,
                        },
                    }
                )
                accepted = host.receive_json()
                routed = listener.receive_json()

    assert accepted['type'] == 'stream.audio_accepted'
    assert routed['type'] == 'stream.audio_chunk'
    assert routed['payload']['sequence'] == 1
    assert routed['payload']['playAt'] == routed['payload']['serverTime'] + 680


def test_listener_count_updates_on_join_and_leave() -> None:
    with make_client() as client:
        with client.websocket_connect('/ws?peerId=host_count') as host:
            host.receive_json()
            assert send_hello(host)['type'] == 'server.ready'
            room_id = create_room(host)

            with client.websocket_connect('/ws?peerId=listener_count') as listener:
                listener.receive_json()
                assert send_hello(listener)['type'] == 'server.ready'
                listener.send_json(
                    {
                        'type': 'room.join',
                        'requestId': 'join-count-1',
                        'roomId': room_id,
                        'payload': {'deviceName': 'Listener', 'platform': 'android'},
                    }
                )
                listener.receive_json()  # room.joined
                host.receive_json()  # participant.joined
                join_count = host.receive_json()

                listener.send_json(
                    {
                        'type': 'room.leave',
                        'requestId': 'leave-count-1',
                        'roomId': room_id,
                        'payload': {},
                    }
                )
                listener.receive_json()  # room.left
                leave_event = host.receive_json()
                leave_count = host.receive_json()

    assert join_count['type'] == 'stream.listener_count'
    assert join_count['payload']['count'] == 1
    assert leave_event['type'] == 'participant.left'
    assert leave_count['type'] == 'stream.listener_count'
    assert leave_count['payload']['count'] == 0


def test_stream_host_stop_closes_rest_created_room() -> None:
    with make_client() as client:
        created = client.post('/rooms', json={'roomName': 'WAN', 'roomId': 'WAN-HSTOP'})
        assert created.status_code == 201

        with client.websocket_connect('/ws?peerId=relay_host_stop') as host:
            host.receive_json()
            assert send_hello(host)['type'] == 'server.ready'
            host.send_json(
                {
                    'type': 'stream.host_start',
                    'requestId': 'host-start-1',
                    'roomId': 'WAN-HSTOP',
                    'payload': {'roomId': 'WAN-HSTOP', 'targetBufferMs': 680},
                }
            )
            assert host.receive_json()['type'] == 'stream.ready'

            with client.websocket_connect('/ws?peerId=listener_host_stop') as listener:
                listener.receive_json()
                assert send_hello(listener)['type'] == 'server.ready'
                listener.send_json(
                    {
                        'type': 'room.join',
                        'requestId': 'join-host-stop-1',
                        'roomId': 'WAN-HSTOP',
                        'payload': {'deviceName': 'Listener', 'platform': 'web'},
                    }
                )
                assert listener.receive_json()['type'] == 'room.joined'
                host.receive_json()  # participant.joined
                host.receive_json()  # stream.listener_count

                host.send_json(
                    {
                        'type': 'stream.host_stop',
                        'requestId': 'host-stop-1',
                        'roomId': 'WAN-HSTOP',
                        'payload': {'roomId': 'WAN-HSTOP'},
                    }
                )
                assert host.receive_json()['type'] == 'stream.host_stopped'
                event_types = {
                    listener.receive_json()['type'],
                    listener.receive_json()['type'],
                    listener.receive_json()['type'],
                }

        lookup = client.get('/rooms/WAN-HSTOP')

    assert 'stream.host_stopped' in event_types
    assert 'stream.listener_count' in event_types
    assert 'room.closed' in event_types
    assert lookup.status_code == 404


def test_stream_host_relay_disconnect_closes_rest_created_room() -> None:
    with make_client() as client:
        created = client.post('/rooms', json={'roomName': 'WAN', 'roomId': 'WAN-HDROP'})
        assert created.status_code == 201

        with client.websocket_connect('/ws?peerId=relay_host_drop') as host:
            host.receive_json()
            assert send_hello(host)['type'] == 'server.ready'
            host.send_json(
                {
                    'type': 'stream.host_start',
                    'requestId': 'host-start-drop-1',
                    'roomId': 'WAN-HDROP',
                    'payload': {'roomId': 'WAN-HDROP', 'targetBufferMs': 680},
                }
            )
            assert host.receive_json()['type'] == 'stream.ready'

            with client.websocket_connect('/ws?peerId=listener_host_drop') as listener:
                listener.receive_json()
                assert send_hello(listener)['type'] == 'server.ready'
                listener.send_json(
                    {
                        'type': 'room.join',
                        'requestId': 'join-host-drop-1',
                        'roomId': 'WAN-HDROP',
                        'payload': {'deviceName': 'Listener', 'platform': 'web'},
                    }
                )
                assert listener.receive_json()['type'] == 'room.joined'
                host.receive_json()  # participant.joined
                host.receive_json()  # stream.listener_count

                host.close()
                event_types = {
                    listener.receive_json()['type'],
                    listener.receive_json()['type'],
                    listener.receive_json()['type'],
                }

        lookup = client.get('/rooms/WAN-HDROP')

    assert 'participant.left' in event_types
    assert 'stream.listener_count' in event_types
    assert 'room.closed' in event_types
    assert lookup.status_code == 404
