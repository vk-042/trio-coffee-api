"""Trio Coffee: a transactional coffee ordering API."""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from functools import wraps
from pathlib import Path

from flask import Flask, abort, g, jsonify, request
from werkzeug.exceptions import HTTPException

SCHEMA = """
CREATE TABLE IF NOT EXISTS menu (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, price_cents INTEGER NOT NULL CHECK(price_cents > 0),
 stock INTEGER NOT NULL CHECK(stock >= 0));
CREATE TABLE IF NOT EXISTS orders (
 id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,
 payload_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 total_cents INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS order_items (
 order_id TEXT NOT NULL REFERENCES orders(id), menu_id INTEGER NOT NULL REFERENCES menu(id),
 name TEXT NOT NULL, quantity INTEGER NOT NULL CHECK(quantity > 0),
 unit_price_cents INTEGER NOT NULL, PRIMARY KEY(order_id, menu_id));
"""


def create_app(config=None):
    app = Flask(__name__)
    app.config.from_mapping(DATABASE=os.getenv('DATABASE', 'instance/trio.sqlite3'),
                            STAFF_API_KEY=os.getenv('STAFF_API_KEY', ''), MAX_CONTENT_LENGTH=16384)
    app.config.update(config or {})
    if len(app.config['STAFF_API_KEY']) < 32:
        raise RuntimeError('Set STAFF_API_KEY to a random secret of at least 32 characters')
    Path(app.config['DATABASE']).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(app.config['DATABASE']) as conn:
        conn.executescript(SCHEMA)
        conn.execute('PRAGMA journal_mode=WAL')

    def db():
        if 'db' not in g:
            g.db = sqlite3.connect(app.config['DATABASE'], timeout=10)
            g.db.row_factory = sqlite3.Row
            g.db.execute('PRAGMA foreign_keys=ON')
        return g.db

    @app.teardown_appcontext
    def close_db(error):
        conn = g.pop('db', None)
        if conn is not None:
            conn.close()

    def staff(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            token = request.headers.get('Authorization', '').removeprefix('Bearer ')
            if not hmac.compare_digest(token.encode(), app.config['STAFF_API_KEY'].encode()):
                abort(401, 'Staff authentication required')
            return fn(*args, **kwargs)
        return wrapped

    def body(required):
        value = request.get_json()
        if not isinstance(value, dict) or set(value) != set(required):
            abort(400, 'Expected exactly these fields: ' + ', '.join(required))
        return value

    def integer(value, low, high):
        if type(value) is not int or not low <= value <= high:
            abort(400, f'Expected an integer between {low} and {high}')
        return value

    def order_data(order_id):
        row = db().execute('SELECT id,status,total_cents,created_at FROM orders WHERE id=?', (order_id,)).fetchone()
        if row is None:
            abort(404, 'Order not found')
        result = dict(row)
        result['currency'] = 'USD'
        result['items'] = [dict(r) for r in db().execute(
            'SELECT menu_id,name,quantity,unit_price_cents FROM order_items WHERE order_id=? ORDER BY menu_id', (order_id,))]
        return result

    @app.before_request
    def request_id():
        g.request_id = secrets.token_hex(12)

    @app.after_request
    def headers(response):
        response.headers['X-Request-ID'] = g.request_id
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Cache-Control'] = 'no-store'
        app.logger.info('%s %s %s %s', g.request_id, request.method, request.path, response.status_code)
        return response

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.name, message=error.description, request_id=g.request_id), error.code

    @app.errorhandler(sqlite3.OperationalError)
    def database_error(error):
        app.logger.exception('Database operation failed')
        return jsonify(error='Service Unavailable', message='Please retry later', request_id=g.request_id), 503

    @app.get('/health')
    def health():
        db().execute('SELECT 1')
        return {'status': 'ok'}

    @app.get('/api/v1/menu')
    def menu():
        return {'items': [dict(r) for r in db().execute('SELECT * FROM menu ORDER BY id')], 'currency': 'USD'}

    @app.post('/api/v1/menu')
    @staff
    def add_menu():
        data = body(['name', 'price_cents', 'stock'])
        if not isinstance(data['name'], str) or not 1 <= len(data['name'].strip()) <= 80:
            abort(400, 'Name must contain 1 to 80 characters')
        price = integer(data['price_cents'], 1, 100000)
        stock = integer(data['stock'], 0, 100000)
        with db() as conn:
            cursor = conn.execute('INSERT INTO menu(name,price_cents,stock) VALUES(?,?,?)', (data['name'].strip(), price, stock))
        return dict(db().execute('SELECT * FROM menu WHERE id=?', (cursor.lastrowid,)).fetchone()), 201

    @app.post('/api/v1/orders')
    def create_order():
        data = body(['items'])
        key = request.headers.get('Idempotency-Key', '')
        if not 8 <= len(key) <= 128 or not key.isascii():
            abort(400, 'An ASCII Idempotency-Key of 8 to 128 characters is required')
        if not isinstance(data['items'], list) or not 1 <= len(data['items']) <= 20:
            abort(400, 'Provide 1 to 20 order items')
        quantities = {}
        for item in data['items']:
            if not isinstance(item, dict) or set(item) != {'menu_id', 'quantity'}:
                abort(400, 'Each item requires menu_id and quantity')
            mid = integer(item['menu_id'], 1, 2147483647)
            quantity = integer(item['quantity'], 1, 20)
            if mid in quantities:
                abort(400, 'Duplicate menu item')
            quantities[mid] = quantity
        fingerprint = hashlib.sha256(json.dumps(sorted(quantities.items())).encode()).hexdigest()
        conn = db()
        # Serialize inventory checks and writes, including concurrent retries.
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            existing = conn.execute('SELECT id,payload_hash FROM orders WHERE idempotency_key=?', (key,)).fetchone()
            if existing:
                if existing['payload_hash'] != fingerprint:
                    abort(409, 'Idempotency key already used with different items')
                return order_data(existing['id']), 200
            lines, total = [], 0
            for mid, quantity in quantities.items():
                row = conn.execute('SELECT * FROM menu WHERE id=?', (mid,)).fetchone()
                if row is None:
                    abort(404, 'Menu item not found')
                if row['stock'] < quantity:
                    abort(409, 'Insufficient stock')
                lines.append((mid, row['name'], quantity, row['price_cents']))
                total += quantity * row['price_cents']
            oid = secrets.token_urlsafe(24)
            conn.execute('INSERT INTO orders(id,idempotency_key,payload_hash,total_cents) VALUES(?,?,?,?)', (oid, key, fingerprint, total))
            for mid, name, quantity, price in lines:
                conn.execute('INSERT INTO order_items VALUES(?,?,?,?,?)', (oid, mid, name, quantity, price))
                conn.execute('UPDATE menu SET stock=stock-? WHERE id=?', (quantity, mid))
        return order_data(oid), 201

    @app.get('/api/v1/orders/<order_id>')
    @staff
    def get_order(order_id):
        return order_data(order_id)

    @app.patch('/api/v1/orders/<order_id>/status')
    @staff
    def status(order_id):
        value = body(['status'])['status']
        transitions = {'pending': {'preparing', 'cancelled'}, 'preparing': {'ready'}, 'ready': {'completed'}, 'completed': set(), 'cancelled': set()}
        if not isinstance(value, str) or value not in transitions:
            abort(400, 'Unknown order status')
        conn = db()
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            order = order_data(order_id)
            if value not in transitions[order['status']]:
                abort(409, 'Invalid order status transition')
            conn.execute('UPDATE orders SET status=? WHERE id=?', (value, order_id))
            if value == 'cancelled':
                for item in order['items']:
                    conn.execute('UPDATE menu SET stock=stock+? WHERE id=?', (item['quantity'], item['menu_id']))
        return order_data(order_id)

    return app
