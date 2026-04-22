import json
import logging
import os
from datetime import datetime

class AuditLogger:
    """
    Appends structured JSON logs for all critical system actions.
    Useful for compliance, backtesting ingestion, and debugging.
    """
    def __init__(self, log_dir="logs"):
        self.log_dir = log_dir
        if not os.path.exists(self.log_dir):
            os.makedirs(self.log_dir)
        self.file_path = os.path.join(self.log_dir, "audit_trail.jsonl")

    def _write(self, event_type: str, data: dict):
        payload = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "event_type": event_type,
            "data": data
        }
        try:
            with open(self.file_path, "a") as f:
                f.write(json.dumps(payload) + "\n")
        except Exception as e:
            logging.error(f"Failed to append to audit log: {e}")

    def log_trade_entry(self, order_id: str, symbol: str, quantity: int, price: float, sl: float, target: float, margin_blocked: float):
        self._write("TRADE_ENTRY", {
            "order_id": order_id, "symbol": symbol, "quantity": quantity,
            "price": price, "stop_loss_trigger": sl, "target": target,
            "margin_blocked": margin_blocked
        })

    def log_trade_exit(self, order_id: str, symbol: str, exit_price: float, pnl: float, reason: str):
        self._write("TRADE_EXIT", {
            "order_id": order_id, "symbol": symbol, "exit_price": exit_price,
            "realized_pnl": pnl, "reason": reason
        })

    def log_circuit_breaker(self, reason: str, realized_pnl: float, floating_pnl: float):
        self._write("CIRCUIT_BREAKER_TRIGGERED", {
            "reason": reason, "realized_pnl": realized_pnl, "floating_pnl": floating_pnl
        })

audit_logger = AuditLogger()
