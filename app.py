import os
import re
import time
import hmac
import requests
from concurrent.futures import ThreadPoolExecutor
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

# ===== 简易查询缓存：同 CA 60 秒内不重复打 API =====
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
    return datetime.now(timezone(timedelta(hours=8)))


def fmt_money(n):
    try:
        n = float(n)
        if n >= 1000000: return f"${n/1000000:.2f}M"
        if n >= 1000: return f"${n/1000:.1f}K"
        return f"${n:.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def to_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def fmt_flag(name, value, safe_when="0"):
    """审计开关：✅=安全，❌=危险，➖=数据缺失。value 为 "0"/"1"。"""
    if value is None or str(value) == "":
        return f"{name}➖"
    ok = (str(value) == safe_when)
    return f"{name}{'✅' if ok else '❌'}"


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
def goplus_evm(chain_id, ca_address):
    """查询 GoPlus EVM 安全数据，失败返回 {}"""
    url = f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}?contract_addresses={ca_address}"
    try:
        res = requests.get(url, timeout=5).json()
        if res.get("code") == 1 and res.get("result"):
            return res["result"].get(ca_address.lower(), {}) or {}
    except Exception as e:
        print(f"[query] GoPlus EVM 异常: {e}")
    return {}


# 地址分类缓存：合约/EOA 判定结果复用，避免重复打 API
_ADDR_CACHE = {}


def classify_holder(addr, chain_id):
    """判断持仓地址是否为合约地址（交易所/质押池/金库等通常是合约或平台地址）"""
    addr = (addr or "").lower()
    if not addr.startswith("0x"):
        return {"is_contract": False}
    if addr in _ADDR_CACHE:
        return _ADDR_CACHE[addr]
    try:
        res = requests.get(
            f"https://api.gopluslabs.io/api/v1/address_security/{addr}?chain_id={chain_id}",
            timeout=5).json()
        info = res.get("result", {}) or {}
        # 实测字段名为 contract_address（"1"=合约）；兼容 is_contract 命名
        out = {"is_contract": info.get("contract_address") == "1" or info.get("is_contract") == "1"}
    except Exception as e:
        print(f"[query] address_security 异常: {e}")
        out = {"is_contract": False}  # 查不到时按个人地址处理，不放大风险
    _ADDR_CACHE[addr] = out
    return out


# 主流锚定资产白名单（BSC）：这些币的 mint 权限属于交易所官方锚定机制
# （充值 1:1 铸造），由真实储备背书，不算项目方增发后门
KNOWN_PEGGED_BSC = {
    "0x7130d2a12b9bcbfae4f2634d864a1ee1ce3ead9c".lower(),  # BTCB
    "0x2170ed0880ac9a755fd29b2688956bd959f933f8".lower(),  # ETH
    "0x55d398326f99059ff775485246999027b3197955".lower(),  # USDT
    "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d".lower(),  # USDC
    "0xe9e7cea3dedca5984780bafc599bd69add087d56".lower(),  # BUSD
    "0xc5f0f7b66764f6ec8c0dffba8a602c790a649b89".lower(),  # FDUSD
    "0x1af3f329e8be154074d8769d06ffa9a48c7a8d55".lower(),  # DAI
    "0xa2e3356610840701bdf5611a539f8356ca64c141".lower(),  # WBETH
}


