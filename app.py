import os
import re
import time
import hmac
import threading
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
UPSTASH_HEADERS = {"Authorization": f"Bearer {UPSTASH_TOKEN}"}

# ⚠️ 你的专属管理员 ID（请确认是 Webhook 推送的真实 from_user_id，而不是邀请链接 code）
ADMIN_USER_ID = "9410sfdk"

# ===== 共享线程池（模块级复用，避免每个请求现建现弃） =====
_QUERY_EXECUTOR = ThreadPoolExecutor(max_workers=16, thread_name_prefix="query")
_STATS_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="stats")


# ===== 防钓鱼：多源黑名单（MetaMask + ScamSniffer） =====
PHISHING_LIST_TTL = 3600  # 每小时刷新一次；后台线程定时更新，不阻塞请求线程
METAMASK_BLACKLIST_URL = "https://raw.githubusercontent.com/MetaMask/eth-phishing-detect/main/src/data.json"
SCAMSNIFFER_BLACKLIST_URL = "https://raw.githubusercontent.com/scamsniffer/scam-database/main/blacklist/domains/list.txt"

_phishing_list_cache = {"domains": set(), "ts": 0.0, "loaded": False}
_phishing_lock = threading.Lock()   # 保护缓存读写
_refresh_lock = threading.Lock()    # 保证同时只有一个线程在下载


def _fetch_lines(url):
    try:
        res = requests.get(url, timeout=15)
        res.raise_for_status()
        return res.text.splitlines()
    except Exception:
        return []


def _load_phishing_blocklist():
    """拉取并合并 MetaMask 与 ScamSniffer 黑名单（网络操作，只在刷新线程里做）。"""
    domains = set()
    try:
        res = requests.get(METAMASK_BLACKLIST_URL, timeout=15)
        res.raise_for_status()
        data = res.json()
        domains.update(d.lower() for d in (data.get("blacklist") or []))
    except Exception as e:
        print(f"[phishing] MetaMask 黑名单拉取失败: {e}")
    for line in _fetch_lines(SCAMSNIFFER_BLACKLIST_URL):
        line = line.strip().lower()
        if line and not line.startswith("#"):
            domains.add(line)
    return domains


def _refresh_phishing_blocklist(force=False, wait=False):
    """加载/刷新黑名单。失败时保留旧缓存，绝不因数据源故障而漏报已确认的钓鱼站。
    wait=True 时阻塞等待下载完成（供外部定时刷新接口使用）；否则非阻塞，请求线程快速返回旧缓存。"""
    now = time.time()
    with _phishing_lock:
        if not force and _phishing_list_cache["loaded"] and now - _phishing_list_cache["ts"] < PHISHING_LIST_TTL:
            return _phishing_list_cache["domains"]
    if not _refresh_lock.acquire(blocking=wait):
        # 已有线程在刷新，直接返回现有缓存（可能为空/稍旧，但绝不阻塞请求线程）
        with _phishing_lock:
            return _phishing_list_cache["domains"]
    try:
        domains = _load_phishing_blocklist()
        if domains:  # 只有拉到了新数据才更新缓存
            with _phishing_lock:
                _phishing_list_cache.update({"domains": domains, "ts": time.time(), "loaded": True})
            print(f"[phishing] 黑名单已加载 {len(domains)} 个域名")
    finally:
        _refresh_lock.release()
    with _phishing_lock:
        return _phishing_list_cache["domains"]


def get_phishing_blocklist():
    return _refresh_phishing_blocklist(force=False)


def check_domain_in_blocklist(domain):
    """精确匹配 + 子域匹配（如 evil.uniswap.com 命中 uniswap.com 的变体子域）。"""
    bl = get_phishing_blocklist()
    if not bl:
        return False
    if domain in bl:
        return True
    parts = domain.split(".")
    for i in range(1, len(parts) - 1):
        if ".".join(parts[i:]) in bl:
            return True
    return False


