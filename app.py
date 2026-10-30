# -*- coding: utf-8 -*-
"""
链上警犬 · Debox Bot
=========================================================
修订版（基于代码审查报告，逐条修复以下缺陷）：

[FIX-1] Solana 路径在 GoPlus/DexScreener 全失败时误判「✅ 扫描通过」→ 改为 no_data
[FIX-2] 钓鱼检测可被 userinfo(@) / 端口绕过 → 增加 normalize_domain 统一清洗
[FIX-3] 仿冒域名识别大面积误报（etherenum.org 拼写错误进白名单、短品牌编辑距离过宽）
        → 删除错误品牌、引入 LEGIT 白名单、短品牌改用「拼接词 + 高危后缀」规则
[FIX-4] WEBHOOK_KEY 未配置时鉴权形同虚设 / 非 ASCII Header 抛 TypeError → encode 后比较 + fail-fast
[FIX-5] _QUERY_CACHE 先写缓存导致失败后 60s 静默 + 无上限增长 → 成功后写入 + TTLCache
[FIX-6] _ADDR_CACHE 未按 chain_id 分键导致跨链误判 → key 改为 f"{chain_id}:{addr}"
[FIX-7] 无行情时串行遍历 7 条链（最坏 35s）且找到后再重复请求 → 并行探测并复用结果
[FIX-8] webhook 同步阻塞 → 200 立即返回，任务丢线程池异步执行
[FIX-9] 消息同时含链接与 CA 时 CA 查询被吞 → 未命中黑名单继续走 CA 分支
[FIX-10] Upstash 统计键无 TTL / 每条 5 次 HTTP → pipeline + expire
[FIX-11] send_message 不检查响应 / 无重试 → 状态码校验 + 一次重试
[FIX-12] 全局缓存多线程无锁、无连接池 → threading.Lock + requests.Session
[FIX-13] avalanche 缺 RPC 兜底、extract_valid_ca 缺词边界、补 GoPlus 风控字段
[FIX-14] 清理死代码；非管理员 /stats 静默忽略，不再泄漏后台指令
"""

import os
import re
import time
import hmac
import threading
import requests
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from flask import Flask, request, jsonify

app = Flask(__name__)

APP_KEY = os.getenv("DEBOX_APP_KEY", "")
WEBHOOK_KEY = os.getenv("DEBOX_WEBHOOK_KEY", "")

# 数据后台配置
UPSTASH_URL = os.getenv("UPSTASH_REDIS_REST_URL", "")
UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")

# ⚠️ 管理员 ID：优先读环境变量。默认值疑似为「邀请链接 code」而非真实 from_user_id，
#    请核对后台推送的真实 user_id 后通过环境变量 DEBOX_ADMIN_ID 覆盖。
ADMIN_USER_ID = os.getenv("DEBOX_ADMIN_ID", "9410sfdk")

# [FIX-4] 关键密钥缺失时不允许带病启动/运行
MISCONFIGURED = not (APP_KEY and WEBHOOK_KEY)

# [FIX-12] 复用连接池，避免每次请求重建 TCP/TLS
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "chain-dog-bot/1.0"})

# [FIX-8] 异步任务线程池（复用，不每请求新建）
_WORKER = ThreadPoolExecutor(max_workers=16, thread_name_prefix="bot-worker")

# 常驻服务（Docker / 云托管 / 服务器）用异步，响应更快；
# Serverless（云函数类）在 HTTP 响应返回后容器可能被冻结，后台线程来不及跑完，
# 这种部署形态请设置 BOT_SYNC_MODE=1 退回同步处理，保证消息必达。
SYNC_MODE = os.getenv("BOT_SYNC_MODE", "0") == "1"

# 放行白名单：这些域名不参与钓鱼检测（平台自有/高可信）
ALLOWED_DOMAINS = ["twitter.com", "x.com", "debox.pro", "t.me", "github.com"]


# ================= 通用小工具 =================
class TTLCache:
    """[FIX-5][FIX-12] 带容量上限与过期时间的线程安全缓存。"""

    def __init__(self, maxsize=2000, ttl=60):
        self._data = OrderedDict()
        self._lock = threading.Lock()
        self.maxsize = maxsize
        self.ttl = ttl

    def get(self, key):
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            value, ts = item
            if time.time() - ts > self.ttl:
                self._data.pop(key, None)
                return None
            self._data.move_to_end(key)
            return value

    def set(self, key, value):
        with self._lock:
            self._data[key] = (value, time.time())
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)


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


# ================= 防钓鱼：多源黑名单（MetaMask + ScamSniffer，1h 刷新） =================
PHISHING_LIST_TTL = 3600
_phishing_list_cache = {"domains": set(), "ts": 0.0, "loaded": False}
_phishing_lock = threading.Lock()
_phishing_loading = False


def _fetch_lines(url):
    try:
        res = SESSION.get(url, timeout=8)
        return res.text.splitlines()
    except Exception:
        return []


