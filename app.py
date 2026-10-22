import os
import re
import requests
from urllib.parse import urlparse
from flask import Flask, request, jsonify

app = Flask(__name__)

APP_KEY = os.getenv("DEBOX_APP_KEY", "")
WEBHOOK_KEY = os.getenv("DEBOX_WEBHOOK_KEY", "")

CA_PATTERN = re.compile(r"0x[a-fA-F0-9]{40}")
URL_PATTERN = re.compile(r"https?://[^\s]+")

CHAIN_MAPPING = {
    "ethereum": "1", "bsc": "56", "polygon": "137",
    "arbitrum": "42161", "base": "8453", "avalanche": "43114", "optimism": "10"
}

def fmt_money(n):
    try:
        n = float(n)
        if n >= 1000000: return f"${n/1000000:.2f}M"
        if n >= 1000: return f"${n/1000:.2f}K"
        return f"${n:.2f}"
    except:
        return "$0"

# ================= 核心逻辑 =================
def get_token_full_info(ca_address):
    dex_url = f"https://api.dexscreener.com/latest/dex/tokens/{ca_address}"
    
    try:
        res = requests.get(dex_url, timeout=5).json()
        pairs = res.get("pairs", [])
    except Exception:
        return "🐕‍🦺 嗅探失败：网络异常，请稍后重试。", None

    if not pairs:
        return "🐕‍🦺 嗅探失败：没查到行情，可能是极早期盘或假地址。", None

    # 核心：聚合所有链的池子总和与交易量总和
    total_liq = sum(float(p.get("liquidity", {}).get("usd", 0) if p.get("liquidity") else 0) for p in pairs)
    total_vol = sum(float(p.get("volume", {}).get("h24", 0) if p.get("volume") else 0) for p in pairs)

    # 寻找主池子以确定链、DEX、基准价格
    best_pair = max(pairs, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) if x.get("liquidity") else 0))
    dex_chain = best_pair.get("chainId", "bsc")
    dex_id = best_pair.get("dexId", "").lower()
    base_symbol = best_pair.get("baseToken", {}).get("symbol", "TOKEN")
    chain_id = CHAIN_MAPPING.get(dex_chain)
    
    price = float(best_pair.get("priceUsd", "0"))
    fdv = float(best_pair.get("fdv", "0"))

    is_dangerous = False
    risk_reasons = []
    
    # 默认的安全展示文本
    safety_line_1 = "✅ 安全：非貔貅 | 税率 买0.0% 卖0.0%"
    safety_line_2 = "🔒 池子锁仓: 0.0% | 👥 前10持仓(剔除黑洞): 0.0%"

    # 查安全
    if chain_id:
        goplus_url = f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}?contract_addresses={ca_address}"
        try:
            g_res = requests.get(goplus_url, timeout=5).json()
            if g_res.get("code") == 1 and g_res.get("result"):
                info = g_res["result"].get(ca_address.lower(), {})
                if info:
                    is_honeypot = info.get("is_honeypot", "0")
                    buy_tax = float(info.get("buy_tax") or 0) * 100
                    sell_tax = float(info.get("sell_tax") or 0) * 100
                    
                    holders = info.get("holders", [])
                    top10_pct = sum(float(h.get("percent") or 0) * 100 for h in holders[:10] if h.get("address", "").lower() not in ["0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead"])
                    
                    lp_holders = info.get("lp_holders", [])
                    total_locked_pct = sum(float(lp.get("percent") or 0) * 100 for lp in lp_holders if lp.get("is_locked") == 1)

                    # 危险检测
                    if is_honeypot == "1":
                        risk_reasons.append("貔貅")
                        is_dangerous = True
                    if buy_tax > 10 or sell_tax > 10:
                        risk_reasons.append("高额税")
                        is_dangerous = True
                    if top10_pct > 80:
                        risk_reasons.append("高度控盘")
                        is_dangerous = True
                    if total_liq < 1000:
                        risk_reasons.append("流动性枯竭")
                        is_dangerous = True

                    # 组装你想看到的格式
                    if is_honeypot == "1":
                        safety_line_1 = f"❌ 危险：貔貅盘（无法卖出） | 税率 买{buy_tax:.1f}% 卖{sell_tax:.1f}%"
                    else:
                        safety_line_1 = f"✅ 安全：非貔貅 | 税率 买{buy_tax:.1f}% 卖{sell_tax:.1f}%"
                        
                    safety_line_2 = f"🔒 池子锁仓: {total_locked_pct:.1f}% | 👥 前10持仓(剔除黑洞): {top10_pct:.1f}%"
        except Exception:
            safety_line_1 = "🛡️ 安全数据暂时无法获取"

    # 消息拼接（完全复刻你的格式）
    if is_dangerous:
        header = "🚨 汪汪汪！！（狂吠）报告老板，发现危险目标！\n"
        risk_text = "\n⚠️ 危险提示: " + " | ".join(risk_reasons)
    else:
        header = "🐕 汪！（摇尾巴）嗅探完毕，目前闻起来没毛病：\n"
        risk_text = ""

    final_text = (
        f"{header}"
        f"💰 价格: ${price:.6f}\n"
        f"📊 市值: {fmt_money(fdv)} | 全链总池子: {fmt_money(total_liq)}\n"
        f"📈 24h量: {fmt_money(total_vol)}\n"
        f"{safety_line_1}\n"
        f"{safety_line_2}\n"
        f"{risk_text}\n"
        f"⚠️ DYOR！"
    )

    # 按钮生成
    if "pancake" in dex_id:
        buy_url = f"https://pancakeswap.finance/swap?outputCurrency={ca_address}"
    elif "uniswap" in dex_id:
        buy_url = f"https://app.uniswap.org/#/swap?outputCurrency={ca_address}"
    else:
        buy_url = f"https://dexscreener.com/{dex_chain}/{ca_address}"

    # 这里已经替换为你提供的产品体验群链接
    keyboard = {
        "inline_keyboard": [
            [
                {"text": f"🛒 买入 {base_symbol}", "url": buy_url},
                {"text": "📊 看K线", "url": f"https://dexscreener.com/{dex_chain}/{ca_address}"}
            ],
            [
                {"text": "💬 加入产品体验群", "url": "https://m.debox.pro/group?id=sajshm5d&code=9410sfdk"}
            ]
        ]
    }

    return final_text, keyboard