# ===== 仿冒域名识别（typosquatting） =====
# 主流合法站点清单：骗子常注册与这些站点高度相似的域名
TRUSTED_BRAND_DOMAINS = [
    "uniswap.org", "pancakeswap.finance", "sushi.com", "curve.fi",
    "aave.com", "compound.finance", "lido.fi", "raydium.io",
    "jup.ag", "orca.so", "metamask.io", "trustwallet.com",
    "binance.com", "okx.com", "bybit.com", "gate.io",
    "coinbase.com", "kucoin.com", "bitget.com", "htx.com",
    "ethereum.org", "solana.com", "debox.pro",
]


def _levenshtein(a, b):
    """编辑距离（纯 Python 实现，域名短字符串足够快）。"""
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        cur = [i + 1]
        for j, cb in enumerate(b):
            cur.append(min(prev[j + 1] + 1, cur[j] + 1, prev[j] + (ca != cb)))
        prev = cur
    return prev[-1]


# 骗子仿冒域名常用拼接词
IMPERSONATION_TOKENS = {
    "app", "io", "official", "login", "exchange", "earn", "verify",
    "airdrop", "finance", "swap", "wallet", "connect", "pro", "auth",
    "secure", "support", "team", "site", "online", "vip", "claim",
    "stake", "launch", "web", "download", "help", "service",
}


def check_domain_lookalike(domain):
    """识别与主流站点高度相似的仿冒域名。
    规则（按 MetaMask fuzzy 思路简化）：
    - 域名长度 <= 8：编辑距离 1 即报警（短域名错一个字母就很可疑）
    - 域名长度 9-15：编辑距离 <= 2
    - 更长域名：编辑距离 <= 2 且长度差 <= 2
    返回命中的合法域名，未命中返回 None。"""
    core = domain.split(".")[0]  # 取主域部分比对
    for brand in TRUSTED_BRAND_DOMAINS:
        brand_core = brand.split(".")[0]
        # 完全相同或就是其子域 → 合法，跳过
        if domain == brand or domain.endswith("." + brand):
            continue
        dist = _levenshtein(core, brand_core)
        limit = 1 if len(brand_core) <= 8 else 2
        if dist <= limit and abs(len(core) - len(brand_core)) <= 2:
            return brand
        # 连字符/拼接仿冒：按分隔符拆词，品牌名是一个词，其余是典型骗子词汇
        # 如 metamask-io.app、uniswap-exchange.net、okx-earn.vip（纯拼接如 sushiswap 不误伤）
        tokens = re.split(r"[-_.]", core)
        if brand_core in tokens and len(tokens) >= 2 and all(
            t == brand_core or t in IMPERSONATION_TOKENS or t.isdigit() for t in tokens
        ):
            return brand
    return None


# ===== 地址正则：同时支持 EVM 与 Solana =====
# 0x 分支加负向前瞻 (?![a-fA-F0-9])：避免把 64 位裸交易 hash 的前 42 字符误当合约地址
CA_PATTERN = re.compile(r"(0x[a-fA-F0-9]{40}(?![a-fA-F0-9]))|([1-9A-HJ-NP-Za-km-z]{32,44})")
URL_PATTERN = re.compile(r"https?://[^\s]+")
# 裸域名（不带 http 前缀）：metamask-io.app、binance-verify.vip 这类
# 只匹配 .常见TLD 结尾、至少两段的域名，避免把普通英文句子误判
DOMAIN_PATTERN = re.compile(r"(?<![\w@.\/])((?:[a-zA-Z0-9][a-zA-Z0-9-]*\.)+(?:com|net|org|io|app|xyz|vip|top|cc|me|pro|finance|site|online|info|biz|link|club|fun|ltd|shop|store|tech|zone|page|dev|one|pw|tv|co|ai|cn|us|uk|jp|kr|de|fr|hk|tw|mo|asia|mobi|gov|edu|id|my|sg|in|au))\b")

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
_QUERY_CACHE_LOCK = threading.Lock()
QUERY_CACHE_TTL = 60
_QUERY_CACHE_MAX = 10000


def _query_cache_is_fresh(addr):
    with _QUERY_CACHE_LOCK:
        ts = _QUERY_CACHE.get(addr)
        return ts is not None and (time.time() - ts) < QUERY_CACHE_TTL