def get_phishing_blocklist():
    """合并 MetaMask eth-phishing-detect 与 ScamSniffer 开源黑名单。
    拉取失败时保留旧缓存，绝不因数据源故障而漏报已确认的钓鱼站。"""
    global _phishing_loading
    now = time.time()
    with _phishing_lock:
        fresh = _phishing_list_cache["loaded"] and now - _phishing_list_cache["ts"] < PHISHING_LIST_TTL
        if fresh:
            return _phishing_list_cache["domains"]
        if _phishing_loading:  # [FIX-12] 并发单飞：避免同一时刻多次全量拉取
            return _phishing_list_cache["domains"]
        _phishing_loading = True

    domains = set()
    try:
        # MetaMask 官方钓鱼域名库
        res = SESSION.get(
            "https://raw.githubusercontent.com/MetaMask/eth-phishing-detect/main/src/data.json",
            timeout=8)
        data = res.json()
        domains.update(d.lower() for d in (data.get("blacklist") or []))
    except Exception:
        pass
    # ScamSniffer 开源黑名单
    for line in _fetch_lines(
            "https://raw.githubusercontent.com/scamsniffer/scam-database/main/blacklist/domains/list.txt"):
        line = line.strip().lower()
        if line and not line.startswith("#"):
            domains.add(line)

    if domains:  # 只有拉到了新数据才更新缓存
        with _phishing_lock:
            _phishing_list_cache.update({"domains": domains, "ts": time.time(), "loaded": True})
        print(f"[phishing] 黑名单已加载 {len(domains)} 个域名")

    with _phishing_lock:
        _phishing_loading = False
        return _phishing_list_cache["domains"]


def normalize_domain(raw):
    """[FIX-2] 把任意形态的输入统一清洗成 registrable 之前的主机名。
    - 去掉 userinfo：https://www.google.com@evil.com/ 的真身是 evil.com
    - 去掉端口：evil.com:8443 -> evil.com
    - 去掉协议、路径、查询串、末尾点"""
    d = (raw or "").strip().lower()
    if not d:
        return ""
    if "://" not in d:
        d = "http://" + d
    try:
        parsed = urlparse(d)
        host = parsed.netloc or parsed.path
    except Exception:
        return ""
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    host = host.split(":")[0]
    host = host.split("/")[0]
    return host.strip(".")


def check_domain_in_blocklist(domain):
    """精确匹配 + 子域匹配（如 evil.uniswap.com 命中 uniswap.com 的变体子域）。"""
    bl = get_phishing_blocklist()
    if not bl or not domain:
        return False
    if domain in bl:
        return True
    parts = domain.split(".")
    for i in range(1, len(parts) - 1):
        if ".".join(parts[i:]) in bl:
            return True
    return False


# ================= 仿冒域名识别（typosquatting） =================
# 主流合法站点清单：骗子常注册与这些站点高度相似的域名
# [FIX-3] 已删除拼写错误的 "etherenum.org"（它导致官网 ethereum.org 被判为仿冒站）
TRUSTED_BRAND_DOMAINS = [
    "uniswap.org", "pancakeswap.finance", "sushi.com", "curve.fi",
    "aave.com", "compound.finance", "lido.fi", "raydium.io",
    "jup.ag", "orca.so", "metamask.io", "trustwallet.com",
    "binance.com", "okx.com", "bybit.com", "gate.io",
    "coinbase.com", "kucoin.com", "bitget.com", "htx.com",
    "ethereum.org", "solana.com", "debox.pro",
    # 钓鱼重灾区的钱包/浏览器/行情站（均为造词，误伤概率低）
    "myetherwallet.com", "etherscan.io", "bscscan.com", "opensea.io",
    "coingecko.com", "coinmarketcap.com", "solflare.com", "trezor.io",
    "imtoken.com", "tokenpocket.pro",
]

# [FIX-3] 明确的合法域名（含各项目自用多域名/常用工具站）：命中即放行，不做任何相似度比对
EXTRA_LEGIT_DOMAINS = [
    "binance.org", "binance.us", "okex.com", "bgate.com", "gatematrix.com",
    "etherscan.io", "bscscan.com", "polygonscan.com", "arbiscan.io", "optimistic.etherscan.io",
    "basescan.org", "snowscan.xyz", "solscan.io", "opensea.io", "magiceden.io",
    "coingecko.com", "coinmarketcap.com", "dexscreener.com", "dextools.io",
    "gmgn.ai", "ave.ai", "birdeye.so", "pump.fun", "jupiter.ag",
    "walletconnect.com", "ledger.com", "trezor.io", "imtoken.com", "tokenpocket.pro",
    "cetus.zone", "turbos.finance", "raydium.io", "orca.so",
]
LEGIT_DOMAINS = set(TRUSTED_BRAND_DOMAINS) | set(EXTRA_LEGIT_DOMAINS)

# [FIX-3] 高危后缀：新 gTLD 里仿冒站点占比极高，短品牌仅在这类后缀下才启用模糊比对
HIGH_RISK_TLDS = {
    "vip", "xyz", "top", "cc", "pw", "tk", "ml", "ga", "cf", "gq",
    "club", "online", "site", "shop", "icu", "loan", "work", "click",
    "bid", "stream", "download", "review", "party", "gdn", "rest", "men",
    "ru", "su", "to", "shop", "fun", "beauty", "quest", "bond", "cyou",
}


