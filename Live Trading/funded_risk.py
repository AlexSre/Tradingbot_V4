# funded_risk.py
import os
import json
from datetime import datetime
from zoneinfo import ZoneInfo
import MetaTrader5 as mt5
from config import START_BALANCE, DAILY_MAX_LOSS_PERCENT, FUNDED_MODE
from utils import log_info, log_error

STATE_FILE = os.path.join(os.path.dirname(__file__), "daily_loss_state.json")

class DailyLossManager:
    def __init__(self):
        self.initial_balance = START_BALANCE
        self.max_daily_loss = self.initial_balance * (DAILY_MAX_LOSS_PERCENT / 100.0)
        self.timezone = ZoneInfo("Europe/Berlin")
        self.current_day = None
        self.day_start_balance = None

        # Încearcă să încarce starea salvată
        if not self.load_state():
            # Dacă nu există stare sau e depășită, inițializează de la zero
            self.reset_to_today()

    def load_state(self) -> bool:
        """Încarcă current_day și day_start_balance din fișier.
           Returnează True dacă data e azi, altfel False."""
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
            saved_day = datetime.fromisoformat(data["current_day"]).date()
            if saved_day == datetime.now(self.timezone).date():
                self.current_day = saved_day
                self.day_start_balance = data["day_start_balance"]
                log_info(f"[DAILY LOSS LOAD] Restored state for {self.current_day}: "
                         f"start balance = {self.day_start_balance:.2f}")
                return True
            else:
                log_info(f"[DAILY LOSS LOAD] State file is from {saved_day}, not today.")
                return False
        except FileNotFoundError:
            log_info("[DAILY LOSS LOAD] No previous state file found.")
            return False
        except Exception as e:
            log_error(f"[DAILY LOSS LOAD] Failed to load state: {e}")
            return False

    def save_state(self):
        """Salvează current_day și day_start_balance în fișier JSON."""
        try:
            data = {
                "current_day": self.current_day.isoformat(),
                "day_start_balance": self.day_start_balance
            }
            with open(STATE_FILE, "w") as f:
                json.dump(data, f)
            log_info(f"[DAILY LOSS SAVE] State saved for {self.current_day}")
        except Exception as e:
            log_error(f"[DAILY LOSS SAVE] Could not save state: {e}")

    def reset_to_today(self):
        """Setează current_day = azi și day_start_balance din cont, apoi salvează starea."""
        now = datetime.now(self.timezone)
        self.current_day = now.date()

        account_info = mt5.account_info()
        if account_info:
            self.day_start_balance = account_info.balance
            log_info(f"[DAILY LOSS INIT] {self.current_day}: start balance = {self.day_start_balance:.2f}, "
                     f"daily loss limit = {self.max_daily_loss:.2f}")
        else:
            self.day_start_balance = self.initial_balance
            log_error(f"[DAILY LOSS INIT] failed to fetch account balance; defaulting to {self.initial_balance:.2f}")

        self.save_state()

    def update_day(self):
        """Verifică dacă a trecut miezul nopții CET/CEST; dacă da, resetează starea."""
        now = datetime.now(self.timezone)
        if now.date() != self.current_day:
            log_info("[DAILY LOSS] New day detected — resetting")
            self.reset_to_today()

    def get_current_daily_loss(self) -> float:
        """Calculează closed și floating P/L relativ la day_start_balance/equity."""
        account_info = mt5.account_info()
        if not account_info:
            log_error("[DAILY LOSS] Failed to fetch account info")
            return 0.0

        current_balance = account_info.balance
        equity = account_info.equity

        closed_pnl = current_balance - self.day_start_balance
        floating_pnl = equity - current_balance
        total_loss = closed_pnl + floating_pnl

        log_info(f"[DAILY LOSS] Closed: {closed_pnl:.2f} | Floating: {floating_pnl:.2f} | "
                 f"Total: {total_loss:.2f} (Limit: -{self.max_daily_loss:.2f})")
        return total_loss

    def should_stop_bot(self) -> bool:
        if not FUNDED_MODE:
            return False
        self.update_day()
        return self.get_current_daily_loss() <= -self.max_daily_loss
