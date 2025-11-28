import os
import sys
import threading
import time
import random
import csv
import json
from datetime import datetime
import logging
import traceback
import glob
from typing import Optional, List, Dict

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import pandas as pd

from web3 import Web3

# Polymarket CLOB Client (v0.28.0+)
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs
from py_clob_client.exceptions import PolyApiException

# ------- 登录 -------
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("PolymarketAutoTrader")

# ------- 常量 -------
POLYGON_RPC = os.environ.get("POLYGON_RPC", "https://polygon-rpc.com")
USDC_ADDRESS_POLYGON = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_DECIMALS = 6

BALANCE_POLL_INTERVAL = 60
FETCH_INTERVAL = 45  # 增加间隔防检测
BASE_URL = "https://gamma-api.polymarket.com/markets"
LATEST_CSV = "markets_latest.csv"
HISTORICAL_DIR = "markets_history"
os.makedirs(HISTORICAL_DIR, exist_ok=True)
MAX_HISTORY_FILES = 5

# ------- 伪装 User-Agent 池 -------
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
]

# ------- 全局 Session + 重试策略 -------
def create_session():
    session = requests.Session()
    retry = Retry(total=5, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

session = create_session()

# ------- 实用功能 -------
def now_ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def safe_float(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default

def get_random_headers():
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "User-Agent": random.choice(USER_AGENTS),
        "Referer": "https://polymarket.com/",
        "Origin": "https://polymarket.com",
    }

def get_proxy():
    if USE_PROXY and PROXY_POOL:
        return random.choice(PROXY_POOL)
    return None

USE_PROXY = False
PROXY_POOL = []

# ------- 区块链余额读取器 -------
class ChainReader:
    def __init__(self):
        self.rpc_url = POLYGON_RPC
        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        self.usdc = self.w3.eth.contract(
            address=Web3.to_checksum_address(USDC_ADDRESS_POLYGON),
            abi=[{
                "constant": True,
                "inputs": [{"name": "owner", "type": "address"}],
                "name": "balanceOf",
                "outputs": [{"name": "", "type": "uint256"}],
                "type": "function"
            }]
        )
        self.eoa_cache = None     # 缓存一次 EOA，避免每次都重新算

    def get_eoa_from_private_key(self, private_key: str):
        if self.eoa_cache:
            return self.eoa_cache
        try:
            eoa = Web3(Web3.HTTPProvider(self.rpc_url)).eth.account.from_key(private_key).address
            self.eoa_cache = eoa
            return eoa
        except:
            return None

    def get_usdc_balance(self, address: str, private_key: str = None):
        try:
            addr = Web3.to_checksum_address(address)
            raw = self.usdc.functions.balanceOf(addr).call()
            balance = raw / 1_000_000  # USDC 6位小数
            return True, "OK（Proxy）", balance
        except Exception as e_proxy:
            if private_key:
                eoa = self.get_eoa_from_private_key(private_key)
                if eoa:
                    try:
                        raw = self.usdc.functions.balanceOf(eoa).call()
                        balance = raw / 1_000_000
                        return True, "OK（EOA自动降级）", balance
                    except Exception as e_eoa:
                        return False, f"EOA查询失败: {e_eoa}", None
            return False, f"Proxy查询失败: {e_proxy}", None

# ------- Polymarket 客户端包装 -------
class PolymarketTrader:
    def __init__(self):
        self.client: ClobClient | None = None
        self.eoa_address: str | None = None      # ← 新增：EOA 地址（签名者）
        self.proxy_address: str | None = None

    def initialize_client(self, private_key: str, proxy_address: str, chain_id: int = 137):
        try:
            self.eoa_address = Web3(Web3.HTTPProvider(POLYGON_RPC)).eth.account.from_key(private_key).address
            self.proxy_address = Web3.to_checksum_address(proxy_address)

            self.client = ClobClient(
                host="https://clob.polymarket.com",
                key=private_key,
                chain_id=chain_id,
                signature_type=2,
                funder=self.proxy_address,
            )

            # 生成并设置 API 凭证
            creds = self.client.create_or_derive_api_creds()
            self.client.set_api_creds(creds)
            logger.info("ClobClient 初始化成功")
            return True, "初始化成功"
        except Exception as e:
            logger.error(f"ClobClient 初始化失败: {e}")
            self.client = None
            return False, str(e)

    def create_and_post_order(self, token_id, side: str, price: float, size: float, retries=3):
        if not self.client:
            return False, "客户端未初始化"

        side = side.upper()  # ← 强制大写，防止出错
        if side not in ["BUY", "SELL"]:
            return False, "side 必须是 BUY 或 SELL"

        for attempt in range(1, retries + 1):
            try:
                order_args = OrderArgs(
                    token_id=str(token_id),
                    price=price,
                    size=size,
                    side=side
                )
                resp = self.client.create_and_post_order(order_args)
                return True, resp
            except PolyApiException as e:
                if attempt == retries:
                    return False, f"API错误: {e}"
                time.sleep(1.5 ** attempt)
            except Exception as e:
                if attempt == retries:
                    return False, str(e)
                time.sleep(1.5 ** attempt)
        return False, "下单失败（重试次数已耗尽）"

# ------- 防检测市场数据抓取 -------
def fetch_markets(limit=50, offset=0, closed=False, retries=5):
    params = {"limit": limit, "offset": offset, "closed": str(closed).lower()}
    proxy = get_proxy()
    headers = get_random_headers()

    for attempt in range(1, retries + 1):
        try:
            time.sleep(random.uniform(0.8, 2.2))
            resp = session.get(
                BASE_URL,
                headers=headers,
                params=params,
                timeout=20,
                proxies={"http": proxy, "https": proxy} if proxy else None
            )
            if resp.status_code == 429:
                wait = 2 ** attempt
                logger.warning("触发限流，等待 %d 秒后重试...", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            logger.warning("第 %d 次请求失败 (offset=%d): %s", attempt, offset, e)
            if attempt < retries:
                time.sleep(2 ** attempt + random.uniform(0, 1))
            else:
                return []
    return []

def extract_token_data(markets):
    records = []
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    for market in markets:
        market_id = market.get("id", "N/A")
        question = market.get("question", "N/A")
        outcomes = (json.loads(market.get("outcomes", "[]"))
                    if isinstance(market.get("outcomes"), str)
                    else market.get("outcomes", []))
        token_ids = (json.loads(market.get("clobTokenIds", "[]"))
                     if isinstance(market.get("clobTokenIds"), str)
                     else market.get("clobTokenIds", []))
        prices = (json.loads(market.get("outcomePrices", "[]"))
                  if isinstance(market.get("outcomePrices"), str)
                  else market.get("outcomePrices", []))
        for i, outcome in enumerate(outcomes):
            token_id = token_ids[i] if i < len(token_ids) else None
            price = prices[i] if i < len(prices) else None
            records.append({
                "market_id": market_id,
                "market_question": question,
                "outcome": outcome,
                "token_id": token_id,
                "price": price,
                "timestamp": now
            })
    return records

def update_latest_csv(records, filename=LATEST_CSV):
    try:
        df_existing = pd.read_csv(filename)
    except FileNotFoundError:
        df_existing = pd.DataFrame(columns=[
            "market_id", "market_question", "outcome", "token_id", "price", "timestamp"
        ])
    except Exception as e:
        logger.error("读取最新 CSV 失败: %s", e)
        df_existing = pd.DataFrame(columns=[
            "market_id", "market_question", "outcome", "token_id", "price", "timestamp"
        ])

    df_new = pd.DataFrame(records)
    df_existing.set_index(["market_id", "outcome"], inplace=True)
    df_new.set_index(["market_id", "outcome"], inplace=True)

    changed = pd.DataFrame()
    for idx in df_new.index:
        if idx not in df_existing.index:
            changed = pd.concat([changed, df_new.loc[[idx]]])
        else:
            old = df_existing.loc[idx]
            new = df_new.loc[idx]
            if (safe_float(old["price"]) != safe_float(new["price"]) or
                    str(old["token_id"]) != str(new["token_id"])):
                changed = pd.concat([changed, df_new.loc[[idx]]])

    df_existing.update(df_new)
    df_combined = pd.concat([df_existing, df_new[~df_new.index.isin(df_existing.index)]])
    df_combined.reset_index(inplace=True)
    try:
        df_combined.to_csv(filename, index=False)
        logger.info("最新 CSV 已更新，总记录数：%d", len(df_combined))
    except Exception as e:
        logger.error("写入最新 CSV 失败: %s", e)
    return changed.reset_index()

def save_historical_csv(records):
    if records.empty:
        return
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = os.path.join(HISTORICAL_DIR, f"markets_{timestamp}.csv")
    try:
        records.to_csv(filename, index=False)
        logger.info("历史 CSV 已保存: %s", filename)
    except Exception as e:
        logger.error("保存历史 CSV 失败: %s", e)

    all_files = sorted(glob.glob(os.path.join(HISTORICAL_DIR, "markets_*.csv")))
    if len(all_files) > MAX_HISTORY_FILES:
        for f in all_files[:-MAX_HISTORY_FILES]:
            try:
                os.remove(f)
                logger.info("已删除旧历史文件: %s", f)
            except Exception as e:
                logger.error("删除旧文件失败: %s, %s", f, e)

def fetch_all_and_update(app):
    offset = 0
    batch_records = []
    limit = 50
    batch_size = 100
    while True:
        markets = fetch_markets(limit=limit, offset=offset, closed=False)
        if not markets:
            break
        records = extract_token_data(markets)
        batch_records.extend(records)
        if len(batch_records) >= batch_size:
            changed = update_latest_csv(batch_records)
            save_historical_csv(changed)
            app.root.after(0, app.update_market_listbox, changed)
            batch_records = []
        if len(markets) < limit:
            break
        offset += limit
        time.sleep(random.uniform(1.0, 3.0))
    if batch_records:
        changed = update_latest_csv(batch_records)
        save_historical_csv(changed)
        app.root.after(0, app.update_market_listbox, changed)

# ------- 图形化应用程序 -------
class PolymarketAutoTraderApp:
    def __init__(self, root):
        self.root = root
        root.title("Polymarket自动交易工具")
        root.geometry("800x700")

        self.chain_reader = ChainReader()
        self.trader = PolymarketTrader()

        self.private_key = None
        self.proxy_address = None
        self.chain_id = 137
        self.bal_update_interval = BALANCE_POLL_INTERVAL
        self._balance_updater_thread = None
        self._balance_updater_stop = threading.Event()
        self.current_balance = None
        self.simulation_mode = tk.BooleanVar(value=True)

        self.order_log = []
        self.csv_export_path = None

        self.market_records = []
        self._market_updater_thread = None
        self._market_updater_stop = threading.Event()

        self._trading_tasks = []

        self._build_ui()

    def _build_ui(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self._build_tab_login(nb)
        self._build_tab_params(nb)
        self._build_tab_market(nb)
        self._build_tab_log(nb)
        self._build_tab_export(nb)

        status_frame = ttk.Frame(self.root)
        status_frame.pack(fill=tk.X, padx=8, pady=4)
        self.lbl_anti_detect = ttk.Label(
            status_frame,
            text="防检测已启用(随机UA + 延迟 + 重试)",
            foreground="green",
            font=("Helvetica", 9, "italic")
        )
        self.lbl_anti_detect.pack(side=tk.LEFT)

    def _build_tab_login(self, nb):
        frm = ttk.Frame(nb)
        nb.add(frm, text="登录 & 余额")
        lbl_priv = ttk.Label(frm, text="私钥:")
        lbl_priv.grid(row=0, column=0, sticky=tk.W, padx=6, pady=6)
        self.entry_priv = tk.Entry(frm, width=90, show="*")
        self.entry_priv.grid(row=0, column=1, columnspan=3, sticky=tk.W+tk.E, padx=6, pady=6)

        lbl_proxy = ttk.Label(frm, text="Funder/Proxy (必需):")
        lbl_proxy.grid(row=1, column=0, sticky=tk.W, padx=6, pady=6)
        self.entry_proxy = tk.Entry(frm, width=60)
        self.entry_proxy.grid(row=1, column=1, sticky=tk.W, padx=6, pady=6)

        lbl_chain = ttk.Label(frm, text="链 ID:")
        lbl_chain.grid(row=1, column=2, sticky=tk.W, padx=6, pady=6)
        self.entry_chain = tk.Entry(frm, width=12)
        self.entry_chain.insert(0, str(self.chain_id))
        self.entry_chain.grid(row=1, column=3, sticky=tk.W, padx=6, pady=6)

        btn_login = ttk.Button(frm, text="登录", command=self.on_login_click)
        btn_login.grid(row=2, column=0, columnspan=4, padx=6, pady=8)

        self.lbl_balance = ttk.Label(frm, text="Proxy-usdc 余额：未知")
        self.lbl_balance.grid(row=3, column=0, columnspan=4, sticky=tk.W, padx=6, pady=6)

        chk_sim = ttk.Checkbutton(frm, text="模拟模式", variable=self.simulation_mode)
        chk_sim.grid(row=4, column=0, columnspan=2, sticky=tk.W, padx=6, pady=6)

        self.lbl_status = ttk.Label(frm, text="状态: 未登录", foreground="red")
        self.lbl_status.grid(row=5, column=0, columnspan=4, sticky=tk.W, padx=6, pady=6)

    def _build_tab_params(self, nb):
        frm = ttk.Frame(nb)
        nb.add(frm, text="参数设置")

        ttk.Label(frm, text="最小买入金额（USDC）:").grid(row=0, column=0, sticky=tk.W, padx=6, pady=6)
        self.entry_min_amount = tk.Entry(frm, width=12)
        self.entry_min_amount.insert(0, "1")
        self.entry_min_amount.grid(row=0, column=1, sticky=tk.W, padx=6, pady=6)

        ttk.Label(frm, text="最大买入金额（USDC）:").grid(row=0, column=2, sticky=tk.W, padx=6, pady=6)
        self.entry_max_amount = tk.Entry(frm, width=12)
        self.entry_max_amount.insert(0, "5")
        self.entry_max_amount.grid(row=0, column=3, sticky=tk.W, padx=6, pady=6)

        ttk.Label(frm, text="最小交易概率（%）:").grid(row=1, column=0, sticky=tk.W, padx=6, pady=6)
        self.entry_min_prob = tk.Entry(frm, width=8)
        self.entry_min_prob.insert(0, "10")
        self.entry_min_prob.grid(row=1, column=1, sticky=tk.W, padx=6, pady=6)

        ttk.Label(frm, text="最大交易概率（%）:").grid(row=1, column=2, sticky=tk.W, padx=6, pady=6)
        self.entry_max_prob = tk.Entry(frm, width=8)
        self.entry_max_prob.insert(0, "90")
        self.entry_max_prob.grid(row=1, column=3, sticky=tk.W, padx=6, pady=6)

        ttk.Label(frm, text="交易次数:").grid(row=2, column=0, sticky=tk.W, padx=6, pady=6)
        self.entry_times = tk.Entry(frm, width=8)
        self.entry_times.insert(0, "3")
        self.entry_times.grid(row=2, column=1, sticky=tk.W, padx=6, pady=6)

        ttk.Label(frm, text="方向:").grid(row=2, column=2, sticky=tk.W, padx=6, pady=6)
        self.side_var = tk.StringVar(value="BUY")
        ttk.Radiobutton(frm, text="买入", value="BUY", variable=self.side_var).grid(row=2, column=3, sticky=tk.W)
        ttk.Radiobutton(frm, text="卖出", value="SELL", variable=self.side_var).grid(row=2, column=4, sticky=tk.W)

        btn_run = ttk.Button(frm, text="运行自动交易", command=self.on_run_trading)
        btn_run.grid(row=3, column=0, columnspan=2, padx=6, pady=10)

        btn_stop = ttk.Button(frm, text="停止所有交易任务", command=self.stop_all_trading)
        btn_stop.grid(row=3, column=2, columnspan=2, padx=6, pady=10)

        ttk.Label(frm, text="最小下单间隔（秒）:").grid(row=4, column=0, sticky=tk.W, padx=6, pady=6)
        self.entry_min_order_interval = tk.Entry(frm, width=8)
        self.entry_min_order_interval.insert(0, "0.8")
        self.entry_min_order_interval.grid(row=4, column=1, sticky=tk.W, padx=6, pady=6)

        ttk.Label(frm, text="最大下单间隔（秒）:").grid(row=4, column=2, sticky=tk.W, padx=6, pady=6)
        self.entry_max_order_interval = tk.Entry(frm, width=8)
        self.entry_max_order_interval.insert(0, "2.5")
        self.entry_max_order_interval.grid(row=4, column=3, sticky=tk.W, padx=6, pady=6)

    def _build_tab_market(self, nb):
        frm = ttk.Frame(nb)
        nb.add(frm, text="市场数据")

        btn_refresh = ttk.Button(frm, text="立即刷新", command=self.on_refresh_markets)
        btn_refresh.pack(anchor=tk.NW, padx=6, pady=6)

        search_frm = ttk.Frame(frm)
        search_frm.pack(fill=tk.X, padx=6, pady=4)
        ttk.Label(search_frm, text="搜索:").pack(side=tk.LEFT)
        self.entry_market_search = tk.Entry(search_frm, width=60)
        self.entry_market_search.pack(side=tk.LEFT, padx=6)
        self.entry_market_search.bind("<KeyRelease>", lambda e: self.update_market_listbox())

        listbox_frm = ttk.Frame(frm)
        listbox_frm.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        scrollbar = ttk.Scrollbar(listbox_frm)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.lst_markets = tk.Listbox(listbox_frm, height=20, yscrollcommand=scrollbar.set)
        self.lst_markets.pack(fill=tk.BOTH, expand=True)
        scrollbar.config(command=self.lst_markets.yview)

        changes_frm = ttk.LabelFrame(frm, text="价格变化监控")
        changes_frm.pack(fill=tk.X, padx=6, pady=6)
        self.txt_price_changes = scrolledtext.ScrolledText(changes_frm, height=6)
        self.txt_price_changes.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self.txt_price_changes.config(state=tk.DISABLED)

    def _build_tab_log(self, nb):
        frm = ttk.Frame(nb)
        nb.add(frm, text="日志")
        self.txt_log = scrolledtext.ScrolledText(frm, height=30)
        self.txt_log.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self.txt_log.config(state=tk.DISABLED)
        btn_export = ttk.Button(frm, text="导出订单 CSV", command=self.export_csv_dialog)
        btn_export.pack(anchor=tk.SE, padx=6, pady=6)

    def _build_tab_export(self, nb):
        frm = ttk.Frame(nb)
        nb.add(frm, text="导出设置")
        ttk.Label(frm, text="CSV 导出路径（可选）:").grid(row=0, column=0, sticky=tk.W, padx=6, pady=6)
        self.entry_csv_path = tk.Entry(frm, width=80)
        self.entry_csv_path.grid(row=0, column=1, padx=6, pady=6)
        btn_choose = ttk.Button(frm, text="选择...", command=self.choose_csv_path)
        btn_choose.grid(row=0, column=2, padx=6, pady=6)

    # ------- 通用方法 -------
    def log(self, message: str):
        ts = now_ts()
        text = f"[{ts}] {message}"
        logger.info(message)
        try:
            self.txt_log.config(state=tk.NORMAL)
            self.txt_log.insert(tk.END, text + "\n")
            self.txt_log.see(tk.END)
            self.txt_log.config(state=tk.DISABLED)
        except Exception:
            pass

    def log_price_change(self, change: dict):
        try:
            market = str(change.get('market_question', '')[:50]) + "..."
            outcome = str(change.get('outcome', ''))
            old_price = safe_float(change.get('old_price'))
            new_price = safe_float(change.get('price'))
            old_str = f"{old_price:.4f}" if old_price is not None else "N/A"
            new_str = f"{new_price:.4f}" if new_price is not None else "N/A"
            text = f"{market} | {outcome} | {old_str} → {new_str}\n"
            self.txt_price_changes.config(state=tk.NORMAL)
            self.txt_price_changes.insert(tk.END, text)
            self.txt_price_changes.see(tk.END)
            self.txt_price_changes.config(state=tk.DISABLED)
        except Exception as e:
            logger.error("记录价格变化失败: %s", e)

    def choose_csv_path(self):
        p = filedialog.asksaveasfilename(defaultextension=".csv",
                                         filetypes=[("CSV files", "*.csv")])
        if p:
            self.entry_csv_path.delete(0, tk.END)
            self.entry_csv_path.insert(0, p)
            self.csv_export_path = p

    def export_csv_dialog(self):
        if not self.order_log:
            messagebox.showinfo("导出", "当前没有订单记录可导出")
            return
        p = self.entry_csv_path.get().strip() or filedialog.asksaveasfilename(defaultextension=".csv")
        if not p:
            return
        try:
            keys = list(self.order_log[0].keys())
            with open(p, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=keys)
                writer.writeheader()
                for row in self.order_log:
                    writer.writerow(row)
            messagebox.showinfo("导出成功", f"已导出到 {p}")
            self.log(f"导出 CSV 到 {p}")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))
            self.log(f"导出失败：{e}")

    # -------- 登录与余额 --------
    def on_login_click(self):
        pk = self.entry_priv.get().strip()
        proxy = self.entry_proxy.get().strip()
        if not pk.startswith("0x"):
            pk = "0x" + pk
        if not pk or len(pk) != 66:
            messagebox.showerror("错误", "私钥格式错误（66位十六进制）")
            return
        if not proxy or not Web3.is_checksum_address(proxy) and not Web3.is_address(proxy):
            messagebox.showerror("错误", "Proxy 地址格式错误")
            return

        self.private_key = pk
        self.proxy_address = Web3.to_checksum_address(proxy)

        def work():
            self.log("正在初始化 ClobClient…")
            ok, msg = self.trader.initialize_client(self.private_key, self.proxy_address, self.chain_id)
            if not ok:
                self.lbl_status.config(text=f"状态: 登录失败", foreground="red")
                self.log(f"登录失败: {msg}")
                return
            self.lbl_status.config(text="状态: 已登录（EOA签名+Proxy交易）", foreground="green")
            self.log(f"登录成功！EOA: {self.trader.eoa_address}")
            self.log(f"Proxy: {self.proxy_address}")
            self.start_balance_updater()
            self.start_market_updater()

        threading.Thread(target=work, daemon=True).start()

    def start_balance_updater(self):
        self.stop_balance_updater()
        self._balance_updater_stop.clear()
        self._balance_updater_thread = threading.Thread(target=self._balance_update_loop,
                                                        daemon=True)
        self._balance_updater_thread.start()
        self.log(f"余额自动更新已启动（每 {self.bal_update_interval}s）")

    def stop_balance_updater(self):
        if self._balance_updater_thread and self._balance_updater_thread.is_alive():
            self._balance_updater_stop.set()
            self._balance_updater_thread.join(timeout=2)
        self._balance_updater_thread = None
        self._balance_updater_stop.clear()

    def _balance_update_loop(self):
        while not self._balance_updater_stop.is_set():
            try:
                if not self.proxy_address:
                    self.root.after(0, lambda: self.lbl_balance.config(
                        text="Proxy USDC 余额：未设置 Proxy 地址"))
                    time.sleep(self.bal_update_interval)
                    continue
                ok, msg, bal = self.chain_reader.get_usdc_balance(self.proxy_address,private_key=self.private_key)
                if ok:
                    self.current_balance = bal
                    self.root.after(0, lambda b=bal: self.lbl_balance.config(
                        text=f"Proxy USDC 余额: {b:.6f}"))
                    self.log(f"Proxy 余额更新: {bal:.6f} USDC")
                else:
                    self.root.after(0, lambda: self.lbl_balance.config(
                        text="Proxy USDC 余额：查询失败"))
                    self.log("Proxy 余额读取失败: " + str(msg))
                time.sleep(self.bal_update_interval)
            except Exception as e:
                self.log("余额更新循环错误: " + str(e))
                time.sleep(self.bal_update_interval)

    # -------- 市场更新 --------
    def start_market_updater(self):
        self.stop_market_updater()
        self._market_updater_stop.clear()
        self._market_updater_thread = threading.Thread(target=self._market_update_loop,
                                                       daemon=True)
        self._market_updater_thread.start()
        self.log(f"市场数据自动抓取已启动（每 {FETCH_INTERVAL//60} 分钟）")

    def stop_market_updater(self):
        if self._market_updater_thread and self._market_updater_thread.is_alive():
            self._market_updater_stop.set()
            self._market_updater_thread.join(timeout=2)
        self._market_updater_thread = None
        self._market_updater_stop.clear()

    def _market_update_loop(self):
        while not self._market_updater_stop.is_set():
            try:
                fetch_all_and_update(self)
                for _ in range(FETCH_INTERVAL):
                    if self._market_updater_stop.is_set():
                        break
                    time.sleep(1)
            except Exception as e:
                self.log(f"市场数据抓取异常: {e}")
                time.sleep(60)

    def on_refresh_markets(self):
        threading.Thread(target=lambda: fetch_all_and_update(self), daemon=True).start()

    def update_market_listbox(self, changed=None):
        search = self.entry_market_search.get().strip().lower()
        self.lst_markets.delete(0, tk.END)
        try:
            df = pd.read_csv(LATEST_CSV)
            self.market_records = df.to_dict('records')
        except Exception as e:
            logger.error("读取最新 CSV 失败: %s", e)
            self.market_records = []

        for rec in self.market_records:
            q = str(rec.get("market_question", ""))[:100]
            o = str(rec.get("outcome", ""))
            p = safe_float(rec.get("price"))
            price_str = f" | 价格: {p:.4f}" if p is not None else " | 价格: N/A"
            display = f"{q} | {o}{price_str}"
            if search and search not in display.lower():
                continue
            if len(display) > 150:
                display = display[:150] + "..."
            self.lst_markets.insert(tk.END, display)

        if changed is not None and not changed.empty:
            for _, row in changed.iterrows():
                self.root.after(0, self.log_price_change, row.to_dict())

    # -------- 自动交易 --------
    def on_run_trading(self):
        try:
            min_amt = float(self.entry_min_amount.get().strip())
            max_amt = float(self.entry_max_amount.get().strip())
            min_prob_pct = float(self.entry_min_prob.get().strip())
            max_prob_pct = float(self.entry_max_prob.get().strip())
            times = int(self.entry_times.get().strip())
            min_interval = float(self.entry_min_order_interval.get().strip())
            max_interval = float(self.entry_max_order_interval.get().strip())
        except Exception:
            messagebox.showerror("参数错误", "请检查所有输入格式")
            return

        if (min_amt <= 0 or max_amt < min_amt or min_prob_pct < 0 or
                max_prob_pct > 100 or times <= 0):
            messagebox.showerror("参数错误", "参数值不合法")
            return

        min_prob = min_prob_pct / 100.0
        max_prob = max_prob_pct / 100.0
        side = self.side_var.get()
        sim = self.simulation_mode.get()

        self.log(f"启动自动交易：概率[{min_prob:.2%}~{max_prob:.2%}] "
                 f"金额[{min_amt}~{max_amt}] 次数{times} 方向{side} 模拟={sim}")

        task = threading.Thread(
            target=self._trading_task,
            args=(min_amt, max_amt, min_prob, max_prob, times,
                  side, min_interval, max_interval, sim),
            daemon=True
        )
        self._trading_tasks.append(task)
        task.start()

    def stop_all_trading(self):
        self.log("正在停止所有交易任务...")
        self._trading_tasks = []

    def _trading_task(self, min_amt, max_amt, min_prob, max_prob,
                      times, side, min_interval, max_interval, simulation):
        try:
            if not os.path.exists(LATEST_CSV):
                self.log("市场数据文件不存在，请等待刷新")
                return

            df = pd.read_csv(LATEST_CSV)
            df = df[pd.notna(df['price']) & pd.notna(df['token_id'])]
            df['price'] = df['price'].astype(float)
            candidates = df[(df['price'] >= min_prob) & (df['price'] <= max_prob)].copy()

            if candidates.empty:
                self.log(f"无符合概率区间的市场（{min_prob:.2%}~{max_prob:.2%}）")
                return

            # 新增：获取每个 token 的最小下单量（关键！）
            def get_min_size(token_id: str) -> int:
                try:
                    url = f"https://clob.polymarket.com/markets/{token_id}"
                    resp = session.get(url, headers=get_random_headers(), timeout=10)
                    if resp.status_code == 200:
                        data = resp.json()
                        min_size = data.get("minimumOrderSize", 1)
                        # 有时字段是字符串
                        return int(float(str(min_size)))
                except:
                    pass
                return 1  # 失败默认 1

            valid_tokens = []
            self.log("正在扫描每个市场的真实最小下单量（min_size），请稍等 10~20 秒...")

            for idx, row in candidates.iterrows():
                token_id = str(row['token_id'])
                price = row['price']
                question = row['market_question']
                outcome = row['outcome']

                min_size = get_min_size(token_id)  # 关键：动态获取
                max_possible_size = max_amt / price
                if max_possible_size < min_size:
                    continue  # 即使给再多钱也买不到起订量 → 直接放弃

                # 计算能买的最大整数股数
                max_affordable_size = int(max_amt / price)
                actual_max_size = max_affordable_size // 1  # 向下取整
                if actual_max_size < min_size:
                    continue

                valid_tokens.append({
                    'token_id': token_id,
                    'price': price,
                    'question': question[:80],
                    'outcome': outcome,
                    'min_size': min_size,
                    'max_size': actual_max_size,
                    'row': row
                })

            if not valid_tokens:
                self.log("没有找到满足「最小下单量」要求的合约！")
                self.log("可能原因：")
                self.log("   1. 金额设置太低（大部分热门市场已要求 min_size=5）")
                self.log("   2. 概率区间内全是高概率/低概率市场（更容易被限额）")
                self.log("建议：最低金额 ≥ 10 USDC，或放宽概率区间")
                return

            self.log(f"扫描完成！找到 {len(valid_tokens)} 个真实可交易合约（已遵守 min_size）")

            # 余额检查
            balance = None
            if not simulation and self.proxy_address:
                ok, _, bal = self.chain_reader.get_usdc_balance(self.proxy_address)
                if ok:
                    balance = bal
                    self.log(f"当前可用余额: {balance:.6f} USDC")
                else:
                    self.log("无法读取余额，跳过余额检查")
                    balance = None

            success_count = 0
            for i in range(times):
                if not valid_tokens:
                    break

                chosen = random.choice(valid_tokens)
                price = chosen['price']
                token_id = chosen['token_id']
                min_size = chosen['min_size']
                max_size = chosen['max_size']

                # 随机整数股数（必须 ≥ min_size）
                size = random.randint(min_size, max_size)
                amt = round(price * size, 6)

                # 余额二次检查
                if balance is not None and amt > balance:
                    max_can_buy = int(balance / price * 0.95)
                    if max_can_buy < min_size:
                        self.log("余额不足以购买起订量，停止交易")
                        break
                    size = max(min_size, max_can_buy - max_can_buy % 5)  # 尽量对齐 5 的倍数
                    amt = round(price * size, 6)

                self.log(f"第{i + 1}/{times} | {chosen['question']} | {chosen['outcome']} "
                         f"| 价格 {price:.4f} | 下单 {size} 股（≥{min_size}）| 花费 {amt:.6f} USDC")

                order_record = {
                    "ts": now_ts(),
                    "market_question": chosen['row']['market_question'],
                    "outcome": chosen['outcome'],
                    "token_id": token_id,
                    "price": price,
                    "amount_usdc": amt,
                    "size": size,
                    "min_size_required": min_size,
                    "side": side,
                    "simulation": simulation,
                    "result": "pending"
                }

                if simulation:
                    self.log("模拟下单成功")
                    order_record["result"] = "simulated_success"
                    success_count += 1
                else:
                    ok, resp = self.trader.create_and_post_order(
                        token_id=token_id, side=side, price=price, size=float(size)
                    )
                    if ok:
                        self.log(f"实盘下单成功！Order ID: {resp.get('order_id', 'N/A')}")
                        order_record["result"] = resp
                        success_count += 1
                        if balance is not None:
                            balance -= amt
                    else:
                        self.log(f"下单失败: {resp}")
                        order_record["result"] = {"error": str(resp)}

                self.order_log.append(order_record)

                if i < times - 1:
                    wait = random.uniform(min_interval, max_interval)
                    self.log(f"等待 {wait:.2f} 秒...")
                    time.sleep(wait)

            self.log(f"本轮交易完成：成功 {success_count}/{times}")

        except Exception as e:
            tb = traceback.format_exc()
            self.log(f"交易任务崩溃: {e}\n{tb}")

# ------- main -------
def main():
    root = tk.Tk()
    app = PolymarketAutoTraderApp(root)

    def on_closing():
        if messagebox.askokcancel("退出", "确定要退出吗？"):
            try:
                app.shutdown()
            finally:
                root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_closing)
    root.mainloop()


if __name__ == "__main__":
    main()