def _is_single_transposition(a, b):
    """相邻两字符调换（最常见的手误之一），如 uniswpa / uniswap。"""
    if len(a) != len(b):
        return False
    diffs = [i for i in range(len(a)) if a[i] != b[i]]
    return len(diffs) == 2 and diffs[1] == diffs[0] + 1 \
        and a[diffs[0]] == b[diffs[1]] and a[diffs[1]] == b[diffs[0]]


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
    """识别与主流站点高度相似的仿冒域名，返回命中的合法域名，否则 None。

    [FIX-3] 三层规则，宁可漏报也不把普通域名误报成钓鱼（群里天天喊狼来了就没人信了）：
      A. 拼接词仿冒：metamask-io.app / uniswap-exchange.net / okx-earn.vip（对所有品牌生效）
      B. 短品牌（主域 < 6 字符，如 jup/okx/gate/orca/aave）：只在「高危后缀」下比对，
         避免 date.com / game.com / pup.com / ora.com 这类纯英文单词被误伤
      C. 常规品牌（>= 6 字符）：编辑距离 <= 1（<=8 字符）或 <= 2（更长），长度差 <= 2
    """
    domain = (domain or "").lower().strip(".")
    if not domain or domain.count(".") < 1:
        return None
    # 白名单及其子域一律放行
    if any(domain == d or domain.endswith("." + d) for d in LEGIT_DOMAINS):
        return None

    parts = domain.split(".")
    core, tld = parts[0], parts[-1]

    for brand in TRUSTED_BRAND_DOMAINS:
        b_parts = brand.split(".")
        brand_core = b_parts[0]
        # 完全相同或就是其子域 → 合法，跳过
        if domain == brand or domain.endswith("." + brand):
            continue

        # A. 连字符/拼接仿冒：品牌名是其中一个词，其余是典型骗子词汇
        tokens = re.split(r"[-_.]", core)
        if brand_core in tokens and len(tokens) >= 2 and all(
                t == brand_core or t in IMPERSONATION_TOKENS or t.isdigit() for t in tokens):
            return brand

        if len(brand_core) < 6:
            # B. 短品牌：仅在域名使用高危后缀时才做模糊匹配
            if tld in HIGH_RISK_TLDS:
                if len(core) == len(brand_core) and _levenshtein(core, brand_core) <= 1:
                    return brand
                if brand_core in core and 0 < len(core) - len(brand_core) <= 2:
                    return brand
            continue

        # C. 常规品牌编辑距离（含相邻字符调换这种常见手误）
        dist = _levenshtein(core, brand_core)
        limit = 1 if len(brand_core) <= 8 else 2
        typo_hit = dist <= limit or _is_single_transposition(core, brand_core)
        if typo_hit and abs(len(core) - len(brand_core)) <= 2:
            return brand
    return None


# ================= 地址/链接匹配 =================
# [FIX-13] 加词边界，避免从更长字符串中切出伪地址
CA_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(0x[a-fA-F0-9]{40})(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])([1-9A-HJ-NP-Za-km-z]{32,44})(?![A-Za-z0-9])"
)
URL_PATTERN = re.compile(r"https?://[^\s]+")
# 裸域名（不带 http 前缀）
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

# [FIX-5] 有界的 TTL 缓存
_QUERY_CACHE = TTLCache(maxsize=5000, ttl=60)
# [FIX-6] key 含 chain_id，避免同一地址跨链串味
_ADDR_CACHE = TTLCache(maxsize=20000, ttl=3600)


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


# ================= 运营数据统计系统 =================
UPS_HEADERS = lambda: {"Authorization": f"Bearer {UPSTASH_TOKEN}"}


def _upstash_available():
    return bool(UPSTASH_URL and UPSTASH_TOKEN)


def record_realtime_group(group_id):
    if not _upstash_available() or not group_id:
        return
    try:
        SESSION.get(f"{UPSTASH_URL}/sadd/all_groups/{group_id}",
                    headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}, timeout=3)
    except Exception as e:
        print(f"[stats] record_realtime_group 异常: {e}")


def record_user_activity(user_id):
    """[FIX-10] 用 pipeline 一次提交，并给日/周/月集合设置 TTL，避免无限膨胀。"""
    if not _upstash_available() or not user_id:
        return
    now = get_beijing_time()
    today_str = now.strftime("%Y%m%d")
    week_str = (now - timedelta(days=now.weekday())).strftime("%Y%m%d")
    month_str = now.strftime("%Y%m")

    day_key = f"active_daily_{today_str}"
    week_key = f"active_weekly_{week_str}"
    month_key = f"active_monthly_{month_str}"

    # 日 8 天 / 周 5 周 / 月 14 个月
    cmds = [
        ["sadd", "all_users", str(user_id)],
        ["incr", "total_queries"],
        ["sadd", day_key, str(user_id)], ["expire", day_key, 8 * 86400],
        ["sadd", week_key, str(user_id)], ["expire", week_key, 35 * 86400],
        ["sadd", month_key, str(user_id)], ["expire", month_key, 400 * 86400],
    ]
    try:
        res = SESSION.post(f"{UPSTASH_URL}/pipeline", json=cmds,
                           headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}, timeout=5)
        if res.status_code == 200:
            return
        print(f"[stats] pipeline 返回 {res.status_code}: {res.text[:200]}")
    except Exception as e:
        print(f"[stats] pipeline 异常: {e}")
    # 降级：逐条执行
    headers = {"Authorization": f"Bearer {UPSTASH_TOKEN}"}
    for cmd in cmds:
        try:
            if cmd[0] == "expire":
                SESSION.get(f"{UPSTASH_URL}/expire/{cmd[1]}/{cmd[2]}", headers=headers, timeout=3)
            elif cmd[0] == "incr":
                SESSION.get(f"{UPSTASH_URL}/incr/{cmd[1]}", headers=headers, timeout=3)
            else:
                SESSION.get(f"{UPSTASH_URL}/sadd/{cmd[1]}/{cmd[2]}", headers=headers, timeout=3)
        except Exception as e:
            print(f"[stats] 降级写入异常: {e}")


