import os
import hmac
import hashlib
import time
import json
from flask import Flask, request, jsonify
from flask_cors import CORS
from pymongo import MongoClient
from bson.objectid import ObjectId
import datetime
from chapa import Chapa
import jwt

# ---- CONFIG ----
app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": ["http://localhost:3000", "http://localhost:5173", "*"]}})
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret")

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
client = MongoClient(MONGO_URI)
db = client.tolodelivery
users = db.users
orders = db.orders

CHAPA_SECRET_KEY = os.getenv("CHAPA_SECRET_KEY", "")
CHAPA_PUBLIC_KEY = os.getenv("CHAPA_PUBLIC_KEY", "")
CHAPA_REDIRECT_URL = os.getenv("CHAPA_REDIRECT_URL", "https://yourdomain.com/order-success")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

chapa = Chapa(CHAPA_SECRET_KEY)


# ---- Utilities: validate Telegram WebApp initData ----
def parse_init_data(init_data_str):
    parts = {}
    for item in init_data_str.split("&"):
        if "=" in item:
            k, v = item.split("=", 1)
            parts[k] = v
    return parts


def validate_webapp_data(bot_token, init_data_str):
    """
    Validate Telegram WebApp initData and return parsed dict (with 'user' as dict when present).
    Raises ValueError on invalid data.
    """
    if not bot_token:
        raise ValueError("BOT_TOKEN not set on server")

    parts = parse_init_data(init_data_str)
    if "hash" not in parts:
        raise ValueError("hash missing in initData")
    received_hash = parts.pop("hash")

    sorted_items = sorted(parts.items())
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted_items)

    secret_key = hmac.new(key=b"WebAppData", msg=bot_token.encode(), digestmod=hashlib.sha256).digest()
    calculated_hash = hmac.new(key=secret_key, msg=data_check_string.encode(), digestmod=hashlib.sha256).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        raise ValueError("Invalid initData hash")

    parsed = {}
    for k, v in parts.items():
        parsed[k] = v
    if "user" in parsed:
        try:
            parsed["user"] = json.loads(parsed["user"])
        except Exception:
            # keep as string if parse fails
            pass
    return parsed


# ---- Auth endpoint (frontend can call to get JWT) ----
@app.route("/api/login/telegram", methods=["POST"])
def login_telegram():
    body = request.json or {}
    init_data = body.get("init_data")
    if not init_data:
        return jsonify({"error": "Missing init_data"}), 400
    try:
        parsed = validate_webapp_data(BOT_TOKEN, init_data)
    except Exception as e:
        return jsonify({"error": "Invalid initData", "details": str(e)}), 403

    user_obj = parsed.get("user")
    if not user_obj:
        return jsonify({"error": "Telegram did not provide user object"}), 400

    phone = user_obj.get("phone_number")
    if not phone:
        phone = f"tg_{user_obj.get('id')}"

    user = users.find_one({"phone": phone})
    if not user:
        new_id = users.insert_one({
            "phone": phone,
            "tg_id": user_obj.get("id"),
            "first_name": user_obj.get("first_name"),
            "created_at": datetime.datetime.utcnow()
        }).inserted_id
        user = users.find_one({"_id": new_id})

    token = jwt.encode({
        "user_id": str(user["_id"]),
        "exp": datetime.datetime.utcnow() + datetime.timedelta(days=7)
    }, app.config["SECRET_KEY"], algorithm="HS256")

    return jsonify({"token": token, "phone": phone}), 200


# ---- Helper: get user from JWT or initData header ----
def get_user_from_header_or_init():
    # Try JWT Authorization first
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth.split("Bearer ")[-1]
        try:
            payload = jwt.decode(token, app.config["SECRET_KEY"], algorithms=["HS256"])
            uid = payload.get("user_id")
            user = users.find_one({"_id": ObjectId(uid)})
            if user:
                return user, None
            return None, "User not found"
        except Exception as e:
            return None, f"Invalid token: {e}"

    # Try Telegram initData header
    init_data = request.headers.get("X-Telegram-Init-Data") or request.json.get("init_data")
    if init_data:
        try:
            parsed = validate_webapp_data(BOT_TOKEN, init_data)
            user_obj = parsed.get("user") or {}
            phone = user_obj.get("phone_number") or f"tg_{user_obj.get('id')}"
            user = users.find_one({"phone": phone})
            if not user:
                new_id = users.insert_one({
                    "phone": phone,
                    "tg_id": user_obj.get("id"),
                    "first_name": user_obj.get("first_name"),
                    "created_at": datetime.datetime.utcnow()
                }).inserted_id
                user = users.find_one({"_id": new_id})
            return user, None
        except Exception as e:
            return None, f"Invalid initData: {e}"

    return None, "No auth provided"


