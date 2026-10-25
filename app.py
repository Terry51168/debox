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

UPSTASH_URL = os.getenv("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")

ADMIN_USER_ID = "9410sfdk"

# 匹配 0x 和 Solana
CA_PATTERN = re.compile(r"(0x[a-fA-F0-9]{40})|([1-9A-HJ-NP-Za-km-z]{32,44})")
URL_PATTERN = re.compile(r"https?://[^\s]+|\b[a-zA-Z0-9.-]+\.(?:com|xyz|net|org|io|top|vip|cc|cn|so|me)(?:/[^\s]*)?")

CHAIN_MAPPING = {"ethereum": "1", "bsc": "56", "polygon": "137", "arbitrum": "42161", "base": "8453", "avalanche": "43114", "optimism": "10"}
CHAIN_NAMES = {"1": "ETH", "56": "BSC", "137": "POL", "42161": "ARB", "8453": "BASE", "43114": "AVAX", "10": "OP"}

BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "ChainDog/1.0"})
_ca_cache = {}

def is_valid_solana_address(addr):
    if not isinstance(addr, str) or not (32 <= len(addr) <= 44): return False
    num = 0
    try:
        for ch in addr: num = num * 58 + BASE58_ALPHABET.index(ch)
    except ValueError: return False
    leading_zeros = len(addr) - len(addr.lstrip('1'))
    byte_len = leading_zeros if num == 0 else leading_zeros + (num.bit_length() + 7) // 8
    return byte_len == 32

def get_beijing_time():
    return datetime.utcnow() + timedelta(hours=8)

def fmt_money(n):
    try:
        n = float(n)
        if n >= 1000000: return f"${n/1000000:.2f}M"
        if n >= 1000: return f"${n/1000:.1f}K"
        return f"${n:.2f}"
    except: return "$0"

# ================= 运营数据统计（豪华版） =================
def record_activity(group_id=None, user_id=None):
    if not UPSTASH_URL or not UPSTASH_TOKEN: return
    cmds = []
    if group_id: cmds.append(["SADD", "all_groups", str(group_id)])
    if user_id:
        now = get_beijing_time()
        today, week, month = now.strftime("%Y%m%d"), (now - timedelta(days=now.weekday())).strftime("%Y%m%d"), now.strftime("%Y%m")
        uid = str(user_id)
        cmds.extend([
            ["SADD", "all_users", uid], ["INCR", "total_queries"],
            ["SADD", f"active_daily_{today}", uid], ["SADD", f"active_weekly_{week}", uid], ["SADD", f"active_monthly_{month}", uid]
        ])
    if cmds:
        try: HTTP.post(f"{UPSTASH_URL}/pipeline", json=cmds, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}, timeout=3)
        except: pass

def get_cardinality(key):
    if not UPSTASH_URL: return 0
    try: return HTTP.get(f"{UPSTASH_URL}/scard/{key}", headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}, timeout=3).json().get("result", 0)
    except: return 0

def calc_growth(current, previous):
    if previous == 0: return "🟢 +100%" if current > 0 else "0%"
    growth = ((current - previous) / previous) * 100
    icon = "🟢 +" if growth >= 0 else "🔴 "
    return f"{icon}{growth:.1f}%"

def get_usage_stats():
    now = get_beijing_time()
    today, yesterday = now.strftime("%Y%m%d"), (now - timedelta(days=1)).strftime("%Y%m%d")
    this_week, last_week = (now - timedelta(days=now.weekday())).strftime("%Y%m%d"), (now - timedelta(days=now.weekday() + 7)).strftime("%Y%m%d")
    this_month, last_month = now.strftime("%Y%m"), (now.replace(day=1) - timedelta(days=1)).strftime("%Y%m")
    
    stats = {"total_groups": get_cardinality("all_groups"), "total_users": get_cardinality("all_users")}
    try: stats["total_queries"] = int(HTTP.get(f"{UPSTASH_URL}/get/total_queries", headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}, timeout=3).json().get("result") or 0)
    except: stats["total_queries"] = 0
    
    today_active = get_cardinality(f"active_daily_{today}")
    yesterday_active = get_cardinality(f"active_daily_{yesterday}")
    this_week_active = get_cardinality(f"active_weekly_{this_week}")
    last_week_active = get_cardinality(f"active_weekly_{last_week}")
    this_month_active = get_cardinality(f"active_monthly_{this_month}")
    last_month_active = get_cardinality(f"active_monthly_{last_month}")
    
    stats["daily_str"] = f"{today_active} 人 (较昨日 {calc_growth(today_active, yesterday_active)})"
    stats["weekly_str"] = f"{this_week_active} 人 (较上周 {calc_growth(this_week_active, last_week_active)})"
    stats["monthly_str"] = f"{this_month_active} 人 (较上月 {calc_growth(this_month_active, last_month_active)})"
    return stats

# ================= 查币核心（极简降噪） =================
def get_token_full_info(ca):
    ca = ca.lower() if ca.startswith("0x") else ca
    if ca in _ca_cache and time.time() - _ca_cache[ca][1] < 300: return _ca_cache[ca][0]

    reply = _get_solana_info(ca) if not ca.startswith("0x") else _get_evm_info(ca)
    _ca_cache[ca] = (reply, time.time())
    return reply

