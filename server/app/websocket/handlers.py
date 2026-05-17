from __future__ import annotations

import logging
import time
from typing import Any, Optional

from fastapi import WebSocket
from pydantic import ValidationError

from ..core.config import Settings
from ..core.security import is_valid_server_connection_pin
from ..models.events import EventEnvelope
from ..services.room_service import RoomError
from ..services.signaling_service import SignalingService
from .connection_manager import ConnectionManager

logger = logging.getLogger(__name__)


class WebSocketEventHandler:
    def __init__(
        self,
        *,
        manager: ConnectionManager,
        signaling: SignalingService,
        settings: Settings,
    ):
        self._manager = manager
        self._signaling = signaling
        self._settings = settings
        self._handshake_accepted: dict[str, bool] = {}
        self._privileged_peers: set[str] = set()

    async def connect(self, *, peer_id: str, websocket: WebSocket) -> None:
        await self._manager.connect(websocket=websocket, peer_id=peer_id)
        self._handshake_accepted[peer_id] = False

    async def send_connection_ready(self, *, peer_id: str) -> None:
        await self._manager.send_to_peer(
            peer_id=peer_id,
            event=EventEnvelope(
                type='connection.ready',
                peerId=peer_id,
                payload={
                    'serverTimestamp': int(time.time() * 1000),
                    'protocolVersion': self._settings.protocol_version,
                    'authenticationRequired': self._settings.require_server_connection_pin,
                },
            ),
        )

    async def disconnect(self, *, peer_id: str) -> Optional[str]:
        room_id = self._manager.disconnect(peer_id=peer_id)
        self._handshake_accepted.pop(peer_id, None)
        self._privileged_peers.discard(peer_id)

        if room_id is not None:
            broadcasts = self._signaling.handle_disconnect(room_id=room_id, peer_id=peer_id)
            for broadcast in broadcasts:
                await self._manager.broadcast_to_room(room_id=room_id, event=broadcast)

        return room_id

    async def handle_message(self, *, peer_id: str, raw_message: dict[str, Any]) -> None:
        try:
            event = EventEnvelope.model_validate(raw_message)
            if not event.peer_id:
                event = event.model_copy(update={'peer_id': peer_id})

            if event.type == 'server.hello':
                await self._handle_server_hello(peer_id=peer_id, event=event)
                return

            if not self._handshake_accepted.get(peer_id, False):
                await self._manager.send_to_peer(
                    peer_id=peer_id,
                    event=EventEnvelope(
                        type='error',
                        requestId=event.request_id,
                        peerId=peer_id,
                        payload={
                            'code': 'handshake_required',
                            'message': 'Send server.hello before signaling events.',
                        },
                    ),
                )
                return

            if (
                self._requires_privileged_auth(event.type)
                and peer_id not in self._privileged_peers
            ):
                await self._manager.send_to_peer(
                    peer_id=peer_id,
                    event=EventEnvelope(
                        type=self._failure_event_type(event.type),
                        requestId=event.request_id,
                        roomId=event.room_id,
                        peerId=peer_id,
                        payload={
                            'code': 'server_connection_pin_required',
                            'message': (
                                'Server Connection PIN is required for host and relay actions.'
                            ),
                        },
                    ),
                )
                return

            response, broadcasts = self._signaling.handle(event)

            success_by_event = {
                'room.create': 'room.created',
                'room.join': 'room.joined',
                'stream.host_start': 'stream.ready',
            }
            expected_success = success_by_event.get(event.type)
            if (
                expected_success is not None
                and response.type == expected_success
                and response.room_id
            ):
                self._manager.register_peer_room(peer_id=peer_id, room_id=response.room_id)

            if event.type in {'room.leave', 'stream.host_stop'}:
                self._manager.unregister_peer_room(peer_id=peer_id)

            await self._manager.send_to_peer(peer_id=peer_id, event=response)

            room_id = response.room_id or event.room_id
            if room_id is None:
                return

            for broadcast in broadcasts:
                failed_peer_ids = await self._manager.broadcast_to_room(
                    room_id=room_id,
                    event=broadcast,
                    exclude_peer_ids={peer_id},
                )
                await self._cleanup_failed_peers(
                    room_id=room_id,
                    failed_peer_ids=failed_peer_ids,
                )

        except ValidationError as exc:
            logger.warning('Invalid websocket payload from %s: %s', peer_id, exc)
            await self._manager.send_to_peer(
                peer_id=peer_id,
                event=EventEnvelope(
                    type='error',
                    peerId=peer_id,
                    payload={
                        'code': 'invalid_event_schema',
                        'message': 'Invalid event schema',
                    },
                ),
            )
        except RoomError as exc:
            event_type = self._failure_event_type(raw_message.get('type'))
            await self._manager.send_to_peer(
                peer_id=peer_id,
                event=EventEnvelope(
                    type=event_type,
                    peerId=peer_id,
                    payload={
                        'code': 'room_operation_failed',
                        'message': str(exc),
                    },
                ),
            )

    async def _handle_server_hello(self, *, peer_id: str, event: EventEnvelope) -> None:
        payload = event.payload
        provided_protocol = str(payload.get('protocolVersion') or '').strip()

        if provided_protocol != self._settings.protocol_version:
            await self._manager.send_to_peer(
                peer_id=peer_id,
                event=EventEnvelope(
                    type='server.unsupported_version',
                    requestId=event.request_id,
                    peerId=peer_id,
                    payload={
                        'code': 'unsupported_protocol_version',
                        'message': 'Unsupported protocol version.',
                        'expectedProtocolVersion': self._settings.protocol_version,
                        'receivedProtocolVersion': provided_protocol,
                    },
                ),
            )
            return

        listener_only = self._is_listener_handshake(payload)
        pin_authenticated = not self._settings.require_server_connection_pin

        if self._settings.require_server_connection_pin:
            server_pin = str(payload.get('serverConnectionPin') or '').strip()
            if not server_pin:
                # Listeners never need the Server Connection PIN; only hosts/relays do.
                if listener_only:
                    self._handshake_accepted[peer_id] = True
                    self._privileged_peers.discard(peer_id)
                    await self._send_server_ready(
                        peer_id=peer_id,
                        request_id=event.request_id,
                        authenticated=False,
                        listener_only=True,
                    )
                    return

                await self._reject_server_handshake(
                    peer_id=peer_id,
                    event=event,
                    response_type='server.auth_required',
                    code='server_connection_pin_required',
                    message='Server Connection PIN is required.',
                )
                return

            expected_pin = self._settings.server_connection_pin.strip()
            if (
                not is_valid_server_connection_pin(server_pin)
                or not is_valid_server_connection_pin(expected_pin)
                or server_pin != expected_pin
            ):
                await self._reject_server_handshake(
                    peer_id=peer_id,
                    event=event,
                    response_type='server.auth_failed',
                    code='server_connection_pin_invalid',
                    message='Server Connection PIN validation failed.',
                )
                return
            pin_authenticated = True

        self._handshake_accepted[peer_id] = True
        # Authenticated listeners may join rooms but must never relay host audio.
        if pin_authenticated and not listener_only:
            self._privileged_peers.add(peer_id)
        else:
            self._privileged_peers.discard(peer_id)
        await self._send_server_ready(
            peer_id=peer_id,
            request_id=event.request_id,
            authenticated=pin_authenticated,
            listener_only=listener_only,
        )

    async def _send_server_ready(
        self,
        *,
        peer_id: str,
        request_id: Optional[str],
        authenticated: bool,
        listener_only: bool,
    ) -> None:
        await self._manager.send_to_peer(
            peer_id=peer_id,
            event=EventEnvelope(
                type='server.ready',
                requestId=request_id,
                peerId=peer_id,
                payload={
                    'status': 'ok',
                    'server': self._settings.app_name,
                    'serverVersion': self._settings.app_version,
                    'protocolVersion': self._settings.protocol_version,
                    'authenticationRequired': (
                        self._settings.require_server_connection_pin and not listener_only
                    ),
                    'authenticated': authenticated,
                    'listenerOnly': listener_only,
                    'capabilities': {
                        'roomLifecycle': True,
                        'syncPing': True,
                        'webrtcMediaTransport': False,
                    },
                },
            ),
        )

    async def _reject_server_handshake(
        self,
        *,
        peer_id: str,
        event: EventEnvelope,
        response_type: str,
        code: str,
        message: str,
    ) -> None:
        self._handshake_accepted[peer_id] = False
        self._privileged_peers.discard(peer_id)
        await self._manager.send_to_peer(
            peer_id=peer_id,
            event=EventEnvelope(
                type=response_type,
                requestId=event.request_id,
                peerId=peer_id,
                payload={
                    'code': code,
                    'message': message,
                },
            ),
        )
        await self._manager.close_peer(
            peer_id=peer_id,
            code=4403,
            reason=code,
        )

    def _is_listener_handshake(self, payload: dict[str, Any]) -> bool:
        role = (
            str(payload.get('clientRole') or payload.get('role') or '')
            .strip()
            .lower()
        )
        listener_only = payload.get('listenerOnly')
        return role == 'listener' or listener_only is True

    def _requires_privileged_auth(self, event_type: str) -> bool:
        if not self._settings.require_server_connection_pin:
            return False
        return event_type in {
            'room.create',
            'stream.host_start',
            'stream.host_stop',
            'stream.audio_chunk',
        }

    def _failure_event_type(self, request_type: Optional[str]) -> str:
        mapping = {
            'room.create': 'room.create_failed',
            'room.join': 'room.join_failed',
            'room.leave': 'room.leave_failed',
            'sync.ping': 'sync.failed',
            'server.hello': 'server.auth_failed',
            'stream.host_start': 'stream.failed',
            'stream.host_stop': 'stream.failed',
            'stream.listener_join': 'stream.failed',
            'stream.audio_chunk': 'stream.failed',
            'stream.ping': 'stream.failed',
        }
        return mapping.get(request_type or '', 'error')

    async def _cleanup_failed_peers(self, *, room_id: str, failed_peer_ids: set[str]) -> None:
        for failed_peer_id in failed_peer_ids:
            stale_room_id = self._manager.disconnect(peer_id=failed_peer_id)
            self._handshake_accepted.pop(failed_peer_id, None)
            self._privileged_peers.discard(failed_peer_id)
            if stale_room_id == room_id:
                cleanup_events = self._signaling.handle_disconnect(
                    room_id=room_id,
                    peer_id=failed_peer_id,
                )
                for cleanup_event in cleanup_events:
                    await self._manager.broadcast_to_room(
                        room_id=room_id,
                        event=cleanup_event,
                        exclude_peer_ids={failed_peer_id},
                    )
