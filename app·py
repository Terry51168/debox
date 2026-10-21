import os
import re
import time
import hashlib
import random
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

APP_KEY = os.getenv("DEBOX_APP_KEY", "")
APP_SECRET = os.getenv("DEBOX_APP_SECRET", "")
WEBHOOK_KEY = os.getenv("DEBOX_WEBHOOK_KEY", "")

CA_PATTERN = re.compile(r"0x[a-fA-F0-9]{40}")

def make_headers():
    nonce = str(random.randint(10000000, 99999999))
    timestamp = str(int(time.time() * 1000))
    raw = f"{APP_SECRET}{nonce}{timestamp}"
    signature = hashlib.sha1(raw.encode()).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-API-KEY": APP_KEY,
        "nonce": nonce,
        "timestamp": timestamp,
        "signature": signature,
    }

def get_inline_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "🐕 查风险", "callback_data": "check_risk"},
                {"text": "📊 查持仓", "callback_data": "check_holdings"}
            ],
            [
                {"text": "📜 使用说明", "callback_data": "show_help"},
                {"text": "🔗 访问DeBox", "url": "https://debox.pro"}
            ]
        ]
    }

def send_card_message(group_id, text, keyboard=None):
    url = "https://open.debox.pro/openapi/messages/group/send"
    payload = {"group_id": group_id, "object_name": "text", "content": text}
    if keyboard:
        payload["reply_markup"] = keyboard
    try:
        requests.post(url, json=payload, headers=make_headers(), timeout=10)
    except Exception:
        pass

def handle_callback(callback_query_id, data, group_id):
    if data == "check_risk":
        reply_text = "🐕‍🦺 请输入或转发你要查询风险的合约地址（0x开头），警犬马上帮你闻一闻！"
    elif data == "check_holdings":
        reply_text = "📊 持仓查询功能正在开发中，很快就能上线啦！"
    elif data == "show_help":
        reply_text = "🐕‍🦺 我是链上警犬，专门负责群内的安全。\n\n你可以：\n1. 直接发CA给我，我帮你查风险；\n2. 点击下方按钮使用快捷功能。"
    else:
        reply_text = "未知指令。"
    url = "https://open.debox.pro/openapi/messages/group/send"
    payload = {"group_id": group_id, "object_name": "text", "content": reply_text}
    requests.post(url, json=payload, headers=make_headers(), timeout=10)

def get_token_risk(ca_address):
    url = f"https://api.gopluslabs.io/api/v1/token_security/56?contract_addresses={ca_address}"
    try:
        resp = requests.get(url, timeout=8)
        data = resp.json()
        if data.get("code") != 1 or not data.get("result"):
            return "🐕‍🦺 嗅探失败：未找到该代币的安全数据。"
        info = data["result"].get(ca_address.lower(), {})
        is_honeypot = info.get("is_honeypot", "0")
        buy_tax = info.get("buy_tax", "0")
        sell_tax = info.get("sell_tax", "0")
        lines = []
        if is_honeypot == "1":
            lines.append("⚠️ 貔貅盘（无法卖出）")
        else:
            lines.append("✅ 非貔貅盘")
        try:
            b_pct, s_pct = float(buy_tax)*100, float(sell_tax)*100
            if b_pct > 10 or s_pct > 10:
                lines.append(f"⚠️ 高额税(买{b_pct:.1f}%/卖{s_pct:.1f}%)")
            else:
                lines.append(f"交易税(买{b_pct:.1f}%/卖{s_pct:.1f}%)")
        except: pass
        return f"🐕‍🦺 嗅探完毕：{'；'.join(lines[:2])}。DYOR！"
    except Exception:
        return "🐕‍🦺 网络嗅探异常，请稍后。"

@app.route("/webhook", methods=["POST"])
def webhook():
    if request.headers.get("X-API-KEY", "") != WEBHOOK_KEY:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    if "callback_query" in data:
        cb = data["callback_query"]
        callback_id = cb.get("id")
        cb_data = cb.get("data")
        group_id = cb.get("message", {}).get("chat", {}).get("id")
        if group_id and cb_data:
            handle_callback(callback_id, cb_data, group_id)
        return jsonify({"status": "ok"}), 200

    message = data.get("message", "")
    group_id = data.get("group_id", "")
    if group_id and message:
        if "@" in message or "菜单" in message or "help" in message.lower():
            keyboard = get_inline_keyboard()
            send_card_message(group_id, "🐕‍🦺 警犬已就位！请点击下方按钮，或直接发送CA给我：", keyboard)
            return jsonify({"status": "ok"}), 200
        match = CA_PATTERN.search(message)
        if match:
            reply_text = get_token_risk(match.group())
            send_card_message(group_id, reply_text)
    return jsonify({"status": "ok"}), 200