def _get_evm_info(ca):
    try: pairs = HTTP.get(f"https://api.dexscreener.com/latest/dex/tokens/{ca}", timeout=3).json().get("pairs", [])
    except: pairs = []
    if not pairs: return "🐕‍🦺 暂未收录该代币行情（可能极早期），请谨慎。"

    best = max(pairs, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) if x.get("liquidity") else 0))
    cid = CHAIN_MAPPING.get(best.get("chainId", "bsc"))
    cname = CHAIN_NAMES.get(cid, best.get("chainId", "未知").upper())
    fdv = float(best.get("fdv", "0"))
    liq = sum(float(p.get("liquidity", {}).get("usd", 0)) for p in pairs if p.get("liquidity"))

    is_dangerous = False
    danger_msg = []
    
    if liq < 1000: is_dangerous = True; danger_msg.append("池子极小")

    tax = "0% / 0%"
    top10 = "0.0%"
    lock = "0%"
    audit_str = "自毁✅ 代理✅ 增发✅ 白名单✅ 暂停✅ 黑名单✅"
    
    if cid:
        try:
            g = HTTP.get(f"https://api.gopluslabs.io/api/v1/token_security/{cid}?contract_addresses={ca}", timeout=3).json()
            info = (g.get("result") or {}).get(ca, {})
            if info:
                if info.get("is_honeypot") == "1": is_dangerous = True; danger_msg.append("貔貅")
                bt, st = float(info.get("buy_tax") or 0) * 100, float(info.get("sell_tax") or 0) * 100
                if bt > 10 or st > 10: is_dangerous = True; danger_msg.append("高税")
                tax = f"{bt:.1f}% / {st:.1f}%"
                
                t10 = sum(float(h.get("percent") or 0) * 100 for h in info.get("holders", [])[:10] if h.get("address", "").lower() not in ["0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead"])
                if t10 > 80: is_dangerous = True; danger_msg.append("死控盘")
                top10 = f"{t10:.1f}%"
                
                lck = sum(float(lp.get("percent") or 0) * 100 for lp in info.get("lp_holders", []) if lp.get("is_locked") == 1)
                if lck == 0: danger_msg.append("未锁")
                lock = f"{lck:.0f}%"

                is_self = info.get("selfdestruct", {}).get("value") == "1" or info.get("is_selfdestruct") == "1"
                is_proxy = info.get("is_proxy") == "1"
                is_mint = info.get("is_mintable") == "1"
                is_white = info.get("is_whitelisted") == "1"
                is_pause = info.get("transfer_pausable") == "1"
                is_black = info.get("is_blacklisted") == "1"

                audit_parts = []
                if is_self: audit_parts.append("自毁❌"); is_dangerous = True; danger_msg.append("自毁")
                else: audit_parts.append("自毁✅")
                
                if is_proxy: audit_parts.append("代理❌"); is_dangerous = True; danger_msg.append("代理")
                else: audit_parts.append("代理✅")
                
                if is_mint: audit_parts.append("增发❌"); is_dangerous = True; danger_msg.append("增发")
                else: audit_parts.append("增发✅")
                
                if is_white: audit_parts.append("白名单❌"); is_dangerous = True; danger_msg.append("白名单")
                else: audit_parts.append("白名单✅")
                
                if is_pause: audit_parts.append("暂停❌"); is_dangerous = True; danger_msg.append("暂停交易")
                else: audit_parts.append("暂停✅")
                
                if is_black: audit_parts.append("黑名单❌"); is_dangerous = True; danger_msg.append("黑名单")
                else: audit_parts.append("黑名单✅")

                audit_str = " ".join(audit_parts)
        except: pass

    if is_dangerous:
        header = "🚨 **警犬狂吠！极度危险！**\n"
        conclusion = f"❌ **结论：极高风险（{'|'.join(set(danger_msg))}），千万别碰！** DYOR."
    else:
        header = "🐕 **警犬摇尾巴！暂未发现风险。**\n"
        conclusion = "✅ **结论：扫描通过，自行判断。** DYOR."

    body = (
        f"📍 {cname} | 市值: {fmt_money(fdv)} | 池子: {fmt_money(liq)}\n"
        f"🩸 税率: {tax}\n"
        f"👥 前10持仓: {top10}\n"
        f"🔒 池子锁仓: {lock}\n"
        f"🛡️ 审计: {audit_str}\n"
    )
    return f"{header}{body}{conclusion}"

