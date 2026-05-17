from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from ..core.config import Settings, get_settings
from ..core.security import is_valid_room_pin, is_valid_server_connection_pin
from ..services.room_service import RoomError, RoomService

router = APIRouter(prefix='/rooms', tags=['rooms'])


class CreateRoomRequest(BaseModel):
    roomName: str = 'SyncWave WAN Room'
    hostPeerId: str = 'api_host'
    hostDeviceName: str = 'API Host'
    hostPlatform: str = 'android'
    pinProtected: bool = False
    pin: Optional[str] = None
    roomId: Optional[str] = None


class CreateRoomResponse(BaseModel):
    roomId: str
    roomName: str
    pinProtected: bool


@router.get('/{room_id}')
def get_room(room_id: str, request: Request) -> dict:
    room_service: RoomService = request.app.state.room_service
    room = room_service.get_room(room_id)
    if room is None:
        raise HTTPException(status_code=404, detail='Room not found')

    payload = room.model_dump(by_alias=True, mode='json')
    payload.pop('pinHash', None)
    return {
        'room': payload,
    }


@router.post('', response_model=CreateRoomResponse, status_code=status.HTTP_201_CREATED)
def create_room(
    payload: CreateRoomRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
) -> CreateRoomResponse:
    _require_server_pin_for_host_action(request=request, settings=settings)
    room_service: RoomService = request.app.state.room_service
    normalized_pin = RoomService.normalize_pin(payload.pin)
    if payload.pinProtected and normalized_pin is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Room PIN is required when pinProtected is true.',
        )
    if normalized_pin is not None and not is_valid_room_pin(normalized_pin):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Room PIN must be exactly 6 digits.',
        )
    try:
        room = room_service.create_room(
            room_name=payload.roomName,
            host_peer_id=payload.hostPeerId,
            host_device_name=payload.hostDeviceName,
            host_platform=payload.hostPlatform,
            pin=normalized_pin,
            room_id=payload.roomId,
            pin_protected=payload.pinProtected or bool(normalized_pin),
        )
    except RoomError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return CreateRoomResponse(
        roomId=room.room_id,
        roomName=room.room_name,
        pinProtected=room.pin_protected,
    )


def _require_server_pin_for_host_action(*, request: Request, settings: Settings) -> None:
    if not settings.require_server_connection_pin:
        return

    provided_pin = (request.headers.get('x-syncwave-server-pin') or '').strip()
    if not provided_pin:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Server Connection PIN is required for host actions.',
        )
    if not is_valid_server_connection_pin(provided_pin):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Server Connection PIN must be exactly 8 digits.',
        )

    expected_pin = settings.server_connection_pin.strip()
    if not is_valid_server_connection_pin(expected_pin) or provided_pin != expected_pin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Server Connection PIN validation failed.',
        )
