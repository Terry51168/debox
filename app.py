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

# 数据后台配置
UPSTASH_URL = os.getenv("UPSTASH_REDIS_REST_URL", "")
UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")

# ⚠️ 你的专属管理员 ID
ADMIN_USER_ID = "9410sfdk"

# ===== 地址正则 =====
CA_PATTERN = re.compile(r"(0x[a-fA-F0-9]{40})|([1-9A-HJ-NP-Za-km-z]{32,44})")
URL_PATTERN = re.compile(r"https?://[^\s]+")

CHAIN_MAPPING = {
    "ethereum": "1", "bsc": "56", "polygon": "137",
    "arbitrum": "42161", "base": "8453", "avalanche": "43114",
    "optimism": "10"
}
CHAIN_NAMES = {
    "1": "ETH", "56": "BSC", "137": "POL",
    "42161": "ARB", "8453": "BASE", "43114": "AVAX",
    "10": "OP"
}

BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

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

# ================= 运营数据统计系统 =================
def record_realtime_group(group_id):
    if not UPSTASH_URL or not UPSTASH_TOKEN or not group_id: return
    try: requests.get(f"{UPSTASH_URL}/sadd/all_groups/{group_id}", headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}, timeout=3)
    except: pass

def record_user_activity(user_id):
    if not UPSTASH_URL or not UPSTASH_TOKEN or not user_id: return
    headers = {"Authorization": f"Bearer {UPSTASH_TOKEN}"}
    now = get_beijing_time()
    today_str, week_str, month_str = now.strftime("%Y%m%d"), (now - timedelta(days=now.weekday())).strftime("%Y%m%d"), now.strftime("%Y%m")
    try:
        requests.get(f"{UPSTASH_URL}/sadd/all_users/{user_id}", headers=headers, timeout=3)
        requests.get(f"{UPSTASH_URL}/incr/total_queries", headers=headers, timeout=3)
        requests.get(f"{UPSTASH_URL}/sadd/active_daily_{today_str}/{user_id}", headers=headers, timeout=3)
        requests.get(f"{UPSTASH_URL}/sadd/active_weekly_{week_str}/{user_id}", headers=headers, timeout=3)
        requests.get(f"{UPSTASH_URL}/sadd/active_monthly_{month_str}/{user_id}", headers=headers, timeout=3)
    except: pass

def get_cardinality(key):
    if not UPSTASH_URL: return 0
    try: return requests.get(f"{UPSTASH_URL}/scard/{key}", headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}, timeout=3).json().get("result", 0)
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
    try: stats["total_queries"] = int(requests.get(f"{UPSTASH_URL}/get/total_queries", headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}, timeout=3).json().get("result") or 0)
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