# ---- Order creation (create order first, then init payment) ----
@app.route("/api/order", methods=["POST"])
def create_order():
    user, err = get_user_from_header_or_init()
    if err:
        return jsonify({"error": err}), 401

    data = request.json or {}
    pickup = data.get("pickup")
    dropoff = data.get("dropoff")
    item = data.get("item", "")
    quantity = data.get("quantity", 1)
    price_val = data.get("price")
    payment_type = data.get("payment", "Cash")  # "Cash", "Chapa" (redirect), or "Inline"

    if not pickup or not dropoff or price_val is None:
        return jsonify({"error": "pickup, dropoff, price required"}), 400

    order = {
        "user_id": str(user["_id"]),
        "phone": user["phone"],
        "pickup": pickup,
        "dropoff": dropoff,
        "item": item,
        "quantity": quantity,
        "payment": payment_type,
        "price": float(price_val),
        "status": "pending",
        "created_at": datetime.datetime.utcnow()
    }

    res = orders.insert_one(order)
    order_id = str(res.inserted_id)

    # If Cash, just return order id
    if payment_type == "Cash":
        return jsonify({"message": "Order created (Cash)", "order_id": order_id}), 201

    # Build Chapa payload with tx_ref = order_id
    customer_info = {
        "amount": float(price_val),
        "currency": "ETB",
        "tx_ref": order_id,
        "email": f"user{user['phone']}@tolo.delivery",
        "first_name": user.get("first_name", "User"),
        "last_name": user.get("phone"),
        "callback_url": CHAPA_REDIRECT_URL,
        "return_url": CHAPA_REDIRECT_URL,
        "customization": {
            "title": "Tolo Delivery Payment",
            "description": f"Payment for order {order_id}"
        }
    }

    try:
        # Initialize Chapa transaction (this registers tx_ref with Chapa)
        res_chapa = chapa.initialize(customer_info, autoRef=False)
        checkout_url = res_chapa["data"].get("checkout_url")
    except Exception as e:
        # If Chapa init fails, keep order but return error
        return jsonify({"error": "Failed to initialize payment", "details": str(e)}), 400

    # If redirect flow requested
    if payment_type == "Chapa":
        return jsonify({
            "order_id": order_id,
            "chapa": {
                "checkout_url": checkout_url,
                "public_key": CHAPA_PUBLIC_KEY
            }
        }), 201

    # If inline flow requested, return public_key + tx_ref (order_id)
    if payment_type == "Inline":
        return jsonify({
            "order_id": order_id,
            "chapa": {
                "public_key": CHAPA_PUBLIC_KEY,
                "tx_ref": order_id,
                "callback_url": CHAPA_REDIRECT_URL,
                "return_url": CHAPA_REDIRECT_URL
            }
        }), 201

    # fallback
    return jsonify({"error": "Unsupported payment type"}), 400


# ---- Chapa callback (verify and update order) ----
@app.route("/api/pay/callback", methods=["POST", "GET"])
def chapa_callback():
    tx_ref = request.args.get("tx_ref") or (request.json or {}).get("tx_ref")
    status = request.args.get("status") or (request.json or {}).get("status")

    if not tx_ref:
        return jsonify({"error": "tx_ref missing"}), 400

    # For inline or redirect, we used order_id as tx_ref
    try:
        orders.update_one({"_id": ObjectId(tx_ref)}, {"$set": {"status": "paid" if status == "success" else "failed"}})
    except Exception as e:
        print("Callback update failed:", e)
        return jsonify({"error": "Invalid order id"}), 400

    return jsonify({"message": "callback processed"}), 200


# ---- run ----
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)