def get_cardinality(key):
    if not _upstash_available():
        return 0
    try:
        res = SESSION.get(f"{UPSTASH_URL}/scard/{key}",
                          headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}, timeout=3).json()
        return res.get("result", 0)
    except Exception as e:
        print(f"[stats] get_cardinality({key}) 异常: {e}")
        return 0


def calc_growth(current, previous):
    if previous == 0:
        return "🟢 +100%" if current > 0 else "0%"
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
    if _upstash_available():
        try:
            res_q = SESSION.get(f"{UPSTASH_URL}/get/total_queries",
                                headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"},
                                timeout=3).json()
            stats["total_queries"] = int(res_q.get("result") or 0)
        except Exception as e:
            print(f"[stats] total_queries 异常: {e}")
            stats["total_queries"] = 0
    else:
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
        res = SESSION.get(url, timeout=6).json()
        if res.get("code") == 1 and res.get("result"):
            return res["result"].get(ca_address.lower(), {}) or {}
    except Exception as e:
        print(f"[query] GoPlus EVM 异常: {e}")
    return {}


def probe_chain_once(chain_id, ca_address):
    """[FIX-7] 并行探测：返回 (chain_id, info)"""
    return (chain_id, goplus_evm(chain_id, ca_address))


def probe_supported_chains(ca_address):
    """[FIX-7] 无行情时并行遍历各链探测安全数据，最坏 ~6s（旧版串行 7*5=35s）。"""
    result = {}
    with ThreadPoolExecutor(max_workers=7) as ex:
        futures = [ex.submit(probe_chain_once, cid, ca_address) for cid in CHAIN_MAPPING.values()]
        for f in as_completed(futures):
            try:
                cid, info = f.result()
                if info:
                    result = {"chain_id": cid, "info": info}
                    for rest in futures:      # 命中后取消尚未跑的任务，省流量
                        rest.cancel()
                    break
            except Exception as e:
                print(f"[query] 链探测异常: {e}")
    return result


# 公共 RPC 端点（用于 eth_getCode 验证地址是否为合约）
# [FIX-13] 补齐 avalanche
RPC_ENDPOINTS = {
    "1":     ["https://ethereum-rpc.publicnode.com", "https://eth.llamarpc.com"],
    "56":    ["https://bsc-rpc.publicnode.com", "https://bsc-dataseed.binance.org"],
    "137":   ["https://polygon-bor-rpc.publicnode.com"],
    "42161": ["https://arbitrum-one-rpc.publicnode.com"],
    "8453":  ["https://base-rpc.publicnode.com"],
    "43114": ["https://avalanche-c-chain-rpc.publicnode.com"],
    "10":    ["https://optimism-rpc.publicnode.com"],
}


def is_contract_via_rpc(addr, chain_id):
    """链上终极验证：eth_getCode 有代码 = 合约。"""
    for rpc in RPC_ENDPOINTS.get(str(chain_id), []):
        try:
            res = SESSION.post(rpc, json={
                "jsonrpc": "2.0", "id": 1, "method": "eth_getCode",
                "params": [addr, "latest"]
            }, timeout=5).json()
            code = (res.get("result") or "0x")
            return code != "0x" and code != "0x0"
        except Exception:
            continue
    return None


def classify_holder(addr, chain_id):
    """判断持仓地址是否为合约地址（交易所/质押池/金库等通常是合约或平台地址）"""
    addr = (addr or "").lower()
    if not addr.startswith("0x"):
        return {"is_contract": False}
    cache_key = f"{chain_id}:{addr}"  # [FIX-6]
    cached = _ADDR_CACHE.get(cache_key)
    if cached is not None:
        return cached
    try:
        res = SESSION.get(
            f"https://api.gopluslabs.io/api/v1/address_security/{addr}?chain_id={chain_id}",
            timeout=5).json()
        info = res.get("result", {}) or {}
        is_contract = info.get("contract_address") == "1" or info.get("is_contract") == "1"
        if not is_contract and not info.get("data_source"):
            rpc_result = is_contract_via_rpc(addr, chain_id)
            if rpc_result is not None:
                is_contract = rpc_result
        out = {"is_contract": is_contract}
    except Exception as e:
        print(f"[query] address_security 异常: {e}")
        out = {"is_contract": False}
    _ADDR_CACHE.set(cache_key, out)
    return out