# 发送消息
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

@app.route("/webhook", methods=["POST"])
def webhook():
    if request.headers.get("X-API-KEY", "") != WEBHOOK_KEY:
        return jsonify({"error": "unauthorized"}), 401
    
    data = request.get_json(silent=True) or {}
    message = data.get("message_raw", data.get("message", ""))
    
    ca_address = data.get("address")
    if not ca_address:
        match = CA_PATTERN.search(message)
        ca_address = match.group() if match else None
        
    group_id = data.get("group_id")
    from_user_id = data.get("from_user_id")
    
    # 处理 CA
    if ca_address:
        reply_text, inline_keyboard = get_token_full_info(ca_address)
        if group_id: 
            send_message("group", group_id, reply_text, inline_keyboard)
        elif from_user_id: 
            send_message("private", from_user_id, reply_text, inline_keyboard)
        return jsonify({"status": "ok"}), 200

    # 防钓鱼
    match_url = URL_PATTERN.search(message)
    if match_url and group_id:
        url = match_url.group()
        try:
            domain = urlparse(url).netloc.lower()
            if not any(domain == d or domain.endswith("." + d) for d in ["twitter.com", "x.com", "debox.pro"]):
                send_message("group", group_id, f"🐕 警告：嗅探到不明链接！\n🌐 域名: `{domain}`\n请群友警惕，谨防钱包被盗！")
        except: pass

    return jsonify({"status": "ok"}), 200

handler = app
