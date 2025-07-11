# funded_risk.py

from config import START_BALANCE, DAILY_MAX_LOSS_PERCENT, MAX_TOTAL_LOSS_PERCENT, FUNDED_MODE
import MetaTrader5 as mt5
import datetime
import pytz
import pandas as pd

# --- Daily Loss Manager (for live trading) ---
class DailyLossManager:
    def __init__(self):
        self.initial_balance = START_BALANCE
        self.max_daily_loss = self.initial_balance * (DAILY_MAX_LOSS_PERCENT / 100)
        self.today = self.get_berlin_now().date()

    def get_berlin_now(self):
        berlin = pytz.timezone('Europe/Berlin')
        return datetime.datetime.now(berlin)

    def update_day(self):
        now = self.get_berlin_now()
        if now.date() != self.today:
            self.today = now.date()

    def get_current_daily_loss(self):
        self.update_day()

        utc_from = datetime.datetime.combine(self.today, datetime.time(0, 0))
        utc_from = utc_from.replace(tzinfo=pytz.UTC)

        now = self.get_berlin_now()

        deals = mt5.history_deals_get(utc_from, now)
        closed_pnl = sum(deal.profit for deal in deals) if deals else 0.0

        positions = mt5.positions_get()
        floating_pnl = sum(pos.profit for pos in positions) if positions else 0.0

        return closed_pnl + floating_pnl

    def should_stop_bot(self):
        if not FUNDED_MODE:
            return False

        total_loss = self.get_current_daily_loss()
        print(f"[RISK CHECK] Today's closed + floating P/L = {total_loss:.2f} USD (Limit: -{self.max_daily_loss:.2f} USD)")
        return total_loss <= -self.max_daily_loss

# --- Backtest Risk Manager (for simulation) ---
class BacktestRiskManager:
    def __init__(self):
        self.start_balance = START_BALANCE
        self.daily_loss_limit = self.start_balance * (DAILY_MAX_LOSS_PERCENT / 100)
        self.max_total_loss = self.start_balance * (MAX_TOTAL_LOSS_PERCENT / 100)
        self.current_day = None
        self.day_start_balance = None

    def update_day(self, timestamp, balance):
        if self.current_day != timestamp.date():
            self.current_day = timestamp.date()
            self.day_start_balance = balance

    def is_daily_loss_exceeded(self, balance):
        if self.day_start_balance is None:
            self.day_start_balance = balance
        loss_today = self.day_start_balance - balance
        return loss_today >= self.daily_loss_limit

    def is_max_total_loss_exceeded(self, balance):
        total_loss = self.start_balance - balance
        return total_loss >= self.max_total_loss
