from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from ..core.security import (
    generate_wan_room_id,
    hash_pin,
    is_valid_room_pin,
    is_valid_wan_room_id,
    verify_pin,
)
from ..models.peer import Peer
from ..models.room import Room


class RoomError(Exception):
    pass


class RoomService:
    def __init__(self, ttl_seconds: int, max_participants: int, pin_hash_secret: str):
        self._ttl_seconds = ttl_seconds
        self._max_participants = max_participants
        self._pin_hash_secret = pin_hash_secret
        self._rooms: dict[str, Room] = {}

    def create_room(
        self,
        *,
        room_name: str,
        host_peer_id: str,
        host_device_name: str,
        host_platform: str,
        pin: Optional[str],
        room_id: Optional[str] = None,
        pin_protected: Optional[bool] = None,
    ) -> Room:
        selected_room_id = (room_id or '').strip().upper() or self._next_room_id()
        if not is_valid_wan_room_id(selected_room_id):
            raise RoomError('WAN room code must match WAN-XXXXX')
        if self._is_room_code_in_use(selected_room_id):
            raise RoomError('Room code already in use')
        normalized_pin = self.normalize_pin(pin)
        protected = pin_protected if pin_protected is not None else bool(normalized_pin)
        if protected and normalized_pin is None:
            raise RoomError('PIN is required for this room')
        if normalized_pin is not None and not is_valid_room_pin(normalized_pin):
            raise RoomError('Room PIN must be exactly 6 digits')
        now = datetime.now(timezone.utc)

        normalized_host_platform = host_platform.strip().lower()
        host_peer = Peer(
            peerId=host_peer_id,
            deviceName=host_device_name,
            platform=normalized_host_platform
            if normalized_host_platform in {'android', 'ios', 'web'}
            else 'unknown',
            role='host',
        )

        room = Room(
            roomId=selected_room_id,
            roomName=room_name,
            hostId=host_peer_id,
            pinProtected=protected,
            pinHash=hash_pin(normalized_pin, self._pin_hash_secret)
            if normalized_pin
            else None,
            createdAt=now,
            expiresAt=now + timedelta(seconds=self._ttl_seconds),
            status='active',
            participants=[host_peer],
        )

        self._rooms[selected_room_id] = room
        return room

    @staticmethod
    def normalize_pin(pin: Optional[str]) -> Optional[str]:
        if pin is None:
            return None
        stripped = pin.strip()
        return stripped or None

    def validate_room_pin(self, *, room_id: str, pin: Optional[str]) -> None:
        """Raises [RoomError] when a protected room is accessed without a valid PIN."""
        room = self._get_active_room(room_id)
        self._assert_valid_room_pin(room, self.normalize_pin(pin))

    def join_room(self, *, room_id: str, peer: Peer, pin: Optional[str]) -> Room:
        room = self._get_active_room(room_id)
        self._assert_valid_room_pin(room, self.normalize_pin(pin))

        if any(existing.peer_id == peer.peer_id for existing in room.participants):
            return room

        if len(room.participants) >= self._max_participants:
            raise RoomError('Room is full')

        room.participants.append(peer)
        return room

    def leave_room(self, *, room_id: str, peer_id: str) -> Optional[Room]:
        room = self._rooms.get(room_id)
        if room is None:
            return None

        room.participants = [peer for peer in room.participants if peer.peer_id != peer_id]

        if peer_id == room.host_id or not room.participants:
            room.status = 'closed'
            self._rooms.pop(room_id, None)

        return room

    def close_room(self, *, room_id: str) -> Room:
        room = self._get_active_room(room_id)
        room.status = 'closed'
        self._rooms.pop(room_id, None)
        return room

    def get_room(self, room_id: str) -> Optional[Room]:
        room = self._rooms.get(room_id)
        if room is None:
            return None

        if room.expires_at <= datetime.now(timezone.utc):
            room.status = 'expired'
        return room

    def active_room_count(self) -> int:
        count = 0
        for room_id in list(self._rooms.keys()):
            room = self.get_room(room_id)
            if room is not None and room.status == 'active':
                count += 1
        return count

    def _assert_valid_room_pin(self, room: Room, normalized_pin: Optional[str]) -> None:
        if not room.pin_protected:
            return
        if normalized_pin is None or room.pin_hash is None:
            raise RoomError('PIN is required for this room')
        if not is_valid_room_pin(normalized_pin):
            raise RoomError('Room PIN must be exactly 6 digits')
        if not verify_pin(
            pin=normalized_pin,
            hashed_pin=room.pin_hash,
            secret=self._pin_hash_secret,
        ):
            raise RoomError('Invalid PIN')

    def _get_active_room(self, room_id: str) -> Room:
        room = self.get_room(room_id)
        if room is None:
            raise RoomError('Room does not exist')

        if room.status != 'active':
            raise RoomError(f'Room is not active ({room.status})')

        return room

    def _next_room_id(self) -> str:
        while True:
            room_id = generate_wan_room_id()
            if not self._is_room_code_in_use(room_id):
                return room_id

    def _is_room_code_in_use(self, room_id: str) -> bool:
        room = self.get_room(room_id)
        return room is not None and room.status == 'active'