def _query_cache_set(addr):
    """写入查询时间戳；带过期清理与上限保护，防止长期运行内存无限增长。"""
    now = time.time()
    with _QUERY_CACHE_LOCK:
        if len(_QUERY_CACHE) >= _QUERY_CACHE_MAX:
            expired = [k for k, ts in _QUERY_CACHE.items() if now - ts >= QUERY_CACHE_TTL]
            for k in expired:
                del _QUERY_CACHE[k]
            if len(_QUERY_CACHE) >= _QUERY_CACHE_MAX:
                _QUERY_CACHE.clear()
        _QUERY_CACHE[addr] = now


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


# ================= 运营数据统计系统 =================
def _fire_and_forget(fn, *args, **kwargs):
    """把慢速的外部统计写入丢到后台线程，避免拖慢 Webhook 响应。"""
    try:
        _STATS_EXECUTOR.submit(fn, *args, **kwargs)
    except Exception as e:
        print(f"[stats] 后台任务提交失败: {e}")


def _do_record_realtime_group(group_id):
    try:
        requests.get(f"{UPSTASH_URL}/sadd/all_groups/{group_id}", headers=UPSTASH_HEADERS, timeout=3)
    except Exception as e:
        print(f"[stats] record_realtime_group 异常: {e}")


def record_realtime_group(group_id):
    if not UPSTASH_URL or not UPSTASH_TOKEN or not group_id: return
    _fire_and_forget(_do_record_realtime_group, group_id)


def _do_record_user_activity(user_id):
    now = get_beijing_time()
    today_str = now.strftime("%Y%m%d")
    start_of_week = now - timedelta(days=now.weekday())
    week_str = start_of_week.strftime("%Y%m%d")
    month_str = now.strftime("%Y%m")
    try:
        requests.get(f"{UPSTASH_URL}/sadd/all_users/{user_id}", headers=UPSTASH_HEADERS, timeout=3)
        requests.get(f"{UPSTASH_URL}/incr/total_queries", headers=UPSTASH_HEADERS, timeout=3)
        requests.get(f"{UPSTASH_URL}/sadd/active_daily_{today_str}/{user_id}", headers=UPSTASH_HEADERS, timeout=3)
        requests.get(f"{UPSTASH_URL}/sadd/active_weekly_{week_str}/{user_id}", headers=UPSTASH_HEADERS, timeout=3)
        requests.get(f"{UPSTASH_URL}/sadd/active_monthly_{month_str}/{user_id}", headers=UPSTASH_HEADERS, timeout=3)
    except Exception as e:
        print(f"[stats] record_user_activity 异常: {e}")


def record_user_activity(user_id):
    if not UPSTASH_URL or not UPSTASH_TOKEN or not user_id: return
    _fire_and_forget(_do_record_user_activity, user_id)


def get_cardinality(key):
    if not UPSTASH_URL: return 0
    try:
        res = requests.get(f"{UPSTASH_URL}/scard/{key}", headers=UPSTASH_HEADERS, timeout=3).json()
        return res.get("result", 0)
    except Exception as e:
        print(f"[stats] get_cardinality({key}) 异常: {e}")
        return 0


