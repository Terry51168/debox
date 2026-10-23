import os
import re
import time
import requests
import threading
import logging
from datetime import datetime, timedelta
from urllib.parse import urlparse
from flask import Flask, request, jsonify

app = Flask(__name__)

APP_KEY = os.getenv("DEBOX_APP_KEY", "")
WEBHOOK_KEY = os.getenv("DEBOX_WEBHOOK_KEY", "")
UPSTASH_URL = os.getenv("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID", "9410sfdk")
USE_ASYNC_SEND = os.getenv("USE_ASYNC_SEND", "1") == "1"

# 支持 EVM 和 Solana 的正则
CA_PATTERN_EVM = re.compile(r"0x[a-fA-F0-9]{40}")
CA_PATTERN_SOL = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
URL_PATTERN = re.compile(r"https?://[^\s]+")

CHAIN_MAPPING = {
    "ethereum": "1", "bsc": "56", "polygon": "137",
    "arbitrum": "42161", "base": "8453", "avalanche": "43114", "optimism": "10"
}
CHAIN_NAMES = {
    "1": "ETH", "56": "BSC", "137": "POL", "42161": "ARB", "8453": "BASE", "43114": "AVAX", "10": "OP",
    "solana": "SOL"
}
BLACKHOLE_ADDRS = {"0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead"}
_ca_cache = {}

def get_beijing_time():
    return datetime.utcnow() + timedelta(hours=8)

def fmt_money(n):
    try:
        n = float(n)
        if n >= 1000000: return f"${n/1000000:.2f}M"
        if n >= 1000: return f"${n/1000:.2f}K"
        return f"${n:.2f}"
    except: return "$0.00"

def record_activity(user_id=None, group_id=None, is_query=False):
    if not UPSTASH_URL or not UPSTASH_TOKEN: return
    now = get_beijing_time()
    today_str = now.strftime("%Y%m%d")
    week_str = (now - timedelta(days=now.weekday())).strftime("%Y%m%d")
    month_str = now.strftime("%Y%m")
    cmds = []
    if group_id: cmds.append(["SADD", "all_groups", str(group_id)])
    if is_query and user_id:
        uid = str(user_id)
        cmds.extend([
            ["SADD", "all_users", uid], ["INCR", "total_queries"],
            ["SADD", f"active_daily_{today_str}", uid],
            ["SADD", f"active_weekly_{week_str}", uid],
            ["SADD", f"active_monthly_{month_str}", uid]
        ])
    if not cmds: return
    try: requests.post(f"{UPSTASH_URL}/pipeline", json=cmds, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}, timeout=5)
    except: pass

def get_cardinality(key):
    if not UPSTASH_URL: return 0
    try:
        res = requests.get(f"{UPSTASH_URL}/scard/{key}", headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}, timeout=3).json()
        return int(res.get("result") or 0)
    except: return 0

def _get_int(key):
    if not UPSTASH_URL: return 0
    try:
        res = requests.get(f"{UPSTASH_URL}/get/{key}", headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}, timeout=3).json()
        return int(res.get("result") or 0)
    except: return 0

def calc_growth(current, previous):
    if previous == 0: return "🟢 +100%" if current > 0 else "0%"
    growth = ((current - previous) / previous) * 100
    return f"{'🟢 +' if growth >= 0 else '🔴 '}{growth:.1f}%"

def get_usage_stats():
    now = get_beijing_time()
    today = now.strftime("%Y%m%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y%m%d")
    this_week = (now - timedelta(days=now.weekday())).strftime("%Y%m%d")
    last_week = (now - timedelta(days=now.weekday() + 7)).strftime("%Y%m%d")
    this_month = now.strftime("%Y%m")
    last_month = (now.replace(day=1) - timedelta(days=1)).strftime("%Y%m")
    
    stats = {
        "total_groups": get_cardinality("all_groups"), "total_users": get_cardinality("all_users"),
        "total_queries": _get_int("total_queries")
    }
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

