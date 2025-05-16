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
        berlin_now = self.get_berlin_now()
        midnight_berlin = datetime.combine(berlin_now.date(), time.min, tzinfo=self.timezone)
        midnight_timestamp = int(midnight_berlin.timestamp())

        log_info(f"[DEBUG] Berlin time now: {berlin_now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        log_info(f"[DEBUG] Filtering deals after UNIX timestamp: {midnight_timestamp}")

        deals = mt5.history_deals_get(position=0)
        closed_pnl = 0.0
        valid_types = [mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_SELL]
        seen_tickets = set()

        if deals:
            filtered = [d for d in deals if d.time >= midnight_timestamp]
            log_info(f"[DEBUG] Found {len(filtered)} deals since Berlin midnight.")
            for deal in filtered:
                if deal.ticket in seen_tickets:
                    continue
                seen_tickets.add(deal.ticket)
                pnl = deal.profit + deal.commission + deal.swap
                if deal.type in valid_types:
                    closed_pnl += pnl
                log_info(
                    f"  → Deal: {deal.ticket} | Time: {datetime.fromtimestamp(deal.time)} | Symbol: {deal.symbol} | Type: {deal.type} | "
                    f"Profit: {deal.profit:.2f} | Comm: {deal.commission:.2f} | Swap: {deal.swap:.2f} | Net: {pnl:.2f}"
                )
        else:
            log_info("[DEBUG] No deals returned by MT5 (history_deals_get).")

        positions = mt5.positions_get()
        floating_pnl = sum(pos.profit for pos in positions) if positions else 0.0

        total_loss = closed_pnl + floating_pnl
        log_info(
            f"[DAILY LOSS CHECK] Closed P/L = {closed_pnl:.2f} | Floating P/L = {floating_pnl:.2f} "
            f"| Total = {total_loss:.2f} USD (Limit: -{self.max_daily_loss:.2f} USD)"
        )
        return total_loss

    def should_stop_bot(self):
        if not FUNDED_MODE:
            return False
        return self.get_current_daily_loss() <= -self.max_daily_loss