def _upstash_get_value(key):
    if not UPSTASH_URL: return None
    try:
        res = requests.get(f"{UPSTASH_URL}/get/{key}", headers=UPSTASH_HEADERS, timeout=3).json()
        return res.get("result")
    except Exception as e:
        print(f"[stats] get({key}) 异常: {e}")
        return None


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

    # 并发拉取所有基数指标 + 累计查询数，把串行 8 次请求压到 1 轮
    keys = {
        "total_groups": "all_groups",
        "total_users": "all_users",
        "today": f"active_daily_{today}",
        "yesterday": f"active_daily_{yesterday}",
        "this_week": f"active_weekly_{this_week}",
        "last_week": f"active_weekly_{last_week}",
        "this_month": f"active_monthly_{this_month}",
        "last_month": f"active_monthly_{last_month}",
    }
    futures = {name: _STATS_EXECUTOR.submit(get_cardinality, key) for name, key in keys.items()}
    q_future = _STATS_EXECUTOR.submit(_upstash_get_value, "total_queries")
    card = {name: f.result() for name, f in futures.items()}

    stats = {
        "total_groups": card["total_groups"],
        "total_users": card["total_users"],
        "total_queries": int(q_future.result() or 0),
    }

    today_active = card["today"]
    yesterday_active = card["yesterday"]
    this_week_active = card["this_week"]
    last_week_active = card["last_week"]
    this_month_active = card["this_month"]
    last_month_active = card["last_month"]

    stats["daily_str"] = f"{today_active} 人 (较昨日 {calc_growth(today_active, yesterday_active)})"
    stats["weekly_str"] = f"{this_week_active} 人 (较上周 {calc_growth(this_week_active, last_week_active)})"
    stats["monthly_str"] = f"{this_month_active} 人 (较上月 {calc_growth(this_month_active, last_month_active)})"
    return stats


# ================= 核心：查币逻辑 =================
def goplus_evm(chain_id, ca_address):
    """查询 GoPlus EVM 安全数据，失败返回 {}"""
    url = f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}?contract_addresses={ca_address}"
    try:
        res = requests.get(url, timeout=5)
        res.raise_for_status()
        data = res.json()
        if data.get("code") == 1 and data.get("result"):
            return data["result"].get(ca_address.lower(), {}) or {}
    except Exception as e:
        print(f"[query] GoPlus EVM 异常: {e}")
    return {}


# 地址分类缓存：合约/EOA 判定结果复用，避免重复打 API（加锁，供线程池并发安全读写）
_ADDR_CACHE = {}
_ADDR_CACHE_LOCK = threading.Lock()
_ADDR_CACHE_MAX = 20000


def _addr_cache_key(addr, chain_id):
    """缓存键含链 ID：同一地址在不同 EVM 链上的合约/EOA 状态可能不同。"""
    return (str(chain_id), addr)


def _addr_cache_get(addr, chain_id):
    with _ADDR_CACHE_LOCK:
        return _ADDR_CACHE.get(_addr_cache_key(addr, chain_id))


def _addr_cache_set(addr, value, chain_id):
    with _ADDR_CACHE_LOCK:
        if len(_ADDR_CACHE) >= _ADDR_CACHE_MAX:
            _ADDR_CACHE.clear()
        _ADDR_CACHE[_addr_cache_key(addr, chain_id)] = value


def _addr_cache_merge(addr, chain_id, **updates):
    with _ADDR_CACHE_LOCK:
        key = _addr_cache_key(addr, chain_id)
        merged = {**(_ADDR_CACHE.get(key, {})), **updates}
        _ADDR_CACHE[key] = merged
        return merged


# 公共 RPC 端点（用于 eth_getCode 验证地址是否为合约，GoPlus 无索引时的兜底）
RPC_ENDPOINTS = {
    "1":    ["https://ethereum-rpc.publicnode.com", "https://eth.llamarpc.com"],
    "56":   ["https://bsc-rpc.publicnode.com", "https://bsc-dataseed.binance.org"],
    "137":  ["https://polygon-bor-rpc.publicnode.com"],
    "42161":["https://arbitrum-one-rpc.publicnode.com"],
    "8453": ["https://base-rpc.publicnode.com"],
    "10":   ["https://optimism-rpc.publicnode.com"],
}


def is_contract_via_rpc(addr, chain_id):
    """链上终极验证：eth_getCode 有代码 = 合约。数据来自节点，不依赖任何索引商。
    节点返回 JSON-RPC 错误/限流时继续尝试下一个节点，避免误判为 EOA。"""
    for rpc in RPC_ENDPOINTS.get(str(chain_id), []):
        try:
            res = requests.post(rpc, json={
                "jsonrpc": "2.0", "id": 1, "method": "eth_getCode",
                "params": [addr, "latest"]
            }, timeout=5).json()
            if res.get("error") is not None or res.get("result") is None:
                continue  # 节点异常/限流 → 换下一个
            return res["result"] != "0x"  # 有代码 → 合约
        except Exception:
            continue
    return None  # 所有节点都失败 → 无法判定


