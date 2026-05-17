from functools import lru_cache
from html import escape
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse
from starlette.responses import Response

from ..core.config import get_settings

router = APIRouter(tags=['stream'])
STATIC_DIR = Path(__file__).resolve().parents[1] / 'static'
APP_ICONS_DIR = STATIC_DIR / 'AppIcons'
TEMPLATES_DIR = Path(__file__).resolve().parent / 'templates'


@lru_cache(maxsize=1)
def _listener_html_template() -> str:
    """Loads the SyncWave browser-listener HTML shell once.

    Canonical sources live under `apps/assets/listener/`; this directory is a
    mirrored copy synchronized at release time.
    """
    return (TEMPLATES_DIR / 'listener.html').read_text(encoding='utf-8')


@lru_cache(maxsize=1)
def _listener_css_text() -> str:
    return (TEMPLATES_DIR / 'listener.css').read_text(encoding='utf-8')


@lru_cache(maxsize=1)
def _listener_js_template() -> str:
    return (TEMPLATES_DIR / 'listener.js').read_text(encoding='utf-8')


def _js_escape_single(value: str) -> str:
    return value.replace('\\', '\\\\').replace("'", "\\'")


def _render_listener_js(
    *,
    pin_required: bool = False,
    room_default_prefix: str = 'WAN-',
) -> str:
    settings = get_settings()
    return (
        _listener_js_template()
        .replace('{{PIN_REQUIRED}}', 'true' if pin_required else 'false')
        .replace('{{ROOM_DEFAULT_PREFIX}}', _js_escape_single(room_default_prefix))
        .replace('{{WS_PATH}}', _js_escape_single(settings.websocket_path))
        .replace('{{APP_VERSION}}', _js_escape_single(settings.app_version))
        .replace('{{PROTOCOL}}', _js_escape_single(settings.protocol_version))
        .replace('{{SOURCE_LABEL}}', _js_escape_single('Internet (WAN)'))
    )


def _resolve_pin_required(request: Request) -> bool:
    params = {key.lower(): value for key, value in request.query_params.items()}
    if params.get('pinprotected') in {'true', '1', 'yes'}:
        return True

    room_id = (params.get('room') or params.get('roomid') or '').strip().upper()
    if not room_id:
        return False

    room_service = getattr(request.app.state, 'room_service', None)
    if room_service is None:
        return False

    room = room_service.get_room(room_id)
    return room is not None and room.pin_protected


@router.get('/AppIcons/SyncWave.png', include_in_schema=False)
def syncwave_listener_cover() -> FileResponse:
    """Branding artwork for the WAN listener template (mirrors LAN cover)."""
    return FileResponse(
        APP_ICONS_DIR / 'SyncWave.png',
        media_type='image/png',
        filename='SyncWave.png',
    )


@router.get('/favicon.ico', include_in_schema=False)
def favicon() -> FileResponse:
    return FileResponse(STATIC_DIR / 'favicon.ico', media_type='image/x-icon')


@router.get('/stream/listener.css', include_in_schema=False)
def browser_listener_css() -> Response:
    return Response(
        content=_listener_css_text(),
        media_type='text/css; charset=utf-8',
        headers={
            'Cache-Control': 'public, max-age=120',
            'X-Content-Type-Options': 'nosniff',
        },
    )


@router.get('/stream/listener.js', include_in_schema=False)
def browser_listener_js(request: Request) -> Response:
    pin_required = _resolve_pin_required(request)
    return Response(
        content=_render_listener_js(
            pin_required=pin_required,
            room_default_prefix='WAN-',
        ),
        media_type='application/javascript; charset=utf-8',
        headers={
            'Cache-Control': 'no-store',
            'X-Content-Type-Options': 'nosniff',
        },
    )


@router.get('/stream/join', response_class=HTMLResponse)
def browser_stream_join(request: Request) -> HTMLResponse:
    params = {k.lower(): v for k, v in request.query_params.items()}
    room = (params.get('room') or params.get('roomid') or '').strip()
    pin = (params.get('pin') or '').strip()
    settings = get_settings()
    template = _listener_html_template()
    listener_js_query = f'?v={quote(settings.app_version)}'
    if room:
        listener_js_query += f'&room={quote(room.upper())}'
    rendered = (
        template.replace('{{INITIAL_ROOM}}', escape(room))
        .replace('{{INITIAL_PIN}}', escape(pin))
        .replace('{{ROOM_DEFAULT_PREFIX}}', 'WAN-')
        .replace('{{APP_VERSION}}', settings.app_version)
        .replace('{{SOURCE_LABEL}}', 'Internet (WAN)')
        .replace('{{WS_PATH}}', settings.websocket_path)
        .replace('{{PROTOCOL}}', settings.protocol_version)
        .replace('{{LISTENER_JS_QUERY}}', listener_js_query)
        .replace(
            '{{COVER_ART_HTML}}',
            '<img class="cover-img" src="/AppIcons/SyncWave.png" alt="" '
            'draggable="false" />',
        )
    )
    return HTMLResponse(content=rendered)
