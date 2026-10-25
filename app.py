import os
import re
import time
import hmac
import threading
import requests
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from flask import Flask, request, jsonify

app = Flask(__name__)

APP_KEY = os.getenv("DEBOX_APP_KEY", "")
WEBHOOK_KEY = os.getenv("DEBOX_WEBHOOK_KEY", "")

# 数据后台配置
UPSTASH_URL = os.getenv("UPSTASH_REDIS_REST_URL", "")
UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")

# ⚠️ 你的专属管理员 ID（请确认是 Webhook 推送的真实 from_user_id，而不是邀请链接 code）
ADMIN_USER_ID = "9410sfdk"

# ===== 地址正则：同时支持 EVM 与 Solana =====
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

# Base58 字符集（用于 Solana 地址严格校验）
BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

# ===== 简易查询缓存：同 CA 60 秒内不重复打 API（防刷屏 + 省 GoPlus 额度）=====
_QUERY_CACHE = {}
QUERY_CACHE_TTL = 60


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
        if ch == '1':
            leading_zeros += 1
        else:
            break
    if num == 0:
        byte_len = leading_zeros
    else:
        byte_len = leading_zeros + (num.bit_length() + 7) // 8
    return byte_len == 32


def get_beijing_time():
    # utcnow() 已废弃，改用时区感知写法
    return datetime.now(timezone(timedelta(hours=8)))