def classify_holder(addr, chain_id):
    """判断持仓地址是否为合约地址（交易所/质押池/金库等通常是合约或平台地址）"""
    addr = (addr or "").lower()
    if not addr.startswith("0x"):
        return {"is_contract": False}
    cached = _addr_cache_get(addr, chain_id)
    if cached is not None:
        return cached
    try:
        res = requests.get(
            f"https://api.gopluslabs.io/api/v1/address_security/{addr}?chain_id={chain_id}",
            timeout=5)
        res.raise_for_status()
        info = res.json().get("result", {}) or {}
        # 实测字段名为 contract_address（"1"=合约）；兼容 is_contract 命名
        is_contract = info.get("contract_address") == "1" or info.get("is_contract") == "1"
        # 关键补刀：GoPlus 无索引数据时（data_source 为空），"0"只是默认值不可信，
        # 必须用链上 eth_getCode 验证——V3 池子合约等未索引地址全靠这步洗清冤屈
        if not is_contract and not info.get("data_source"):
            rpc_result = is_contract_via_rpc(addr, chain_id)
            if rpc_result is not None:
                is_contract = rpc_result
        out = {"is_contract": is_contract}
    except Exception as e:
        print(f"[query] address_security 异常: {e}")
        out = {"is_contract": False}  # 查不到时按个人地址处理，不放大风险
    _addr_cache_set(addr, out, chain_id)
    return out


# 主流锚定资产白名单（按链分组）：这些币的 mint 权限属于官方锚定机制
# （充值 1:1 铸造 / 法币储备），由真实储备背书，不算项目方增发后门
KNOWN_PEGGED = {
    "56": {  # BSC
        "0x7130d2a12b9bcbfae4f2634d864a1ee1ce3ead9c",  # BTCB
        "0x2170ed0880ac9a755fd29b2688956bd959f933f8",  # ETH
        "0x55d398326f99059ff775485246999027b3197955",  # USDT
        "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",  # USDC
        "0xe9e7cea3dedca5984780bafc599bd69add087d56",  # BUSD
        "0xc5f0f7b66764f6ec8c0dffba8a602c790a649b89",  # FDUSD
        "0x1af3f329e8be154074d8769d06ffa9a48c7a8d55",  # DAI
        "0xa2e3356610840701bdf5611a539f8356ca64c141",  # WBETH
    },
    "1": {   # Ethereum
        "0xdac17f958d2ee523a2206206994597c13d831ec7",  # USDT
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # USDC
        "0x6b175474e89094c44da98b954eedeac495271d0f",  # DAI
        "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",  # WETH
        "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599",  # WBTC
    },
    "137": { # Polygon
        "0xc2132d05d31c914a87c6611c10748aeb04b58e8f",  # USDT
        "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",  # USDC
        "0x8f3cf7ad23cd3cadbd9735aff958023239c6a063",  # DAI
    },
    "42161": { # Arbitrum
        "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9",  # USDT
        "0xaf88d065e77c8cc2239327c5edb3a432268e5831",  # USDC
        "0x82af49447d8a07e3bd95bd0d56f35241523fbab1",  # WETH
    },
    "8453": { # Base
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
        "0x4200000000000000000000000000000000000006",  # WETH
    },
    "10": {  # Optimism
        "0x94b008aa00579c1307b0ef2c499ad98a8ce58e58",  # USDT
        "0x0b2c639c533813f4aa9d7837caf62653d097ff85",  # USDC
        "0x4200000000000000000000000000000000000006",  # WETH
    },
}

# 黑洞/死地址：LP 销毁是最安全的状态，不算个人集中
BURN_ADDRESSES = {
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
}


