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

# 全局连接池，极速复用
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "ChainDog/1.0"})

# 防刷缓存
_ca_cache = {}

def is_valid_solana_address(addr):
    if not isinstance(addr, str) or not (32 <= len(addr) <= 44):
        return False
    num = 0
    try:
        for ch in addr:
            num = num * 58 + BASE58_ALPHABET.index(ch)
    except ValueError:
        return False
    leading_zeros = 0
    for ch in addr:
        if ch == '1': leading_zeros += 1
        else: break
    byte_len = leading_zeros if num == 0 else leading_zeros + (num.bit_length() + 7) // 8
    return byte_len == 32

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

# ================= 优化：使用 Pipeline 批量写入数据 =================
def record_activity_pipeline(group_id=None, user_id=None):
    if not UPSTASH_URL or not UPSTASH_TOKEN: return
    headers = {"Authorization": f"Bearer {UPSTASH_TOKEN}"}
    cmds = []
    
    if group_id:
        cmds.append(["SADD", "all_groups", str(group_id)])
        
    if user_id:
        now = get_beijing_time()
        today_str = now.strftime("%Y%m%d")
        week_str = (now - timedelta(days=now.weekday())).strftime("%Y%m%d")
        month_str = now.strftime("%Y%m")
        uid = str(user_id)
        cmds.extend([
            ["SADD", "all_users", uid],
            ["INCR", "total_queries"],
            ["SADD", f"active_daily_{today_str}", uid],
            ["SADD", f"active_weekly_{week_str}", uid],
            ["SADD", f"active_monthly_{month_str}", uid]
        ])
        
    if not cmds: return
    try:
        # 一次请求打包发完，极大降低失败率
        HTTP.post(f"{UPSTASH_URL}/pipeline", json=cmds, headers=headers, timeout=3)
    except: pass

def get_cardinality(key):
    if not UPSTASH_URL: return 0
    try:
        res = HTTP.get(f"{UPSTASH_URL}/scard/{key}", headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}, timeout=3).json()
        return res.get("result", 0)
    except: return 0

def calc_growth(current, previous):
    if previous == 0: return "🟢 +100%" if current > 0 else "0%"
    growth = ((current - previous) / previous) * 100
    icon = "🟢 +" if growth >= 0 else "🔴 "
    return f"{icon}{growth:.1f}%"

def get_usage_stats():
    now = get_beijing_time()
    today = now.strftime("%Y%m%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y%m%d")
    this_week = (now - timedelta(days=now.weekday())).strftime("%Y%m%d")
    last_week = (now - timedelta(days=now.weekday() + 7)).strftime("%Y%m%d")
    this_month = now.strftime("%Y%m")
    last_month = (now.replace(day=1) - timedelta(days=1)).strftime("%Y%m")

    stats = {}
    stats["total_groups"] = get_cardinality("all_groups")
    stats["total_users"] = get_cardinality("all_users")
    try:
        res_q = HTTP.get(f"{UPSTASH_URL}/get/total_queries", headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}, timeout=3).json()
        stats["total_queries"] = int(res_q.get("result") or 0)
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

# ================= 核心查币逻辑 =================
def get_token_full_info(ca_address):
    ca_address = ca_address.lower() if ca_address.startswith("0x") else ca_address
    
    now_ts = time.time()
    if ca_address in _ca_cache and now_ts - _ca_cache[ca_address][1] < 300:
        return _ca_cache[ca_address][0]

    reply = _get_solana_token_info(ca_address) if not ca_address.startswith("0x") else _get_evm_token_info(ca_address)
    _ca_cache[ca_address] = (reply, now_ts)
    return reply