def fmt_money(n):
    try:
        n = float(n)
        if n >= 1000000: return f"${n/1000000:.2f}M"
        if n >= 1000: return f"${n/1000:.2f}K"
        return f"${n:.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def to_float(v, default=0.0):
    """安全转 float：None / 空串 / 非数字统一兜底"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ================= 运营数据统计系统 =================
def record_realtime_group(group_id):
    if not UPSTASH_URL or not UPSTASH_TOKEN or not group_id: return
    try:
        requests.get(f"{UPSTASH_URL}/sadd/all_groups/{group_id}", headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}, timeout=3)
    except Exception as e:
        print(f"[stats] record_realtime_group 异常: {e}")


def record_user_activity(user_id):
    if not UPSTASH_URL or not UPSTASH_TOKEN or not user_id: return
    headers = {"Authorization": f"Bearer {UPSTASH_TOKEN}"}
    now = get_beijing_time()
    today_str = now.strftime("%Y%m%d")
    start_of_week = now - timedelta(days=now.weekday())
    week_str = start_of_week.strftime("%Y%m%d")
    month_str = now.strftime("%Y%m")
    try:
        requests.get(f"{UPSTASH_URL}/sadd/all_users/{user_id}", headers=headers, timeout=3)
        requests.get(f"{UPSTASH_URL}/incr/total_queries", headers=headers, timeout=3)
        requests.get(f"{UPSTASH_URL}/sadd/active_daily_{today_str}/{user_id}", headers=headers, timeout=3)
        requests.get(f"{UPSTASH_URL}/sadd/active_weekly_{week_str}/{user_id}", headers=headers, timeout=3)
        requests.get(f"{UPSTASH_URL}/sadd/active_monthly_{month_str}/{user_id}", headers=headers, timeout=3)
    except Exception as e:
        print(f"[stats] record_user_activity 异常: {e}")


def get_cardinality(key):
    if not UPSTASH_URL: return 0
    try:
        res = requests.get(f"{UPSTASH_URL}/scard/{key}", headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}, timeout=3).json()
        return res.get("result", 0)
    except Exception as e:
        print(f"[stats] get_cardinality({key}) 异常: {e}")
        return 0


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
        res_q = requests.get(f"{UPSTASH_URL}/get/total_queries", headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}, timeout=3).json()
        stats["total_queries"] = int(res_q.get("result") or 0)
    except Exception as e:
        print(f"[stats] total_queries 异常: {e}")
        stats["total_queries"] = 0

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


# ================= 核心：查币逻辑 =================
def get_token_full_info(ca_address):
    if not ca_address.startswith("0x"):
        return _get_solana_token_info(ca_address)

    dex_url = f"https://api.dexscreener.com/latest/dex/tokens/{ca_address}"
    try:
        res = requests.get(dex_url, timeout=5).json()
        pairs = res.get("pairs", []) or []
    except Exception as e:
        print(f"[query] DexScreener 异常: {e}")
        pairs = []

    if not pairs:
        return "🐕‍🦺 嗅探失败：没查到行情数据，可能是极早期盘、假地址，或不在已收录的链上。"

    def liq_of(p):
        return to_float((p.get("liquidity") or {}).get("usd"))

    def vol_of(p):
        return to_float((p.get("volume") or {}).get("h24"))

    best_pair = max(pairs, key=liq_of)
    dex_chain = best_pair.get("chainId", "bsc")
    chain_id = CHAIN_MAPPING.get(dex_chain)
    chain_name = CHAIN_NAMES.get(chain_id, dex_chain.upper())
    fdv = to_float(best_pair.get("fdv"))  # 修复：fdv 可能为 null，原 float(None) 会崩

    total_liq = sum(liq_of(p) for p in pairs)
    total_vol = sum(vol_of(p) for p in pairs)

    msg_lines = []
    is_dangerous = False
    risk_reasons = []

    msg_lines.append(f"📊 市值: {fmt_money(fdv)} | 全链总流动性: {fmt_money(total_liq)} ({len(pairs)}个池子)")
    msg_lines.append(f"📈 24h总量: {fmt_money(total_vol)}")

    if total_liq < 1000:
        risk_reasons.append("全链总流动性极低 (<$1000)")
        is_dangerous = True

    if not chain_id:
        msg_lines.append(f"🛡️ 暂不支持该链({chain_name})的深度安全检测。")
    else:
        goplus_url = f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}?contract_addresses={ca_address}"
        try:
            g_res = requests.get(goplus_url, timeout=5).json()
            if g_res.get("code") == 1 and g_res.get("result"):
                info = g_res["result"].get(ca_address.lower(), {}) or {}
                if not info:
                    msg_lines.append("🛡️ 暂未获取到该合约的安全数据。")
                else:
                    is_honeypot = info.get("is_honeypot", "0")
                    buy_tax = to_float(info.get("buy_tax")) * 100
                    sell_tax = to_float(info.get("sell_tax")) * 100
                    top10_pct = sum(to_float(h.get("percent")) * 100 for h in (info.get("holders") or [])[:10]
                                    if (h.get("address", "") or "").lower() not in
                                    ["0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead"])

                    if is_honeypot == "1": risk_reasons.append("貔貅盘（无法卖出）"); is_dangerous = True
                    if buy_tax > 10 or sell_tax > 10: risk_reasons.append(f"高额交易税(买{buy_tax:.1f}%/卖{sell_tax:.1f}%)"); is_dangerous = True
                    if top10_pct > 80: risk_reasons.append(f"持仓高度集中(前10占{top10_pct:.1f}%)"); is_dangerous = True

                    msg_lines.append(f"🛡️ 税率: 买{buy_tax:.1f}% 卖{sell_tax:.1f}% | 前10持仓(剔除黑洞): {top10_pct:.1f}%")

                    total_locked_pct, lock_text_lines = 0.0, []
                    now_ts = int(time.time())
                    for lp in (info.get("lp_holders") or []):
                        if lp.get("is_locked") == 1:
                            pct = to_float(lp.get("percent")) * 100
                            total_locked_pct += pct
                            end_time_str = ""
                            details = lp.get("locked_detail") or []
                            if details:
                                end_time = details[0].get("end_time")
                                if end_time and str(end_time).isdigit():
                                    end_int = int(end_time)
                                    if end_int > now_ts:
                                        dt = datetime.fromtimestamp(end_int)
                                        end_time_str = f"至 {dt.strftime('%Y-%m-%d')}"
                                    else: end_time_str = "已过期"
                            lock_text_lines.append(f"  ↳ 锁仓 {pct:.1f}% {end_time_str}")

                    if total_locked_pct > 0:
                        msg_lines.append(f"🔒 池子总锁仓: {total_locked_pct:.1f}%")
                        msg_lines.extend(lock_text_lines)
                    else:
                        msg_lines.append(f"🔒 池子锁仓: 0.0% (未检测到锁定)")
            else:
                msg_lines.append("🛡️ 安全数据暂时无法获取。")
        except Exception as e:
            print(f"[query] GoPlus EVM 异常: {e}")
            msg_lines.append("🛡️ 安全数据查询异常。")

    header = "🚨 汪汪汪！！（狂吠）报告老板，发现危险目标！\n" if is_dangerous else "🐕 汪！（摇尾巴）嗅探完毕，目前闻起来没毛病：\n"
    risk_text = "\n⚠️ 危险提示: " + " | ".join(risk_reasons) if risk_reasons else ""
    return f"{header}📍 {chain_name} 链数据如下：\n" + "\n".join(msg_lines) + risk_text + "\n\n⚠️ DYOR！"


def _get_solana_token_info(ca_address):
    dex_url = f"https://api.dexscreener.com/latest/dex/tokens/{ca_address}"
    try:
        res = requests.get(dex_url, timeout=5).json()
        pairs = res.get("pairs", []) or []
    except Exception as e:
        print(f"[query] DexScreener SOL 异常: {e}")
        pairs = []

    if not pairs:
        return "🐕‍🦺 嗅探失败：DexScreener 未收录该 Solana 代币，可能是极早期盘、假地址，或池子尚未建立。"

    def liq_of(p):
        return to_float((p.get("liquidity") or {}).get("usd"))

    best_pair = max(pairs, key=liq_of)
    fdv = to_float(best_pair.get("fdv"))
    total_liq = sum(liq_of(p) for p in pairs)
    total_vol = sum(to_float((p.get("volume") or {}).get("h24")) for p in pairs)

    lines = [
        f"📊 市值: {fmt_money(fdv)} | 全链总流动性: {fmt_money(total_liq)} ({len(pairs)}个池子)",
        f"📈 24h总量: {fmt_money(total_vol)}",
    ]
    is_dangerous = False
    risks = []

    goplus_url = f"https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses={ca_address}"
    try:
        g_res = requests.get(goplus_url, timeout=5).json()
        if g_res.get("code") == 1 and g_res.get("result"):
            info = g_res["result"].get(ca_address, {}) or {}
            if info:
                mint_status = (info.get("mintable") or {}).get("status", "0")
                freeze_status = (info.get("freezable") or {}).get("status", "0")
                close_status = (info.get("closable") or {}).get("status", "0")

                if mint_status == "1": risks.append("Mint 权限未放弃（可无限增发砸盘）"); is_dangerous = True
                if freeze_status == "1": risks.append("Freeze 权限未放弃（可冻结账户，无法卖出）"); is_dangerous = True
                if close_status == "1": risks.append("Close 权限未放弃（可关闭账户转移余额）"); is_dangerous = True

                holders = info.get("holders", []) or []
                top10_pct = sum(to_float(h.get("percent")) * 100 for h in holders[:10])
                if top10_pct > 80: risks.append(f"前10持仓高度集中 ({top10_pct:.1f}%)"); is_dangerous = True

                lines.append(f"🛡️ Mint权限: {'⚠️ 未放弃' if mint_status == '1' else '✅ 已放弃'} | Freeze权限: {'⚠️ 未放弃' if freeze_status == '1' else '✅ 已放弃'}")
                lines.append(f"👥 前10持仓(估算): {top10_pct:.1f}%")
    except Exception as e:
        print(f"[query] GoPlus SOL 异常: {e}")

    if total_liq < 1000: risks.append("流动性极低 (<$1000)"); is_dangerous = True

    header = "🚨 汪汪汪！！（狂吠）Solana 目标存在危险信号！\n" if is_dangerous else "🐕 汪！（摇尾巴）Solana 嗅探完毕，权限已放弃，目前闻起来没毛病：\n"
    risk_text = "\n⚠️ 危险提示: " + " | ".join(risks) if risks else ""
    return f"{header}📍 SOL 链数据如下：\n" + "\n".join(lines) + risk_text + "\n\n⚠️ DYOR！"


def send_message(chat_type, chat_id, content):
    url = "https://open.debox.pro/openapi/bot/sendMessage"
    headers = {"Content-Type": "application/json", "X-API-KEY": APP_KEY}
    payload = {"chat_type": chat_type, "chat_id": chat_id, "content": content, "parse_mode": "text"}
    try:
        requests.post(url, json=payload, headers=headers, timeout=10)
    except Exception as e:
        print(f"==== 发送消息异常: {e} ====")


def extract_valid_ca(message):
    """
    从消息中提取第一个通过校验的 CA 地址。
    先剔除 URL，避免 URL 路径里的长字符串污染 Base58 匹配。
    """
    cleaned = URL_PATTERN.sub(" ", message)
    for m in CA_PATTERN.finditer(cleaned):
        addr = m.group()
        if addr.startswith("0x") or is_valid_solana_address(addr):
            return addr
    return None


# ================= Webhook 路由 =================
@app.route("/webhook", methods=["POST"])
def webhook():
    if not hmac.compare_digest(request.headers.get("X-API-KEY", ""), WEBHOOK_KEY):
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    message = data.get("message_raw", data.get("message", ""))
    group_id = data.get("group_id")
    from_user_id = data.get("from_user_id")

    if group_id:
        record_realtime_group(group_id)

    if message:
        # 1. 使用教程
        if any(k in message.lower() for k in ["/help", "教程", "菜单", "说明"]):
            help_text = (
                "🐕‍🦺 链上警犬使用说明\n\n"
                "👉 查代币安全：直接在群内发送合约地址（支持 EVM 0x 开头 及 Solana Base58 地址），警犬会立刻帮你排雷。\n"
                "👉 防钓鱼雷达：群内出现不明链接，警犬会自动调用安全接口拦截。\n"
                "👉 深度数据：自动聚合全链池子，EVM 检测貔貅/税率/锁仓，Solana 检测 Mint/Freeze 权限。\n\n"
                "💡 觉得好用？点击下方链接加入产品体验群，获取最新功能！\n"
                "🔗 体验群链接：https://m.debox.pro/group?id=sajshm5d&code=9410sfdk"
            )
            if group_id: send_message("group", group_id, help_text)
            elif from_user_id: send_message("private", from_user_id, help_text)
            return jsonify({"status": "ok"}), 200

        # 2. 管理员查后台
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
                    f"数据更新于 {current_time}"
                )
                send_message("private", from_user_id, admin_msg)
            elif from_user_id:
                send_message("private", from_user_id, "🐕 警告：这是内部管理指令，你没有权限查看哦！")
            return jsonify({"status": "ok"}), 200

        # 3. 精准防钓鱼检测（仅群聊触发）
        match_url = URL_PATTERN.search(message)
        if match_url and group_id:
            url = match_url.group()
            try:
                domain = urlparse(url).netloc.lower()
                allowed_domains = ["twitter.com", "x.com", "debox.pro"]
                is_safe = any(domain == d or domain.endswith("." + d) for d in allowed_domains)

                if not is_safe:
                    try:
                        resp = requests.get(f"https://api.gopluslabs.io/api/v1/phishing_site?url={url}", timeout=3).json()
                        # 兼容 result 直接为 dict 或以 url 为 key 的嵌套 dict，且值可能是 "1"/1
                        result = resp.get("result") or {}
                        phishing_flag = None
                        if isinstance(result, dict):
                            if result.get("phishing_site") is not None:
                                phishing_flag = str(result.get("phishing_site"))
                            else:
                                for v in result.values():
                                    if isinstance(v, dict) and v.get("phishing_site") is not None:
                                        phishing_flag = str(v.get("phishing_site"))
                                        break
                        if resp.get("code") == 1 and phishing_flag == "1":
                            reply_text = f"🚨 高危警告：GoPlus 安全库检测到该链接为钓鱼网站！\n🌐 域名: {domain}\n请勿点击，谨防钱包被盗！"
                        else:
                            reply_text = f"🐕 安全提示：该链接未在官方白名单内，请群友警惕：\n🌐 域名: {domain}"
                    except Exception:
                        reply_text = f"🐕 警告：嗅探到不明链接！\n\n🌐 域名: {domain}\n警犬闻到了钓鱼的风险，请群友警惕，不要随意点击！"

                    send_message("group", group_id, reply_text)
                    return jsonify({"status": "ok"}), 200
            except Exception as e:
                print(f"[phishing] 异常: {e}")

        # 4. 查 CA 风险（先剔除 URL 再匹配，避免污染）
        ca_address = extract_valid_ca(message)
        if ca_address:
            # 简易去重：同 CA 60 秒内只查一次
            now_ts = time.time()
            cached = _QUERY_CACHE.get(ca_address)
            if cached and now_ts - cached < QUERY_CACHE_TTL:
                return jsonify({"status": "ok", "cached": True}), 200
            _QUERY_CACHE[ca_address] = now_ts

            # 异步处理：先立即返回 200，避免 DeBox 回调超时导致重试/重复播报
            def _query_task():
                reply_text = get_token_full_info(ca_address)
                if group_id: send_message("group", group_id, reply_text)
                elif from_user_id: send_message("private", from_user_id, reply_text)
                record_user_activity(from_user_id)

            threading.Thread(target=_query_task, daemon=True).start()
            return jsonify({"status": "ok"}), 200

    return jsonify({"status": "ok"}), 200


handler = app
