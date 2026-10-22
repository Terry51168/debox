import os
import re
import time
import hashlib
import random
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# 从环境变量读取配置
APP_KEY = os.getenv("DEBOX_APP_KEY", "")
APP_SECRET = os.getenv("DEBOX_APP_SECRET", "")
WEBHOOK_KEY = os.getenv("DEBOX_WEBHOOK_KEY", "")

# 匹配 0x 开头的合约地址
CA_PATTERN = re.compile(r"0x[a-fA-F0-9]{40}")

# 生成 API 请求签名
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

# 发送消息到群组（带日志打印）
def send_group_message(group_id, content):
    url = "https://open.debox.pro/openapi/messages/group/send"
    payload = {
        "group_id": group_id,
        "object_name": "text",
        "content": content
    }
    try:
        # 发起请求
        resp = requests.post(url, json=payload, headers=make_headers(), timeout=10)
        # 打印 DeBox 返回的详细结果
        print(f"==== DeBox发消息状态码: {resp.status_code} ====")
        print(f"==== DeBox发消息返回内容: {resp.text} ====")
    except Exception as e:
        print(f"==== 发送消息异常: {e} ====")

# 查询代币安全信息
def get_token_risk(ca_address):
    # 默认查 BSC (56)，你可以根据需要修改
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
        except: 
            pass
        
        return f"🐕‍🦺 嗅探完毕：{'；'.join(lines[:2])}。DYOR！"
    except Exception as e:
        print(f"==== GoPlus查询异常: {e} ====")
        return "🐕‍🦺 网络嗅探异常，请稍后。"

# Webhook 接收入口
@app.route("/webhook", methods=["POST"])
def webhook():
    # 校验 Webhook 密钥
    if request.headers.get("X-API-KEY", "") != WEBHOOK_KEY:
        print("==== 警告：X-API-KEY 校验失败！ ====")
        return jsonify({"error": "unauthorized"}), 401
    
    data = request.get_json(silent=True) or {}
    
    # 打印 DeBox 发过来的原始数据，方便排查格式
    print(f"==== 收到 DeBox 原始数据: {data} ====")
    
    message = data.get("message", "")
    group_id = data.get("group_id", "")
    
    # 确保是群消息且有文本
    if group_id and message:
        match = CA_PATTERN.search(message)
        if match:
            ca_address = match.group()
            print(f"==== 识别到 CA 地址: {ca_address}，开始查询 ====")
            
            reply_text = get_token_risk(ca_address)
            print(f"==== 准备回复内容: {reply_text} ====")
            
            # 发送消息
            send_group_message(group_id, reply_text)
        else:
            print("==== 收到群消息，但里面没有 CA 地址 ====")
    else:
        print("==== 收到消息，但没有 group_id (可能是私聊或回调) ====")

    return jsonify({"status": "ok"}), 200

# Vercel 需要的入口
handler = app
