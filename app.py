import os
import re
import time
import requests
from urllib.parse import urlparse
from flask import Flask, request, jsonify

app = Flask(__name__)

APP_KEY = os.getenv("DEBOX_APP_KEY", "")
WEBHOOK_KEY = os.getenv("DEBOX_WEBHOOK_KEY", "")

CA_PATTERN = re.compile(r"0x[a-fA-F0-9]{40}")
URL_PATTERN = re.compile(r"https?://[^\s]+")

# 常用链的 ID 映射（用于 GoPlus 查安全数据）
CHAIN_MAPPING = {
    "ethereum": "1",
    "bsc": "56",
    "polygon": "137",
    "arbitrum": "42161",
    "base": "8453",
    "avalanche": "43114"
}

def get_token_full_info(ca_address):
    dex_url = f"https://api.dexscreener.com/latest/dex/tokens/{ca_address}"
    
    price_info = ""
    safety_info = ""
    is_dangerous = False
    chain_id = "56" # 默认 BSC，后面会自动识别

    # 1. 查行情与流动性 (找流动性最大的那个池子)
    try:
        res = requests.get(dex_url, timeout=5).json()
        if res.get("pairs") and len(res["pairs"]) > 0:
            # 核心升级：按流动性降序排序，取最大的池子
            best_pair = max(res["pairs"], key=lambda x: float(x.get("liquidity", {}).get("usd", 0) if x.get("liquidity") else 0))
            
            dex_chain = best_pair.get("chainId", "bsc")
            chain_id = CHAIN_MAPPING.get(dex_chain, "56") # 自动识别链
            
            price = float(best_pair.get("priceUsd", "0"))
            fdv = float(best_pair.get("fdv", "0"))
            liq = float(best_pair.get("liquidity", {}).get("usd", "0") if best_pair.get("liquidity") else 0)
            vol = float(best_pair.get("volume", {}).get("h24", "0") if best_pair.get("volume") else 0)
            
            def fmt(n):
                if n >= 1000000: return f"{n/1000000:.2f}M"
                if n >= 1000: return f"{n/1000:.2f}K"
                return f"{n:.2f}"
            
            price_info = f"💰 价格: ${price:.6f}\n"
            price_info += f"📊 市值: ${fmt(fdv)} | 池子: ${fmt(liq)}\n"
            price_info += f"📈 24h量: ${fmt(vol)}\n"
            
            if liq < 1000:
                price_info += f"⚠️ 危险：池子流动性极低 (<$1000)，随时可能撤池！\n"
                is_dangerous = True
        else:
            return "🐕‍🦺 嗅探失败：没查到行情数据，可能是刚开盘的极早期盘或假地址。"
    except Exception as e:
        print(f"查行情失败: {e}")

    # 2. 查安全（貔貅、税率、锁仓、前10持仓）
    goplus_url = f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}?contract_addresses={ca_address}"
    try:
        res = requests.get(goplus_url, timeout=5).json()
        if res.get("code") == 1 and res.get("result"):
            info = res["result"].get(ca_address.lower(), {})
            is_honeypot = info.get("is_honeypot", "0")
            buy_tax = float(info.get("buy_tax", "0")) * 100
            sell_tax = float(info.get("sell_tax", "0")) * 100
            
            # 提取前10持仓，过滤掉黑洞和销毁地址
            holders = info.get("holders", [])
            top10_pct = 0
            if holders:
                for h in holders[:10]:
                    addr = h.get("address", "").lower()
                    # 剔除常见的黑洞/销毁地址
                    if addr not in ["0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead"]:
                        top10_pct += float(h.get("percent", "0")) * 100
            
            # 提取锁仓比例
            lp_lock = 0
            lp_holders = info.get("lp_holders", [])
            if lp_holders:
                lp_lock = float(lp_holders[0].get("percent", "0")) * 100

            # 核心逻辑：危险判定
            if is_honeypot == "1":
                safety_info += "🚨 极度危险：貔貅盘（无法卖出）！\n"
                is_dangerous = True
            elif buy_tax > 10 or sell_tax > 10:
                safety_info += f"⚠️ 高额交易税（买{buy_tax:.1f}% 卖{sell_tax:.1f}%），请谨慎！\n"
                is_dangerous = True
            elif top10_pct > 80:
                safety_info += f"⚠️ 持仓高度集中（前10名占 {top10_pct:.1f}%），庄家随时砸盘！\n"
                is_dangerous = True
            else:
                safety_info += f"✅ 安全：非貔貅 | 税率 买{buy_tax:.1f}% 卖{sell_tax:.1f}%\n"
                
            safety_info += f"🔒 池子锁仓: {lp_lock:.1f}% | 👥 前10持仓(剔除黑洞): {top10_pct:.1f}%\n"
    except Exception as e:
        print(f"查安全失败: {e}")
        safety_info = "🛡️ 安全数据暂时无法获取。\n"

    # 3. 组装最终台词（分级情绪化表达）
    if is_dangerous:
        header = "🚨 汪汪汪！！（狂吠）报告老板，发现危险目标！\n"
    else:
        header = "🐕 汪！（摇尾巴）嗅探完毕，目前闻起来没毛病：\n"
        
    return f"{header}{price_info}{safety_info}⚠️ DYOR！"


def send_message(chat_type, chat_id, content):
    url = "https://open.debox.pro/openapi/bot/sendMessage"
    headers = {"Content-Type": "application/json", "X-API-KEY": APP_KEY}
    payload = {
        "chat_type": chat_type,
        "chat_id": chat_id,
        "content": content,
        "parse_mode": "text",
    }
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
    group_id = data.get("group_id")
    from_user_id = data.get("from_user_id")
    
    if message:
        # 优先查 CA 风险
        match_ca = CA_PATTERN.search(message)
        if match_ca:
            ca_address = match_ca.group()
            reply_text = get_token_full_info(ca_address)
            
            if group_id:
                send_message("group", group_id, reply_text)
            elif from_user_id:
                send_message("private", from_user_id, reply_text)
            return jsonify({"status": "ok"}), 200

        # 升级版防钓鱼：精确比对域名，防止 fake-twitter.com 绕过
        match_url = URL_PATTERN.search(message)
        if match_url and group_id:
            url = match_url.group()
            try:
                # 提取真实的域名 (例如 https://twitte.com.evil.com -> twitte.com.evil.com)
                domain = urlparse(url).netloc.lower()
                # 白名单
                allowed_domains = ["twitter.com", "x.com", "debox.pro"]
                # 检查域名是否完全匹配白名单，或者以 .twitter.com 结尾 (比如 mobile.twitter.com)
                is_safe = any(domain == d or domain.endswith("." + d) for d in allowed_domains)
                
                if not is_safe:
                    reply_text = f"🐕 警告：嗅探到不明链接！\n\n🌐 域名: `{domain}`\n警犬闻到了钓鱼的风险，请群友警惕，不要随意点击授权钱包！"
                    send_message("group", group_id, reply_text)
            except:
                pass

    return jsonify({"status": "ok"}), 200

handler = app
