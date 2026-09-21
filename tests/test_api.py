from concurrent.futures import ThreadPoolExecutor
import pytest
from trio import create_app

KEY = 'test-only-secret-' * 3
AUTH = {'Authorization': 'Bearer ' + KEY}

@pytest.fixture
def app(tmp_path):
    return create_app({'TESTING': True, 'DATABASE': str(tmp_path / 'test.sqlite3'), 'STAFF_API_KEY': KEY})

@pytest.fixture
def client(app):
    return app.test_client()

def coffee(client, stock=10):
    response = client.post('/api/v1/menu', json={'name': 'Latte', 'price_cents': 450, 'stock': stock}, headers=AUTH)
    assert response.status_code == 201
    return response.json['id']

def order(client, mid, quantity=1, key='unique-key-1'):
    return client.post('/api/v1/orders', json={'items': [{'menu_id': mid, 'quantity': quantity}]}, headers={'Idempotency-Key': key})

def test_health_and_auth(client):
    assert client.get('/health').json == {'status': 'ok'}
    assert client.post('/api/v1/menu', json={}).status_code == 401
    assert client.get('/api/v1/orders/anything').status_code == 401
    assert client.get('/missing').json['error'] == 'Not Found'
    assert client.get('/health').headers['X-Request-ID']

def test_order_prices_and_replay(client):
    mid = coffee(client)
    first = order(client, mid, 2)
    assert first.status_code == 201
    assert first.json['total_cents'] == 900
    assert first.json['items'][0]['name'] == 'Latte'
    replay = order(client, mid, 2)
    assert replay.status_code == 200
    assert replay.json == first.json
    assert client.get('/api/v1/menu').json['items'][0]['stock'] == 8
    assert order(client, mid, 3).status_code == 409
    assert client.get('/api/v1/orders/' + first.json['id'], headers=AUTH).json == first.json

def test_insufficient_stock_atomic(client):
    mid = coffee(client, 1)
    other = coffee(client, 0)
    response = client.post('/api/v1/orders', json={'items': [{'menu_id': mid, 'quantity': 1}, {'menu_id': other, 'quantity': 1}]}, headers={'Idempotency-Key': 'atomic-test'})
    assert response.status_code == 409
    assert client.get('/api/v1/menu').json['items'][0]['stock'] == 1
    assert order(client, mid).status_code == 201

def test_cancel_once(client):
    mid = coffee(client, 2)
    oid = order(client, mid, 2).json['id']
    url = '/api/v1/orders/' + oid + '/status'
    assert client.patch(url, json={'status': 'cancelled'}, headers=AUTH).status_code == 200
    assert client.patch(url, json={'status': 'cancelled'}, headers=AUTH).status_code == 409
    assert client.get('/api/v1/menu').json['items'][0]['stock'] == 2

def test_lifecycle(client):
    oid = order(client, coffee(client)).json['id']
    url = '/api/v1/orders/' + oid + '/status'
    assert client.patch(url, json={'status': 'completed'}, headers=AUTH).status_code == 409
    for status in ['preparing', 'ready', 'completed']:
        assert client.patch(url, json={'status': status}, headers=AUTH).json['status'] == status
    assert client.patch(url, json={'status': 'cancelled'}, headers=AUTH).status_code == 409

@pytest.mark.parametrize('quantity', [0, -1, 21, True, 1.5, '2', None])
def test_invalid_quantity(client, quantity):
    assert order(client, coffee(client), quantity).status_code == 400

@pytest.mark.parametrize('payload', [[], None, {}, {'items': []}, {'items': [None]}, {'items': [{'menu_id': 1, 'quantity': 1}], 'total_cents': 1}])
def test_invalid_body(client, payload):
    assert client.post('/api/v1/orders', json=payload, headers={'Idempotency-Key': 'test-key'}).status_code in (400, 415)

def test_missing_and_duplicate_items(client):
    assert order(client, 999).status_code == 404
    mid = coffee(client)
    assert client.post('/api/v1/orders', json={'items': [{'menu_id': mid, 'quantity': 1}]}).status_code == 400
    item = {'menu_id': mid, 'quantity': 1}
    assert client.post('/api/v1/orders', json={'items': [item, item]}, headers={'Idempotency-Key': 'duplicate'}).status_code == 400

def test_concurrent_last_item(app, client):
    mid = coffee(client, 1)
    def buy(index):
        with app.test_client() as c:
            return order(c, mid, key=f'concurrent-{index}').status_code
    with ThreadPoolExecutor(max_workers=4) as pool:
        codes = list(pool.map(buy, range(4)))
    assert sorted(codes) == [201, 409, 409, 409]
    assert client.get('/api/v1/menu').json['items'][0]['stock'] == 0

def test_concurrent_replay(app, client):
    mid = coffee(client, 1)
    def buy(_):
        with app.test_client() as c:
            r = order(c, mid)
            return r.status_code, r.json['id']
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(buy, range(4)))
    assert sorted(r[0] for r in results) == [200, 200, 200, 201]
    assert len({r[1] for r in results}) == 1

def test_persistence(app, client):
    coffee(client)
    second = create_app(dict(app.config)).test_client()
    assert len(second.get('/api/v1/menu').json['items']) == 1

def test_secret_required(tmp_path):
    with pytest.raises(RuntimeError):
        create_app({'STAFF_API_KEY': '', 'DATABASE': str(tmp_path / 'x.db')})