# 主流锚定资产白名单（按链分组）
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
    "137": {  # Polygon
        "0xc2132d05d31c914a87c6611c10748aeb04b58e8f",  # USDT
        "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",  # USDC
        "0x8f3cf7ad23cd3cadbd9735aff958023239c6a063",  # DAI
    },
    "42161": {  # Arbitrum
        "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9",  # USDT
        "0xaf88d065e77c8cc2239327c5edb3a432268e5831",  # USDC
        "0x82af49447d8a07e3bd95bd0d56f35241523fbab1",  # WETH
    },
    "8453": {  # Base
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
        "0x4200000000000000000000000000000000000006",  # WETH
    },
    "10": {  # Optimism
        "0x94b008aa00579c1307b0ef2c499ad98a8ce58e58",  # USDT
        "0x0b2c639c533813f4aa9d7837caf62653d097ff85",  # USDC
        "0x4200000000000000000000000000000000000006",  # WETH
    },
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

    contract_pct = 0.0
    if holders and chain_id:
        with ThreadPoolExecutor(max_workers=10) as ex:
            results = list(ex.map(lambda ap: classify_holder(ap[0], chain_id), holders))
        contract_pct = sum(pct for (addr, pct), r in zip(holders, results) if r.get("is_contract"))
        eoa_pct = top10_pct - contract_pct
    else:
        eoa_pct = top10_pct

    holders_str = (f"{top10_pct:.1f}%（个人 {eoa_pct:.1f}% / 合约 {contract_pct:.1f}%）"
                   if contract_pct > 0 else f"{top10_pct:.1f}%")

    lp_locked_pct = None
    lp_holders = info.get("lp_holders") or []
    if lp_holders:
        locked = sum(to_float(h.get("percent")) * 100 for h in lp_holders
                     if str(h.get("is_locked")) == "1")
        if locked > 0:
            lp_locked_pct = min(locked, 100.0)

    lp_personal_warn = False
    if lp_holders and lp_locked_pct is None and chain_id:
        burn_addrs = {"0x0000000000000000000000000000000000000000",
                      "0x000000000000000000000000000000000000dead"}
        real_lp = [h for h in lp_holders
                   if (h.get("address", "") or "").lower() not in burn_addrs]
        if real_lp:
            top_lp = max(real_lp, key=lambda h: to_float(h.get("percent")))
            top_lp_pct = to_float(top_lp.get("percent")) * 100
            if top_lp_pct > 50 and not classify_holder(top_lp.get("address", ""), chain_id).get("is_contract"):
                lp_personal_warn = True

    meta_items = [
        ("🩸 税率", f"{buy_tax:.1f}% / {sell_tax:.1f}%"),
        ("👥 前10", holders_str),
    ]
    if lp_locked_pct is not None:
        icon = "🔒" if lp_locked_pct >= 80 else ("🔓" if lp_locked_pct <= 20 else "⚠️")
        meta_items.append((f"{icon} LP锁仓", f"{lp_locked_pct:.1f}%"))

    fatal_defs = [
        ("开源", info.get("is_open_source"), "1", "未开源"),
        ("改余额", info.get("owner_change_balance"), "0", "可改余额"),
        ("藏Owner", info.get("hidden_owner"), "0", "隐藏Owner"),
        ("收回权", info.get("can_take_back_ownership"), "0", "可收回权限"),
        ("增发", info.get("is_mintable"), "0", "增发"),
        ("黑名单", info.get("is_blacklisted"), "0", "黑名单"),
        # [FIX-13] 卖不掉 = 和貔貅等价的直接损失，按致命项处理
        ("全卖不出", info.get("cannot_sell_all"), "0", "无法全部卖出"),
    ]
    warn_defs = [
        ("暂停", info.get("transfer_pausable"), "暂停交易"),
        ("白名单", info.get("is_whitelisted"), "白名单限制"),
        ("代理", info.get("is_proxy"), "代理合约"),
        # [FIX-13] 补充：GoPlus 其他值得提示的非致命权限（沿用可控项口径：缺失不计未知）
        ("个人滑点", info.get("personal_slippage_modifiable"), "可设个人滑点"),
        ("免授权转账", info.get("transfer_without_approval"), "免授权转账"),
        ("改滑点", info.get("slippage_modifiable"), "可改滑点"),
        ("交易冷却", info.get("trading_cooldown"), "交易冷却机制"),
        ("巨鲸限制", info.get("anti_whale_modifiable"), "反鲸可变更"),
        ("外部调用", info.get("external_call"), "存在外部调用"),
    ]

    fatal_items, warn_items, unknown_items = [], [], []

    # 创建者/Owner 背景核查
    MALICIOUS_FLAGS = ["phishing_activities", "stealing_attack",
                       "cybercrime", "money_laundering", "blackmail_activities"]
    suspicious_roles = []
    for role, role_addr in (("创建者", info.get("creator_address")),
                            ("Owner", info.get("owner_address"))):
        ra = (role_addr or "").lower()
        if not ra.startswith("0x"):
            continue
        ck = f"{chain_id}:{ra}"  # [FIX-6] 与 classify_holder 共用同一套键
        cached = _ADDR_CACHE.get(ck) or {}
        if "flags" in cached:
            bad_flags = cached["flags"]
        else:
            bad_flags = []
            try:
                res = SESSION.get(
                    f"https://api.gopluslabs.io/api/v1/address_security/{ra}?chain_id={chain_id}",
                    timeout=5).json()
                rinfo = res.get("result", {}) or {}
                if rinfo.get("data_source"):
                    bad_flags = [f for f in MALICIOUS_FLAGS if str(rinfo.get(f)) == "1"]
            except Exception:
                bad_flags = []
            cached = {"is_contract": cached.get("is_contract", False), "flags": bad_flags}
            _ADDR_CACHE.set(ck, cached)
        if bad_flags:
            suspicious_roles.append(f"{role}涉黑")
    if suspicious_roles:
        fatal_items.extend(suspicious_roles)
        risks.append("创建者有前科")

    for name, val, safe_when, risk_label in fatal_defs:
        # 主流锚定币的增发是官方锚定机制（充值 1:1 铸造），不算后门
        if name == "增发" and chain_id and ca_address \
                and ca_address.lower() in KNOWN_PEGGED.get(str(chain_id), set()):
            continue
        if val is None or str(val) == "":
            unknown_items.append(name)
        elif str(val) != safe_when:
            fatal_items.append(name)
            risks.append(risk_label)
    for name, val, warn_label in warn_defs:
        if str(val) == "1":
            warn_items.append(warn_label)

    if str(info.get("self_destruct") or "") == "1":
        warn_items.append("可自毁")

    if lp_locked_pct is not None and lp_locked_pct <= 20:
        warn_items.append("LP未锁")

    if lp_personal_warn:
        warn_items.append("LP集中个人")

    if str(info.get("is_honeypot")) == "1":
        risks.append("貔貅")
    if buy_tax > 10 or sell_tax > 10:
        risks.append("高税")
    if eoa_pct > 80:
        risks.append("死控盘")

    return meta_items, (fatal_items, warn_items, unknown_items), risks


