from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app, create_app


def test_root_redirects_to_project_repository() -> None:
    with TestClient(app) as client:
        response = client.get('/', follow_redirects=False)

    assert response.status_code in {302, 307}
    assert response.headers['location'] == 'https://github.com/OpenCodeQuark/syncwave'


def test_root_redirect_uses_env_override(monkeypatch) -> None:
    monkeypatch.setenv('GITHUB_REDIRECT', 'https://example.com/custom')
    get_settings.cache_clear()
    test_app = create_app()

    with TestClient(test_app) as client:
        response = client.get('/', follow_redirects=False)

    assert response.status_code in {302, 307}
    assert response.headers['location'] == 'https://example.com/custom'
    get_settings.cache_clear()


def test_health_endpoint_returns_json() -> None:
    with TestClient(app) as client:
        response = client.get('/health')

    assert response.status_code == 200
    payload = response.json()
    assert payload['status'] == 'ok'
    assert payload['service'] == 'SyncWave Signaling Server'
    assert 'timestamp' in payload


def test_status_endpoint_returns_json() -> None:
    with TestClient(app) as client:
        response = client.get('/status')

    assert response.status_code == 200
    payload = response.json()
    assert payload['app'] == 'SyncWave Signaling Server'
    assert payload['version'] == '1.1.5'
    assert payload['status'] == 'ok'
    assert payload['websocketPath'] == '/ws'
    assert isinstance(payload['activeRooms'], int)
    assert isinstance(payload['activeConnections'], int)


def test_global_error_handler_returns_structured_json() -> None:
    with TestClient(app) as client:
        response = client.get('/rooms/UNKNOWN-ROOM')

    assert response.status_code == 404
    payload = response.json()
    assert 'error' in payload
    assert payload['error']['code'] == 'http_404'
    assert 'message' in payload['error']


def test_stream_join_route_serves_html() -> None:
    with TestClient(app) as client:
        response = client.get('/stream/join?ROOM=WAN-RM01P')

        assert response.status_code == 200
        assert 'text/html' in response.headers['content-type']
        assert 'SyncWave' in response.text
        assert 'WAN-RM01P' in response.text
        assert 'SyncWave 1.1.5' in response.text
        assert 'Internet (WAN)' in response.text
        assert '/stream/listener.js' in response.text
        assert 'listener.css' in response.text
        assert '{{COVER_ART_HTML}}' not in response.text
        assert '/AppIcons/SyncWave.png' in response.text

        js = client.get('/stream/listener.js')
        assert js.status_code == 200
        assert 'enqueueChunk' in js.text
        assert 'schedulePlayback' in js.text
        assert 'recordClockOffset' in js.text


def test_app_icons_syncwave_png() -> None:
    with TestClient(app) as client:
        response = client.get('/AppIcons/SyncWave.png')

    assert response.status_code == 200
    assert response.headers['content-type'].startswith('image/png')


def test_stream_join_case_insensitive_pin() -> None:
    with TestClient(app) as client:
        response = client.get('/stream/join?ROOM=WAN-RM01P&PIN=123456')

    assert response.status_code == 200
    assert 'value="123456"' in response.text


def test_listener_js_pin_required_for_protected_room() -> None:
    with TestClient(app) as client:
        created = client.post(
            '/rooms',
            json={
                'roomName': 'WAN',
                'roomId': 'WAN-PINJS',
                'pin': '654321',
                'pinProtected': True,
            },
        )
        assert created.status_code == 201

        protected_js = client.get('/stream/listener.js?room=WAN-PINJS')
        assert protected_js.status_code == 200
        assert 'const PIN_REQUIRED = true;' in protected_js.text

        open_js = client.get('/stream/listener.js')
        assert open_js.status_code == 200
        assert 'const PIN_REQUIRED = false;' in open_js.text


def test_stream_join_includes_room_in_listener_js_query() -> None:
    with TestClient(app) as client:
        response = client.get('/stream/join?room=WAN-QUERY')

    assert response.status_code == 200
    assert 'listener.js?v=' in response.text
    assert 'room=WAN-QUERY' in response.text


def test_favicon_route_serves_icon() -> None:
    with TestClient(app) as client:
        response = client.get('/favicon.ico')

    assert response.status_code == 200
    assert response.headers['content-type'].startswith('image/x-icon')