def _get_evm_token_info(ca_address):
    dex_url = f"https://api.dexscreener.com/latest/dex/tokens/{ca_address}"
    try:
        # 超时设为 3 秒，防止 Vercel 超过 10 秒限制
        pairs = HTTP.get(dex_url, timeout=3).json().get("pairs", [])
    except: pairs = []

    if not pairs:
        return "🐕‍🦺 嗅探失败：没查到行情数据，可能是极早期盘、假地址。"

    best_pair = max(pairs, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) if x.get("liquidity") else 0))
    chain_id = CHAIN_MAPPING.get(best_pair.get("chainId", "bsc"))
    chain_name = CHAIN_NAMES.get(chain_id, best_pair.get("chainId", "UNKNOWN").upper())

    fdv = float(best_pair.get("fdv", "0"))
    total_liq, total_vol = 0.0, 0.0
    for p in pairs:
        if p.get("liquidity"): total_liq += float(p.get("liquidity").get("usd", 0))
        if p.get("volume"): total_vol += float(p.get("volume").get("h24", 0))

    msg_lines, risk_reasons = [], []
    is_dangerous = False

    msg_lines.append(f"📊 市值: {fmt_money(fdv)} | 全链总流动性: {fmt_money(total_liq)} ({len(pairs)}个池子)")
    msg_lines.append(f"📈 24h总量: {fmt_money(total_vol)}")

    if total_liq < 1000:
        risk_reasons.append("全链总流动性极低 (<$1000)")
        is_dangerous = True

    if chain_id:
        goplus_url = f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}?contract_addresses={ca_address}"
        try:
            g_res = HTTP.get(goplus_url, timeout=3).json()
            if g_res.get("code") == 1 and g_res.get("result"):
                info = g_res["result"].get(ca_address, {})
                if info:
                    is_honeypot = info.get("is_honeypot", "0")
                    buy_tax = float(info.get("buy_tax") or 0) * 100
                    sell_tax = float(info.get("sell_tax") or 0) * 100
                    top10_pct = sum(float(h.get("percent") or 0)*100 for h in info.get("holders", [])[:10] if h.get("address","").lower() not in ["0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead"])

                    if is_honeypot == "1": risk_reasons.append("貔貅盘（无法卖出）"); is_dangerous = True
                    if buy_tax > 10 or sell_tax > 10: risk_reasons.append(f"高额交易税(买{buy_tax:.1f}%/卖{sell_tax:.1f}%)"); is_dangerous = True
                    if top10_pct > 80: risk_reasons.append(f"持仓高度集中(前10占{top10_pct:.1f}%)"); is_dangerous = True

                    msg_lines.append(f"🛡️ 税率: 买{buy_tax:.1f}% 卖{sell_tax:.1f}% | 前10持仓: {top10_pct:.1f}%")

                    total_locked_pct, lock_text_lines = 0.0, []
                    for lp in info.get("lp_holders", []):
                        if lp.get("is_locked") == 1:
                            pct = float(lp.get("percent") or 0) * 100
                            total_locked_pct += pct
                            end_time_str = ""
                            details = lp.get("locked_detail") or []
                            if details:
                                end_time = details[0].get("end_time")
                                if end_time and str(end_time).isdigit():
                                    end_int = int(end_time)
                                    if end_int > int(time.time()):
                                        dt = datetime.fromtimestamp(end_int)
                                        end_time_str = f"至 {dt.strftime('%Y-%m-%d')}"
                                    else: end_time_str = "已过期"
                            lock_text_lines.append(f"  ↳ 锁仓 {pct:.1f}% {end_time_str}")
                    
                    if total_locked_pct > 0:
                        msg_lines.append(f"🔒 池子总锁仓: {total_locked_pct:.1f}%")
                        msg_lines.extend(lock_text_lines)
                    else: msg_lines.append(f"🔒 池子锁仓: 0.0%")
        except: pass
    else:
        msg_lines.append(f"🛡️ 暂不支持该链({chain_name})的安全检测。")

    header = "🚨 汪汪汪！！（狂吠）报告老板，发现危险目标！\n" if is_dangerous else "🐕 汪！（摇尾巴）嗅探完毕，目前闻起来没毛病：\n"
    risk_text = "\n⚠️ 危险提示: " + " | ".join(risk_reasons) if risk_reasons else ""
    return f"{header}📍 {chain_name} 链数据如下：\n" + "\n".join(msg_lines) + risk_text + "\n\n⚠️ DYOR！"