def build_evm_lines(info, chain_id=None, ca_address=None):
    """根据 GoPlus 数据生成元信息条目、异常审计项、风险标签列表"""
    risks = []
    buy_tax = to_float(info.get("buy_tax")) * 100
    sell_tax = to_float(info.get("sell_tax")) * 100
    holders = [(h.get("address", "") or "", to_float(h.get("percent")) * 100)
               for h in (info.get("holders") or [])[:10]
               if (h.get("address", "") or "").lower() not in BURN_ADDRESSES]
    top10_pct = sum(pct for _, pct in holders)

    # 细化：前10里拆出合约地址（质押池/金库/交易所等）与个人地址（并行查询不拖慢回复）
    contract_pct = 0.0
    if holders and chain_id:
        results = list(_QUERY_EXECUTOR.map(lambda ap: classify_holder(ap[0], chain_id), holders))
        contract_pct = sum(pct for (addr, pct), r in zip(holders, results) if r["is_contract"])
        eoa_pct = top10_pct - contract_pct
    else:
        eoa_pct = top10_pct

    if contract_pct > 0:
        holders_str = f"{top10_pct:.1f}%（个人 {eoa_pct:.1f}% / 合约 {contract_pct:.1f}%）"
    else:
        holders_str = f"{top10_pct:.1f}%"

    # LP 锁仓：池子代币（LP）被锁定的比例。
    # 注意：交易所官方流动性（BTCB）与 V3 集中流动性（PancakeSwap V3）不走锁仓合约，
    # is_locked 全为 0 但并非"可撤池跑路"，0% 一律视为"无法判定"，不显示不报警，
    # 只有明确检测到锁仓记录（>0%）才据此展示与判断，避免冤枉主流币。
    lp_locked_pct = None
    lp_holders = info.get("lp_holders") or []
    if lp_holders:
        locked = sum(to_float(h.get("percent")) * 100 for h in lp_holders
                     if str(h.get("is_locked")) == "1")
        if locked > 0:
            lp_locked_pct = min(locked, 100.0)

    # 盲区补刀：锁仓记录全 0 时，查 LP 最大持有者身份。
    # 个人地址(EOA)独占大额 LP = 项目方可随时撤池 → 报"LP集中个人"；
    # 合约地址（交易所/金库/锁仓合约，如币安钱包）→ 正常，不报警
    lp_personal_warn = False
    if lp_holders and lp_locked_pct is None and chain_id:
        real_lp = [h for h in lp_holders
                   if (h.get("address", "") or "").lower() not in BURN_ADDRESSES]
        if real_lp:
            top_lp = max(real_lp, key=lambda h: to_float(h.get("percent")))
            top_lp_pct = to_float(top_lp.get("percent")) * 100
            if top_lp_pct > 50 and not classify_holder(top_lp.get("address", ""), chain_id)["is_contract"]:
                lp_personal_warn = True

    # 持有人总数：热度数据，与安全无关，不展示（保持报告纯安全视角）
    # holder_count = int(to_float(info.get("holder_count")))

    meta_items = [
        ("🩸 税率", f"{buy_tax:.1f}% / {sell_tax:.1f}%"),
        ("👥 前10", holders_str),
    ]
    if lp_locked_pct is not None:
        icon = "🔒" if lp_locked_pct >= 80 else ("🔓" if lp_locked_pct <= 20 else "⚠️")
        meta_items.append((f"{icon} LP锁仓", f"{lp_locked_pct:.1f}%"))

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

    # 创建者/Owner 背景核查：合约可以写得干净，但惯犯开的合约一票否决
    # 检查 creator_address 与 owner_address 的恶意前科（钓鱼/盗窃/网诈/洗钱）
    MALICIOUS_FLAGS = ["phishing_activities", "stealing_attack",
                       "cybercrime", "money_laundering", "blackmail_activities"]
    suspicious_roles = []
    for role, role_addr in (("创建者", info.get("creator_address")),
                            ("Owner", info.get("owner_address"))):
        ra = (role_addr or "").lower()
        if not ra.startswith("0x"):
            continue
        cached = _addr_cache_get(ra, chain_id) or {}
        if "flags" in cached:
            bad_flags = cached["flags"]
        else:
            bad_flags = []
            try:
                res = requests.get(
                    f"https://api.gopluslabs.io/api/v1/address_security/{ra}?chain_id={chain_id}",
                    timeout=5)
                res.raise_for_status()
                rinfo = res.json().get("result", {}) or {}
                if rinfo.get("data_source"):  # 有真实索引数据才采信，默认值不算数
                    bad_flags = [f for f in MALICIOUS_FLAGS if str(rinfo.get(f)) == "1"]
            except Exception:
                bad_flags = []
            _addr_cache_merge(ra, chain_id, is_contract=cached.get("is_contract", False),
                              flags=bad_flags)
        if bad_flags:
            suspicious_roles.append(f"{role}涉黑")
    if suspicious_roles:
        fatal_items.extend(suspicious_roles)
        risks.append("创建者有前科")

    for name, val, safe_when, risk_label in fatal_defs:
        # 主流锚定币的增发是官方锚定机制（充值 1:1 铸造），不算后门，直接跳过
        if name == "增发" and chain_id and ca_address \
                and ca_address.lower() in KNOWN_PEGGED.get(chain_id, set()):
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

    # LP 锁仓过低：项目方可随时撤池，属可控偏高风险
    if lp_locked_pct is not None and lp_locked_pct <= 20:
        warn_items.append("LP未锁")

    # LP 集中在个人地址：无锁仓记录但 LP 握在项目方个人钱包里，随时可撤
    if lp_personal_warn:
        warn_items.append("LP集中个人")

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
    elif warn_items:
        conclusion = "⚠️ 有需留意的可控项，自行判断"
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
        res = requests.get(dex_url, timeout=5)
        res.raise_for_status()
        pairs = res.json().get("pairs", []) or []
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
        # 无行情（极早期）：并发探测所有支持的链，尽快拿到安全数据（最坏 35s → 约 5s）
        chain_id, chain_name, symbol = None, "", "未知"
        chain_ids = list(CHAIN_MAPPING.values())
        probes = list(_QUERY_EXECUTOR.map(lambda cid: (cid, goplus_evm(cid, ca_address)), chain_ids))
        for cid, info_probe in probes:
            if info_probe:
                chain_id = cid
                chain_name = CHAIN_NAMES.get(cid, cid)
                break

    meta_items = [("🪙 代币", f"{symbol} · {chain_name or '未知链'}")]
    if pairs:
        meta_items.insert(1, ("💰 池子", fmt_money(total_liq)))
    else:
        meta_items.insert(1, ("💰 池子", "$0.00"))  # 无池子也显示，用户要求

    # 查不到代币数据（可能是钱包地址或未收录合约）
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
        res = requests.get(dex_url, timeout=5)
        res.raise_for_status()
        pairs = res.json().get("pairs", []) or []
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
        res = requests.get(goplus_url, timeout=5)
        res.raise_for_status()
        g_data = res.json()
        if g_data.get("code") == 1 and g_data.get("result"):
            info = g_data["result"].get(ca_address, {}) or {}
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
        res = requests.post(url, json=payload, headers=headers, timeout=10)
        if res.status_code != 200:
            print(f"[send] 发送失败 status={res.status_code} body={res.text[:200]}")
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


