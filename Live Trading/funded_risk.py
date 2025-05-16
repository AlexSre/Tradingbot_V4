from datetime import datetime, time
from zoneinfo import ZoneInfo
import MetaTrader5 as mt5
from config import START_BALANCE, DAILY_MAX_LOSS_PERCENT, FUNDED_MODE
from utils import log_info, log_error

class DailyLossManager:
    def __init__(self):
        self.initial_balance = START_BALANCE
        self.max_daily_loss = self.initial_balance * (DAILY_MAX_LOSS_PERCENT / 100)
        self.timezone = ZoneInfo("Europe/Berlin")
        self.today = self.get_berlin_now().date()

    def get_berlin_now(self):
        return datetime.now(self.timezone)

    def update_day(self):
        today = self.get_berlin_now().date()
        if today != self.today:
            self.today = today
            log_info("New day detected — resetting daily loss tracking.")

    def get_current_daily_loss(self):
    # Ora Berlin de azi la 00:00
    berlin_now = self.get_berlin_now()
    midnight_berlin = self.timezone.localize(datetime.combine(berlin_now.date(), time.min))
    utc_from = midnight_berlin.astimezone(pytz.UTC)

    utc_now = datetime.utcnow()

    # Tranzacții închise din ziua respectivă
    deals = mt5.history_deals_get(utc_from, utc_now)
    closed_pnl = 0.0

    if deals:
        for deal in deals:
            # Verifică doar tranzacțiile reale de BUY/SELL (tip 1, 2)
            if deal.type in [mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_SELL]:
                closed_pnl += (deal.profit + deal.commission + deal.swap)

    # Profit/pierdere nerealizat(ă) pe pozițiile deschise
    positions = mt5.positions_get()
    floating_pnl = sum(pos.profit for pos in positions) if positions else 0.0

    total = closed_pnl + floating_pnl
    log_info(f"[DAILY LOSS CHECK] Closed: {closed_pnl:.2f} | Floating: {floating_pnl:.2f} | Total: {total:.2f} | Limit: -{self.max_daily_loss:.2f}")
    return total


    def should_stop_bot(self):
        if not FUNDED_MODE:
            return False
        return self.get_current_daily_loss() <= -self.max_daily_loss