def get_token_full_info(ca_address):
    ca_address = ca_address.lower()
    now_ts = time.time()
    if ca_address in _ca_cache and now_ts - _ca_cache[ca_address][1] < 300:
        return _ca_cache[ca_address][0]

    dex_url = f"https://api.dexscreener.com/latest/dex/tokens/{ca_address}"
    try:
        res = requests.get(dex_url, timeout=5).json()
        pairs = res.get("pairs", [])
    except: return "🐕‍🦺 嗅探失败：网络异常"

    if not pairs:
        return "🐕‍🦺 嗅探失败：DexScreener 暂未收录该代币行情（可能是极早期盘或假地址）。"

    best_pair = max(pairs, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) if x.get("liquidity") else 0))
    dex_chain = best_pair.get("chainId", "bsc")
    chain_id = CHAIN_MAPPING.get(dex_chain)
    chain_name = CHAIN_NAMES.get(chain_id, dex_chain.upper())

    price = float(best_pair.get("priceUsd", "0"))
    fdv = float(best_pair.get("fdv", "0"))
    total_liq, total_vol = 0.0, 0.0
    for p in pairs:
        liq_data = p.get("liquidity")
        vol_data = p.get("volume")
        if liq_data: total_liq += float(liq_data.get("usd", 0))
        if vol_data: total_vol += float(vol_data.get("h24", 0))

    msg_lines = [
        f"💰 价格: ${price:.6f}",
        f"📊 市值: {fmt_money(fdv)} | 全链总流动性: {fmt_money(total_liq)} ({len(pairs)}个池子)",
        f"📈 24h总量: {fmt_money(total_vol)}"
    ]
    is_dangerous = False
    risk_reasons = []
    if total_liq < 1000:
        risk_reasons.append("全链总流动性极低 (<$1000)")
        is_dangerous = True

    # 只有 EVM 链才去查 GoPlus，Solana 直接跳过
    if chain_id:
        goplus_url = f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}?contract_addresses={ca_address}"
        try:
            g_res = requests.get(goplus_url, timeout=5).json()
            if g_res.get("code") == 1 and g_res.get("result"):
                info = g_res["result"].get(ca_address, {})
                if info:
                    is_honeypot = info.get("is_honeypot", "0")
                    buy_tax = float(info.get("buy_tax") or 0) * 100
                    sell_tax = float(info.get("sell_tax") or 0) * 100
                    holders = info.get("holders", [])
                    top10_pct = sum(float(h.get("percent") or 0)*100 for h in holders[:10] if h.get("address","").lower() not in BLACKHOLE_ADDRS)
                    if is_honeypot == "1": risk_reasons.append("貔貅盘（无法卖出）"); is_dangerous = True
                    if buy_tax > 10 or sell_tax > 10: risk_reasons.append(f"高额交易税(买{buy_tax:.1f}%/卖{sell_tax:.1f}%)"); is_dangerous = True
                    if top10_pct > 80: risk_reasons.append(f"持仓高度集中(前10占{top10_pct:.1f}%)"); is_dangerous = True
                    msg_lines.append(f"🛡️ 税率: 买{buy_tax:.1f}% 卖{sell_tax:.1f}% | 前10持仓(剔除黑洞): {top10_pct:.1f}%")
                    lp_holders = info.get("lp_holders", [])
                    total_locked_pct = 0.0
                    lock_lines = []
                    for lp in lp_holders:
                        if lp.get("is_locked") == 1:
                            pct = float(lp.get("percent") or 0) * 100
                            total_locked_pct += pct
                            end_str = ""
                            details = lp.get("locked_detail") or []
                            if details:
                                end_time = details[0].get("end_time")
                                if end_time and str(end_time).isdigit():
                                    end_int = int(end_time)
                                    if end_int > time.time():
                                        dt = datetime.fromtimestamp(end_int)
                                        end_str = f"至 {dt.strftime('%Y-%m-%d')}"
                                    else: end_str = "已过期"
                            lock_lines.append(f"  ↳ 锁仓 {pct:.1f}% {end_str}".rstrip())
                    if total_locked_pct > 0:
                        msg_lines.append(f"🔒 池子总锁仓: {total_locked_pct:.1f}%")
                        msg_lines.extend(lock_lines)
                    else:
                        msg_lines.append("🔒 池子锁仓: 0.0% (未检测到锁定)")
        except: pass
    elif chain_name == "SOL":
        msg_lines.append("🛡️ Solana 链暂不支持深度安全检测，请自行评估风险。")

    header = "🚨 汪汪汪！！（狂吠）报告老板，发现危险目标！\n" if is_dangerous else "🐕 汪！（摇尾巴）嗅探完毕，目前闻起来没毛病：\n"
    risk_text = "\n⚠️ 危险提示: " + " | ".join(risk_reasons) if risk_reasons else ""
    final_text = f"{header}📍 {chain_name} 链数据如下：\n" + "\n".join(msg_lines) + risk_text + "\n\n⚠️ DYOR！"
    _ca_cache[ca_address] = (final_text, now_ts)
    return final_text

