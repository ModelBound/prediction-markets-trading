"""Main trading cycle orchestrator - runs every ~15 minutes."""
import json
import logging
import time
from datetime import datetime

from gemini_agent import TradingAgent, build_cycle_prompt
from market_provider import get_market_client
from portfolio import PortfolioManager, save_json, CYCLE_LOG_FILE, load_json
from notes_manager import NotesManager
from settlement_detector import SettlementDetector
from learning_engine import LearningEngine
from reviewer_agent import ReviewerAgent
from rules_parser import RulesParser
from free_research import gather_free_research
import config

import os

# Ensure data directory exists before configuring logging
os.makedirs("data", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("data/trading.log"),
    ],
)
logger = logging.getLogger(__name__)


class TradingCycle:
    """Orchestrates the complete trading cycle."""

    def __init__(self):
        self.kalshi = get_market_client()
        self.agent = TradingAgent()
        self.portfolio = PortfolioManager(self.kalshi)
        self.notes = NotesManager()
        self.settlement_detector = SettlementDetector(self.kalshi)
        self.learning_engine = LearningEngine(self.notes)
        self.reviewer = ReviewerAgent()
        self.rules_parser = RulesParser()
        self.cycle_number = self._get_last_cycle_number() + 1
        self.previous_reasoning = None
        self._rejected_tickers = self._load_recent_rejections()
        self._traded_tickers = {}  # ticker -> trade_cycle (don't repeat same market)

    def _get_last_cycle_number(self) -> int:
        logs = load_json(CYCLE_LOG_FILE, [])
        if logs:
            return logs[-1].get("cycle", 0)
        return 0

    def _load_recent_rejections(self) -> dict:
        """Seed rejection cooldown from review logs so restarts don't forget bad loops."""
        rejected = {}
        review_log = load_json("data/review_log.json", [])
        for entry in review_log[-50:]:
            if entry.get("outcome") == "rejected" and entry.get("ticker"):
                rejected[entry["ticker"]] = entry.get("cycle", self.cycle_number)
        return rejected

    def _mark_rejected(self, ticker: str):
        if ticker:
            self._rejected_tickers[ticker] = self.cycle_number

    def _is_recently_rejected(self, ticker: str) -> bool:
        rejected_cycle = self._rejected_tickers.get(ticker)
        if rejected_cycle is None:
            return False
        if self.cycle_number - rejected_cycle >= config.REJECTION_COOLDOWN_CYCLES:
            self._rejected_tickers.pop(ticker, None)
            return False
        return True

    def run_cycle(self) -> dict:
        """Execute one complete trading cycle."""
        cycle_start = datetime.utcnow()
        cycle_log = {
            "cycle": self.cycle_number,
            "start": cycle_start.isoformat(),
            "mode": config.TRADING_MODE,
        }

        logger.info(f"\n{'='*60}")
        logger.info(f"TRADING CYCLE #{self.cycle_number} - {cycle_start.isoformat()}")
        logger.info(f"Mode: {config.TRADING_MODE}")
        logger.info(f"{'='*60}")

        try:
            # Step 0: Check if trading is enabled via bankroll
            bankroll = self._get_trading_budget()
            cycle_log["budget_active"] = bankroll.get("active", False)
            cycle_log["budget_cents"] = bankroll.get("budget_cents", 0)

            # Step 1: Check settlements and learn from outcomes
            resolved = self.settlement_detector.check_settlements()
            if resolved:
                cycle_log["settlements_found"] = len(resolved)
                # Learn from new settlements
                lessons_generated = self.learning_engine.process_new_settlements(resolved)
                cycle_log["lessons_generated"] = lessons_generated

            # Step 2: Reset cycle spending and refresh portfolio
            self.portfolio.reset_cycle_spend()
            self._refresh_portfolio()
            cycle_log["balance"] = self.portfolio.state["cash_balance"]
            cycle_log["account_value"] = self.portfolio.state["total_account_value"]

            # If no bankroll set, just log status and skip trading logic
            if not bankroll.get("active"):
                logger.info("Trading INACTIVE (no bankroll set). Monitoring only.")
                cycle_log["action"] = "monitor"
                cycle_log["pass_reason"] = "No trading bankroll set. Use dashboard to set one."
                self._save_cycle_log(cycle_log, cycle_start)
                self.cycle_number += 1
                return cycle_log

            # Calculate current bankroll value:
            # Bankroll = initial amount. It gets deployed into positions.
            # When positions settle (win), cash comes back > what was spent.
            # When positions settle (lose), cash doesn't come back.
            # The bankroll's "available to trade" = initial - amount_in_positions
            initial_bankroll = bankroll["budget_cents"]

            # Count value of open positions as deployed capital
            position_value = sum(
                pos.get("quantity", 0) * pos.get("current_bid", pos.get("entry_price", 0))
                for pos in self.portfolio.state["positions"]
            )

            # Available to trade = actual cash in account (Kalshi is source of truth)
            # The deployed/returned tracking was broken - just use real cash balance
            effective_balance = self.portfolio.state["cash_balance"]

            # Total bankroll value = cash portion + position value
            bankroll_total = effective_balance + position_value

            # Check if bankroll is busted:
            # Only busted if no cash available AND no open positions
            has_positions = len(self.portfolio.state["positions"]) > 0
            if effective_balance <= 0 and not has_positions:
                logger.info(f"Bankroll BUSTED. No cash and no positions.")
                self._deactivate_bankroll(bankroll, "Bankroll depleted - all positions settled")
                cycle_log["action"] = "busted"
                cycle_log["pass_reason"] = "Bankroll depleted"
                self._save_cycle_log(cycle_log, cycle_start)
                self.cycle_number += 1
                return cycle_log

            # If bankroll is fully deployed but positions exist, just monitor
            if effective_balance <= 0 and has_positions:
                logger.info(f"Bankroll fully deployed in positions. Monitoring until settlement.")

            logger.info(
                f"Bankroll: ${initial_bankroll/100:.2f} initial | "
                f"Available: ${effective_balance/100:.2f} | "
                f"Positions: ${position_value/100:.2f} | "
                f"Total: ${bankroll_total/100:.2f}"
            )

            # Step 2: Get market data
            markets = self._get_market_data()
            cycle_log["markets_available"] = len(markets)

            if not markets:
                logger.warning("No markets available. Skipping cycle.")
                cycle_log["action"] = "skip"
                cycle_log["skip_reason"] = "No markets available"
                self._save_cycle_log(cycle_log, cycle_start)
                return cycle_log

            # Step 3: Update position prices from market data
            self._update_position_prices(markets)

            # Step 4: Research top opportunities
            free_data = gather_free_research(markets)

            # LLM-powered research is the expensive part; reuse fresh cache first.
            research = {}
            cached_research = self._load_research_cache(config.RESEARCH_CACHE_TTL_HOURS)
            should_research = (
                self.cycle_number % config.RESEARCH_INTERVAL_CYCLES == 1
                or not cached_research
            )
            if should_research:
                logger.info("Researching top market opportunities (LLM)...")
                research = self.agent.research_markets_batch(markets)
                cycle_log["research_count"] = len(research)
                self._save_research_cache(research)
            else:
                research = cached_research
                logger.info(f"Using cached research ({len(research)} markets)")

            # Merge free research into LLM research (free takes priority as it's current)
            for ticker, data in free_data.items():
                if ticker in research:
                    research[ticker] = data + "\n\n" + research[ticker]
                else:
                    research[ticker] = data

            # Skip LLM call entirely if no budget to trade with
            if effective_balance <= 0:
                logger.info("No budget available — skipping LLM call")
                cycle_log["action"] = "pass"
                cycle_log["pass_reason"] = "No trading budget available (fully deployed)"
                self._save_cycle_log(cycle_log, cycle_start)
                self.cycle_number += 1
                return cycle_log

            # Step 5: Build prompt and get agent decision (with research + learning)
            learning_context = self.learning_engine.get_learning_context()
            decision = self._get_agent_decision(markets, effective_balance, research, learning_context)
            cycle_log["reasoning"] = decision.get("reasoning", "")
            cycle_log["action"] = decision.get("action", "pass")

            # Step 5: Execute trades if any
            if decision.get("action") == "trade" and decision.get("trades"):
                trade_results = self._execute_trades(decision["trades"], markets, research, effective_balance)
                cycle_log["trades"] = trade_results
                cycle_log["trades_attempted"] = len(decision["trades"])
                cycle_log["trades_executed"] = sum(1 for t in trade_results if t.get("success"))
            else:
                cycle_log["pass_reason"] = decision.get("pass_reason", "No edge found")
                logger.info(f"PASS: {cycle_log['pass_reason']}")

            # Step 6: Update notes and learning
            if decision.get("notes_update"):
                self.notes.update_from_cycle(decision["notes_update"])

            # Store reasoning for next cycle
            self.previous_reasoning = decision.get("reasoning", "")
            cycle_log["markets_data"] = [
                {
                    "ticker": m.get("ticker"),
                    "days_to_close": m.get("days_to_close"),
                    "hours_to_close": m.get("hours_to_close"),
                    "is_live_event": m.get("is_live_event", False),
                }
                for m in markets
            ]

        except Exception as e:
            logger.error(f"Cycle error: {e}", exc_info=True)
            cycle_log["error"] = str(e)
            cycle_log["action"] = "error"

        self._save_cycle_log(cycle_log, cycle_start)
        self.cycle_number += 1
        return cycle_log

    def _refresh_portfolio(self):
        """Refresh portfolio state from API."""
        logger.info("Refreshing portfolio from Kalshi API...")

        balance = self.kalshi.get_balance()
        if "error" not in balance:
            self.portfolio.state["cash_balance"] = balance.get("balance", 0)
            logger.info(f"Cash balance: ${balance.get('balance', 0) / 100:.2f}")
        else:
            logger.error(f"Failed to get balance: {balance}")

        positions = self.kalshi.get_positions()
        if "error" not in positions:
            raw = positions.get("market_positions", [])
            self.portfolio.state["positions"] = self.portfolio._process_positions(raw)
            logger.info(f"Open positions: {len(self.portfolio.state['positions'])}")
        else:
            logger.error(f"Failed to get positions: {positions}")

    def _get_market_data(self) -> list:
        """Fetch and process available markets, filtering for liquid ones."""
        logger.info("Fetching market data...")

        # Strategy: Get markets from multiple sources to find diverse opportunities
        raw_markets = []

        # 1. Get default markets (usually multivariate junk, but some real ones)
        events_resp = self.kalshi.get_markets(status="open", limit=200)
        raw_markets.extend(events_resp.get("markets", []))

        # 2. Dynamically discover active series by scanning the series list
        # Rotate through series in batches to discover new markets over time
        series_resp = self.kalshi._request("GET", "/series", params={"limit": 200})
        all_series = series_resp.get("series", [])

        # Pick a rotating batch of 80 series (wider scan for discovery)
        batch_start = (self.cycle_number * 80) % max(len(all_series), 1)
        series_batch = all_series[batch_start:batch_start + 80]

        # Always include known active series across all categories
        priority_series = [
            # ESPORTS & LIVE GAMES
            "KXVALORANTGAME", "KXSOL15M", "KXVENFUTVEGAME",
            # TENNIS
            "KXATPSETWINNER", "KXITFMATCH", "KXTABLETENNIS",
            # MOTORSPORT
            "KXF1RACEPODIUM", "KXINDYCARRACE",
            # TEAM SPORTS (individual games)
            "KXNHLGAME", "KXMLBGAME", "KXNBA1HWINNER",
            # WEATHER (daily settlement)
            "KXHIGHTDC", "KXHIGHTNYC", "KXHIGHTCHI", "KXHIGHPHIL",
            "KXLOWTSEA", "KXTEMPNYCH", "KXHIGHCHI",
            # COMMODITIES & ECONOMICS
            "KXBRENTW", "KXWTI", "KXCPI", "KXJOBS",
            "KXAAAGASD", "KXAAAGASMAXCA",
            "KXNEWTARIFFS", "KXSOLMAXY",
            # SPORTS PROPS
            "KXNBASERIESCOMEBACK", "KXNBASERIES3PMLEADER",
            # POLITICS & CULTURE
            "KXNHL", "KXNBA", "CONTROLH", "CONTROLS",
            "KXOSCARNOMPIC",
        ]

        series_to_scan = list({s.get("ticker") for s in series_batch} | set(priority_series))

        for series_ticker in series_to_scan:
            if not series_ticker:
                continue
            resp = self.kalshi._request("GET", "/markets", params={
                "status": "open", "limit": 10, "series_ticker": series_ticker
            })
            if "error" not in resp:
                raw_markets.extend(resp.get("markets", []))

        # Deduplicate by ticker
        seen = set()
        unique_markets = []
        for m in raw_markets:
            ticker = m.get("ticker", "")
            if ticker not in seen:
                seen.add(ticker)
                unique_markets.append(m)

        # Filter out multivariate/combo markets (KXMVE prefix)
        markets = [
            m for m in unique_markets
            if not m.get("ticker", "").startswith("KXMVE")
        ]
        logger.info(f"Found {len(unique_markets)} unique, {len(markets)} after filtering combos")

        # Parse into our format with correct price fields
        enriched = []
        for market in markets:
            ticker = market.get("ticker", "")

            # Parse dollar-string prices to cents
            yes_bid = int(float(market.get("yes_bid_dollars", "0") or "0") * 100)
            yes_ask = int(float(market.get("yes_ask_dollars", "0") or "0") * 100)
            no_bid = int(float(market.get("no_bid_dollars", "0") or "0") * 100)
            no_ask = int(float(market.get("no_ask_dollars", "0") or "0") * 100)
            volume_24h = float(market.get("volume_24h_fp", "0") or "0")

            # Skip markets with no price data at all
            if yes_bid == 0 and yes_ask == 0 and no_bid == 0:
                continue

            yes_price = yes_bid if yes_bid > 0 else yes_ask
            no_price = no_bid if no_bid > 0 else no_ask
            spread = yes_ask - yes_bid if yes_ask > yes_bid else 0

            # Do not show the LLM markets where neither side has an executable
            # ask inside our tradeable price band. These create repeated 1-4¢
            # longshot proposals that deterministic pre-flight will reject.
            yes_tradeable = config.MIN_TRADE_PRICE_CENTS <= yes_ask <= 95
            no_tradeable = config.MIN_TRADE_PRICE_CENTS <= no_ask <= 95
            if not (yes_tradeable or no_tradeable):
                continue

            enriched.append({
                "ticker": ticker,
                "title": market.get("title", ""),
                "yes_price": yes_price,
                "no_price": no_price,
                "yes_bid": yes_bid,
                "yes_ask": yes_ask,
                "no_bid": no_bid,
                "no_ask": no_ask,
                "spread": spread,
                "volume": volume_24h,
                "close_time": market.get("close_time", ""),
                "category": market.get("category", market.get("event_ticker", "")),
                "rules_raw": market.get("rules_primary", ""),
                "rules": "",  # Will be filled with parsed summary
            })

        # Parse rules for enriched markets
        for m in enriched:
            raw_rules = m.get("rules_raw", "")
            if raw_rules:
                parsed = self.rules_parser.parse(raw_rules)
                m["rules"] = self.rules_parser.summarize(parsed)
                m["rules_parsed"] = parsed.is_parseable
            else:
                m["rules"] = ""
                m["rules_parsed"] = False

        # Filter: markets with meaningful volume (>$500/day)
        high_volume = [m for m in enriched if m.get("volume", 0) >= 500]

        # Also include live-event markets with lower volume but active prices
        # (these are matches/games that just opened and haven't accumulated volume yet)
        live_event_series = {"KXNHLGAME", "KXNBAGAME", "KXMLBGAME", "KXATPSETWINNER",
                           "KXFOPENMENSINGLE", "KXVALORANTGAME", "KXF1RACEPODIUM",
                           "KXINDYCARRACE", "KXTABLETENNIS", "KXITFMATCH", "KXSOL15M",
                           "KXHIGHTDC", "KXHIGHTNYC", "KXHIGHTCHI", "KXHIGHPHIL",
                           "KXLOWTSEA", "KXTEMPNYCH"}
        live_events = [
            m for m in enriched
            if m.get("volume", 0) >= 50
            and m["ticker"].split("-")[0] in live_event_series
            and m.get("yes_bid", 0) > 0
        ]

        # Also include markets closing within 24 hours with any volume
        from datetime import timezone
        now = datetime.utcnow().replace(tzinfo=timezone.utc)
        closing_soon = []
        for m in enriched:
            close_str = m.get("close_time", "")
            if not close_str or m.get("volume", 0) < 100:
                continue
            try:
                close_time = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
                days_until_close = (close_time - now).total_seconds() / 86400
                if days_until_close > 0:
                    m["days_to_close"] = days_until_close
                    m["hours_to_close"] = days_until_close * 24
                if 0 < days_until_close <= 1:  # Closing within 24 hours
                    closing_soon.append(m)
            except (ValueError, TypeError):
                continue

        # Combine with DIVERSITY: cap both series and broad groups so one sport
        # or championship theme cannot crowd out the full opportunity set.
        seen_tickers = set()
        series_count = {}  # track how many markets per series
        group_count = {}
        result = []

        def _get_series(ticker):
            """Extract series prefix from ticker (e.g., KXBRENTW from KXBRENTW-26JUN0517-T97.99)."""
            parts = ticker.split("-")
            return parts[0] if parts else ticker

        def _get_group(series):
            if series in {"KXVALORANTGAME", "KXSOL15M", "KXVENFUTVEGAME"}:
                return "esports"
            if series in {"KXATPSETWINNER", "KXITFMATCH", "KXTABLETENNIS", "KXFOPENMENSINGLE"}:
                return "tennis"
            if series in {"KXNHLGAME", "KXNBAGAME", "KXMLBGAME", "KXNBA1HWINNER", "KXNBA", "KXNHL", "KXMLB",
                          "KXNBASERIESCOMEBACK", "KXNBASERIES3PMLEADER"}:
                return "team_sports"
            if series in {"KXF1RACEPODIUM", "KXINDYCARRACE"}:
                return "motorsport"
            if series in {"CONTROLH", "CONTROLS", "KXOSCARNOMPIC", "KXNEWTARIFFS"}:
                return "politics_culture"
            if series in {"KXBRENTW", "KXWTI", "KXCPI", "KXJOBS", "KXAAAGASD", "KXAAAGASMAXCA", "KXSOLMAXY"}:
                return "macro_finance"
            return "other"

        all_candidates = []

        # Pool 1: Live events (matches happening now — highest priority)
        for m in live_events:
            m["is_live_event"] = True
            all_candidates.append(m)

        # Pool 2: Closing soon with volume
        for m in sorted(closing_soon, key=lambda x: x.get("days_to_close", 999)):
            if m.get("volume", 0) >= 500:
                all_candidates.append(m)

        # Pool 2: High volume markets
        for m in sorted(high_volume, key=lambda x: x.get("volume", 0), reverse=True):
            all_candidates.append(m)

        # Pool 3: Closing soon with any volume
        for m in sorted(closing_soon, key=lambda x: x.get("days_to_close", 999)):
            all_candidates.append(m)

        # Select with diversity cap
        championship_series = {"KXNBA", "KXNHL", "KXMLB", "CONTROLH", "CONTROLS"}
        weather_series = {"KXHIGHTDC", "KXHIGHTNYC", "KXHIGHTCHI", "KXHIGHPHIL", "KXLOWTSEA", "KXTEMPNYCH"}

        # Build set of tickers/events we already hold — exclude from feed
        held_tickers = set(pos.get("ticker", "") for pos in self.portfolio.state.get("positions", []))
        held_event_prefixes = set()
        for t in held_tickers:
            if t.count("-") >= 2:
                held_event_prefixes.add("-".join(t.split("-")[:2]))

        for m in all_candidates:
            ticker = m["ticker"]
            if ticker in seen_tickers:
                continue
            series = _get_series(ticker)

            # EXCLUDE weather entirely - the LLM over-indexes on it
            if series in weather_series:
                continue

            # EXCLUDE markets we already hold positions in
            if ticker in held_tickers or any(ticker.startswith(ep) for ep in held_event_prefixes):
                continue

            # EXCLUDE recently rejected tickers so the LLM cannot loop on one bad idea
            if self._is_recently_rejected(ticker):
                continue

            # Caps: 1 per championship, configurable cap for everything else.
            if series in championship_series:
                max_for_series = 1
            else:
                max_for_series = config.MAX_MARKETS_PER_SERIES

            if series_count.get(series, 0) >= max_for_series:
                continue

            group = _get_group(series)
            if group_count.get(group, 0) >= config.MAX_MARKETS_PER_GROUP:
                continue

            seen_tickers.add(ticker)
            series_count[series] = series_count.get(series, 0) + 1
            group_count[group] = group_count.get(group, 0) + 1
            result.append(m)
            if len(result) >= config.MARKETS_PER_CYCLE:
                break

        if result:
            unique_series = len(set(_get_series(m["ticker"]) for m in result))
            live_count = sum(1 for m in result if m.get("is_live_event"))
            logger.info(
                f"Markets: {len(high_volume)} high-vol, "
                f"{len(live_events)} live events, "
                f"{len(closing_soon)} closing <24h. "
                f"Showing {len(result)} from {unique_series} series across {len(group_count)} groups."
            )
            logger.info(f"Top: {result[0]['ticker']} vol=${result[0]['volume']:.0f} bid={result[0]['yes_bid']}¢")
        else:
            logger.warning("No liquid markets found after filtering")
        return result

    def _update_position_prices(self, markets: list):
        """Update position bid prices from market data."""
        market_prices = {}
        for m in markets:
            ticker = m["ticker"]
            market_prices[ticker] = {
                "yes_bid": m.get("yes_bid", m.get("yes_price", 50)),
                "no_bid": m.get("no_bid", m.get("no_price", 50)),
            }
        self.portfolio.update_position_prices(market_prices)

    def _get_trading_budget(self) -> dict:
        """Load trading bankroll from disk."""
        budget_file = "data/trading_budget.json"
        if os.path.exists(budget_file):
            with open(budget_file, "r") as f:
                return json.load(f)
        return {"budget_cents": 0, "active": False, "realized_pnl": 0}

    def _deactivate_bankroll(self, bankroll: dict, reason: str):
        """Deactivate the bankroll (e.g. when busted)."""
        bankroll["active"] = False
        bankroll["deactivated_at"] = datetime.utcnow().isoformat()
        bankroll["deactivated_reason"] = reason
        with open("data/trading_budget.json", "w") as f:
            json.dump(bankroll, f, indent=2)
        logger.info(f"Bankroll deactivated: {reason}")

    def _save_research_cache(self, research: dict):
        """Cache research results for reuse across cycles."""
        cache = {"research": research, "cached_at": datetime.utcnow().isoformat()}
        with open("data/research_cache.json", "w") as f:
            json.dump(cache, f, indent=2)

    def _load_research_cache(self, max_age_hours: int | None = None) -> dict:
        """Load cached research results."""
        cache_file = "data/research_cache.json"
        if os.path.exists(cache_file):
            with open(cache_file, "r") as f:
                cache = json.load(f)
            if max_age_hours is not None:
                cached_at = cache.get("cached_at")
                if cached_at:
                    try:
                        age_hours = (datetime.utcnow() - datetime.fromisoformat(cached_at)).total_seconds() / 3600
                        if age_hours > max_age_hours:
                            logger.info(f"Research cache is stale ({age_hours:.1f}h old)")
                            return {}
                    except (ValueError, TypeError):
                        return {}
            return cache.get("research", {})
        return {}

    def _update_bankroll_pnl(self, pnl_cents: int):
        """Update the bankroll's realized PnL after a trade settles or nets."""
        budget_file = "data/trading_budget.json"
        if os.path.exists(budget_file):
            with open(budget_file, "r") as f:
                bankroll = json.load(f)
            bankroll["realized_pnl"] = bankroll.get("realized_pnl", 0) + pnl_cents
            with open(budget_file, "w") as f:
                json.dump(bankroll, f, indent=2)
            logger.info(f"Bankroll PnL updated: {pnl_cents:+}¢ (total: {bankroll['realized_pnl']:+}¢)")

    def _track_bankroll_deployment(self, cost_cents: int):
        """Track money deployed from the bankroll into positions."""
        budget_file = "data/trading_budget.json"
        if os.path.exists(budget_file):
            with open(budget_file, "r") as f:
                bankroll = json.load(f)
            bankroll["total_deployed"] = bankroll.get("total_deployed", 0) + cost_cents
            with open(budget_file, "w") as f:
                json.dump(bankroll, f, indent=2)
            logger.info(f"Bankroll deployed: {cost_cents}¢ (total deployed: {bankroll['total_deployed']}¢)")

    def _record_prediction(
        self,
        ticker: str,
        title: str,
        side: str,
        price: int,
        ai_probability: int,
        count: int,
        order_id: str | None = None,
    ):
        """Record a prediction for the scorecard."""
        scorecard_file = "data/scorecard.json"
        if os.path.exists(scorecard_file):
            with open(scorecard_file, "r") as f:
                scorecard = json.load(f)
        else:
            scorecard = {"predictions": [], "wins": 0, "losses": 0, "pending": 0}

        scorecard["predictions"].append({
            "ticker": ticker,
            "title": title,
            "side": side,
            "price": price,
            "count": count,
            "order_id": order_id,
            "ai_probability": ai_probability,
            "predicted_at": datetime.utcnow().isoformat(),
            "result": None,
        })
        scorecard["pending"] = scorecard.get("pending", 0) + 1

        with open(scorecard_file, "w") as f:
            json.dump(scorecard, f, indent=2)
        logger.info(f"Prediction recorded: {side.upper()} {count}x {ticker} @ {price}¢ (AI: {ai_probability}%)")

    def _log_blocked(self, trade: dict, reason: str, markets: list = None):
        """Log a blocked trade attempt to the review log so it shows in the activity feed."""
        ticker = trade.get("ticker", "")
        market_data = next((m for m in (markets or []) if m.get("ticker") == ticker), {})
        review_log_file = "data/review_log.json"
        try:
            if os.path.exists(review_log_file):
                with open(review_log_file, "r") as f:
                    log = json.load(f)
            else:
                log = []

            log.append({
                "timestamp": datetime.utcnow().isoformat(),
                "cycle": self.cycle_number,
                "outcome": "blocked",
                "ticker": ticker,
                "title": market_data.get("title", trade.get("title", ticker)),
                "side": trade.get("side", ""),
                "price": trade.get("price", 0),
                "ai_probability": trade.get("my_probability", 0),
                "edge": 0,
                "confidence": 0,
                "concerns": [reason],
                "recommendation": reason,
                "market_volume": market_data.get("volume", 0),
                "market_spread": market_data.get("spread", 0),
                "days_to_close": market_data.get("days_to_close"),
                "category": market_data.get("category", ""),
            })

            if len(log) >= 500:
                archive_name = f"data/review_log_archive_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
                with open(archive_name, "w") as f:
                    json.dump(log, f, indent=2)
                log = []

            with open(review_log_file, "w") as f:
                json.dump(log, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to log blocked trade: {e}")

    def _log_review(self, trade: dict, review, market_data: dict, outcome: str):
        """Log every reviewer decision to data/review_log.json for trend analysis."""
        review_log_file = "data/review_log.json"
        try:
            if os.path.exists(review_log_file):
                with open(review_log_file, "r") as f:
                    log = json.load(f)
            else:
                log = []

            entry = {
                "timestamp": datetime.utcnow().isoformat(),
                "cycle": self.cycle_number,
                "outcome": outcome,  # "approved" or "rejected"
                "ticker": trade.get("ticker", ""),
                "title": market_data.get("title", trade.get("title", "")),
                "side": trade.get("side", ""),
                "price": trade.get("price", 0),
                "ai_probability": trade.get("my_probability", 0),
                "edge": trade.get("my_probability", 0) - trade.get("price", 0),
                "confidence": review.confidence,
                "concerns": review.concerns,
                "recommendation": review.recommendation,
                "market_volume": market_data.get("volume", 0),
                "market_spread": market_data.get("spread", 0),
                "days_to_close": market_data.get("days_to_close"),
                "category": market_data.get("category", ""),
            }
            log.append(entry)

            # Archive when hitting 500 entries
            if len(log) >= 500:
                archive_name = f"data/review_log_archive_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
                with open(archive_name, "w") as f:
                    json.dump(log, f, indent=2)
                logger.info(f"Archived {len(log)} review entries to {archive_name}")
                log = []

            with open(review_log_file, "w") as f:
                json.dump(log, f, indent=2)

        except Exception as e:
            logger.warning(f"Failed to log review: {e}")

    def _get_agent_decision(self, markets: list, effective_balance: int = None, research: dict = None, learning_context: str = None) -> dict:
        """Get trading decision from the agent."""
        logger.info("Consulting trading agent...")

        portfolio_state = {
            **self.portfolio.state,
            "open_positions": len(self.portfolio.state["positions"]),
        }

        # Override cash balance with effective budget if provided
        if effective_balance is not None:
            portfolio_state["trading_budget"] = effective_balance

        prompt = build_cycle_prompt(
            portfolio_state=portfolio_state,
            markets=markets,
            positions=self.portfolio.state["positions"],
            notes=self.notes.get_recent(10),
            previous_reasoning=self.previous_reasoning,
            cycle_number=self.cycle_number,
            research=research,
            learning_context=learning_context,
        )

        decision = self.agent.analyze_and_decide(prompt)
        logger.info(f"Agent decision: {decision.get('action', 'unknown')}")

        if decision.get("opportunities"):
            logger.info(f"Opportunities identified: {len(decision['opportunities'])}")

        return decision

    def _calculate_contract_count(
        self,
        trade: dict,
        calculated_edge: int,
        effective_balance: int,
        price: int,
        is_speculative: bool,
    ) -> int:
        """Choose contract count in code so sizing is not left to the LLM."""
        if price <= 0:
            return 0

        account_value = max(self.portfolio.state.get("total_account_value", effective_balance), effective_balance, 1)
        cycle_limit = account_value if account_value < 1000 else int(account_value * config.MAX_CYCLE_SPEND_PCT)
        cycle_remaining = max(0, cycle_limit - self.portfolio.state.get("cycle_spend", 0))
        fee_buffered_cash = int(effective_balance / (1 + config.TRADING_FEE_PCT))
        max_affordable_spend = max(0, min(cycle_remaining, fee_buffered_cash))

        if is_speculative:
            target_spend = int(config.SPECULATIVE_BET_MAX_DOLLARS * 100)
        else:
            if calculated_edge >= 15:
                target_pct = config.HIGH_EDGE_TRADE_SPEND_PCT
            elif calculated_edge >= 10:
                target_pct = config.MAX_TRADE_SPEND_PCT
            else:
                target_pct = config.MIN_TRADE_SPEND_PCT
            target_spend = int(effective_balance * target_pct)

        # Respect account-level concentration even before portfolio validation.
        max_concentration_spend = int(account_value * config.MAX_CONCENTRATION_PCT)
        target_spend = min(target_spend, max_concentration_spend, max_affordable_spend)

        sized_count = target_spend // price
        if sized_count < 1 and max_affordable_spend >= price:
            sized_count = 1

        return max(sized_count, 0)

    def _get_open_order_tickers(self) -> tuple[set, set]:
        """Return tickers/event prefixes with resting orders that already reserve cash."""
        tickers = set()
        event_prefixes = set()
        resp = self.kalshi.get_orders(status="resting")
        if "error" in resp:
            logger.warning(f"Could not fetch resting orders for duplicate check: {resp}")
            return tickers, event_prefixes
        for order in resp.get("orders", []):
            remaining = float(order.get("remaining_count_fp", 0) or 0)
            if remaining <= 0:
                continue
            ticker = order.get("ticker", "")
            if not ticker:
                continue
            tickers.add(ticker)
            if ticker.count("-") >= 2:
                event_prefixes.add("-".join(ticker.split("-")[:2]))
        return tickers, event_prefixes

    def _filled_count(self, order: dict) -> int:
        """Return filled contracts from a Kalshi order payload."""
        raw = order.get("fill_count_fp", order.get("fill_count", 0))
        try:
            return int(float(raw or 0))
        except (TypeError, ValueError):
            return 0

    def _is_filled_order(self, order: dict) -> bool:
        """Only filled orders should become trades/predictions."""
        return order.get("status") == "executed" and self._filled_count(order) > 0

    def _cancel_unfilled_order(self, order: dict):
        """Prevent resting orders from filling later after we declined to score them."""
        order_id = order.get("order_id")
        if order_id and order.get("status") in ("resting", "open"):
            logger.info(f"Canceling unfilled resting order: {order_id}")
            self.kalshi.cancel_order(order_id)

    def _actual_order_side(self, order: dict, requested_side: str) -> str:
        """Use Kalshi's executed side as the source of truth."""
        return str(order.get("side") or order.get("outcome_side") or requested_side).lower()

    def _actual_fill_price_cents(self, order: dict, filled_count: int, requested_price: int) -> int:
        """Derive average filled price from Kalshi fill cost when available."""
        if filled_count <= 0:
            return requested_price

        total_cost = 0.0
        for key in ("taker_fill_cost_dollars", "maker_fill_cost_dollars"):
            try:
                total_cost += float(order.get(key) or 0)
            except (TypeError, ValueError):
                pass

        if total_cost > 0:
            return int(round((total_cost * 100) / filled_count))

        return requested_price

    def _executable_price(self, side: str, proposed_price: int, market_data: dict) -> int:
        """Prefer the current ask so approved trades fill instead of resting."""
        ask_key = "yes_ask" if side == "yes" else "no_ask"
        try:
            ask = int(market_data.get(ask_key, 0) or 0)
        except (TypeError, ValueError):
            ask = 0

        if 1 <= ask <= 99:
            return ask
        return proposed_price

    def _execute_trades(self, trades: list, markets: list = None, research: dict = None, effective_balance: int = 0) -> list:
        """Execute validated trades."""
        results = []
        research = research or {}
        open_order_tickers, open_order_event_prefixes = self._get_open_order_tickers()

        # Build ticker -> market metadata map from markets
        title_map = {}
        market_map = {}
        if markets:
            for m in markets:
                title_map[m["ticker"]] = m.get("title", "")
                market_map[m["ticker"]] = m

        for trade in trades:
            ticker = trade.get("ticker", "")
            side = str(trade.get("side", "yes")).lower()
            trade["side"] = side
            count = int(trade.get("count", 0) or 0)
            price = int(trade.get("price", 0) or 0)
            title = title_map.get(ticker, trade.get("title", ""))
            market_data = market_map.get(ticker, {})

            # Use the current ask price for immediate fills; re-check edge below at this executable price.
            executable_price = self._executable_price(side, price, market_data)
            if executable_price != price:
                logger.info(
                    f"Execution price update: {ticker} {side.upper()} "
                    f"{price}¢ -> {executable_price}¢ current ask"
                )
                price = executable_price
                trade["price"] = price

            logger.info(f"Attempting trade: {side.upper()} {count}x {ticker} @ {price}¢")

            # (Rejection cache removed - every proposal goes fresh to reviewer)

            # Skip tickers we already have a position in (don't pile into same market)
            if ticker in self._traded_tickers:
                trade_cycle = self._traded_tickers[ticker]
                if self.cycle_number - trade_cycle < 12:  # Don't repeat for ~4 hours
                    logger.info(f"Skipping {ticker} — already traded {self.cycle_number - trade_cycle} cycles ago")
                    self._log_blocked(trade, "Already traded recently", markets)
                    results.append({"ticker": ticker, "success": False, "reason": "Already have position in this market"})
                    continue

            if ticker in open_order_tickers:
                logger.info(f"Skipping {ticker} — already has a resting order")
                self._log_blocked(trade, "Already has resting order", markets)
                results.append({"ticker": ticker, "success": False, "reason": "Already has resting order"})
                continue

            # Also check if we already hold this position from the portfolio
            existing_position = any(
                pos.get("ticker") == ticker for pos in self.portfolio.state.get("positions", [])
            )
            if existing_position:
                logger.info(f"Skipping {ticker} — already holding position")
                self._log_blocked(trade, "Already holding this position", markets)
                results.append({"ticker": ticker, "success": False, "reason": "Already holding this position"})
                continue

            # Don't bet on multiple brackets of the same event
            event_prefix = "-".join(ticker.split("-")[:2]) if ticker.count("-") >= 2 else ticker
            if event_prefix in open_order_event_prefixes:
                logger.info(f"Skipping {ticker} — already have a resting order on this event ({event_prefix})")
                self._log_blocked(trade, f"Already have resting order on event {event_prefix}", markets)
                results.append({"ticker": ticker, "success": False, "reason": "Already have resting order on this event"})
                continue

            existing_same_event = any(
                pos.get("ticker", "").startswith(event_prefix)
                for pos in self.portfolio.state.get("positions", [])
            )
            if existing_same_event:
                logger.info(f"Skipping {ticker} — already have a position on this event ({event_prefix})")
                self._log_blocked(trade, f"Already betting on event {event_prefix}", markets)
                results.append({"ticker": ticker, "success": False, "reason": "Already betting on this event"})
                continue

            # PRE-FLIGHT EDGE CHECK: reject obviously bad proposals
            ai_prob = trade.get("my_probability", 0)
            # Normalize: if probability is a fraction (0-1), convert to percentage
            if 0 < ai_prob <= 1:
                ai_prob = int(ai_prob * 100)
            # For YES bets, ai_prob is the probability of YES.
            # For NO bets, the probability of NO is 100 - ai_prob.
            if side == "yes":
                calculated_edge = ai_prob - price
            else:
                calculated_edge = (100 - ai_prob) - price

            is_speculative = trade.get("speculative", False)
            min_edge = config.SPECULATIVE_EDGE_MIN_CENTS if is_speculative else config.MIN_EDGE_CENTS

            if calculated_edge < min_edge:
                logger.info(f"Pre-flight reject: {ticker} edge={calculated_edge}¢ below minimum {min_edge}¢")
                self._mark_rejected(ticker)
                self._log_blocked(trade, f"Edge too low: {calculated_edge}¢", markets)
                results.append({"ticker": ticker, "success": False, "reason": f"Edge too low: {calculated_edge}¢"})
                continue

            if ai_prob <= 0 or ai_prob > 99:
                logger.info(f"Pre-flight reject: {ticker} invalid probability={ai_prob}")
                results.append({"ticker": ticker, "success": False, "reason": f"Invalid probability: {ai_prob}"})
                continue

            if price < config.MIN_TRADE_PRICE_CENTS and not trade.get("allow_longshot", False):
                logger.info(f"Pre-flight reject: {ticker} price={price}¢ below minimum {config.MIN_TRADE_PRICE_CENTS}¢")
                self._mark_rejected(ticker)
                self._log_blocked(trade, f"Price too low: {price}¢", markets)
                results.append({"ticker": ticker, "success": False, "reason": f"Price too low: {price}¢"})
                continue

            original_count = count
            count = self._calculate_contract_count(trade, calculated_edge, effective_balance, price, is_speculative)
            trade["count"] = count
            if count != original_count:
                logger.info(
                    f"Position sizing: {ticker} {original_count} -> {count} contracts "
                    f"(edge={calculated_edge}¢, price={price}¢, cash=${effective_balance/100:.2f})"
                )
            if count < 1:
                results.append({"ticker": ticker, "success": False, "reason": "Insufficient budget for sized trade"})
                continue

            # Validate risk limits
            valid, reason = self.portfolio.validate_trade(ticker, count, price)
            if not valid:
                logger.warning(f"Trade rejected (risk): {reason}")
                results.append({
                    "ticker": ticker,
                    "success": False,
                    "reason": reason,
                })
                continue

            # Reviewer gate - second AI validates the trade
            if self._should_research_before_review(market_data, calculated_edge, min_edge, research.get(ticker)):
                research[ticker] = self._research_trade_for_review(market_data)

            # Find related markets (same event) for redirect suggestions
            series = ticker.split("-")[0] if "-" in ticker else ticker
            event_prefix = "-".join(ticker.split("-")[:2]) if ticker.count("-") >= 2 else series
            related_markets = [
                m for m in (markets or [])
                if m.get("ticker", "").startswith(event_prefix)
                and m.get("ticker") != ticker
                and m.get("yes_bid", 0) > 0
            ]

            review_context = {
                "market_data": market_data,
                "research": research.get(ticker, "No research available"),
                "reasoning": trade.get("reasoning", ""),
                "rules": market_data.get("rules", ""),
                "research_quality": trade.get("research_quality", "unknown"),
                "related_markets": related_markets,
            }
            review = self.reviewer.review_trade(trade, review_context)

            if not review.approved:
                # Check if reviewer suggested an alternative (redirect)
                if review.suggested_ticker and review.suggested_ticker != ticker:
                    logger.warning(
                        f"Reviewer suggested redirect {ticker} -> {review.suggested_ticker}, "
                        "but auto-redirect execution is disabled"
                    )
                    self._mark_rejected(review.suggested_ticker)

                logger.warning(
                    f"Trade REJECTED by reviewer (confidence: {review.confidence}%): "
                    f"{review.recommendation}"
                )
                self._mark_rejected(ticker)
                self._log_review(trade, review, market_data, "rejected")
                results.append({
                    "ticker": ticker,
                    "success": False,
                    "reason": f"Reviewer rejected: {review.recommendation}",
                    "review_confidence": review.confidence,
                    "review_concerns": review.concerns,
                })
                continue

            logger.info(f"Trade APPROVED by reviewer (confidence: {review.confidence}%)")
            self._log_review(trade, review, market_data, "approved")

            # Execute
            if side == "yes":
                response = self.kalshi.buy_yes(ticker, count, price)
            else:
                response = self.kalshi.buy_no(ticker, count, price)

            if "error" in response:
                logger.error(f"Trade failed: {response}")
                results.append({
                    "ticker": ticker,
                    "success": False,
                    "reason": response.get("message", "API error"),
                })
            else:
                order = response.get("order", {})
                if not self._is_filled_order(order):
                    self._cancel_unfilled_order(order)
                    logger.warning(
                        f"Order not filled; not recording as trade: "
                        f"status={order.get('status')} filled={self._filled_count(order)}"
                    )
                    results.append({
                        "ticker": ticker,
                        "success": False,
                        "reason": f"Order not filled: {order.get('status', 'unknown')}",
                        "order_id": order.get("order_id"),
                    })
                    continue

                filled_count = self._filled_count(order)
                actual_side = self._actual_order_side(order, side)
                actual_price = self._actual_fill_price_cents(order, filled_count, price)
                logger.info(f"Trade executed: {order.get('order_id', 'unknown')}")
                self.portfolio.record_trade({
                    "ticker": ticker,
                    "title": title,
                    "side": actual_side,
                    "count": filled_count,
                    "price": actual_price,
                    "order_id": order.get("order_id"),
                    "status": order.get("status"),
                })
                # Track deployment against bankroll
                trade_cost = filled_count * actual_price
                self._track_bankroll_deployment(trade_cost)
                # Record this ticker as traded (prevent repeats)
                self._traded_tickers[ticker] = self.cycle_number
                # Record prediction for scorecard
                ai_prob = trade.get("my_probability", trade.get("probability", 0))
                self._record_prediction(
                    ticker,
                    title,
                    actual_side,
                    actual_price,
                    ai_prob,
                    filled_count,
                    order.get("order_id"),
                )
                results.append({
                    "ticker": ticker,
                    "success": True,
                    "order_id": order.get("order_id"),
                    "status": order.get("status"),
                })

        return results

    def _should_research_before_review(
        self,
        market_data: dict,
        calculated_edge: float,
        min_edge: float,
        existing_research: str | None,
    ) -> bool:
        """Spend research only for actionable trades that would otherwise lack support."""
        if existing_research and existing_research != "No research available":
            return False
        if calculated_edge < max(10, min_edge + 3):
            return False
        if market_data.get("is_live_event"):
            return True

        volume = float(market_data.get("volume", 0) or 0)
        spread = float(market_data.get("spread", 99) or 99)
        hours_to_close = float(market_data.get("hours_to_close", 999) or 999)
        return volume >= 500 and spread <= 5 and hours_to_close <= 48

    def _research_trade_for_review(self, market_data: dict) -> str:
        """Gather last-mile research for a trade that passed deterministic checks."""
        ticker = market_data.get("ticker", "?")
        logger.info(f"Gathering just-in-time research for reviewer: {ticker}")
        try:
            result = self.agent.research_market_thorough(market_data)
            summary = result.get("summary", "")
            return summary or "No research available"
        except Exception as e:
            logger.warning(f"Just-in-time research failed for {ticker}: {e}")
            return "No research available"

    def _save_cycle_log(self, cycle_log: dict, cycle_start: datetime):
        """Save cycle log to file."""
        cycle_log["duration_seconds"] = (datetime.utcnow() - cycle_start).total_seconds()

        logs = load_json(CYCLE_LOG_FILE, [])
        logs.append(cycle_log)
        # Keep last 1000 cycles
        if len(logs) > 1000:
            logs = logs[-1000:]
        save_json(CYCLE_LOG_FILE, logs)

        logger.info(
            f"Cycle #{cycle_log['cycle']} complete. "
            f"Action: {cycle_log.get('action', '?')} | "
            f"Duration: {cycle_log['duration_seconds']:.1f}s"
        )
