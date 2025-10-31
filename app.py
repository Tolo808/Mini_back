import os
from flask import Flask, request, jsonify
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
import jwt
import datetime
from pymongo import MongoClient
from bson.objectid import ObjectId
from utilis import compute_distance_via_gebeta

app = Flask(__name__)
CORS(app)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret')

MONGO_URI = os.getenv('MONGO_URI', 'mongodb://mongo:PzrrEgUjKWnVRXZnYgPjCqmgtZRrAkef@nozomi.proxy.rlwy.net:32153')
client = MongoClient(MONGO_URI)
db = client.tolodelivery
users = db.users
orders = db.orders

def token_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        if 'Authorization' in request.headers:
            token = request.headers['Authorization'].split('Bearer ')[-1]
        if not token:
            return jsonify({'error': 'Token missing'}), 401
        try:
            payload = jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
            user_id = payload['user_id']
            user = users.find_one({'_id': ObjectId(user_id)})
            if not user:
                raise Exception('User not found')
            request.user = user
        except jwt.ExpiredSignatureError:
            return jsonify({'error': 'Token expired'}), 401
        except Exception as e:
            return jsonify({'error': 'Invalid token', 'details': str(e)}), 401
        return f(*args, **kwargs)
    return decorated

@app.route('/api/register', methods=['POST'])
def register():
    data = request.json or {}
    phone = (data.get('phone') or '').strip()
    password = data.get('password')
    if not phone or not password:
        return jsonify({'error': 'phone and password required'}), 400
    if users.find_one({'phone': phone}):
        return jsonify({'error': 'Phone already registered'}), 400
    pw_hash = generate_password_hash(password)
    user = {'phone': phone, 'password': pw_hash, 'created_at': datetime.datetime.utcnow()}
    res = users.insert_one(user)
    return jsonify({'message': 'User created', 'user_id': str(res.inserted_id)}), 201

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json or {}
    phone = (data.get('phone') or '').strip()
    password = data.get('password')
    if not phone or not password:
        return jsonify({'error': 'phone and password required'}), 400
    user = users.find_one({'phone': phone})
    if not user or not check_password_hash(user['password'], password):
        return jsonify({'error': 'Invalid credentials'}), 401
    token = jwt.encode({
        'user_id': str(user['_id']),
        'exp': datetime.datetime.utcnow() + datetime.timedelta(days=7)
    }, app.config['SECRET_KEY'], algorithm='HS256')
    return jsonify({'token': token, 'phone': user['phone']}), 200

@app.route('/api/price', methods=['POST'])
def price():
    data = request.json or {}
    pickup = data.get('pickup')
    dropoff = data.get('dropoff')
    if not pickup or not dropoff:
        return jsonify({'error': 'pickup and dropoff required'}), 400
    try:
        distance_km = compute_distance_via_gebeta(pickup, dropoff)
    except Exception as e:
        return jsonify({'error': 'Failed to compute distance', 'details': str(e)}), 500
    base_fee = float(os.getenv('BASE_FEE', 40))
    per_km = float(os.getenv('PER_KM', 12))
    price_val = base_fee + (per_km * distance_km)
    return jsonify({'price': round(price_val, 2), 'distance_km': round(distance_km, 3)})

@app.route('/api/order', methods=['POST'])
@token_required
def create_order():
    data = request.json or {}
    pickup = data.get('pickup')
    dropoff = data.get('dropoff')
    price_val = data.get('price')
    if not pickup or not dropoff or price_val is None:
        return jsonify({'error': 'pickup, dropoff, price required'}), 400
    order = {
        'user_id': str(request.user['_id']),
        'phone': request.user['phone'],
        'pickup': pickup,
        'dropoff': dropoff,
        'price': float(price_val),
        'status': 'pending',
        'created_at': datetime.datetime.utcnow()
    }
    res = orders.insert_one(order)
    return jsonify({'message': 'Order created', 'order_id': str(res.inserted_id)}), 201

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=True)