# ================= 黑名单手动刷新接口（供外部 cron / 云函数定时器预热用） =================
# 同时支持 GET/POST：curl 与部分只支持 GET 的云平台定时器（如 Vercel Cron）都能触发
@app.route("/refresh-blocklist", methods=["GET", "POST"])
def refresh_blocklist():
    header_key = request.headers.get("X-API-KEY", "")
    if not WEBHOOK_KEY or not hmac.compare_digest(header_key, WEBHOOK_KEY):
        return jsonify({"error": "unauthorized"}), 401
    try:
        domains = _refresh_phishing_blocklist(force=True, wait=True)
        if not domains:
            return jsonify({"status": "error", "error": "黑名单拉取失败（数据源不可用）"}), 502
        return jsonify({"status": "ok", "domains": len(domains)}), 200
    except Exception as e:
        print(f"[phishing] 手动刷新异常: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500


# ================= Webhook 路由 =================
@app.route("/webhook", methods=["POST"])
def webhook():
    header_key = request.headers.get("X-API-KEY", "")
    # 鉴权 fail-closed：环境变量未配置时直接拒绝，避免误放行
    if not WEBHOOK_KEY or not hmac.compare_digest(header_key, WEBHOOK_KEY):
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        data = {}  # 非对象（如 JSON 数组）直接兜底，避免 .get() 抛异常
    # message_raw 为空/缺失时回退到 message
    message = data.get("message_raw") or data.get("message") or ""
    if not isinstance(message, str):
        message = str(message)  # 消息体非字符串（dict/list 等）时强转，避免 .lower() 抛异常
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

        # 3. 精准防钓鱼检测（仅群聊触发；静默模式：只有确认钓鱼才报警）
        # 支持：带协议链接（https://xxx）和裸域名（metamask-io.app）
        match_url = URL_PATTERN.search(message)
        _domain_target = None
        if match_url:
            _domain_target = urlparse(match_url.group()).netloc.lower()
        else:
            m_dom = DOMAIN_PATTERN.search(message)
            if m_dom:
                _domain_target = m_dom.group(1).lower()
        if _domain_target and group_id:
            domain = _domain_target
            url = f"https://{domain}"  # 供 GoPlus 查询用
            allowed_domains = ["twitter.com", "x.com", "debox.pro", "t.me", "github.com"]
            if not any(domain == d or domain.endswith("." + d) for d in allowed_domains):
                source = None  # 命中来源
                # 第1层：本地黑名单（MetaMask + ScamSniffer，毫秒级）
                if check_domain_in_blocklist(domain):
                    source = "MetaMask/ScamSniffer 安全库"
                else:
                    # 第2层：仿冒域名识别（与主流站点高度相似）
                    lookalike = check_domain_lookalike(domain)
                    if lookalike:
                        source = f"仿冒域名检测（高度疑似 {lookalike} 的仿冒站点）"
                    else:
                        # 第3层：GoPlus 实时接口兜底
                        try:
                            resp = requests.get(f"https://api.gopluslabs.io/api/v1/phishing_site?url={url}", timeout=3)
                            resp.raise_for_status()
                            g_data = resp.json()
                            result = g_data.get("result") or {}
                            phishing_flag = None
                            if isinstance(result, dict):
                                if result.get("phishing_site") is not None:
                                    phishing_flag = str(result.get("phishing_site"))
                                else:
                                    for v in result.values():
                                        if isinstance(v, dict) and v.get("phishing_site") is not None:
                                            phishing_flag = str(v.get("phishing_site"))
                                            break
                            if g_data.get("code") == 1 and phishing_flag == "1":
                                source = "GoPlus 安全库"
                        except Exception:
                            pass  # GoPlus 超时不报警（宁可漏一条也不误报）

                if source:
                    reply_text = (
                        f"🚨 高危警告：{source}检测到该链接为钓鱼网站！\n"
                        f"🌐 域名: {domain}\n请勿点击，谨防钱包被盗！"
                    )
                    send_message("group", group_id, reply_text)
                # 未命中任何黑名单 → 静默放行；继续往下走，不拦截同一消息里的合约地址

        # 4. 查 CA 风险（先剔除 URL 再匹配，避免污染）
        ca_address = extract_valid_ca(message)
        if ca_address:
            if _query_cache_is_fresh(ca_address):
                return jsonify({"status": "ok", "cached": True}), 200

            try:
                reply_text, markup = get_token_full_info(ca_address)
                _query_cache_set(ca_address)  # 查询成功才写缓存，失败 60 秒内可重试
                if group_id: send_message("group", group_id, reply_text, markup)
                elif from_user_id: send_message("private", from_user_id, reply_text, markup)
                record_user_activity(from_user_id)
            except Exception as e:
                print(f"[query] 查币任务异常: {e}")
            return jsonify({"status": "ok"}), 200

    return jsonify({"status": "ok"}), 200


def start_background_tasks():
    """启动时预热一次黑名单（daemon 线程，失败不影响主流程）。
    之后的定时刷新由外部 cron / 云函数定时器调用 POST /refresh-blocklist 完成。"""
    threading.Thread(target=_refresh_phishing_blocklist, kwargs={"force": True}, daemon=True).start()


start_background_tasks()

handler = app