def build_evm_lines(info, chain_id=None, ca_address=None):
    """根据 GoPlus 数据生成元信息条目、异常审计项、风险标签列表"""
    risks = []
    buy_tax = to_float(info.get("buy_tax")) * 100
    sell_tax = to_float(info.get("sell_tax")) * 100
    holders = [(h.get("address", "") or "", to_float(h.get("percent")) * 100)
               for h in (info.get("holders") or [])[:10]
               if (h.get("address", "") or "").lower() not in
               ["0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead"]]
    top10_pct = sum(pct for _, pct in holders)

    # 细化：前10里拆出合约地址（质押池/金库/交易所等）与个人地址（并行查询不拖慢回复）
    contract_pct = 0.0
    if holders and chain_id:
        with ThreadPoolExecutor(max_workers=10) as ex:
            results = list(ex.map(lambda ap: classify_holder(ap[0], chain_id), holders))
        contract_pct = sum(pct for (addr, pct), r in zip(holders, results) if r["is_contract"])
        eoa_pct = top10_pct - contract_pct
    else:
        eoa_pct = top10_pct

    if contract_pct > 0:
        holders_str = f"{top10_pct:.1f}%（个人 {eoa_pct:.1f}% / 合约 {contract_pct:.1f}%）"
    else:
        holders_str = f"{top10_pct:.1f}%"

    meta_items = [
        ("🩸 税率", f"{buy_tax:.1f}% / {sell_tax:.1f}%"),
        ("👥 前10", holders_str),
    ]

    # 审计分级：致命❌ / 可控⚠️ / 未知➖；全绿时不显示审计段落
    # 致命项：存在即可被利用造成直接损失
    fatal_defs = [
        ("开源", info.get("is_open_source"), "1", "未开源"),
        ("改余额", info.get("owner_change_balance"), "0", "可改余额"),
        ("藏Owner", info.get("hidden_owner"), "0", "隐藏Owner"),
        ("收回权", info.get("can_take_back_ownership"), "0", "可收回权限"),
        ("增发", info.get("is_mintable"), "0", "增发"),
        ("黑名单", info.get("is_blacklisted"), "0", "黑名单"),
    ]
    # 可控项：权限存在但不必然后门，仅提示留意
    # 与致命项逻辑不同：数据缺失不算"未知"（缺失≠风险，GoPlus 覆盖不全很常见），
    # 只有明确查到 "1" 才提示，避免常驻噪音
    # （致命项必须保留未知栏：如"开源"没查到，无法确认安全，缺失本身就是风险信号）
    warn_defs = [
        ("暂停", info.get("transfer_pausable"), "暂停交易"),
        ("白名单", info.get("is_whitelisted"), "白名单限制"),
        ("代理", info.get("is_proxy"), "代理合约"),
    ]

    fatal_items, warn_items, unknown_items = [], [], []
    for name, val, safe_when, risk_label in fatal_defs:
        # 主流锚定币的增发是官方锚定机制（充值 1:1 铸造），不算后门，直接跳过
        if name == "增发" and ca_address and ca_address.lower() in KNOWN_PEGGED_BSC:
            continue
        if val is None or str(val) == "":
            unknown_items.append(name)
        elif str(val) != safe_when:
            fatal_items.append(name)
            risks.append(risk_label)
    for name, val, warn_label in warn_defs:
        if str(val) == "1":
            warn_items.append(warn_label)

    # 自毁：同可控项逻辑，仅明确查到 "1" 时提示，缺失不进未知栏
    if str(info.get("self_destruct") or "") == "1":
        warn_items.append("可自毁")

    if info.get("is_honeypot") == "1": risks.append("貔貅")
    if buy_tax > 10 or sell_tax > 10: risks.append("高税")
    # 死控盘只看个人地址（EOA）持仓：合约持仓多为质押池/金库/交易所，不直接算砸盘风险
    if eoa_pct > 80: risks.append("死控盘")

    return meta_items, (fatal_items, warn_items, unknown_items), risks


def risk_level(risks, warn_items, unknown_items):
    """三级风险定级：red=极高危 / orange=需留意 / green=安全"""
    if risks:
        return "red"
    if warn_items or unknown_items:
        return "orange"
    return "green"


# 最终版式：标题无装饰杠杠，等级靠 🐕/⚠️/🚨/❓ 区分
LEVEL_STYLE = {
    "red":     {"title": "🚨 链上警犬 · 极度危险"},
    "orange":  {"title": "⚠️ 链上警犬 · 需留意"},
    "green":   {"title": "🐕 链上警犬 · 安全报告"},
    "unknown": {"title": "❓ 链上警犬 · 数据不足"},
}


