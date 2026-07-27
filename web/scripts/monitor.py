#!/usr/bin/env python3
"""A股盘中实时监控 - 每5分钟推送飞书"""

import json
import os
import subprocess
import time
import requests
from datetime import datetime, time as dtime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
UNIVERSE_PATH = Path(
    os.environ.get(
        "MONITOR_UNIVERSE_PATH",
        REPO_ROOT / "web" / "data" / "universe.json",
    )
)
FEISHU_CHAT = os.environ.get("FEISHU_CHAT", "").strip()

# 交易时间
MORNING_START, MORNING_END = dtime(9, 25), dtime(11, 35)
AFTERNOON_START, AFTERNOON_END = dtime(12, 55), dtime(15, 5)


def is_market_open():
    now = datetime.now().time()
    return (MORNING_START <= now <= MORNING_END) or (AFTERNOON_START <= now <= AFTERNOON_END)


def load_universe():
    with open(UNIVERSE_PATH) as f:
        return json.load(f).get('entries', [])


def fetch_tencent(codes):
    """腾讯行情接口"""
    url = f'http://qt.gtimg.cn/q={",".join(codes)}'
    r = requests.get(url, timeout=10)
    results = []
    for raw in r.text.strip().split(';'):
        raw = raw.strip().strip("v_").strip("'").strip()
        if not raw:
            continue
        p = raw.split('~')
        if len(p) < 35:
            continue
        price = float(p[3])
        prev = float(p[4])
        chg = round((price - prev) / prev * 100, 2)
        limit_up = float(p[33])
        is_limit_up = abs(price - limit_up) < 0.01
        results.append({
            'code': p[2], 'name': p[1], 'price': price, 'prev': prev, 'chg': chg,
            'open': float(p[5]),
            'high': float(p[31]) if len(p) > 31 else price,
            'low': float(p[32]) if len(p) > 32 else price,
            'turnover': float(p[38]) if len(p) > 38 else 0,
            'amount': float(p[37]) if len(p) > 37 else 0,
            'is_limit_up': is_limit_up,
        })
    return results


def fetch_indices():
    idx_codes = ['sh000001', 'sz399001', 'sz399006', 'sh000688']
    url = f'http://qt.gtimg.cn/q={",".join(idx_codes)}'
    r = requests.get(url, timeout=10)
    results = {}
    for raw in r.text.strip().split(';'):
        raw = raw.strip().strip("v_").strip("'").strip()
        if not raw:
            continue
        p = raw.split('~')
        price = float(p[3])
        prev = float(p[4])
        results[p[2]] = {
            'name': p[1], 'price': price,
            'chg': round((price - prev) / prev * 100, 2)
        }
    return results


def build_message(all_data, indices, stocks):
    now = datetime.now().strftime("%H:%M")

    # 指数
    idx_map = {
        '000001': '上证', '399001': '深证',
        '399006': '创业板', '000688': '科创50'
    }
    market_str = " | ".join([
        f"{idx_map.get(code, code)} {v['price']:.2f} ({v['chg']:+.2f}%)"
        for code, v in indices.items() if code in idx_map
    ])

    # 统计
    up_count = sum(1 for d in all_data if d['chg'] > 0)
    down_count = sum(1 for d in all_data if d['chg'] < 0)
    flat_count = sum(1 for d in all_data if d['chg'] == 0)
    lups = [d for d in all_data if d['is_limit_up']]

    # 真实机会: 非涨停, -3%~+2%, 换手>2%, 排除南大光电(持有观察)
    candidates = [d for d in all_data
                  if not d['is_limit_up']
                  and -3.0 <= d['chg'] <= 2.0
                  and d['turnover'] > 2
                  and d['code'] != '300346']
    candidates.sort(key=lambda x: -x['amount'])

    # 南大光电
    nd = next((d for d in all_data if d['code'] == '300346'), {})
    nd_price = nd.get('price', 0)
    nd_chg = nd.get('chg', 0)

    # 构建消息
    msg = f"截至 {now}\n\n📊 大盘\n{market_str}\n\n🔴 涨停 {len(lups)} 只"
    if lups:
        msg += f"：{', '.join(d['name'] for d in lups)}"
    msg += "\n— 这些不追\n"
    msg += f"\n📈 Universe: 涨{up_count} | 跌{down_count} | 平{flat_count}"

    if candidates:
        msg += f"\n\n🔍 真实可关注"
        for d in candidates[:3]:
            theme = next(
                (s.get('theme', '') for s in stocks if s['symbol'] == d['code']), '')
            direction = "红盘承接" if d['price'] > d['open'] else "绿盘回踩"
            msg += f"\n\n• {d['name']}({d['code']}) {theme}\n  现价 {d['price']:.2f} ({d['chg']:+.2f}%) 换手{d['turnover']:.1f}% {direction}\n  高 {d['high']:.2f} / 低 {d['low']:.2f} 额 {d['amount']:.0f}万"

    msg += f"\n\n⚠️ 南大光电: {nd_price:.2f} ({nd_chg:+.2f}%) — 持有观察，不加仓\n\n涨停股已剔除，只输出真实买点区间。每5分钟刷新。"

    return msg


def send_to_feishu(msg):
    if not FEISHU_CHAT:
        raise RuntimeError("FEISHU_CHAT is required, for example feishu:oc_xxx")
    r = subprocess.run(
        ["hermes", "send", "--to", FEISHU_CHAT, msg],
        capture_output=True, text=True, timeout=15
    )
    return r.stdout.strip() or r.stderr.strip()


if __name__ == "__main__":
    stocks = load_universe()
    print(f"🔍 盘中监控已启动 | Universe: {len(stocks)}只 | 5分钟轮询")

    while True:
        if not is_market_open():
            time.sleep(60)
            continue

        try:
            # 分批拉取（每批40个）
            all_data = []
            for i in range(0, len(stocks), 40):
                batch = stocks[i:i+40]
                codes = [
                    f"{'sh' if s['symbol'].startswith('6') else 'sz'}{s['symbol']}"
                    for s in batch
                ]
                all_data.extend(fetch_tencent(codes))

            indices = fetch_indices()
            msg = build_message(all_data, indices, stocks)
            result = send_to_feishu(msg)
            print(f"[{datetime.now().strftime('%H:%M')}] 推送: {result}")

        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M')}] 错误: {e}")

        time.sleep(300)