# ================= 深度审计查币逻辑 =================
def get_token_full_info(ca_address):
    if not ca_address.startswith("0x"): return _get_solana_token_info(ca_address)

    try: pairs = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{ca_address}", timeout=5).json().get("pairs", [])
    except: pairs = []

    has_dex_data = False
    chain_id = "56"
    chain_name = "未知"
    fdv, total_liq, total_vol = 0.0, 0.0, 0.0

    if pairs:
        has_dex_data = True
        best_pair = max(pairs, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) if x.get("liquidity") else 0))
        chain_id = CHAIN_MAPPING.get(best_pair.get("chainId", "bsc"))
        chain_name = CHAIN_NAMES.get(chain_id, best_pair.get("chainId", "未知").upper())
        fdv = float(best_pair.get("fdv", "0"))
        total_liq = sum(float(p.get("liquidity", {}).get("usd", 0)) for p in pairs if p.get("liquidity"))
        total_vol = sum(float(p.get("volume", {}).get("h24", 0)) for p in pairs if p.get("volume"))

    is_dangerous = False
    risk_reasons = []
    tax_str, top10_str, lock_str = "0% / 0%", "0%", "0%"
    audit_str = "自毁✅ 代理✅ 增发✅ 白名单✅ 暂停✅ 黑名单✅"

    if total_liq < 1000 and has_dex_data: 
        is_dangerous = True; risk_reasons.append("池子极小")

    if chain_id:
        try:
            g_res = requests.get(f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}?contract_addresses={ca_address}", timeout=5).json()
            info = g_res.get("result", {}).get(ca_address.lower(), {})
            if info:
                # 基础风险
                if info.get("is_honeypot") == "1": is_dangerous = True; risk_reasons.append("貔貅")
                bt, st = float(info.get("buy_tax") or 0) * 100, float(info.get("sell_tax") or 0) * 100
                if bt > 10 or st > 10: is_dangerous = True; risk_reasons.append("高税")
                tax_str = f"{bt:.1f}% / {st:.1f}%"

                top10_pct = sum(float(h.get("percent") or 0) * 100 for h in info.get("holders", [])[:10] if h.get("address","").lower() not in ["0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead"])
                if top10_pct > 80: is_dangerous = True; risk_reasons.append("死控盘")
                top10_str = f"{top10_pct:.1f}%"

                total_locked = sum(float(lp.get("percent") or 0) * 100 for lp in info.get("lp_holders", []) if lp.get("is_locked") == 1)
                if total_locked == 0 and has_dex_data: risk_reasons.append("未锁")
                lock_str = f"{total_locked:.0f}%"

                # 🌟 深度审计解析
                is_self = info.get("selfdestruct", {}).get("value") == "1" or info.get("is_selfdestruct") == "1"
                is_proxy = info.get("is_proxy") == "1"
                is_mint = info.get("is_mintable") == "1"
                is_white = info.get("is_whitelisted") == "1"
                is_pause = info.get("transfer_pausable") == "1"
                is_black = info.get("is_blacklisted") == "1"

                audit_parts = []
                if is_self: audit_parts.append("自毁❌"); is_dangerous = True; risk_reasons.append("自毁")
                else: audit_parts.append("自毁✅")
                if is_proxy: audit_parts.append("代理❌"); is_dangerous = True; risk_reasons.append("代理")
                else: audit_parts.append("代理✅")
                if is_mint: audit_parts.append("增发❌"); is_dangerous = True; risk_reasons.append("增发")
                else: audit_parts.append("增发✅")
                if is_white: audit_parts.append("白名单❌"); is_dangerous = True; risk_reasons.append("白名单")
                else: audit_parts.append("白名单✅")
                if is_pause: audit_parts.append("暂停❌"); is_dangerous = True; risk_reasons.append("暂停交易")
                else: audit_parts.append("暂停✅")
                if is_black: audit_parts.append("黑名单❌"); is_dangerous = True; risk_reasons.append("黑名单")
                else: audit_parts.append("黑名单✅")
                audit_str = " ".join(audit_parts)
            else:
                if not has_dex_data: return "🐕‍🦺 该代币极早期：无行情且无安全数据，请极度谨慎！"
        except: pass

    header = "🚨 **警犬狂吠！极度危险！**\n" if is_dangerous else "🐕 **警犬摇尾巴！暂未发现风险。**\n"
    conclusion = f"❌ **结论：极高风险（{'|'.join(set(risk_reasons))}），千万别碰！** DYOR." if is_dangerous else "✅ **结论：扫描通过，自行判断。** DYOR."
    
    if has_dex_data:
        body = (
            f"📍 {chain_name} | 市值: {fmt_money(fdv)} | 池子: {fmt_money(total_liq)}\n"
            f"🩸 税: {tax_str} | 👥 前10: {top10_str} | 🔒 锁仓: {lock_str}\n"
            f"🛡️ 审计: {audit_str}\n"
        )
    else:
        body = (
            f"📍 未收录行情(极早期) | 安全检测如下\n"
            f"🩸 税: {tax_str} | 👥 前10: {top10_str}\n"
            f"🛡️ 审计: {audit_str}\n"
        )
    return f"{header}{body}{conclusion}"

def _get_solana_token_info(ca_address):
    try: pairs = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{ca_address}", timeout=5).json().get("pairs", [])
    except: pairs = []
    
    has_dex_data = bool(pairs)
    fdv, total_liq = 0.0, 0.0
    if has_dex_data:
        best_pair = max(pairs, key=lambda x: float((x.get("liquidity") or {}).get("usd", 0) or 0))
        fdv = float(best_pair.get("fdv", "0") or 0)
        total_liq = sum(float((p.get("liquidity") or {}).get("usd", 0) or 0) for p in pairs)
    
    is_dangerous = False
    risks = []
    if total_liq < 1000 and has_dex_data: is_dangerous = True; risks.append("池子极小")

    mint, freeze, top10 = "✅", "✅", "0.0%"
    try:
        g_res = requests.get(f"https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses={ca_address}", timeout=5).json()
        info = g_res.get("result", {}).get(ca_address, {})
        if info:
            if (info.get("mintable") or {}).get("status") == "1": mint = "❌"; is_dangerous = True; risks.append("可增发")
            if (info.get("freezable") or {}).get("status") == "1": freeze = "❌"; is_dangerous = True; risks.append("可冻结")
            t10 = sum(float(h.get("percent", 0) or 0) * 100 for h in info.get("holders", [])[:10])
            if t10 > 80: is_dangerous = True; risks.append("死控盘")
            top10 = f"{t10:.1f}%"
        else:
            if not has_dex_data: return "🐕‍🦺 该代币极早期：无行情且无安全数据，请极度谨慎！"
    except: pass

    header = "🚨 **警犬狂吠！极度危险！**\n" if is_dangerous else "🐕 **警犬摇尾巴！暂未发现风险。**\n"
    conclusion = f"❌ **结论：极高风险（{'|'.join(set(risks))}），千万别碰！** DYOR." if is_dangerous else "✅ **结论：扫描通过，自行判断。** DYOR."
    
    if has_dex_data:
        body = (
            f"📍 SOL | 市值: {fmt_money(fdv)} | 池子: {fmt_money(total_liq)}\n"
            f"🛡️ 权限: Mint {mint} | Freeze {freeze}\n"
            f"👥 前10持仓: {top10}\n"
        )
    else:
        body = (
            f"📍 未收录行情(极早期) | 安全检测如下\n"
            f"🛡️ 权限: Mint {mint} | Freeze {freeze}\n"
            f"👥 前10持仓: {top10}\n"
        )
    return f"{header}{body}{conclusion}"

