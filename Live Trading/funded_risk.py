from datetime import datetime, time
import pytz
import MetaTrader5 as mt5
from config import START_BALANCE, DAILY_MAX_LOSS_PERCENT, FUNDED_MODE
from utils import log_info, log_error

class DailyLossManager:
    def __init__(self):
        self.initial_balance = START_BALANCE
        self.max_daily_loss = self.initial_balance * (DAILY_MAX_LOSS_PERCENT / 100)
        self.timezone = pytz.timezone("Europe/Berlin")
        self.today = self.get_berlin_now().date()

    def get_berlin_now(self):
        return datetime.now(self.timezone)

    def update_day(self):
        today = self.get_berlin_now().date()
        if today != self.today:
            self.today = today
            log_info("New day detected — resetting daily loss tracking.")

    def get_current_daily_loss(self):
        # Always calculate fresh CE(S)T midnight from live Berlin time
        berlin_now = self.get_berlin_now()
        midnight_berlin = self.timezone.localize(datetime.combine(berlin_now.date(), time.min))
        utc_from = midnight_berlin.astimezone(pytz.UTC)

        # Get closed trades today
        deals = mt5.history_deals_get(utc_from, datetime.utcnow())
        closed_pnl = 0.0

        if deals:
            for deal in deals:
                if deal.type in [1, 2]:  # BUY or SELL only
                    closed_pnl += deal.profit + deal.commission + deal.swap

        # Get open position floating P/L (if any)
        positions = mt5.positions_get()
        floating_pnl = sum(pos.profit for pos in positions) if positions else 0.0

        total_loss = closed_pnl + floating_pnl
        log_info(f"[DAILY LOSS CHECK] Closed + Floating P/L = {total_loss:.2f} USD (Limit: -{self.max_daily_loss:.2f} USD)")
        return total_loss

    def should_stop_bot(self):
        if not FUNDED_MODE:
            return False

        return self.get_current_daily_loss() <= -self.max_daily_loss
