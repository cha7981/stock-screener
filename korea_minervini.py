import os
import time
import random
import traceback
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests
from pykrx import stock


# ============================================================
# K-Minervini v1 Settings
# ============================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

# GitHub Actions runtime control
MAX_UNIVERSE = int(os.environ.get("KOREA_MAX_UNIVERSE", "600"))  # 유동성 상위 분석 종목 수
SLEEP_SEC = float(os.environ.get("KOREA_SLEEP_SEC", "0.15"))

# Liquidity / price filters
MIN_PRICE = 2000
MIN_AVG_TURNOVER = 3_000_000_000      # 20일 평균 거래대금 30억
GOOD_TURNOVER = 5_000_000_000         # 50억
STRONG_TURNOVER = 10_000_000_000      # 100억

# Score thresholds
BREAKOUT_SCORE = 80
PULLBACK_SCORE = 75
WATCH_SCORE = 70

# Pullback settings
PULLBACK_MIN_DD = -12.0
PULLBACK_MAX_DD = -3.0
NEAR_MA20_PCT = 4.0
NEAR_MA60_PCT = 6.0

# Breakout settings
PIVOT_LOOKBACK = 30
BREAKOUT_NEAR_PCT = 3.0
MAX_RISK_PCT = 8.0

KST = timezone(timedelta(hours=9))


# ============================================================
# Telegram
# ============================================================
def send_telegram_message(message: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("❌ TELEGRAM_TOKEN 또는 CHAT_ID가 없습니다. GitHub Secrets를 확인하세요.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    try:
        # Telegram sendMessage 길이 제한 대응
        chunks = []
        text = message

        while len(text) > 3900:
            cut = text.rfind("\n", 0, 3900)
            if cut == -1:
                cut = 3900
            chunks.append(text[:cut])
            text = text[cut:].lstrip()

        chunks.append(text)

        ok = True

        for chunk in chunks:
            res = requests.post(
                url,
                json={
                    "chat_id": CHAT_ID,
                    "text": chunk,
                },
                timeout=20,
            )

            if res.status_code != 200