def assemble_report(meta_items, audit, risks, low_liq=False, dex_url=None, no_data=False):
    """海报式卡片：分级标题 + 图标键值对 + 审计分级段落；no_data=True 表示安全数据完全查不到"""
    fatal_items, warn_items, unknown_items = audit
    if low_liq: risks = risks + ["流动性极低"]

    if no_data:
        # 安全数据完全查不到（GoPlus 无此合约），不能装作"扫描通过"误导用户
        lines = [LEVEL_STYLE["unknown"]["title"]]
        for label, value in meta_items:
            lines.append(f"{label}　{value}")
        lines.append("▱▱▱▱▱▱▱▱▱▱")
        lines.append("❓ 数据不足，无法判定（可能是未上线的新币，请谨慎）")
        return "\n".join(lines)

    level = risk_level(risks, warn_items, unknown_items)
    style = LEVEL_STYLE[level]

    if risks:
        conclusion = f"🚨 极度危险（{'、'.join(risks)}），千万别碰！"
    else:
        conclusion = "✅ 扫描通过，自行判断"

    lines = [style["title"]]
    for label, value in meta_items:
        lines.append(f"{label}　{value}")
    lines.append("▱▱▱▱▱▱▱▱▱▱")

    # 审计按优先级显示：有致命项时只显示致命项（其他项再好，随时可被后门利用，无参考价值）
    if fatal_items:
        lines.append(f"❌ 致命　{' | '.join(fatal_items)}")
    else:
        if warn_items:
            lines.append(f"⚠️ 可控　{' | '.join(warn_items)}")
        if unknown_items:
            # 未知项太多时只报数量（新币数据索引中，全列出来是一大坨）
            if len(unknown_items) > 4:
                lines.append(f"➖ 未知　{len(unknown_items)} 项（数据索引中）")
            else:
                lines.append(f"➖ 未知　{' | '.join(unknown_items)}")

    lines.append(conclusion)
    return "\n".join(lines)


def get_token_full_info(ca_address):
    """返回 (报告文本, reply_markup 或 None)"""
    if not ca_address.startswith("0x"):
        return _get_solana_token_info(ca_address)

    dex_url = f"https://api.dexscreener.com/latest/dex/tokens/{ca_address}"
    pairs = []
    try:
        res = requests.get(dex_url, timeout=5).json()
        pairs = res.get("pairs", []) or []
    except Exception as e:
        print(f"[query] DexScreener 异常: {e}")

    def liq_of(p):
        return to_float((p.get("liquidity") or {}).get("usd"))

    # 只统计本代币作为基础代币(baseToken)的池子：
    # 排除"别的代币拿它当计价货币"的池子，那些流动性不属于本代币
    own_pairs = [p for p in pairs
                 if (p.get("baseToken") or {}).get("address", "").lower() == ca_address.lower()]
    if own_pairs:
        pairs = own_pairs

    low_liq = False
    if pairs:
        best_pair = max(pairs, key=liq_of)
        dex_chain = best_pair.get("chainId", "bsc")
        chain_id = CHAIN_MAPPING.get(dex_chain)
        chain_name = CHAIN_NAMES.get(chain_id, dex_chain.upper())
        symbol = (best_pair.get("baseToken") or {}).get("symbol", "未知")
        total_liq = sum(liq_of(p) for p in pairs)
        if total_liq < 1000: low_liq = True
        # 详情页直达链接（DexScreener 交易对页）
        dex_page = best_pair.get("url") or dex_url
    else:
        # 无行情（极早期）：遍历支持的链尝试拿到安全数据
        chain_id, chain_name, symbol = None, "", "未知"
        for cid in set(CHAIN_MAPPING.values()):
            info_probe = goplus_evm(cid, ca_address)
            if info_probe:
                chain_id = cid
                chain_name = CHAIN_NAMES.get(cid, cid)
                break

    meta_items = [("🪙 代币", f"{symbol} · {chain_name or '未知链'}")]
    if pairs:
        meta_items.insert(1, ("💰 池子", fmt_money(total_liq)))
    else:
        meta_items.insert(1, ("💰 池子", "$0.00"))  # 无池子也显示，用户要求

    if not chain_id:
        return assemble_report(meta_items, ([], [], []), [], no_data=True), None

    info = goplus_evm(chain_id, ca_address)
    if not info:
        return assemble_report(meta_items, ([], [], []), [], no_data=True), None

    meta, audit, risks = build_evm_lines(info, chain_id, ca_address)
    meta_items.extend(meta)
    return assemble_report(meta_items, audit, risks, low_liq, dex_page if pairs else None), None


