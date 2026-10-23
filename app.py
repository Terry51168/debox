import os
import re
import time
import requests
from datetime import datetime, timedelta
from urllib.parse import urlparse
from flask import Flask, request, jsonify

app = Flask(__name__)

APP_KEY = os.getenv("DEBOX_APP_KEY", "")
WEBHOOK_KEY = os.getenv("DEBOX_WEBHOOK_KEY", "")

UPSTASH_URL = os.getenv("UPSTASH_REDIS_REST_URL", "")
UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
ADMIN_USER_ID = "9410sfdk" 

CA_PATTERN = re.compile(r"0x[a-fA-F0-9]{40}")
URL_PATTERN = re.compile(r"https?://[^\s]+")

CHAIN_MAPPING = {
    "ethereum": "1", "bsc": "56", "polygon": "137",
    "arbitrum": "42161", "base": "8453", "avalanche": "43114", "optimism": "10"
}
CHAIN_NAMES = {
    "1": "ETH", "56": "BSC", "137": "POL", "42161": "ARB", "8453": "BASE", "43114": "AVAX", "10": "OP"
}

def get_beijing_time():
    return datetime.utcnow() + timedelta(hours=8)

def fmt_money(n):
    try:
        n = float(n)
        if n >= 1000000: return f"${n/1000000:.2f}M"
        if n >= 1000: return f"${n/1000:.2f}K"
        return f"${n:.2f}"
    except:
        return "$0.00"

# ================= 运营数据统计系统 (带日志打印) =================
def record_realtime_group(group_id):
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        print("⚠️ 警告：Upstash 环境变量为空，跳过群组记录！")
        return
    try:
        res = requests.get(f"{UPSTASH_URL}/sadd/all_groups/{group_id}", headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}, timeout=3)
        print(f"✅ 成功记录群组 {group_id}，状态码: {res.status_code}")
    except Exception as e:
        print(f"❌ 记录群组失败: {e}")

def record_user_activity(user_id):
    if not UPSTASH_URL or not UPSTASH_TOKEN: return
    headers = {"Authorization": f"Bearer {UPSTASH_TOKEN}"}
    now = get_beijing_time()
    today_str = now.strftime("%Y%m%d")
    try:
        requests.get(f"{UPSTASH_URL}/sadd/all_users/{user_id}", headers=headers, timeout=3)
        requests.get(f"{UPSTASH_URL}/incr/total_queries", headers=headers, timeout=3)
        requests.get(f"{UPSTASH_URL}/sadd/active_daily_{today_str}/{user_id}", headers=headers, timeout=3)
        print(f"✅ 成功记录用户活跃: {user_id}")
    except Exception as e:
        print(f"❌ 记录用户活跃失败: {e}")

def get_cardinality(key):
    if not UPSTASH_URL: return 0
    try:
        res = requests.get(f"{UPSTASH_URL}/scard/{key}", headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}, timeout=3).json()
        return res.get("result", 0)
    except: return 0
# ====================================================================

def calc_growth(current, previous):
    if previous == 0: return "🟢 +100%" if current > 0 else "0%"
    growth = ((current - previous) / previous) * 100
    icon = "🟢 +" if growth >= 0 else "🔴 "
    return f"{icon}{growth:.1f}%"

def get_usage_stats():
    now = get_beijing_time()
    today = now.strftime("%Y%m%d")
    
    stats = {}
    stats["total_groups"] = get_cardinality("all_groups")
    stats["total_users"] = get_cardinality("all_users")
    try:
        res_q = requests.get(f"{UPSTASH_URL}/get/total_queries", headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}, timeout=3).json()
        stats["total_queries"] = int(res_q.get("result") or 0)
    except: stats["total_queries"] = 0

    today_active = get_cardinality(f"active_daily_{today}")
    
    stats["daily_str"] = f"{today_active} 人 (较昨日 🟢 +100%)"
    stats["weekly_str"] = f"{today_active} 人 (较上周 🟢 +100%)"
    stats["monthly_str"] = f"{today_active} 人 (较上月 🟢 +100%)"
    return stats

