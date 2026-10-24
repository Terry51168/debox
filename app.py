"""
链上警犬 · DeBox 群机器人 (Vercel 完美适配版)
"""
import os
import re
import time
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests
from flask import Flask, request, jsonify

# =========================================================
# 日志 / 基础配置
# =========================================================
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("chaindog")

app = Flask(__name__)

APP_KEY = os.getenv("DEBOX_APP_KEY", "")
WEBHOOK_KEY = os.getenv("DEBOX_WEBHOOK_KEY", "")

UPSTASH_URL = os.getenv("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")

ADMIN_USER_ID = "9410sfdk"

LOW_LIQ_USD = 1000.0
HIGH_TAX_PCT = 10.0
HIGH_TOP10_PCT = 80.0

T_DEX = 5
T_SAFE = 5
T_STATS = 3
T_SEND = 10

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "ChainDog/1.0 (+debox-bot)"})

# =========================================================
# 正则 / 常量
# =========================================================
CA_PATTERN = re.compile(
    r"(?<![0-9A-Za-z])(0x[a-fA-F0-9]{40})(?![0-9A-Za-z])"
    r"|(?<![1-9A-HJ-NP-Za-km-z])([1-9A-HJ-NP-Za-km-z]{32,44})(?![1-9A-HJ-NP-Za-km-z])"
)
URL_PATTERN = re.compile(r"https?://[^\s]+")

CHAIN_MAPPING = {
    "ethereum": "1", "bsc": "56", "polygon": "137",
    "arbitrum": "42161", "base": "8453", "avalanche": "43114",
    "optimism": "10",
}
CHAIN_NAMES = {
    "1": "ETH", "56": "BSC", "137": "POL",
    "42161": "ARB", "8453": "BASE", "43114": "AVAX", "10": "OP",
}

BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
BASE58_INDEX = {c: i for i, c in enumerate(BASE58_ALPHABET)}
SAFE_DOMAINS = {"twitter.com", "x.com", "debox.pro"}

HELP_TEXT = (
    "🐕‍🦺 链上警犬使用说明\n\n"
    "👉 查代币安全：直接在群内发送合约地址（支持 EVM 0x 开头 及 Solana Base58 地址），警犬会立刻帮你排雷。\n"
    "👉 防钓鱼雷达：群内出现不明链接，警犬会自动拦截并发出警告。\n"
    "👉 深度数据：自动聚合全链池子，EVM 检测貔貅/税率/锁仓，Solana 检测 Mint/Freeze 权限。\n\n"
    "💡 觉得好用？点击下方链接加入产品体验群，获取最新功能！\n"
    "🔗 体验群链接：https://m.debox.pro/group?id=sajshm5d&code=9410sfdk"
)

# =========================================================
# 通用工具
# =========================================================
def beijing_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=8)