def _get_solana_token_info(ca_address):
    try:
        pairs = HTTP.get(f"https://api.dexscreener.com/latest/dex/tokens/{ca_address}", timeout=3).json().get("pairs", [])
    except: pairs = []

    if not pairs:
        return "🐕‍🦺 嗅探失败：DexScreener 未收录该 Solana 代币，池子可能尚未建立。"

    best_pair = max(pairs, key=lambda x: float((x.get("liquidity") or {}).get("usd", 0) or 0))
    fdv = float(best_pair.get("fdv", "0") or 0)
    total_liq = sum(float((p.get("liquidity") or {}).get("usd", 0) or 0) for p in pairs)
    total_vol = sum(float((p.get("volume") or {}).get("h24", 0) or 0) for p in pairs)

    lines, risks = [f"📊 市值: {fmt_money(fdv)} | 全链总流动性: {fmt_money(total_liq)} ({len(pairs)}个池子)", f"📈 24h总量: {fmt_money(total_vol)}"], []
    is_dangerous = False

    try:
        g_res = HTTP.get(f"https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses={ca_address}", timeout=3).json()
        if g_res.get("code") == 1 and g_res.get("result"):
            info = g_res["result"].get(ca_address, {})
            if info:
                mint = (info.get("mintable") or {}).get("status", "0")
                freeze = (info.get("freezable") or {}).get("status", "0")
                close = (info.get("closable") or {}).get("status", "0")

                if mint == "1": risks.append("Mint 权限未放弃（可无限增发砸盘）"); is_dangerous = True
                if freeze == "1": risks.append("Freeze 权限未放弃（可冻结账户，无法卖出）"); is_dangerous = True
                if close == "1": risks.append("Close 权限未放弃"); is_dangerous = True

                holders = info.get("holders", []) or []
                top10_pct = sum(float(h.get("percent", 0) or 0) * 100 for h in holders[:10])
                if top10_pct > 80: risks.append(f"前10持仓高度集中 ({top10_pct:.1f}%)"); is_dangerous = True

                lines.append(f"🛡️ Mint: {'⚠️ 未放弃' if mint == '1' else '✅ 已放弃'} | Freeze: {'⚠️ 未放弃' if freeze == '1' else '✅ 已放弃'}")
                lines.append(f"👥 前10持仓(估算): {top10_pct:.1f}%")
    except: pass

    if total_liq < 1000: risks.append("流动性极低 (<$1000)"); is_dangerous = True

    header = "🚨 汪汪汪！！（狂吠）Solana 目标存在危险信号！\n" if is_dangerous else "🐕 汪！（摇尾巴）Solana 嗅探完毕，权限已放弃，目前闻起来没毛病：\n"
    risk_text = "\n⚠️ 危险提示: " + " | ".join(risks) if risks else ""
    return f"{header}📍 SOL 链数据如下：\n" + "\n".join(lines) + risk_text + "\n\n⚠️ DYOR！"

def send_message(chat_type, chat_id, content):
    try:
        # 发送超时设为 5 秒
        HTTP.post("https://open.debox.pro/openapi/bot/sendMessage", 
            json={"chat_type": chat_type, "chat_id": chat_id, "content": content, "parse_mode": "text"},
            headers={"Content-Type": "application/json", "X-API-KEY": APP_KEY}, timeout=5)
    except: pass

# ================= Webhook 入口 =================
@app.route("/webhook", methods=["POST"])
def webhook():
    if request.headers.get("X-API-KEY", "") != WEBHOOK_KEY:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    message = data.get("message_raw", data.get("message", ""))
    group_id = data.get("group_id")
    from_user_id = data.get("from_user_id")
    
    # 优先识别 DeBox 卡片中的地址
    ca_address = data.get("address")

    if group_id:
        record_activity_pipeline(group_id=group_id)

    if message or ca_address:
        # 1. 教程
        if message and any(k in message.lower() for k in ["/help", "教程", "菜单", "说明"]):
            help_text = (
                "🐕‍🦺 链上警犬使用说明\n\n"
                "👉 查代币安全：直接在群内发送合约地址（支持 EVM 0x 开头 及 Solana Base58 地址）。\n"
                "👉 防钓鱼雷达：群内出现不明链接，警犬会自动拦截。\n"
                "👉 深度数据：全链池子聚合，EVM 查貔貅/锁仓，Solana 查 Mint/Freeze 权限。\n\n"
                "💡 觉得好用？点击下方链接加入产品体验群：\n"
                "🔗 https://m.debox.pro/group?id=sajshm5d&code=9410sfdk"
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
            elif from_user_id:
                send_message("private", from_user_id, "🐕 警告：这是内部管理指令，你没有权限查看哦！")
            return jsonify({"status": "ok"}), 200

        # 3. 找 CA 地址
        if not ca_address and message:
            match_ca = CA_PATTERN.search(message)
            if match_ca:
                ca_address = match_ca.group()

        if ca_address:
            is_valid_ca = ca_address.startswith("0x") or is_valid_solana_address(ca_address)
            if is_valid_ca:
                reply_text = get_token_full_info(ca_address)
                if group_id: send_message("group", group_id, reply_text)
                elif from_user_id: send_message("private", from_user_id, reply_text)
                
                record_activity_pipeline(user_id=from_user_id)
                return jsonify({"status": "ok"}), 200

        # 4. 防钓鱼
        if message and group_id:
            match_url = URL_PATTERN.search(message)
            if match_url:
                domain = urlparse(match_url.group()).netloc.lower()
                if not any(domain == d or domain.endswith("." + d) for d in ["twitter.com", "x.com", "debox.pro"]):
                    send_message("group", group_id, f"🐕 警告：嗅探到不明链接！\n\n🌐 域名: {domain}\n警犬闻到了钓鱼的风险，请群友警惕！")

    return jsonify({"status": "ok"}), 200

handler = app