def send_message(chat_type, chat_id, content):
    url = "https://open.debox.pro/openapi/bot/sendMessage"
    headers = {"Content-Type": "application/json", "X-API-KEY": APP_KEY}
    payload = {"chat_type": chat_type, "chat_id": chat_id, "content": content, "parse_mode": "text"}
    try: requests.post(url, json=payload, headers=headers, timeout=10)
    except: pass

def _process_message(data):
    message = data.get("message_raw", data.get("message", ""))
    group_id = data.get("group_id")
    from_user_id = data.get("from_user_id")
    
    ca_address = data.get("address")
    
    # 识别逻辑升级
    if not ca_address and message:
        match_evm = CA_PATTERN_EVM.search(message)
        if match_evm:
            ca_address = match_evm.group()
        else:
            match_sol = CA_PATTERN_SOL.search(message)
            if match_sol:
                ca_address = match_sol.group()

    if not message and not ca_address:
        if group_id: record_activity(group_id=group_id)
        return

    record_activity(user_id=from_user_id, group_id=group_id, is_query=bool(ca_address))

    if message and any(k in message.lower() for k in ["/help", "教程", "菜单", "说明"]):
        help_text = (
            "🐕‍🦺 链上警犬使用说明\n\n"
            "👉 查代币安全：直接在群内发送合约地址（支持 0x 开头的 EVM 地址，以及 Solana 地址），警犬会立刻帮你排雷。\n"
            "👉 防钓鱼雷达：群内出现不明链接，警犬会自动拦截并发出警告。\n"
            "👉 深度数据：自动聚合全链池子，并检测锁仓比例与庄家控盘率。\n\n"
            "💡 觉得好用？点击下方链接加入产品体验群，获取最新功能！\n"
            "🔗 体验群链接：https://m.debox.pro/group?id=sajshm5d&code=9410sfdk"
        )
        if group_id: send_message("group", group_id, help_text)
        elif from_user_id: send_message("private", from_user_id, help_text)
        return

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
        elif from_user_id:
            send_message("private", from_user_id, "🐕 警告：这是内部管理指令，你没有权限查看哦！")
        return

    if ca_address:
        reply_text = get_token_full_info(ca_address)
        if group_id: send_message("group", group_id, reply_text)
        elif from_user_id: send_message("private", from_user_id, reply_text)
        return

    if message:
        match_url = URL_PATTERN.search(message)
        if match_url and group_id:
            domain = urlparse(match_url.group()).netloc.lower()
            if not any(domain == d or domain.endswith("." + d) for d in ["twitter.com", "x.com", "debox.pro"]):
                send_message("group", group_id, f"🐕 警告：嗅探到不明链接！\n\n🌐 域名: {domain}\n警犬闻到了钓鱼的风险，请群友警惕！")

@app.route("/webhook", methods=["POST"])
def webhook():
    if request.headers.get("X-API-KEY", "") != WEBHOOK_KEY:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    if USE_ASYNC_SEND:
        threading.Thread(target=_process_message, args=(data,), daemon=True).start()
        return jsonify({"status": "ok"}), 200
    else:
        _process_message(data)
        return jsonify({"status": "ok"}), 200

handler = app
