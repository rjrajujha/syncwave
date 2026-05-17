from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from starlette.websockets import WebSocketState

from .handlers import WebSocketEventHandler

logger = logging.getLogger(__name__)


def build_websocket_router(
    handler: WebSocketEventHandler,
    *,
    websocket_path: str = '/ws',
) -> APIRouter:
    router = APIRouter()

    @router.websocket(websocket_path)
    async def websocket_endpoint(websocket: WebSocket) -> None:
        peer_id = websocket.query_params.get('peerId') or f'peer_{uuid.uuid4().hex[:8]}'
        await handler.connect(peer_id=peer_id, websocket=websocket)
        await handler.send_connection_ready(peer_id=peer_id)

        try:
            while True:
                payload = await websocket.receive_json()
                if isinstance(payload, dict):
                    await handler.handle_message(peer_id=peer_id, raw_message=payload)
        except WebSocketDisconnect:
            pass
        except (RuntimeError, ValueError, ValidationError) as exc:
            logger.info('Peer websocket receive failed: %s error=%s', peer_id, exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                'Peer websocket closed after unexpected error: %s error=%s',
                peer_id,
                exc,
            )
        finally:
            room_id = await handler.disconnect(peer_id=peer_id)
            logger.info('Peer disconnected: %s room=%s', peer_id, room_id)
            if websocket.application_state == WebSocketState.CONNECTED:
                try:
                    await websocket.close()
                except RuntimeError:
                    pass

    return router