def risk_level(risks, warn_items, unknown_items):
    """三级风险定级：red=极高危 / orange=需留意 / green=安全"""
    if risks:
        return "red"
    if warn_items or unknown_items:
        return "orange"
    return "green"


LEVEL_STYLE = {
    "red":     {"title": "🚨 链上警犬 · 极度危险"},
    "orange":  {"title": "⚠️ 链上警犬 · 需留意"},
    "green":   {"title": "🐕 链上警犬 · 安全报告"},
    "unknown": {"title": "❓ 链上警犬 · 数据不足"},
}


def assemble_report(meta_items, audit, risks, low_liq=False, dex_url=None, no_data=False):
    """海报式卡片：分级标题 + 图标键值对 + 审计分级段落"""
    fatal_items, warn_items, unknown_items = audit
    risks = list(risks)
    if low_liq:
        risks.append("流动性极低")

    if no_data:
        # 安全数据完全查不到，绝不能装作“扫描通过”误导用户
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

    if fatal_items:
        lines.append(f"❌ 致命　{' | '.join(fatal_items)}")
    else:
        if warn_items:
            lines.append(f"⚠️ 可控　{' | '.join(warn_items)}")
        if unknown_items:
            if len(unknown_items) > 4:
                lines.append(f"➖ 未知　{len(unknown_items)} 项（数据索引中）")
            else:
                lines.append(f"➖ 未知　{' | '.join(unknown_items)}")

    lines.append(conclusion)
    return "\n".join(lines)


def fetch_dex_pairs(ca_address, log_tag="DexScreener"):
    try:
        res = SESSION.get(f"https://api.dexscreener.com/latest/dex/tokens/{ca_address}", timeout=6).json()
        return res.get("pairs", []) or []
    except Exception as e:
        print(f"[query] {log_tag} 异常: {e}")
        return []


def liq_of(p):
    return to_float((p.get("liquidity") or {}).get("usd"))


def own_pairs_only(pairs, ca_address):
    """只统计本代币作为 baseToken 的池子"""
    own = [p for p in pairs
           if (p.get("baseToken") or {}).get("address", "").lower() == ca_address.lower()]
    return own or pairs


def get_token_full_info(ca_address):
    """返回 (报告文本, reply_markup 或 None)"""
    if not ca_address.startswith("0x"):
        return _get_solana_token_info(ca_address)

    pairs = own_pairs_only(fetch_dex_pairs(ca_address, "DexScreener"), ca_address)

    dex_page = None
    total_liq = 0.0
    # [FIX-1] 没有任何池子 = 完全没有流动性，必须报警（旧代码这里会漏报）
    low_liq = False
    info = None
    if pairs:
        best_pair = max(pairs, key=liq_of)
        chain_id = CHAIN_MAPPING.get(best_pair.get("chainId", "bsc"))
        dex_chain = best_pair.get("chainId", "bsc")
        chain_name = CHAIN_NAMES.get(chain_id, dex_chain.upper())
        symbol = (best_pair.get("baseToken") or {}).get("symbol", "未知")
        total_liq = sum(liq_of(p) for p in pairs)
        low_liq = total_liq < 1000
        dex_page = best_pair.get("url")
    else:
        chain_id, chain_name, symbol = None, "", "未知"
        low_liq = True
        # [FIX-7] 并行探测 + 复用结果，不再“探测一次 + 再查一次”
        found = probe_supported_chains(ca_address)
        if found:
            chain_id = found["chain_id"]
            chain_name = CHAIN_NAMES.get(chain_id, chain_id)
            info = found["info"]

    meta_items = [("🪙 代币", f"{symbol} · {chain_name or '未知链'}"),
                  ("💰 池子", fmt_money(total_liq))]

    # 查不到所在链（可能是钱包地址或未收录合约）
    if not chain_id:
        return assemble_report(meta_items, ([], [], []), [], low_liq=low_liq, no_data=True), None

    if info is None:
        info = goplus_evm(chain_id, ca_address)
    if not info:
        return assemble_report(meta_items, ([], [], []), [], low_liq=low_liq, no_data=True), None

    meta, audit, risks = build_evm_lines(info, chain_id, ca_address)
    meta_items.extend(meta)
    return assemble_report(meta_items, audit, risks, low_liq, dex_page), None


