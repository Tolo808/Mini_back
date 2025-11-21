import os
import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
from pymongo import MongoClient
from bson.objectid import ObjectId

# ---- CONFIG ----
app = Flask(__name__)

CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=True)


MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
client = MongoClient(MONGO_URI)
db = client.tolodelivery
orders = db.orders


@app.route("/api/order", methods=["POST"])
def create_order():
    data = request.json
    required = ["senderPhone", "receiverPhone", "pickup", "dropoff", "item", "quantity", "price"]
    if not all(k in data for k in required):
        return jsonify({"error": "Missing fields"}), 400

    order = {
        "senderPhone": data["senderPhone"],
        "receiverPhone": data["receiverPhone"],
        "pickup": data["pickup"],
        "dropoff": data["dropoff"],
        "item": data["item"],
        "quantity": data["quantity"],
        "price": data["price"],
        "payment": data.get("payment", "Cash"),
        "status": "pending",
        "created_at": datetime.datetime.utcnow()
    }
    order_id = orders.insert_one(order).inserted_id
    return jsonify({"message": "Order saved", "order_id": str(order_id)}), 201

# ---- GET ALL ORDERS (optional, for testing) ----
@app.route("/api/orders", methods=["GET"])
def get_orders():
    all_orders = list(orders.find())
    for o in all_orders:
        o["_id"] = str(o["_id"])
    return jsonify(all_orders), 200

import requests
from flask import request, jsonify

CHAPA_SECRET = "CHASECK_TEST-KYMunDF7BLYxSt36wF4cM3VbDMy7ay36"

@app.post("/api/pay")
def init_payment():
    data = request.json

    # Validate fields
    required = ["order_id", "amount", "email", "phone"]
    if not all(k in data for k in required):
        return jsonify({"error": "Missing fields"}), 400

    order_id = data["order_id"]
    amount = data["amount"]
    email = data["email"]
    phone = data["phone"]

    tx_ref = f"Tolo-{order_id}"

    payload = {
        "amount": str(amount),
        "currency": "ETB",
        "email": email,
        "first_name": "Customer",
        "last_name": "User",
        "phone_number": phone,
        "tx_ref": tx_ref,

        # For testing — these MUST be valid
        "callback_url": "https://api.chapa.co/redirect",
        "return_url": "http://localhost:5173/payment-success",

        "customization": {
            "title": "Tolo Delivery Payment",
            "description": "Payment for delivery order",
            "logo": "https://your-logo-link.png"
        }
    }

    headers = {
        "Authorization": f"Bearer {CHAPA_SECRET}",
        "Content-Type": "application/json"
    }

    r = requests.post(
        "https://api.chapa.co/v1/transaction/initialize",
        json=payload,
        headers=headers
    )

    res = r.json()

    if res.get("status") == "success":
        return jsonify({
            "checkout_url": res["data"]["checkout_url"],
            "tx_ref": tx_ref
        })

    return jsonify({"error": "Payment Init Failed", "details": res}), 400


@app.get("/api/chapa/callback")
def chapa_callback():
    tx_ref = request.args.get("tx_ref")

    r = requests.get(f"https://api.chapa.co/v1/transaction/verify/{tx_ref}",
        headers={"Authorization": f"Bearer {CHAPA_SECRET}"}
    )
    data = r.json()

    if data["status"] == "success":
        order_id = tx_ref.split("-")[1]
        db.orders.update_one({"_id": ObjectId(order_id)}, {"$set": {"payment": "paid"}})

    return "Payment Verified"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)
