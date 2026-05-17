import re

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app


def test_wan_room_code_generation_and_uniqueness() -> None:
    app = create_app()
    with TestClient(app) as client:
        first = client.post('/rooms', json={'roomName': 'WAN One'})
        second = client.post('/rooms', json={'roomName': 'WAN Two'})

    assert first.status_code == 201
    assert second.status_code == 201

    first_code = first.json()['roomId']
    second_code = second.json()['roomId']
    assert re.match(r'^WAN-[A-Z0-9]{5}$', first_code)
    assert re.match(r'^WAN-[A-Z0-9]{5}$', second_code)
    assert first_code != second_code


def test_duplicate_wan_room_code_rejected_when_manually_provided() -> None:
    app = create_app()
    with TestClient(app) as client:
        first = client.post('/rooms', json={'roomName': 'WAN', 'roomId': 'WAN-ABCDE'})
        duplicate = client.post('/rooms', json={'roomName': 'WAN', 'roomId': 'WAN-ABCDE'})

    assert first.status_code == 201
    assert duplicate.status_code == 409


def test_room_code_cleanup_releases_name() -> None:
    app = create_app()
    with TestClient(app) as client:
        created = client.post('/rooms', json={'roomName': 'WAN', 'roomId': 'WAN-ZZ999'})
        assert created.status_code == 201
        room_service = client.app.state.room_service
        room = room_service.get_room('WAN-ZZ999')
        assert room is not None
        room_service.leave_room(room_id='WAN-ZZ999', peer_id=room.host_id)

        recreated = client.post('/rooms', json={'roomName': 'WAN', 'roomId': 'WAN-ZZ999'})

    assert recreated.status_code == 201


def test_invalid_wan_room_code_rejected() -> None:
    app = create_app()
    with TestClient(app) as client:
        response = client.post('/rooms', json={'roomName': 'WAN', 'roomId': 'W-123'})

    assert response.status_code == 409


def test_pin_protected_requires_pin_on_rest_create() -> None:
    app = create_app()
    with TestClient(app) as client:
        response = client.post(
            '/rooms',
            json={'roomName': 'WAN', 'roomId': 'WAN-PIN03', 'pinProtected': True},
        )

    assert response.status_code == 400
    assert 'PIN is required' in response.json()['error']['message']


def test_invalid_room_pin_rejected() -> None:
    app = create_app()
    with TestClient(app) as client:
        response = client.post(
            '/rooms',
            json={'roomName': 'WAN', 'roomId': 'WAN-PIN01', 'pin': '12345'},
        )

    assert response.status_code == 400
    assert 'exactly 6 digits' in response.json()['error']['message']


def test_room_lookup_does_not_expose_pin_hash() -> None:
    app = create_app()
    with TestClient(app) as client:
        created = client.post(
            '/rooms',
            json={'roomName': 'WAN', 'roomId': 'WAN-PIN02', 'pin': '123456'},
        )
        response = client.get('/rooms/WAN-PIN02')

    assert created.status_code == 201
    assert response.status_code == 200
    room = response.json()['room']
    assert room['pinProtected'] is True
    assert 'pinHash' not in room


def test_protected_server_requires_pin_for_room_creation(monkeypatch) -> None:
    monkeypatch.setenv('REQUIRE_SERVER_CONNECTION_PIN', 'true')
    monkeypatch.setenv('SERVER_CONNECTION_PIN', '12345678')
    get_settings.cache_clear()
    app = create_app()

    with TestClient(app) as client:
        missing = client.post('/rooms', json={'roomName': 'WAN'})
        malformed = client.post(
            '/rooms',
            headers={'x-syncwave-server-pin': '123456789'},
            json={'roomName': 'WAN'},
        )
        wrong = client.post(
            '/rooms',
            headers={'x-syncwave-server-pin': '87654321'},
            json={'roomName': 'WAN'},
        )
        created = client.post(
            '/rooms',
            headers={'x-syncwave-server-pin': '12345678'},
            json={'roomName': 'WAN'},
        )

    assert missing.status_code == 401
    assert malformed.status_code == 400
    assert 'exactly 8 digits' in malformed.json()['error']['message']
    assert wrong.status_code == 403
    assert created.status_code == 201
    get_settings.cache_clear()