def send_message(chat_type, chat_id, content):
    try: requests.post("https://open.debox.pro/openapi/bot/sendMessage", json={"chat_type": chat_type, "chat_id": chat_id, "content": content, "parse_mode": "text"}, headers={"Content-Type": "application/json", "X-API-KEY": APP_KEY}, timeout=10)
    except: pass

# ================= Webhook 路由 =================
@app.route("/webhook", methods=["POST"])
def webhook():
    if request.headers.get("X-API-KEY", "") != WEBHOOK_KEY:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    message = data.get("message_raw", data.get("message", ""))
    group_id = data.get("group_id")
    from_user_id = data.get("from_user_id")
    # 🌟 核心修复：读取 DeBox 卡片自带的地址
    ca_address = data.get("address")

    if group_id: record_realtime_group(group_id)

    if message or ca_address:
        # 1. 使用教程
        if message and any(k in message.lower() for k in ["/help", "教程", "菜单", "说明"]):
            help_text = (
                "🐕‍🦺 链上警犬使用说明\n\n"
                "👉 查代币安全：直接在群内发送合约地址，警犬会立刻帮你排雷。\n"
                "👉 六维合约审计：自动检测自毁、代理、增发、白名单、暂停、黑名单。\n\n"
                "🔗 体验群链接：https://m.debox.pro/group?id=sajshm5d&code=9410sfdk"
            )
            if group_id: send_message("group", group_id, help_text)
            elif from_user_id: send_message("private", from_user_id, help_text)
            return jsonify({"status": "ok"}), 200

        # 2. 管理员查后台
        if message and ("/admin" in message or "/stats" in message):
            if str(from_user_id) == ADMIN_USER_ID or str(from_user_id) == f"@{ADMIN_USER_ID}":
                stats = get_usage_stats()
                admin_msg = (
                    f"📊 链上警犬 · 实时运营看板\n\n"
                    f"👥 服务群组 (实时): {stats['total_groups']} 个\n"
                    f"🔍 累计查币次数: {stats['total_queries']:,} 次\n"
                    f"🧑‍💻 累计独立用户: {stats['total_users']:,} 人\n\n"
                    f"📈 活跃用户环比 (日/周/月):\n"
                    f"🔸 今日: {stats['daily_str']}\n"
                    f"🔸 本周: {stats['weekly_str']}\n"
                    f"🔸 本月: {stats['monthly_str']}\n\n"
                    f"*数据更新于 {get_beijing_time().strftime('%H:%M')}*"
                )
                send_message("private", from_user_id, admin_msg)
            return jsonify({"status": "ok"}), 200

        # 3. 查 CA（优先读取卡片地址，其次正则提取）
        if not ca_address and message:
            match_ca = CA_PATTERN.search(message)
            if match_ca: ca_address = match_ca.group()

        if ca_address:
            is_valid_ca = ca_address.startswith("0x") or is_valid_solana_address(ca_address)
            if is_valid_ca:
                reply_text = get_token_full_info(ca_address)
                if group_id: send_message("group", group_id, reply_text)
                elif from_user_id: send_message("private", from_user_id, reply_text)
                record_user_activity(from_user_id)
                return jsonify({"status": "ok"}), 200

        # 4. 精准防钓鱼（放在最后兜底）
        if message and group_id:
            match_url = URL_PATTERN.search(message)
            if match_url:
                url = match_url.group()
                try:
                    domain = urlparse(url).netloc.lower()
                    if not any(domain == d or domain.endswith("." + d) for d in ["twitter.com", "x.com", "debox.pro"]):
                        resp = requests.get(f"https://api.gopluslabs.io/api/v1/phishing_site?url={url}", timeout=3).json()
                        if resp.get("code") == 1 and (resp.get("result") or {}).get("phishing_site") == 1:
                            send_message("group", group_id, f"🚨 **高危拦截！发现钓鱼网站！**\n🌐 `{domain}` \n请不要点击，极易被盗钱包！")
                            return jsonify({"status": "ok"}), 200
                except: pass

    return jsonify({"status": "ok"}), 200

handler = app
