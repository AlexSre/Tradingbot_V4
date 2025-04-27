import logging
import MetaTrader5 as mt5
import datetime

logging.basicConfig(
    filename="bot.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

def log_info(msg):
    logging.info(msg)
    print(f"[INFO] {msg}")

def log_error(msg):
    logging.error(msg)
    print(f"[ERROR] {msg}")

def log_trade(action, symbol, price, lot_size, sl, tp):
    msg = f"TRADE EXECUTED: {action.upper()} {symbol} at {price} | Lot: {lot_size} | SL: {sl} | TP: {tp if tp else 'None'}"
    log_info(msg)

# In utils.py
class DailyLossManager:
    def __init__(self, initial_balance, max_daily_loss_percent):
        self.initial_balance = initial_balance
        self.max_daily_loss = initial_balance * (max_daily_loss_percent / 100)
        self.reset_day = datetime.datetime.now().date()

    def update(self):
        today = datetime.datetime.now().date()
        if today != self.reset_day:
            self.reset_day = today
            log_info("Daily loss tracking reset for new day.")

    def get_current_daily_loss(self):
        history_deals = mt5.history_deals_get(
            datetime.datetime.combine(self.reset_day, datetime.time.min),
            datetime.datetime.now()
        )
        closed_pnl = sum(deal.profit for deal in history_deals) if history_deals else 0.0

        positions = mt5.positions_get()
        floating_pnl = sum(pos.profit for pos in positions) if positions else 0.0

        total_loss = closed_pnl + floating_pnl
        return total_loss

    def should_stop_bot(self):
        total_loss = self.get_current_daily_loss()
        log_info(f"[DAILY LOSS CHECK] Current P/L: {total_loss:.2f}, Max allowed: {-self.max_daily_loss:.2f}")
        return total_loss <= -self.max_daily_loss