def _get_solana_token_info(ca_address):
    """返回 (报告文本, reply_markup 或 None)"""
    dex_url = f"https://api.dexscreener.com/latest/dex/tokens/{ca_address}"
    pairs = []
    try:
        res = requests.get(dex_url, timeout=5).json()
        pairs = res.get("pairs", []) or []
    except Exception as e:
        print(f"[query] DexScreener SOL 异常: {e}")

    def liq_of(p):
        return to_float((p.get("liquidity") or {}).get("usd"))

    # 同 EVM：只统计本代币作为基础代币的池子
    own_pairs = [p for p in pairs
                 if (p.get("baseToken") or {}).get("address", "").lower() == ca_address.lower()]
    if own_pairs:
        pairs = own_pairs

    risks = []
    low_liq = False
    meta_items = [("🪙 代币", "SOL")]
    meta_items.append(("💰 池子", "$0.00"))  # 默认 $0.00，查到池子后覆盖
    dex_page = None
    if pairs:
        best_pair = max(pairs, key=liq_of)
        symbol = (best_pair.get("baseToken") or {}).get("symbol", "SOL")
        total_liq = sum(liq_of(p) for p in pairs)
        if total_liq < 1000: low_liq = True  # 流动性极低由 assemble_report 统一追加，避免重复
        meta_items[0] = ("🪙 代币", f"{symbol} · SOL")
        meta_items[1] = ("💰 池子", fmt_money(total_liq))
        dex_page = best_pair.get("url") or dex_url

    fatal_items, warn_items, unknown_items = [], [], []
    goplus_url = f"https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses={ca_address}"
    try:
        g_res = requests.get(goplus_url, timeout=5).json()
        if g_res.get("code") == 1 and g_res.get("result"):
            info = g_res["result"].get(ca_address, {}) or {}
            if info:
                mint_status = (info.get("mintable") or {}).get("status", "")
                freeze_status = (info.get("freezable") or {}).get("status", "")
                close_status = (info.get("closable") or {}).get("status", "")
                holders = info.get("holders", []) or []
                top10_pct = sum(to_float(h.get("percent")) * 100 for h in holders[:10])

                meta_items.append(("👥 前10", f"{top10_pct:.1f}%"))

                # 分级：Mint/Freeze 可增发/冻结他人资产=致命；Close 仅提示
                if mint_status == "1": fatal_items.append("Mint未弃"); risks.append("Mint未放弃")
                elif mint_status == "": unknown_items.append("Mint")
                if freeze_status == "1": fatal_items.append("Freeze未弃"); risks.append("Freeze未放弃")
                elif freeze_status == "": unknown_items.append("Freeze")
                if close_status == "1": warn_items.append("Close未弃")
                if top10_pct > 80: risks.append("死控盘")
    except Exception as e:
        print(f"[query] GoPlus SOL 异常: {e}")

    return assemble_report(meta_items, (fatal_items, warn_items, unknown_items), risks, low_liq, dex_page), None


def send_message(chat_type, chat_id, content, reply_markup=None):
    url = "https://open.debox.pro/openapi/bot/sendMessage"
    headers = {"Content-Type": "application/json", "X-API-KEY": APP_KEY}
    payload = {"chat_type": chat_type, "chat_id": chat_id, "content": content, "parse_mode": "text"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(url, json=payload, headers=headers, timeout=10)
    except Exception as e:
        print(f"==== 发送消息异常: {e} ====")


def extract_valid_ca(message):
    """剔除 URL 后提取第一个通过校验的 CA 地址"""
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
            now_ts = time.time()
            cached = _QUERY_CACHE.get(ca_address)
            if cached and now_ts - cached < QUERY_CACHE_TTL:
                return jsonify({"status": "ok", "cached": True}), 200
            _QUERY_CACHE[ca_address] = now_ts

            try:
                reply_text, markup = get_token_full_info(ca_address)
                if group_id: send_message("group", group_id, reply_text, markup)
                elif from_user_id: send_message("private", from_user_id, reply_text, markup)
                record_user_activity(from_user_id)
            except Exception as e:
                print(f"[query] 查币任务异常: {e}")
            return jsonify({"status": "ok"}), 200

    return jsonify({"status": "ok"}), 200


handler = app