def _to_float(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default

def fmt_money(n) -> str:
    n = _to_float(n)
    if n >= 1_000_000: return f"${n / 1_000_000:.2f}M"
    if n >= 1_000: return f"${n / 1_000:.2f}K"
    return f"${n:.2f}"

def safe_get(url: str, timeout: float = 5.0, **kwargs):
    try:
        resp = HTTP.get(url, timeout=timeout, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.warning("GET %s failed: %s", url, e)
        return None

def is_valid_solana_address(addr: str) -> bool:
    if not isinstance(addr, str) or not (32 <= len(addr) <= 44): return False
    num = 0
    for ch in addr:
        idx = BASE58_INDEX.get(ch)
        if idx is None: return False
        num = num * 58 + idx
    leading_zeros = len(addr) - len(addr.lstrip("1"))
    if num == 0: byte_len = leading_zeros
    else: byte_len = leading_zeros + (num.bit_length() + 7) // 8
    return byte_len == 32

# =========================================================
# Upstash Redis REST
# =========================================================
def _upstash_ready() -> bool:
    return bool(UPSTASH_URL and UPSTASH_TOKEN)

def _upstash_pipeline(commands: list) -> list:
    if not _upstash_ready() or not commands: return []
    try:
        resp = HTTP.post(
            f"{UPSTASH_URL}/pipeline",
            json=commands,
            headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"},
            timeout=T_STATS,
        )
        resp.raise_for_status()
        return resp.json() or []
    except Exception as e:
        log.warning("Upstash pipeline failed: %s", e)
        return []

def record_realtime_group(group_id):
    if not _upstash_ready() or not group_id: return
    _upstash_pipeline([["SADD", "all_groups", str(group_id)]])

def record_user_activity(user_id):
    if not _upstash_ready() or not user_id: return
    uid = str(user_id)
    now = beijing_now()
    today = now.strftime("%Y%m%d")
    week = (now - timedelta(days=now.weekday())).strftime("%Y%m%d")
    month = now.strftime("%Y%m")
    _upstash_pipeline([
        ["SADD", "all_users", uid],
        ["INCR", "total_queries"],
        ["SADD", f"active_daily_{today}", uid],
        ["SADD", f"active_weekly_{week}", uid],
        ["SADD", f"active_monthly_{month}", uid],
    ])

def _growth(current: int, previous: int) -> str:
    if previous == 0: return "🟢 +100%" if current > 0 else "0%"
    g = (current - previous) / previous * 100
    icon = "🟢 +" if g >= 0 else "🔴 "
    return f"{icon}{g:.1f}%"

def get_usage_stats() -> dict:
    now = beijing_now()
    today = now.strftime("%Y%m%d")
    yest = (now - timedelta(days=1)).strftime("%Y%m%d")
    this_week = (now - timedelta(days=now.weekday())).strftime("%Y%m%d")
    last_week = (now - timedelta(days=now.weekday() + 7)).strftime("%Y%m%d")
    this_month = now.strftime("%Y%m")
    last_month = (now.replace(day=1) - timedelta(days=1)).strftime("%Y%m")

    keys = [
        "all_groups", "all_users", "total_queries",
        f"active_daily_{today}", f"active_daily_{yest}",
        f"active_weekly_{this_week}", f"active_weekly_{last_week}",
        f"active_monthly_{this_month}", f"active_monthly_{last_month}",
    ]
    commands = [["GET", k] if k == "total_queries" else ["SCARD", k] for k in keys]
    results = _upstash_pipeline(commands)

    def val(i: int) -> int:
        if i >= len(results): return 0
        try: return int(results[i].get("result") or 0)
        except Exception: return 0

    stats = {"total_groups": val(0), "total_users": val(1), "total_queries": val(2)}
    d_now, d_prev = val(3), val(4)
    w_now, w_prev = val(5), val(6)
    m_now, m_prev = val(7), val(8)

    stats["daily_str"] = f"{d_now} 人 (较昨日 {_growth(d_now, d_prev)})"
    stats["weekly_str"] = f"{w_now} 人 (较上周 {_growth(w_now, w_prev)})"
    stats["monthly_str"] = f"{m_now} 人 (较上月 {_growth(m_now, m_prev)})"
    return stats

# =========================================================
# 行情与安全检测
# =========================================================
def fetch_dex_pairs(ca: str) -> list:
    data = safe_get(f"https://api.dexscreener.com/latest/dex/tokens/{ca}", timeout=T_DEX)
    return data.get("pairs") if data else []

def summarize_pairs(pairs: list) -> dict:
    best = max(pairs, key=lambda x: _to_float((x.get("liquidity") or {}).get("usd")))
    total_liq = sum(_to_float((p.get("liquidity") or {}).get("usd")) for p in pairs)
    total_vol = sum(_to_float((p.get("volume") or {}).get("h24")) for p in pairs)
    fdv = _to_float(best.get("fdv"))
    return {"best": best, "pair_count": len(pairs), "total_liq": total_liq, "total_vol": total_vol, "fdv": fdv}

def _fmt_market(summary: dict) -> str:
    return (
        f"📊 市值: {fmt_money(summary['fdv'])} | "
        f"全链总流动性: {fmt_money(summary['total_liq'])} ({summary['pair_count']}个池子)\n"
        f"📈 24h总量: {fmt_money(summary['total_vol'])}"
    )

def _evm_security(ca: str, chain_id: str):
    lines, risks = [], []
    g = safe_get(f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}?contract_addresses={ca}", timeout=T_SAFE)
    if not g or g.get("code") != 1:
        lines.append("🛡️ 安全数据暂时无法获取。")
        return lines, risks

    info = (g.get("result") or {}).get(ca.lower())
    if not info:
        lines.append("🛡️ 暂未获取到该合约的安全数据。")
        return lines, risks

    if info.get("is_honeypot") == "1": risks.append("貔貅盘（无法卖出）")
    buy_tax = _to_float(info.get("buy_tax")) * 100
    sell_tax = _to_float(info.get("sell_tax")) * 100
    if buy_tax > HIGH_TAX_PCT or sell_tax > HIGH_TAX_PCT: risks.append(f"高额交易税(买{buy_tax:.1f}%/卖{sell_tax:.1f}%)")

    dead = {"0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead"}
    holders = info.get("holders") or []
    top10 = sum(_to_float(h.get("percent")) * 100 for h in holders[:10] if (h.get("address") or "").lower() not in dead)
    if top10 > HIGH_TOP10_PCT: risks.append(f"持仓高度集中(前10占{top10:.1f}%)")

    lines.append(f"🛡️ 税率: 买{buy_tax:.1f}% 卖{sell_tax:.1f}% | 前10持仓(剔除黑洞): {top10:.1f}%")

    now_ts = int(time.time())
    total_locked = 0.0
    lock_lines = []
    for lp in info.get("lp_holders") or []:
        if lp.get("is_locked") != 1: continue
        pct = _to_float(lp.get("percent")) * 100
        total_locked += pct
        end_str = ""
        details = lp.get("locked_detail") or []
        if details:
            end = details[0].get("end_time")
            if end and str(end).isdigit():
                end_int = int(end)
                if end_int > now_ts:
                    dt = datetime.fromtimestamp(end_int, tz=timezone.utc) + timedelta(hours=8)
                    end_str = f"至 {dt.strftime('%Y-%m-%d')}"
                else: end_str = "已过期"
        lock_lines.append(f"  ↳ 锁仓 {pct:.1f}% {end_str}".rstrip())

    if total_locked > 0:
        lines.append(f"🔒 池子总锁仓: {total_locked:.1f}%")
        lines.extend(lock_lines[:5])
    else:
        lines.append("🔒 池子锁仓: 0.0% (未检测到锁定)")
    return lines, risks

def _solana_security(ca: str):
    lines, risks = [], []
    g = safe_get(f"https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses={ca}", timeout=T_SAFE)
    if not g or g.get("code") != 1:
        lines.append("🛡️ GoPlus Solana 安全数据暂时不可用。")
        return lines, risks

    info = (g.get("result") or {}).get(ca)
    if not info:
        lines.append("🛡️ GoPlus 未返回该代币的安全数据。")
        return lines, risks

    mint = (info.get("mintable") or {}).get("status", "0")
    freeze = (info.get("freezable") or {}).get("status", "0")
    close = (info.get("closable") or {}).get("status", "0")

    if mint == "1": risks.append("Mint 权限未放弃（可无限增发砸盘）")
    if freeze == "1": risks.append("Freeze 权限未放弃（可冻结账户，无法卖出）")
    if close == "1": risks.append("Close 权限未放弃（可关闭账户转移余额）")

    holders = info.get("holders") or []
    top10 = sum(_to_float(h.get("percent")) * 100 for h in holders[:10])
    if top10 > HIGH_TOP10_PCT: risks.append(f"前10持仓高度集中 ({top10:.1f}%)")

    lines.append(f"🛡️ Mint权限: {'⚠️ 未放弃' if mint == '1' else '✅ 已放弃'} | Freeze权限: {'⚠️ 未放弃' if freeze == '1' else '✅ 已放弃'}")
    lines.append(f"👥 前10持仓(估算): {top10:.1f}%")
    if holders: lines.append(f"👨‍👩‍👧 收录持有人数: {len(holders)}")
    return lines, risks

def _compose_reply(chain_name: str, lines: list, risks: list, kind: str) -> str:
    if risks:
        head = "🚨 汪汪汪！！（狂吠）Solana 目标存在危险信号！" if kind == "sol" else "🚨 汪汪汪！！（狂吠）报告老板，发现危险目标！"
        uniq = list(dict.fromkeys(risks))
        risk_text = "\n⚠️ 危险提示: " + " | ".join(uniq)
    else:
        head = "🐕 汪！（摇尾巴）Solana 嗅探完毕，权限已放弃，目前闻起来没毛病：" if kind == "sol" else "🐕 汪！（摇尾巴）嗅探完毕，目前闻起来没毛病："
        risk_text = ""
    return f"{head}\n📍 {chain_name} 链数据如下：\n" + "\n".join(lines) + risk_text + "\n\n⚠️ DYOR！"

def get_token_full_info(ca: str) -> str:
    if not ca.startswith("0x"): return _check_solana(ca)
    return _check_evm(ca)

def _check_evm(ca: str) -> str:
    pairs = fetch_dex_pairs(ca)
    if not pairs: return "🐕‍🦺 嗅探失败：没查到行情数据，可能是极早期盘、假地址，或不在已收录的链上。"
    summary = summarize_pairs(pairs)
    dex_chain = summary["best"].get("chainId", "bsc")
    chain_id = CHAIN_MAPPING.get(dex_chain)
    chain_name = CHAIN_NAMES.get(chain_id, str(dex_chain).upper())

    lines = [_fmt_market(summary)]
    risks = []
    if summary["total_liq"] < LOW_LIQ_USD: risks.append(f"全链总流动性极低 (<${int(LOW_LIQ_USD)})")

    if not chain_id:
        lines.append(f"🛡️ 暂不支持该链({chain_name})的深度安全检测。")
    else:
        sec_lines, sec_risks = _evm_security(ca, chain_id)
        lines.extend(sec_lines)
        risks.extend(sec_risks)
    return _compose_reply(chain_name, lines, risks, kind="evm")

def _check_solana(ca: str) -> str:
    pairs = fetch_dex_pairs(ca)
    if not pairs: return "🐕‍🦺 嗅探失败：DexScreener 未收录该 Solana 代币。"
    summary = summarize_pairs(pairs)
    lines = [_fmt_market(summary)]
    risks = []
    sec_lines, sec_risks = _solana_security(ca)
    lines.extend(sec_lines)
    risks.extend(sec_risks)
    if summary["total_liq"] < LOW_LIQ_USD: risks.append(f"流动性极低 (<${int(LOW_LIQ_USD)})")
    return _compose_reply("SOL", lines, risks, kind="sol")

# =========================================================
# 发送与处理
# =========================================================
def send_message(chat_type: str, chat_id, content: str) -> None:
    if not APP_KEY:
        log.error("DEBOX_APP_KEY 未配置")
        return
    payload = {"chat_type": chat_type, "chat_id": chat_id, "content": content, "parse_mode": "text"}
    try:
        r = HTTP.post("https://open.debox.pro/openapi/bot/sendMessage", json=payload, headers={"Content-Type": "application/json", "X-API-KEY": APP_KEY}, timeout=T_SEND)
        if r.status_code >= 400: log.warning("sendMessage HTTP %s: %s", r.status_code, r.text[:200])
    except Exception as e: log.error("sendMessage 异常: %s", e)

def _respond(text: str, group_id, from_user_id) -> None:
    if group_id: send_message("group", group_id, text)
    elif from_user_id: send_message("private", from_user_id, text)

def process_message(message: str, group_id, from_user_id) -> None:
    lower = message.lower()
    if any(k in lower for k in ("/help", "教程", "菜单", "说明")):
        _respond(HELP_TEXT, group_id, from_user_id)
        return

    if "/admin" in lower or "/stats" in lower:
        uid = str(from_user_id or "")
        if uid == ADMIN_USER_ID or uid == f"@{ADMIN_USER_ID}":
            stats = get_usage_stats()
            admin_msg = (
                "📊 链上警犬 · 实时运营看板\n\n"
                f"👥 服务群组 (实时): {stats['total_groups']} 个\n"
                f"🔍 累计查币次数: {stats['total_queries']:,} 次\n"
                f"🧑‍💻 累计独立用户: {stats['total_users']:,} 人\n\n"
                "📈 活跃用户环比 (日/周/月):\n"
                f"🔸 今日: {stats['daily_str']}\n"
                f"🔸 本周: {stats['weekly_str']}\n"
                f"🔸 本月: {stats['monthly_str']}\n\n"
                f"*数据更新于 {beijing_now().strftime('%H:%M')}*"
            )
            if from_user_id: send_message("private", from_user_id, admin_msg)
        elif from_user_id: send_message("private", from_user_id, "🐕 警告：这是内部管理指令，你没有权限查看哦！")
        return

    ca_match = CA_PATTERN.search(message)
    if ca_match:
        ca = ca_match.group()
        valid = ca.startswith("0x") or is_valid_solana_address(ca)
        if valid:
            try:
                reply = get_token_full_info(ca)
            except Exception:
                log.exception("get_token_full_info failed")
                reply = "🐕‍🦺 嗅探过程中遇到点麻烦，请稍后再试。"
            _respond(reply, group_id, from_user_id)
            record_user_activity(from_user_id)
            return

    if group_id:
        bad_domains = []
        for m in URL_PATTERN.finditer(message):
            host = (urlparse(m.group()).hostname or "").lower()
            if not host: continue
            if any(host == d or host.endswith("." + d) for d in SAFE_DOMAINS): continue
            if host not in bad_domains: bad_domains.append(host)
        if bad_domains:
            domains_text = "\n".join(f"🌐 {d}" for d in bad_domains[:5])
            send_message("group", group_id, f"🐕 警告：嗅探到不明链接！\n\n{domains_text}\n警犬闻到了钓鱼的风险，请群友警惕，不要随意点击授权钱包！")

# =========================================================
# 路由入口
# =========================================================
@app.route("/webhook", methods=["POST"])
def webhook():
    expected = str(WEBHOOK_KEY or "")
    provided = str(request.headers.get("X-API-KEY", "") or "")
    
    if not expected or provided != expected:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    
    # 兼容 DeBox 卡片直接发 address 的情况
    ca_address = data.get("address")
    message = data.get("message_raw") or data.get("message") or ""
    
    group_id = data.get("group_id")
    from_user_id = data.get("from_user_id")

    if group_id:
        record_realtime_group(group_id)

    # 🚀 核心：在 Vercel 上必须同步执行，不然后台线程会被直接掐断
    if ca_address:
        # 卡片直发，跳过正则提取
        try:
            reply = get_token_full_info(ca_address)
        except Exception:
            reply = "🐕‍🦺 嗅探过程中遇到点麻烦，请稍后再试。"
        _respond(reply, group_id, from_user_id)
        record_user_activity(from_user_id)
    elif message:
        process_message(message, group_id, from_user_id)

    return jsonify({"status": "ok"}), 200

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "time": beijing_now().isoformat()}), 200

handler = app
