import re

with open("src/agents/execution_agent.py", "r") as f:
    content = f.read()

new_chase_exit_order = """    def _chase_exit_order(self, symbol: str, side: str, quantity: int, token: int):
        \"\"\"
        Dynamic Order Chasing for Exits.
        Starts with a LIMIT order at mid-price, then trails it towards market price.
        Includes a Liquidity Filter: deep OTM/worthless options (premium < ₹2) immediately exit at MARKET.
        \"\"\"
        if self.paper_trade or not self.kite:
            # Paper trade: Just use a market order approximation
            return self.place_order(symbol, side, quantity, order_type="MARKET")

        mid_price = None
        bid = None
        ask = None
        if self.streamer and token:
            tick = self.streamer.latest_ticks.get(token)
            if tick and "depth" in tick:
                try:
                    bid = tick["depth"]["buy"][0]["price"]
                    ask = tick["depth"]["sell"][0]["price"]
                    mid_price = round((bid + ask) / 2.0, 1)
                except: pass

        # Liquidity Filter 1: practically worthless
        if mid_price and mid_price < 2.0:
            logger.info(f"⚡ [CHASE EXIT] {symbol} is practically worthless (Mid: ₹{mid_price}). Executing MARKET.")
            return self.place_order(symbol, side, quantity, order_type="MARKET")

        # Liquidity Filter 2: Bid-Ask spread >10%
        if bid and ask and bid > 0:
            spread_pct = (ask - bid) / bid
            if spread_pct > 0.10:
                logger.warning(f"⚠️ [CHASE EXIT] Highly illiquid strike {symbol}. Spread: {spread_pct*100:.1f}%. Be careful.")

        if not mid_price:
            logger.warning(f"⚠️ [CHASE EXIT] No depth available for {symbol}. Falling back to MARKET.")
            return self.place_order(symbol, side, quantity, order_type="MARKET")

        logger.info(f"⚡ [CHASE EXIT] Placing initial LIMIT for {symbol} at {mid_price}")
        order_id = self.place_order(symbol, side, quantity, order_type="LIMIT", limit_price=mid_price)
        if not order_id: return None

        # Chase logic
        max_attempts = 3
        attempts = 0
        while attempts < max_attempts:
            time.sleep(0.5) # Wait 500ms

            try:
                orders = _api_executor.execute(self.kite.orders)
                order = next((o for o in orders if o["order_id"] == order_id), None)
                if not order or order["status"] in ("COMPLETE", "CANCELLED", "REJECTED"):
                    logger.info(f"✅ [CHASE EXIT] Order {order_id} resolved (Status: {order.get('status') if order else 'N/A'})")
                    return order_id

                # Not filled. Update price slightly worse
                tick = self.streamer.latest_ticks.get(token)
                if not tick or "depth" not in tick:
                    attempts += 1
                    continue

                # If we are BUYING to cover, we must pay the ASK. If SELLING, hit the BID.
                worse_price = tick["depth"]["sell"][0]["price"] if side == "BUY" else tick["depth"]["buy"][0]["price"]

                if worse_price != order.get("price"):
                    logger.info(f"⚡ [CHASE EXIT] Modifying {order_id} to new price {worse_price}")
                    _api_executor.execute(self.kite.modify_order,
                        variety=self.kite.VARIETY_REGULAR,
                        order_id=order_id,
                        price=worse_price
                    )
                attempts += 1
            except Exception as e:
                logger.error(f"❌ [CHASE EXIT] Attempt {attempts} failed: {e}")
                break

        # Final fallback to market
        try:
            orders = _api_executor.execute(self.kite.orders)
            order = next((o for o in orders if o["order_id"] == order_id), None)
            if order and order["status"] not in ("COMPLETE", "CANCELLED", "REJECTED"):
                logger.warning(f"⏰ [CHASE EXIT] Timeout reached for {order_id}. Converting to MARKET.")
                _api_executor.execute(self.kite.modify_order,
                    variety=self.kite.VARIETY_REGULAR,
                    order_id=order_id,
                    order_type=self.kite.ORDER_TYPE_MARKET
                )
        except: pass
        return order_id"""

content = content[:content.find("    def _chase_exit_order(self, symbol: str, side: str, quantity: int, token: int):")] + new_chase_exit_order + "\n\n" + content[content.find("    def close_position(self, order_id: str):"):]

with open("src/agents/execution_agent.py", "w") as f:
    f.write(content)