def _get_solana_info(ca):
    try: pairs = HTTP.get(f"https://api.dexscreener.com/latest/dex/tokens/{ca}", timeout=3).json().get("pairs", [])
    except: pairs = []
    if not pairs: return "🐕‍🦺 暂未收录该 Solana 代币，请谨慎。"

    best = max(pairs, key=lambda x: float((x.get("liquidity") or {}).get("usd", 0) or 0))
    fdv = float(best.get("fdv", "0") or 0)
    liq = sum(float((p.get("liquidity") or {}).get("usd", 0) or 0) for p in pairs)
    
    is_dangerous = False
    danger_msg = []
    if liq < 1000: is_dangerous = True; danger_msg.append("池子极小")

    mint, freeze, top10 = "✅", "✅", "0.0%"
    try:
        g = HTTP.get(f"https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses={ca}", timeout=3).json()
        info = (g.get("result") or {}).get(ca, {})
        if info:
            if (info.get("mintable") or {}).get("status") == "1": mint = "❌"; is_dangerous = True; danger_msg.append("可增发")
            if (info.get("freezable") or {}).get("status") == "1": freeze = "❌"; is_dangerous = True; danger_msg.append("可冻结")
            t10 = sum(float(h.get("percent", 0) or 0) * 100 for h in info.get("holders", [])[:10])
            if t10 > 80: is_dangerous = True; danger_msg.append("死控盘")
            top10 = f"{t10:.1f}%"
    except: pass

    if is_dangerous:
        header = "🚨 **警犬狂吠！极度危险！**\n"
        conclusion = f"❌ **结论：极高风险（{'|'.join(set(danger_msg))}），千万别碰！** DYOR."
    else:
        header = "🐕 **警犬摇尾巴！暂未发现风险。**\n"
        conclusion = "✅ **结论：扫描通过，自行判断。** DYOR."

    body = (
        f"📍 SOL | 市值: {fmt_money(fdv)} | 池子: {fmt_money(liq)}\n"
        f"🛡️ 权限: Mint {mint} | Freeze {freeze}\n"
        f"👥 前10持仓: {top10}\n"
    )
    return f"{header}{body}{conclusion}"

def send_message(chat_type, chat_id, content):
    try: HTTP.post("https://open.debox.pro/openapi/bot/sendMessage", json={"chat_type": chat_type, "chat_id": chat_id, "content": content, "parse_mode": "text"}, headers={"Content-Type": "application/json", "X-API-KEY": APP_KEY}, timeout=5)
    except: pass

# ================= Webhook 路由 =================
@app.route("/webhook", methods=["POST"])
def webhook():
    if request.headers.get("X-API-KEY", "") != WEBHOOK_KEY:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    message = data.get("message_raw", data.get("message", ""))
    group_id, from_user_id, ca_address = data.get("group_id"), data.get("from_user_id"), data.get("address")

    if group_id: record_activity(group_id=group_id)

    if message or ca_address:
        # 1. 防钓鱼
        if message and group_id:
            for m in URL_PATTERN.finditer(message):
                domain = (urlparse(m.group()).hostname or "").lower()
                if not domain or any(domain == d or domain.endswith("." + d) for d in ["twitter.com", "x.com", "debox.pro"]): continue
                
                try:
                    resp = HTTP.get(f"https://api.gopluslabs.io/api/v1/phishing_site?url={m.group()}", timeout=3).json()
                    if resp.get("code") == 1 and (resp.get("result") or {}).get("phishing_site") == 1:
                        send_message("group", group_id, f"🚨 **高危拦截！发现钓鱼网站！**\n🌐 `{domain}` \n请不要点击，极易被盗钱包！")
                        return jsonify({"status": "ok"}), 200
                except: pass
                continue

        # 2. 后台指令（豪华看板完全体）
        if message:
            if "/admin" in message or "/stats" in message:
                if str(from_user_id) == ADMIN_USER_ID or str(from_user_id) == f"@{ADMIN_USER_ID}":
                    stats = get_usage_stats()
                    current_time = get_beijing_time().strftime('%H:%M')
                    admin_msg = (
                        f"📊 链上警犬 · 实时运营看板\n\n"
                        f"👥 服务群组 (实时): {stats['total_groups']} 个\n"
                        f"🔍 累计查币次数: {stats['total_queries']:,} 次\n"
                        f"🧑‍💻 累计独立用户: {stats['total_users']:,} 人\n\n"
                        f"📈 活跃用户环比 (日/周/月):\n"
                        f"🔸 今日: {stats['daily_str']}\n"
                        f"🔸 本周: {stats['weekly_str']}\n"
                        f"🔸 本月: {stats['monthly_str']}\n\n"
                        f"*数据更新于 {current_time}*"
                    )
                    send_message("private", from_user_id, admin_msg)
                elif from_user_id:
                    send_message("private", from_user_id, "🐕 警告：这是内部管理指令，你没有权限查看哦！")
                return jsonify({"status": "ok"}), 200

        # 3. 查币
        if not ca_address and message:
            clean_msg = URL_PATTERN.sub("", message)
            match_ca = CA_PATTERN.search(clean_msg)
            if match_ca: ca_address = match_ca.group()

        if ca_address:
            if ca_address.startswith("0x") or is_valid_solana_address(ca_address):
                reply_text = get_token_full_info(ca_address)
                send_message("group" if group_id else "private", group_id or from_user_id, reply_text)
                record_activity(user_id=from_user_id)
                return jsonify({"status": "ok"}), 200

    return jsonify({"status": "ok"}), 200

handler = app