def _get_solana_token_info(ca_address):
    """返回 (报告文本, reply_markup 或 None)

    [FIX-1] 旧版在 GoPlus 失败/无数据时审计项全空 → 误输出「✅ 扫描通过」。
    现改为：拿不到任何 GoPlus 数据 → no_data（❓ 数据不足）。
    """
    pairs = own_pairs_only(fetch_dex_pairs(ca_address, "DexScreener SOL"), ca_address)

    risks = []
    low_liq = False
    dex_page = None
    total_liq = 0.0
    meta_items = [("🪙 代币", "SOL")]
    meta_items.append(("💰 池子", "$0.00"))
    if pairs:
        best_pair = max(pairs, key=liq_of)
        symbol = (best_pair.get("baseToken") or {}).get("symbol", "SOL")
        total_liq = sum(liq_of(p) for p in pairs)
        low_liq = total_liq < 1000
        meta_items[0] = ("🪙 代币", f"{symbol} · SOL")
        meta_items[1] = ("💰 池子", fmt_money(total_liq))
        dex_page = best_pair.get("url")
    else:
        low_liq = True  # [FIX-1] 无池子 = 无流动性

    fatal_items, warn_items, unknown_items = [], [], []
    goplus_url = f"https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses={ca_address}"
    got_data = False
    try:
        g_res = SESSION.get(goplus_url, timeout=6).json()
        if g_res.get("code") == 1 and g_res.get("result"):
            result = g_res["result"]
            info = result.get(ca_address) or result.get(ca_address.lower()) or {}
            if info:
                got_data = True
                mint_status = str((info.get("mintable") or {}).get("status", ""))
                freeze_status = str((info.get("freezable") or {}).get("status", ""))
                close_status = str((info.get("closable") or {}).get("status", ""))
                holders = info.get("holders", []) or []
                top10_pct = sum(to_float(h.get("percent")) * 100 for h in holders[:10])

                meta_items.append(("👥 前10", f"{top10_pct:.1f}%"))

                if mint_status == "1":
                    fatal_items.append("Mint未弃"); risks.append("Mint未放弃")
                elif mint_status == "":
                    unknown_items.append("Mint")
                if freeze_status == "1":
                    fatal_items.append("Freeze未弃"); risks.append("Freeze未放弃")
                elif freeze_status == "":
                    unknown_items.append("Freeze")
                if close_status == "1":
                    warn_items.append("Close未弃")
                if top10_pct > 80:
                    risks.append("死控盘")
    except Exception as e:
        print(f"[query] GoPlus SOL 异常: {e}")

    if not got_data:
        # 拿不到安全数据就不能给绿灯
        return assemble_report(meta_items, ([], [], []), [], low_liq=low_liq, dex_url=dex_page, no_data=True), None

    return assemble_report(meta_items, (fatal_items, warn_items, unknown_items), risks, low_liq, dex_page), None


# ================= 消息发送 =================
def send_message(chat_type, chat_id, content, reply_markup=None):
    """[FIX-11] 检查响应状态码并带一次重试，失败留痕。"""
    url = "https://open.debox.pro/openapi/bot/sendMessage"
    headers = {"Content-Type": "application/json", "X-API-KEY": APP_KEY}
    payload = {"chat_type": chat_type, "chat_id": chat_id, "content": content, "parse_mode": "text"}
    if reply_markup:
        payload["reply_markup"] = reply_markup

    for attempt in range(2):
        try:
            resp = SESSION.post(url, json=payload, headers=headers, timeout=10)
            if resp.status_code == 200:
                try:
                    body = resp.json()
                    if body and str(body.get("code")) not in ("", "0", "200"):
                        print(f"==== 发送返回业务错误: {str(body)[:200]} ====")
                except ValueError:
                    pass
                return True
            print(f"==== 发送消息失败 status={resp.status_code} body={resp.text[:200]} ====")
        except Exception as e:
            print(f"==== 发送消息异常(第{attempt+1}次): {e} ====")
        time.sleep(0.5)
    return False


def extract_valid_ca(message):
    """剔除 URL 后提取第一个通过校验的 CA 地址"""
    cleaned = URL_PATTERN.sub(" ", message or "")
    for m in CA_PATTERN.finditer(cleaned):
        addr = m.group()
        if addr.startswith("0x") or is_valid_solana_address(addr):
            return addr
    return None


def extract_domain(message):
    """[FIX-2] 统一提取域名：优先带协议链接，其次裸域名；统一做 userinfo/端口清洗。"""
    match_url = URL_PATTERN.search(message or "")
    if match_url:
        return normalize_domain(match_url.group())
    m_dom = DOMAIN_PATTERN.search(message or "")
    if m_dom:
        return normalize_domain(m_dom.group(1))
    return ""


# ================= 鉴权 =================
def _auth_ok(incoming):
    """[FIX-4] 空密钥不放行；先 encode 避免非 ASCII 字符触发 TypeError。"""
    if not WEBHOOK_KEY or not incoming:
        return False
    try:
        return hmac.compare_digest(incoming.encode("utf-8"), WEBHOOK_KEY.encode("utf-8"))
    except Exception:
        return False


# ================= 业务处理 =================
HELP_TEXT = (
    "🐕‍🦺 链上警犬使用说明\n\n"
    "👉 查代币安全：直接在群内发送合约地址（支持 EVM 0x 开头 及 Solana Base58 地址），警犬会立刻帮你排雷。\n"
    "👉 防钓鱼雷达：群内出现不明链接，警犬会自动调用安全接口拦截。\n"
    "👉 深度数据：自动聚合全链池子，EVM 检测貔貅/税率/锁仓，Solana 检测 Mint/Freeze 权限。\n\n"
    "💡 觉得好用？点击下方链接加入产品体验群，获取最新功能！"
)


