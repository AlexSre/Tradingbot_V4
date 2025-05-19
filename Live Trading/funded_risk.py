from datetime import datetime, time
from zoneinfo import ZoneInfo
import MetaTrader5 as mt5
from config import START_BALANCE, DAILY_MAX_LOSS_PERCENT, FUNDED_MODE
from utils import log_info, log_error

class DailyLossManager:
    def __init__(self):
        # Set timezone and initialize today's date
        self.timezone = ZoneInfo("Europe/Berlin")
        now = datetime.now(self.timezone)
        self.today = now.date()

        # Fetch starting balance at the beginning of the day
        account_info = mt5.account_info()
        if account_info is None:
            log_error("Failed to get account info at init. Using START_BALANCE.")
            self.day_start_balance = START_BALANCE
        else:
            self.day_start_balance = account_info.balance

        # Calculate maximum allowed daily loss
        self.max_daily_loss = START_BALANCE * (DAILY_MAX_LOSS_PERCENT / 100)
        log_info(
            f"[DAILY LOSS INIT] Start Balance: {self.day_start_balance:.2f} | "
            f"Max Loss: {self.max_daily_loss:.2f}"
        )

    def update_day(self):
        # Reset tracking at Berlin midnight
        now = datetime.now(self.timezone)
        if now.date() != self.today:
            self.today = now.date()
            account_info = mt5.account_info()
            if account_info is None:
                log_error("Failed to get account info at day reset.")
            else:
                self.day_start_balance = account_info.balance
                log_info(f"[DAILY LOSS RESET] New day detected. Start Balance reset to {self.day_start_balance:.2f}")

    def get_closed_pnl(self):
        # Closed P/L is the difference in account balance since start of day
        account_info = mt5.account_info()
        if account_info is None:
            log_error("Failed to fetch account info for closed P/L.")
            return 0.0
        return account_info.balance - self.day_start_balance

    def get_floating_pnl(self):
        # Floating P/L is the sum of open position profits/losses
        positions = mt5.positions_get()
        if positions is None:
            log_error("Failed to fetch open positions for floating P/L.")
            return 0.0
        return sum(pos.profit for pos in positions)

    def get_current_daily_loss(self):
        closed = self.get_closed_pnl()
        floating = self.get_floating_pnl()
        total = closed + floating
        log_info(
            f"[DAILY LOSS] Closed: {closed:.2f} | Floating: {floating:.2f} | "
            f"Total: {total:.2f} (Limit: -{self.max_daily_loss:.2f})"
        )
        return total

    def should_stop_bot(self):
        if not FUNDED_MODE:
            return False
        return self.get_current_daily_loss() <= -self.max_daily_loss