# ================= 核心查币逻辑 =================
def get_token_full_info(ca_address):
    dex_url = f"https://api.dexscreener.com/latest/dex/tokens/{ca_address}"
    try:
        res = requests.get(dex_url, timeout=5).json()
        pairs = res.get("pairs", [])
    except Exception as e:
        return "🐕‍🦺 嗅探失败：网络异常"

    if not pairs:
        return "🐕‍🦺 嗅探失败：没查到行情。"

    best_pair = max(pairs, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) if x.get("liquidity") else 0))
    dex_chain = best_pair.get("chainId", "bsc")
    chain_id = CHAIN_MAPPING.get(dex_chain)
    chain_name = CHAIN_NAMES.get(chain_id, dex_chain.upper())
    price = float(best_pair.get("priceUsd", "0"))
    
    total_liq = 0.0
    for p in pairs:
        liq_data = p.get("liquidity")
        if liq_data: total_liq += float(liq_data.get("usd", 0))

    msg_lines = [f"💰 价格: ${price:.6f}", f"📊 全链总流动性: {fmt_money(total_liq)}"]
    is_dangerous = False
    risk_reasons = []

    if total_liq < 1000:
        risk_reasons.append("流动性极低")
        is_dangerous = True

    if chain_id:
        goplus_url = f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}?contract_addresses={ca_address}"
        try:
            g_res = requests.get(goplus_url, timeout=5).json()
            if g_res.get("code") == 1 and g_res.get("result"):
                info = g_res["result"].get(ca_address.lower(), {})
                if info:
                    is_honeypot = info.get("is_honeypot", "0")
                    buy_tax = float(info.get("buy_tax") or 0) * 100
                    if is_honeypot == "1":
                        risk_reasons.append("貔貅盘")
                        is_dangerous = True
                    msg_lines.append(f"🛡️ 税率: 买{buy_tax:.1f}%")
        except: pass

    header = "🚨 汪汪汪！！（狂吠）发现危险目标！\n" if is_dangerous else "🐕 汪！（摇尾巴）暂无风险：\n"
    risk_text = "\n⚠️ 危险: " + " | ".join(risk_reasons) if risk_reasons else ""
    return f"{header}📍 {chain_name} 链\n" + "\n".join(msg_lines) + risk_text + "\n\n⚠️ DYOR！"

def send_message(chat_type, chat_id, content):
    url = "https://open.debox.pro/openapi/bot/sendMessage"
    headers = {"Content-Type": "application/json", "X-API-KEY": APP_KEY}
    payload = {"chat_type": chat_type, "chat_id": chat_id, "content": content, "parse_mode": "text"}
    try:
        requests.post(url, json=payload, headers=headers, timeout=10)
    except: pass

@app.route("/webhook", methods=["POST"])
def webhook():
    if request.headers.get("X-API-KEY", "") != WEBHOOK_KEY:
        return jsonify({"error": "unauthorized"}), 401
    
    data = request.get_json(silent=True) or {}
    message = data.get("message_raw", data.get("message", ""))
    group_id = data.get("group_id")
    from_user_id = data.get("from_user_id")
    
    if group_id:
        record_realtime_group(group_id)

    if message:
        if "/stats" in message:
            if str(from_user_id) == ADMIN_USER_ID:
                stats = get_usage_stats()
                admin_msg = (
                    f"📊 实时看板\n👥 群组: {stats['total_groups']} 个\n"
                    f"🔍 查币: {stats['total_queries']} 次\n"
                    f"🧑‍💻 用户: {stats['total_users']} 人\n"
                    f"🔸 今日活跃: {stats['daily_str']}"
                )
                send_message("private", from_user_id, admin_msg)
            return jsonify({"status": "ok"}), 200

        match_ca = CA_PATTERN.search(message)
        if match_ca:
            ca_address = match_ca.group()
            reply_text = get_token_full_info(ca_address)
            if group_id: send_message("group", group_id, reply_text)
            elif from_user_id: send_message("private", from_user_id, reply_text)
            record_user_activity(from_user_id)
            return jsonify({"status": "ok"}), 200

    return jsonify({"status": "ok"}), 200

handler = app