def handle_message(group_id, from_user_id, message):
    """实际业务逻辑，在后台线程里跑，不阻塞 webhook 响应。"""
    try:
        if group_id:
            record_realtime_group(group_id)

        msg_lower = (message or "").lower()

        # 1. 使用教程
        if any(k in msg_lower for k in ["/help", "教程", "菜单", "说明"]):
            # 邀请链接默认沿用旧值；建议改为环境变量注入，便于换群不用改代码
            invite = os.getenv("DEBOX_INVITE_LINK", "https://m.debox.pro/group?id=sajshm5d&code=9410sfdk")
            text = HELP_TEXT + (f"\n🔗 体验群链接：{invite}" if invite else "")
            if group_id:
                send_message("group", group_id, text)
            elif from_user_id:
                send_message("private", from_user_id, text)
            return

        # 2. 管理员查后台
        if "/admin" in msg_lower or "/stats" in msg_lower:
            # [FIX-14] 非管理员静默忽略，不再回复“这是内部管理指令”以免暴露后台命令
            if from_user_id and (str(from_user_id) == ADMIN_USER_ID or str(from_user_id) == f"@{ADMIN_USER_ID}"):
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
            else:
                print(f"[audit] 非管理员 {from_user_id} 尝试使用后台指令")
            return

        # 3. 精准防钓鱼检测（仅群聊；静默模式：只有确认钓鱼才报警）
        domain = extract_domain(message)
        if domain and group_id:
            # 已清洗：无 userinfo、无端口
            if not any(domain == d or domain.endswith("." + d) for d in ALLOWED_DOMAINS):
                source = None
                if check_domain_in_blocklist(domain):
                    source = "MetaMask/ScamSniffer 安全库"
                else:
                    lookalike = check_domain_lookalike(domain)
                    if lookalike:
                        source = f"仿冒域名检测（高度疑似 {lookalike} 的仿冒站点）"
                    else:
                        try:
                            resp = SESSION.get(
                                f"https://api.gopluslabs.io/api/v1/phishing_site?url=https://{domain}",
                                timeout=4).json()
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
                                source = "GoPlus 安全库"
                        except Exception:
                            pass  # GoPlus 超时不报警（宁可漏一条也不误报）

                if source:
                    reply_text = (
                        f"🚨 高危警告：{source}检测到该链接为钓鱼网站！\n"
                        f"🌐 域名: {domain}\n请勿点击，谨防钱包被盗！"
                    )
                    send_message("group", group_id, reply_text)
                    return
                # [FIX-9] 未命中黑名单 → 不 return，继续尝试识别 CA

        # 4. 查 CA 风险
        ca_address = extract_valid_ca(message)
        if ca_address:
            # [FIX-5] 命中即去重（防刷屏）；但只有查询成功后才写入缓存，
            #         避免查询失败后 60 秒内用户重复发送被静默吞掉
            if _QUERY_CACHE.get(ca_address):
                return
            try:
                reply_text, markup = get_token_full_info(ca_address)
                ok = False
                if group_id:
                    ok = send_message("group", group_id, reply_text, markup)
                elif from_user_id:
                    ok = send_message("private", from_user_id, reply_text, markup)
                if ok:
                    _QUERY_CACHE.set(ca_address, time.time())
                    record_user_activity(from_user_id)
            except Exception as e:
                print(f"[query] 查币任务异常: {e}")
    except Exception as e:
        print(f"[handle] 消息处理异常: {e}")


# ================= Webhook 路由 =================
@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({"status": "ok" if not MISCONFIGURED else "misconfigured"}), 200


@app.route("/webhook", methods=["POST"])
def webhook():
    # [FIX-4] 密钥未配置时拒绝服务，避免“无鉴权裸奔”
    if MISCONFIGURED:
        print("[auth] DEBOX_APP_KEY / DEBOX_WEBHOOK_KEY 未配置，拒绝处理")
        return jsonify({"error": "server misconfigured"}), 500

    if not _auth_ok(request.headers.get("X-API-KEY", "")):
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    message = data.get("message_raw", data.get("message", ""))
    group_id = data.get("group_id")
    from_user_id = data.get("from_user_id")

    if message:
        if SYNC_MODE:
            handle_message(group_id, from_user_id, message)   # Serverless 形态：同步保证必达
        else:
            # [FIX-8] 立即返回 200，任务异步执行，杜绝平台超时重试导致的重复推送
            _WORKER.submit(handle_message, group_id, from_user_id, message)

    return jsonify({"status": "ok"}), 200


handler = app


def validate_config():
    missing = [k for k, v in (("DEBOX_APP_KEY", APP_KEY), ("DEBOX_WEBHOOK_KEY", WEBHOOK_KEY)) if not v]
    if missing:
        raise SystemExit(f"[config] 缺少必需环境变量: {', '.join(missing)}")
    if os.getenv("DEBOX_ADMIN_ID") is None:
        print("[config] 警告：DEBOX_ADMIN_ID 未设置，正在使用默认值，请确认它是真实的 from_user_id 而非邀请码。")


if __name__ == "__main__":
    validate_config()
    get_phishing_blocklist()  # 预热黑名单
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), threaded=True)
