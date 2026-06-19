"""Local web dashboard for monitoring the trading agent."""
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from string import Template
from urllib.parse import urlparse, parse_qs

# EST timezone (UTC-5) / EDT (UTC-4)
EST = timezone(timedelta(hours=-4))  # EDT during daylight saving


def _accordion(text: str, preview_len: int = 60) -> str:
    """Render text as an expandable accordion cell."""
    if not text or len(text) <= preview_len:
        return text or ""
    safe_text = text.replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    preview = safe_text[:preview_len].rstrip()
    return (
        f'<span class="expandable">{preview}...'
        f'<span class="expand-content">{safe_text}</span></span>'
    )


def to_est(utc_str: str) -> str:
    """Convert a UTC ISO timestamp string to EST display format with full precision."""
    if not utc_str:
        return ""
    try:
        # Parse the timestamp - assume UTC if no timezone specified
        if "+" not in utc_str and "Z" not in utc_str:
            utc_str = utc_str + "+00:00"
        dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        est_dt = dt.astimezone(EST)
        # Format: Jun 2, 5:56:03.421 PM
        ms = est_dt.strftime("%f")[:3]
        return est_dt.strftime(f"%b %-d, %-I:%M:%S.{ms} %p")
    except (ValueError, TypeError):
        return utc_str


def now_est() -> str:
    """Current time in EST."""
    dt = datetime.now(EST)
    ms = dt.strftime("%f")[:3]
    return dt.strftime(f"%b %-d, %-I:%M:%S.{ms} %p EST")

import config
from kalshi_client import KalshiClient
from portfolio import load_json, PORTFOLIO_FILE, TRADES_FILE, CYCLE_LOG_FILE

PORT = 8888


def get_live_data():
    """Fetch live data from Kalshi API."""
    try:
        client = KalshiClient()
        balance = client.get_balance()
        positions = client.get_positions()
        return {
            "balance": balance.get("balance", 0),
            "portfolio_value": balance.get("portfolio_value", 0),
            "positions": positions.get("market_positions", []),
            "connected": True,
        }
    except Exception as e:
        cached = get_cached_live_data()
        if cached:
            cached["error"] = str(e)
            cached["connected"] = False
            cached["source"] = "cached_portfolio_state"
            return cached
        return {"error": str(e), "connected": False, "balance": 0, "portfolio_value": 0, "positions": []}


def get_cached_live_data():
    """Use the droplet-synced portfolio snapshot when local Kalshi API is unavailable."""
    state = load_json(PORTFOLIO_FILE, {})
    if not state:
        return None

    positions = []
    for pos in state.get("positions", []):
        quantity = int(pos.get("quantity", 0) or 0)
        if quantity == 0:
            continue
        signed_qty = quantity if pos.get("side") == "yes" else -quantity
        positions.append({
            "ticker": pos.get("ticker", pos.get("market_ticker", "")),
            "market_ticker": pos.get("market_ticker", pos.get("ticker", "")),
            "position_fp": signed_qty,
            "position": signed_qty,
        })

    return {
        "balance": state.get("cash_balance", 0),
        "portfolio_value": max(0, state.get("total_account_value", 0) - state.get("cash_balance", 0)),
        "positions": positions,
        "connected": False,
    }


def get_cycle_logs():
    return load_json(CYCLE_LOG_FILE, [])


def get_trade_history():
    """Load trade history and enrich with market titles if missing."""
    trades = load_json(TRADES_FILE, [])

    # Backfill titles for trades that don't have them
    needs_title = [t for t in trades if not t.get("title") and t.get("ticker")]
    if needs_title:
        try:
            client = KalshiClient()
            for t in needs_title:
                resp = client.get_market(t["ticker"])
                market = resp.get("market", {})
                if market.get("title"):
                    t["title"] = market["title"]
        except Exception:
            pass  # Don't fail dashboard if API is down

    return trades


def get_agent_notes():
    notes_file = "data/agent_notes.json"
    if os.path.exists(notes_file):
        with open(notes_file, "r") as f:
            return json.load(f)
    return []


def get_review_log():
    """Load the reviewer decision log."""
    review_log_file = "data/review_log.json"
    if os.path.exists(review_log_file):
        with open(review_log_file, "r") as f:
            return json.load(f)
    return []


def get_trading_budget():
    budget_file = "data/trading_budget.json"
    if os.path.exists(budget_file):
        with open(budget_file, "r") as f:
            return json.load(f)
    return {"budget_cents": 0, "active": False, "set_at": None}


def get_scorecard():
    """Load the AI prediction scorecard."""
    scorecard_file = "data/scorecard.json"
    if os.path.exists(scorecard_file):
        with open(scorecard_file, "r") as f:
            return json.load(f)
    return {"predictions": [], "wins": 0, "losses": 0, "pending": 0}


def update_scorecard_from_settlements():
    """Check Kalshi settlements API and update scorecard with results."""
    scorecard_file = "data/scorecard.json"
    scorecard = get_scorecard()

    try:
        client = KalshiClient()
        resp = client.get_settlements()
        settlements = resp.get("settlements", [])

    except Exception:
        return scorecard

    # Match settlements to our predictions
    settlement_map = {
        s.get("ticker") or s.get("market_ticker"): s
        for s in settlements
        if s.get("ticker") or s.get("market_ticker")
    }
    predictions = scorecard.get("predictions", [])

    for pred in predictions:
        ticker = pred.get("ticker", "")
        settlement = settlement_map.get(ticker)
        if not settlement:
            continue

        outcome = _settlement_outcome(settlement)
        pred_side = str(pred.get("side", "")).lower()
        if outcome not in ("yes", "no") or pred_side not in ("yes", "no"):
            continue

        settled_count = _settled_count_for_side(settlement, pred_side)
        if settled_count <= 0:
            continue

        pred_count = int(pred.get("count") or settled_count)
        pred["result"] = "win" if outcome == pred_side else "loss"
        pred["outcome"] = outcome
        pred["settled_at"] = settlement.get("settled_time") or settlement.get("settlement_ts") or datetime.utcnow().isoformat()
        pred["pnl_cents"] = _settlement_pnl_cents(pred, settlement, pred_count, settled_count)

    # Recalculate totals from prediction rows so repeated dashboard refreshes are idempotent.
    scorecard["wins"] = sum(1 for p in predictions if p.get("result") == "win")
    scorecard["losses"] = sum(1 for p in predictions if p.get("result") == "loss")
    scorecard["pending"] = sum(1 for p in predictions if not p.get("result"))
    scorecard["total_pnl_cents"] = sum(
        p.get("pnl_cents", 0) for p in predictions if p.get("pnl_cents") is not None
    )
    resolved = scorecard["wins"] + scorecard["losses"]
    scorecard["win_rate"] = (scorecard["wins"] / resolved * 100) if resolved else 0
    scorecard["last_updated"] = datetime.utcnow().isoformat()
    scorecard["predictions"] = predictions

    os.makedirs("data", exist_ok=True)
    with open(scorecard_file, "w") as f:
        json.dump(scorecard, f, indent=2)

    return scorecard


def _settlement_outcome(settlement: dict) -> str:
    """Return normalized YES/NO settlement outcome from Kalshi's payload variants."""
    return str(
        settlement.get("market_result")
        or settlement.get("result")
        or settlement.get("settled_outcome")
        or ""
    ).lower()


def _settled_count_for_side(settlement: dict, side: str) -> float:
    key_prefix = "yes" if side == "yes" else "no"
    try:
        return float(settlement.get(f"{key_prefix}_count_fp", settlement.get(f"{key_prefix}_count", 0)) or 0)
    except (TypeError, ValueError):
        return 0.0


def _settlement_pnl_cents(prediction: dict, settlement: dict, pred_count: int, settled_count: float) -> int:
    side = str(prediction.get("side", "")).lower()
    result = prediction.get("result")
    cost_key = "yes_total_cost_dollars" if side == "yes" else "no_total_cost_dollars"
    try:
        total_cost_cents = float(settlement.get(cost_key, 0) or 0) * 100
    except (TypeError, ValueError):
        total_cost_cents = 0.0
    try:
        fee_cents = float(settlement.get("fee_cost", 0) or 0) * 100
    except (TypeError, ValueError):
        fee_cents = 0.0

    if settled_count > 0 and total_cost_cents > 0:
        ratio = pred_count / settled_count
        cost_cents = total_cost_cents * ratio
        fee_cents = fee_cents * ratio
    else:
        cost_cents = pred_count * float(prediction.get("price", 0) or 0)

    revenue_cents = pred_count * 100 if result == "win" else 0
    return int(round(revenue_cents - cost_cents - fee_cents))


def record_prediction(ticker: str, title: str, side: str, price: int, probability: int):
    """Record a new AI prediction for the scorecard."""
    scorecard_file = "data/scorecard.json"
    scorecard = get_scorecard()

    scorecard["predictions"].append({
        "ticker": ticker,
        "title": title,
        "side": side,
        "price": price,
        "ai_probability": probability,
        "predicted_at": datetime.utcnow().isoformat(),
        "result": None,
    })
    scorecard["pending"] = scorecard.get("pending", 0) + 1

    os.makedirs("data", exist_ok=True)
    with open(scorecard_file, "w") as f:
        json.dump(scorecard, f, indent=2)


DROPLET_IP = "159.89.224.165"
DROPLET_SSH_KEY = os.path.expanduser("~/.ssh/id_rsa_digitalocean")
DROPLET_DATA_PATH = "/opt/trading-agent/data"
DROPLET_API_URL = f"http://{DROPLET_IP}:9090"


def _sync_to_droplet(filename: str):
    """Push a data file to the droplet via HTTP POST."""
    local_path = os.path.join("data", filename)
    try:
        import requests as req
        with open(local_path, "r") as f:
            data = json.load(f)
        resp = req.post(f"{DROPLET_API_URL}/{filename}", json=data, timeout=5)
        if resp.status_code == 200:
            logging.getLogger(__name__).info(f"Synced {filename} to droplet via HTTP")
        else:
            logging.getLogger(__name__).error(f"Sync failed: {resp.status_code}")
    except Exception as e:
        logging.getLogger(__name__).error(f"Sync error: {e}")


def _sync_from_droplet(filename: str):
    """Pull a data file from the droplet via HTTP GET."""
    local_path = os.path.join("data", filename)
    try:
        import requests as req
        resp = req.get(f"{DROPLET_API_URL}/{filename}", timeout=5)
        if resp.status_code == 200:
            os.makedirs("data", exist_ok=True)
            with open(local_path, "w") as f:
                json.dump(resp.json(), f, indent=2)
        else:
            logging.getLogger(__name__).warning(f"Pull failed: {resp.status_code}")
    except Exception as e:
        logging.getLogger(__name__).warning(f"Pull error: {e}")


def set_trading_budget(amount_dollars=None):
    if amount_dollars in (None, "", "cash"):
        _sync_from_droplet("portfolio_state.json")
        live = get_live_data()
        amount_cents = int(live.get("balance", 0))
    else:
        amount_cents = int(float(amount_dollars) * 100)

    budget = {
        "budget_cents": amount_cents,
        "active": True,
        "realized_pnl": 0,
        "total_deployed": 0,
        "total_returned": 0,
        "set_at": datetime.utcnow().isoformat(),
        "source": "kalshi_cash_balance" if amount_dollars in (None, "", "cash") else "manual",
    }
    os.makedirs("data", exist_ok=True)
    with open("data/trading_budget.json", "w") as f:
        json.dump(budget, f, indent=2)
    # Push to droplet so agent picks it up
    _sync_to_droplet("trading_budget.json")
    return budget


def stop_trading():
    budget = {"budget_cents": 0, "active": False, "set_at": datetime.utcnow().isoformat()}
    os.makedirs("data", exist_ok=True)
    with open("data/trading_budget.json", "w") as f:
        json.dump(budget, f, indent=2)
    # Push to droplet
    _sync_to_droplet("trading_budget.json")
    return budget


def render_positions_table(positions):
    if not positions:
        return '<p style="color:#8b949e;">No open positions</p>'
    rows = ""
    # Fetch titles for positions
    title_map = {}
    try:
        client = KalshiClient()
        for pos in positions:
            ticker = pos.get("ticker", pos.get("market_ticker", ""))
            if ticker and ticker not in title_map:
                resp = client.get_market(ticker)
                market = resp.get("market", {})
                title_map[ticker] = market.get("title", ticker)
    except Exception:
        pass

    for pos in positions:
        if pos.get("position", pos.get("position_fp", 0)) == 0:
            continue
        pos_val = float(pos.get("position_fp", pos.get("position", 0)) or 0)
        side = "YES" if pos_val > 0 else "NO"
        qty = abs(int(pos_val)) if pos_val else 0
        if qty == 0:
            continue
        ticker = pos.get("ticker", "?")
        title = title_map.get(ticker, ticker)
        rows += f"<tr><td>{title}</td><td>{side}</td><td>{qty}</td></tr>"

    if not rows:
        return '<p style="color:#8b949e;">No open positions</p>'
    return f'<table><tr><th>Market</th><th>Side</th><th>Contracts</th></tr>{rows}</table>'


def render_cycles_table(cycles):
    if not cycles:
        return '<p style="color:#8b949e;">No cycles recorded yet</p>'
    rows = ""
    for c in sorted(cycles[-20:], key=lambda x: x.get("start", ""), reverse=True):
        action = c.get("action", "?")
        badge_class = "badge-pass" if action in ("pass", "monitor") else "badge-trade" if action == "trade" else "badge-error"
        ts = to_est(c.get("start", ""))
        reasoning = c.get("reasoning", c.get("pass_reason", ""))
        balance = c.get("balance", 0) / 100 if c.get("balance") else 0
        rows += (
            f'<tr><td class="timestamp">{ts}</td>'
            f'<td><span class="badge {badge_class}">{action}</span></td>'
            f'<td>${balance:.2f}</td>'
            f'<td>{c.get("duration_seconds", 0):.1f}s</td>'
            f'<td>{_accordion(reasoning)}</td></tr>'
        )
    return f'<table><tr><th>Time</th><th>Action</th><th>Balance</th><th>Duration</th><th>Reasoning</th></tr>{rows}</table>'


def render_trades_table(trades):
    if not trades:
        return '<p style="color:#8b949e;">No trades yet</p>'
    rows = ""
    for t in sorted(trades, key=lambda x: x.get("timestamp", ""), reverse=True)[:20]:
        ticker = t.get("ticker", "?")
        title = t.get("title", "")
        display_name = title if title else ticker
        side = t.get("side", "?").upper()
        count = t.get("count", 0)
        price = t.get("price", 0)
        cost = count * price
        ts = to_est(t.get("timestamp", ""))
        status = t.get("status", "?")

        if status == "executed":
            status_html = '<span class="badge badge-trade">LIVE</span>'
        elif status in ("settled", "won"):
            status_html = '<span class="badge badge-pass">WON</span>'
        elif status in ("lost",):
            status_html = '<span class="badge badge-error">LOST</span>'
        else:
            status_html = f'<span class="badge">{status}</span>'

        rows += (
            f'<tr><td class="timestamp">{ts}</td>'
            f"<td>{display_name}</td>"
            f"<td>{side}</td>"
            f"<td>{count}</td>"
            f"<td>{price}&cent;</td>"
            f"<td>${cost/100:.2f}</td>"
            f"<td>{status_html}</td></tr>"
        )
    return f'<table><tr><th>Time</th><th>Market</th><th>Side</th><th>Qty</th><th>Price</th><th>Cost</th><th>Status</th></tr>{rows}</table>'


def render_notes_table(notes):
    if not notes:
        return '<p style="color:#8b949e;">No notes yet</p>'
    rows = ""
    for n in sorted(notes[-10:], key=lambda x: x.get("created_at", ""), reverse=True):
        cat = n.get("category", "?")
        title = n.get("title", "")[:50]
        content = n.get("content", "")
        ts = to_est(n.get("created_at", ""))
        rows += (
            f'<tr><td class="timestamp">{ts}</td>'
            f"<td>{cat}</td><td>{title}</td>"
            f'<td>{_accordion(content)}</td></tr>'
        )
    return f'<table><tr><th>Time</th><th>Category</th><th>Title</th><th>Content</th></tr>{rows}</table>'


def render_scorecard_table(predictions):
    if not predictions:
        return '<p style="color:#8b949e; margin-top:12px;">No predictions yet. Scorecard updates when markets settle.</p>'
    rows = ""
    # Sort: settled first (by settled_at desc), then pending (by predicted_at desc)
    settled = sorted([p for p in predictions if p.get("result")], key=lambda x: x.get("settled_at", ""), reverse=True)
    pending = sorted([p for p in predictions if not p.get("result")], key=lambda x: x.get("predicted_at", ""), reverse=True)
    sorted_preds = (settled + pending)[:15]

    for p in sorted_preds:
        title = p.get("title", p.get("ticker", "?"))
        side = p.get("side", "?").upper()
        price = p.get("price", 0)
        ai_prob = p.get("ai_probability", "?")
        result = p.get("result")
        ts = to_est(p.get("settled_at", p.get("predicted_at", "")))

        if result == "win":
            result_html = '<span class="badge badge-pass">CORRECT ✓</span>'
        elif result == "loss":
            result_html = '<span class="badge badge-error">WRONG ✗</span>'
        else:
            result_html = '<span class="badge badge-trade">PENDING</span>'

        pnl = p.get("pnl_cents")
        pnl_str = f" ({pnl:+}¢)" if pnl is not None else ""

        rows += (
            f'<tr><td class="timestamp">{ts}</td>'
            f"<td>{title}</td>"
            f"<td>{side}</td>"
            f"<td>{price}&cent;</td>"
            f"<td>{ai_prob}%</td>"
            f"<td>{result_html}{pnl_str}</td></tr>"
        )
    return f'<table style="margin-top:12px;"><tr><th>Date</th><th>Market</th><th>Side</th><th>Market Price</th><th>AI Estimate</th><th>Result</th></tr>{rows}</table>'


def render_lessons_table(notes):
    """Render the AI Lessons Learned table from winning/losing pattern notes."""
    learning = [n for n in notes if n.get("category") in ("winning_pattern", "losing_pattern")]
    if not learning:
        return '<p style="color:#8b949e;">No lessons yet. The AI learns from settled trades.</p>'

    rows = ""
    for n in sorted(learning, key=lambda x: x.get("created_at", ""), reverse=True):
        ts = to_est(n.get("created_at", ""))
        category = n.get("category", "")
        title = n.get("title", "")

        ticker = ""
        if " - " in title:
            ticker = title.split(" - ")[-1][:30]

        market_type = ""
        if ": " in title:
            parts = title.split(": ")
            if len(parts) >= 2:
                market_type = parts[1].split(" - ")[0]

        content = n.get("content", "")

        if category == "winning_pattern":
            badge = '<span class="badge badge-pass">WIN LESSON</span>'
        else:
            badge = '<span class="badge badge-error">LOSS LESSON</span>'

        rows += (
            f'<tr><td class="timestamp">{ts}</td>'
            f"<td>{badge}</td>"
            f"<td>{market_type}</td>"
            f"<td>{ticker}</td>"
            f'<td>{_accordion(content)}</td></tr>'
        )

    return f'<table><tr><th>Time</th><th>Type</th><th>Category</th><th>Trade</th><th>Lesson</th></tr>{rows}</table>'


def render_review_log_table(reviews):
    if not reviews:
        return '<p style="color:#8b949e;">No reviewer decisions yet.</p>'

    # Summary stats
    approved = sum(1 for r in reviews if r.get("outcome") == "approved")
    rejected = sum(1 for r in reviews if r.get("outcome") == "rejected")
    total = approved + rejected
    approval_rate = f"{approved/total*100:.0f}%" if total > 0 else "—"

    # Rejection reasons tally
    all_concerns = []
    for r in reviews:
        if r.get("outcome") == "rejected":
            all_concerns.extend(r.get("concerns", []))

    summary = (
        f'<p style="color:#8b949e; margin-bottom:10px;">'
        f'Approved: <span class="positive">{approved}</span> | '
        f'Rejected: <span class="negative">{rejected}</span> | '
        f'Approval rate: {approval_rate}</p>'
    )

    rows = ""
    for r in sorted(reviews[-20:], key=lambda x: x.get("timestamp", ""), reverse=True):
        ts = to_est(r.get("timestamp", ""))
        title = r.get("title", r.get("ticker", "?"))[:45]
        outcome = r.get("outcome", "?")
        outcome_html = ('<span class="badge badge-pass">APPROVED</span>'
                       if outcome == "approved"
                       else '<span class="badge badge-error">REJECTED</span>')
        confidence = r.get("confidence", 0)
        edge = r.get("edge", 0)
        concerns = "; ".join(r.get("concerns", []))

        rows += (
            f'<tr><td class="timestamp">{ts}</td>'
            f"<td>{title}</td>"
            f"<td>{r.get('side', '?').upper()}</td>"
            f"<td>{r.get('price', 0)}&cent;</td>"
            f"<td>{edge:+}&cent;</td>"
            f"<td>{outcome_html}</td>"
            f"<td>{confidence}%</td>"
            f'<td>{_accordion(concerns)}</td></tr>'
        )

    table = f'<table><tr><th>Time</th><th>Market</th><th>Side</th><th>Price</th><th>Edge</th><th>Decision</th><th>Confidence</th><th>Concerns</th></tr>{rows}</table>'
    return summary + table


def render_activity_feed(trades, reviews, scorecard_predictions, lessons, cycles=None):
    """Render a unified activity feed combining all events chronologically."""
    events = []

    # Add recent cycles (pass/trade attempts) so the feed is never empty
    if cycles:
        for c in cycles[-20:]:
            action = c.get("action", "")
            if action == "pass":
                events.append({
                    "ts": c.get("start", ""),
                    "type": "cycle",
                    "badge": "PASS",
                    "badge_class": "badge-pass",
                    "title": "Agent cycle — no trade",
                    "detail": c.get("pass_reason", c.get("reasoning", ""))[:100],
                    "status": f"{c.get('markets_available', '?')} markets scanned",
                    "extra": "",
                })

    # Add trades
    for t in trades:
        events.append({
            "ts": t.get("timestamp", ""),
            "type": "trade",
            "badge": "TRADE",
            "badge_class": "badge-trade",
            "title": t.get("title", t.get("ticker", "?")),
            "detail": f"{t.get('side','?').upper()} {t.get('count',0)}x @ {t.get('price',0)}&cent; = ${t.get('count',0)*t.get('price',0)/100:.2f}",
            "status": t.get("status", ""),
            "extra": f"Redirected from {t['redirected_from']}" if t.get("redirected_from") else "",
        })

    # Add rejections and blocks
    for r in reviews:
        if r.get("outcome") == "rejected":
            events.append({
                "ts": r.get("timestamp", ""),
                "type": "rejected",
                "badge": "REJECTED",
                "badge_class": "badge-error",
                "title": r.get("title", r.get("ticker", "?")),
                "detail": f"{r.get('side','?').upper()} @ {r.get('price',0)}&cent; | Edge: {r.get('edge',0):+}&cent;",
                "status": "",
                "extra": "; ".join(r.get("concerns", [])),
            })
        elif r.get("outcome") == "blocked":
            events.append({
                "ts": r.get("timestamp", ""),
                "type": "rejected",
                "badge": "BLOCKED",
                "badge_class": "badge-error",
                "title": r.get("title", r.get("ticker", "?")),
                "detail": r.get("recommendation", ""),
                "status": "",
                "extra": "",
            })

    # Add settlements
    for p in scorecard_predictions:
        if p.get("result"):
            events.append({
                "ts": p.get("settled_at", p.get("predicted_at", "")),
                "type": "settled",
                "badge": "WON" if p["result"] == "win" else "LOST",
                "badge_class": "badge-pass" if p["result"] == "win" else "badge-error",
                "title": p.get("title", p.get("ticker", "?")),
                "detail": f"{p.get('side','?').upper()} @ {p.get('price',0)}&cent; | AI: {p.get('ai_probability','?')}%",
                "status": f"{p.get('pnl_cents',0):+}&cent;",
                "extra": "",
            })

    # Add lessons
    for n in lessons:
        events.append({
            "ts": n.get("created_at", ""),
            "type": "lesson",
            "badge": "LESSON",
            "badge_class": "badge-pass" if n.get("category") == "winning_pattern" else "badge-error",
            "title": n.get("title", "")[:50],
            "detail": "",
            "status": "",
            "extra": n.get("content", ""),
        })

    events.sort(key=lambda x: x.get("ts", ""), reverse=True)

    if not events:
        return '<p style="color:#8b949e;">No activity yet.</p>'

    rows = ""
    for e in events[:60]:
        ts = to_est(e["ts"])
        badge_html = f'<span class="badge {e["badge_class"]}">{e["badge"]}</span>'
        status_html = f' <span style="color:#58a6ff;">{e["status"]}</span>' if e["status"] else ""
        extra_html = _accordion(e["extra"]) if e.get("extra") else ""

        rows += (
            f'<tr data-type="{e["type"]}">'
            f'<td class="timestamp">{ts}</td>'
            f'<td>{badge_html}</td>'
            f'<td>{e["title"]}</td>'
            f'<td>{e["detail"]}{status_html}</td>'
            f'<td>{extra_html}</td>'
            f'</tr>'
        )

    return f'<table><tr><th>Time</th><th>Event</th><th>Market</th><th>Details</th><th>Context</th></tr>{rows}</table>'


HTML_PAGE = Template("""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Kalshi Trading Agent</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f1117; color: #e1e4e8; padding: 20px; }
        .container { max-width: 1200px; margin: 0 auto; }
        h1 { color: #58a6ff; margin-bottom: 20px; font-size: 1.8em; }
        h2 { color: #8b949e; margin: 20px 0 10px; font-size: 1.2em; text-transform: uppercase; letter-spacing: 1px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 16px; margin-bottom: 24px; }
        .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 20px; }
        .card-label { color: #8b949e; font-size: 0.85em; margin-bottom: 4px; }
        .card-value { font-size: 1.8em; font-weight: 600; }
        .positive { color: #3fb950; }
        .negative { color: #f85149; }
        .neutral { color: #58a6ff; }
        .status-active { color: #3fb950; }
        .status-inactive { color: #8b949e; }
        .control-panel { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 20px; margin-bottom: 24px; }
        .control-panel input { background: #0d1117; border: 1px solid #30363d; color: #e1e4e8; padding: 8px 12px; border-radius: 4px; font-size: 1em; width: 100px; }
        .control-panel button { padding: 8px 16px; border-radius: 4px; border: none; font-size: 0.9em; cursor: pointer; margin-left: 8px; font-weight: 500; }
        .btn-trade { background: #238636; color: white; }
        .btn-trade:hover { background: #2ea043; }
        .btn-stop { background: #da3633; color: white; }
        .btn-stop:hover { background: #f85149; }
        .btn-refresh { background: #30363d; color: #e1e4e8; }
        .btn-refresh:hover { background: #484f58; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; }
        th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid #21262d; font-size: 0.9em; }
        th { color: #8b949e; font-weight: 500; }
        tr:hover { background: #1c2128; }
        .badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 0.75em; font-weight: 500; }
        .badge-pass { background: #1f2d1f; color: #3fb950; }
        .badge-trade { background: #1f2937; color: #58a6ff; }
        .badge-error { background: #2d1f1f; color: #f85149; }
        .mode-indicator { display: inline-block; padding: 4px 10px; border-radius: 4px; font-size: 0.8em; font-weight: 600; margin-left: 12px; }
        .mode-production { background: rgba(248,81,73,0.13); color: #f85149; border: 1px solid #f85149; }
        .mode-demo { background: rgba(63,185,80,0.13); color: #3fb950; border: 1px solid #3fb950; }
        .timestamp { color: #8b949e; font-size: 0.8em; }
        .expandable { color: #8b949e; cursor: pointer; }
        .expandable::before { content: '▶ '; font-size: 0.7em; }
        .expandable.open::before { content: '▼ '; }
        .expand-content { display: none; padding: 8px 0; white-space: pre-wrap; word-break: break-word; color: #c9d1d9; font-size: 0.85em; line-height: 1.5; }
        .expand-content.open { display: block; }
        .section { margin-bottom: 32px; }
        .refresh-time { color: #8b949e; font-size: 0.8em; float: right; margin-top: 4px; }
        .notification { padding: 14px 20px; border-radius: 8px; margin-bottom: 20px; font-weight: 500; }
        .notification-deployed { background: rgba(56,139,253,0.15); border: 1px solid #388bfd; color: #58a6ff; }
        .notification-busted { background: rgba(248,81,73,0.15); border: 1px solid #f85149; color: #f85149; }
        .notification-profit { background: rgba(63,185,80,0.15); border: 1px solid #3fb950; color: #3fb950; }

        /* Tab Navigation */
        .tab-bar { background: #161b22; border-bottom: 1px solid #30363d; border-radius: 8px 8px 0 0; margin-bottom: 0; display: flex; overflow-x: auto; }
        .tab-btn { padding: 12px 24px; color: #8b949e; border: none; border-bottom: 2px solid transparent; background: none; cursor: pointer; font-size: 0.95em; font-weight: 500; white-space: nowrap; transition: color 0.2s, border-color 0.2s; }
        .tab-btn:hover { color: #c9d1d9; }
        .tab-btn.active { color: #e1e4e8; border-bottom: 2px solid #58a6ff; }
        .tab-content { display: none; padding-top: 24px; }
        .tab-content.active { display: block; }
        .filter-btn { padding: 6px 14px; border-radius: 16px; border: 1px solid #30363d; background: none; color: #8b949e; cursor: pointer; font-size: 0.85em; }
        .filter-btn:hover { border-color: #58a6ff; color: #c9d1d9; }
        .filter-btn.active { background: #58a6ff; color: #0f1117; border-color: #58a6ff; font-weight: 500; }
        .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; margin-bottom: 24px; }
        .stat-card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 24px; text-align: center; }
        .stat-card .card-label { font-size: 0.9em; margin-bottom: 8px; }
        .stat-card .card-value { font-size: 2.2em; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Kalshi Trading Agent
            <span class="mode-indicator mode-$mode">$mode_upper</span>
            <span id="health-indicator" style="display:inline-block; margin-left:12px; padding:4px 10px; border-radius:4px; font-size:0.7em; font-weight:600; background:rgba(139,148,158,0.13); color:#8b949e; border:1px solid #30363d;">⏳ Checking...</span>
            <span class="refresh-time">Last refresh: $refresh_time</span>
        </h1>

        $notification

        <!-- Tab Navigation -->
        <div class="tab-bar">
            <button class="tab-btn active" data-tab="overview">Overview</button>
            <button class="tab-btn" data-tab="feed">Activity Feed</button>
        </div>

        <!-- TAB 1: Overview -->
        <div class="tab-content active" id="tab-overview">
            <div class="stats-grid">
                <div class="stat-card">
                    <div class="card-label">Return on Investment</div>
                    <div class="card-value $roi_class">$roi</div>
                </div>
                <div class="stat-card">
                    <div class="card-label">Profit</div>
                    <div class="card-value $pnl_class">$$$total_pnl</div>
                </div>
                <div class="stat-card">
                    <div class="card-label">Win Rate</div>
                    <div class="card-value $win_rate_class">$win_rate</div>
                </div>
                <div class="stat-card">
                    <div class="card-label">Account Value</div>
                    <div class="card-value">$$$account_value</div>
                </div>
                <div class="stat-card">
                    <div class="card-label">Available Cash</div>
                    <div class="card-value neutral">$$$balance</div>
                </div>
                <div class="stat-card">
                    <div class="card-label">Open Bets</div>
                    <div class="card-value">$open_positions</div>
                </div>
            </div>

            <div class="control-panel">
                <h2 style="margin-top:0">Trading Controls</h2>
                <p style="color:#8b949e; margin-bottom:12px;">
                    Budget: <strong class="$budget_status_class">$budget_status</strong> $budget_info
                </p>
                <p style="color:#8b949e; margin-top:-4px; margin-bottom:12px; font-size:0.9em;">
                    Trading budget follows live Kalshi available cash: <strong>$$$available_cash</strong>.
                </p>
                <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
                    <label style="color:#8b949e;">Available cash $$</label>
                    <input type="number" id="trade-amount" step="0.50" min="0" max="$available_cash" value="$available_cash" readonly />
                    <button class="btn-trade" onclick="startTrading()">Start Trading With Available Cash</button>
                    <button class="btn-stop" onclick="stopTrading()">Stop Trading</button>
                    <button class="btn-refresh" onclick="window.location.reload()">Refresh</button>
                </div>
                <div id="status-msg" style="margin-top:12px; display:none; padding:10px 14px; border-radius:6px; font-size:0.9em;"></div>
            </div>

            <div class="section">
                <h2>Open Positions</h2>
                $positions_table
            </div>
        </div>

        <!-- TAB 2: Activity Feed -->
        <div class="tab-content" id="tab-feed">
            <div style="margin-bottom:16px; display:flex; gap:8px; flex-wrap:wrap;">
                <button class="filter-btn active" data-filter="all">All</button>
                <button class="filter-btn" data-filter="trade">Trades</button>
                <button class="filter-btn" data-filter="rejected">Rejected</button>
                <button class="filter-btn" data-filter="settled">Settled</button>
                <button class="filter-btn" data-filter="lesson">Lessons</button>
                <button class="filter-btn" data-filter="cycle">Cycles</button>
            </div>
            $activity_feed
        </div>
    </div>

    <script>
        function showStatus(msg, type) {
            const el = document.getElementById('status-msg');
            el.style.display = 'block';
            el.textContent = msg;
            if (type === 'success') {
                el.style.background = 'rgba(63,185,80,0.15)';
                el.style.border = '1px solid #3fb950';
                el.style.color = '#3fb950';
            } else if (type === 'error') {
                el.style.background = 'rgba(248,81,73,0.15)';
                el.style.border = '1px solid #f85149';
                el.style.color = '#f85149';
            } else {
                el.style.background = 'rgba(56,139,253,0.15)';
                el.style.border = '1px solid #388bfd';
                el.style.color = '#58a6ff';
            }
            console.log('[Dashboard]', type.toUpperCase() + ':', msg);
        }

        async function startTrading() {
            const amount = document.getElementById('trade-amount').value;
            if (!confirm('Confirm: Use $$' + amount + ' of available Kalshi cash as the trading budget?\\n\\nThe agent reads live Kalshi cash before each cycle and will not use stale deployed/returned tracking.')) {
                console.log('[Dashboard] Trade cancelled by user');
                return;
            }

            showStatus('Sending to agent...', 'info');
            console.log('[Dashboard] POST /trade amount=cash');

            try {
                const resp = await fetch('/trade', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                    body: 'amount=cash'
                });
                console.log('[Dashboard] Response:', resp.status, resp.statusText);

                if (resp.ok || resp.redirected) {
                    showStatus('Trading budget set to current Kalshi cash ($$' + amount + ') and synced to agent.', 'success');
                    console.log('[Dashboard] Budget synced successfully');
                    setTimeout(() => window.location.reload(), 2000);
                } else {
                    const text = await resp.text();
                    showStatus('Failed: ' + resp.status + ' ' + text, 'error');
                    console.error('[Dashboard] Error:', text);
                }
            } catch (e) {
                showStatus('Network error: ' + e.message, 'error');
                console.error('[Dashboard] Network error:', e);
            }
        }

        async function stopTrading() {
            if (!confirm('Stop trading? The agent will stop placing new trades.\\n\\nExisting positions will remain until settlement.')) {
                console.log('[Dashboard] Stop cancelled by user');
                return;
            }

            showStatus('Stopping agent...', 'info');
            console.log('[Dashboard] POST /stop');

            try {
                const resp = await fetch('/stop', {
                    method: 'POST'
                });
                console.log('[Dashboard] Response:', resp.status, resp.statusText);

                if (resp.ok || resp.redirected) {
                    showStatus('Trading stopped. Agent will only monitor positions.', 'success');
                    console.log('[Dashboard] Trading stopped');
                    setTimeout(() => window.location.reload(), 2000);
                } else {
                    showStatus('Failed to stop: ' + resp.status, 'error');
                }
            } catch (e) {
                showStatus('Network error: ' + e.message, 'error');
                console.error('[Dashboard] Network error:', e);
            }
        }

        console.log('[Dashboard] Loaded at', new Date().toISOString());
        console.log('[Dashboard] Mode:', '$mode_upper');

        // Sync from droplet on load (non-blocking)
        fetch('/api/sync').then(r => r.json()).then(d => {
            console.log('[Dashboard] Sync result:', d);
            if (d.synced) console.log('[Dashboard] Data synced from droplet');
        }).catch(e => console.warn('[Dashboard] Sync failed (offline?):', e.message));

        // Auto-refresh every 60 seconds
        setInterval(() => {
            console.log('[Dashboard] Auto-refreshing...');
            window.location.reload();
        }, 60000);

        // Accordion expand/collapse for table rows
        document.addEventListener('click', function(e) {
            if (e.target.classList.contains('expandable')) {
                e.target.classList.toggle('open');
                const content = e.target.querySelector('.expand-content');
                if (content) content.classList.toggle('open');
            }
        });

        // Activity feed filters
        document.querySelectorAll('.filter-btn').forEach(btn => {
            btn.addEventListener('click', function() {
                document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
                this.classList.add('active');
                const filter = this.dataset.filter;
                document.querySelectorAll('#tab-feed tr[data-type]').forEach(row => {
                    row.style.display = (filter === 'all' || row.dataset.type === filter) ? '' : 'none';
                });
            });
        });

        // Health check - pings droplet every 15 seconds
        function checkHealth() {
            const el = document.getElementById('health-indicator');
            fetch('http://159.89.224.165:9090/health', {mode: 'cors'})
                .then(r => r.json())
                .then(d => {
                    if (d.status === 'ok') {
                        // Check if last cycle is stale (>25 min old)
                        if (d.last_cycle) {
                            const lastCycle = new Date(d.last_cycle + 'Z');
                            const minutesAgo = (Date.now() - lastCycle.getTime()) / 60000;
                            if (minutesAgo > 25) {
                                el.textContent = '● Agent Stale (' + Math.round(minutesAgo) + 'm ago)';
                                el.style.background = 'rgba(210,153,34,0.13)';
                                el.style.color = '#d2992a';
                                el.style.borderColor = '#d2992a';
                            } else {
                                el.textContent = '● Agent Online';
                                el.style.background = 'rgba(63,185,80,0.13)';
                                el.style.color = '#3fb950';
                                el.style.borderColor = '#3fb950';
                            }
                        } else {
                            el.textContent = '● API Up (no cycles)';
                            el.style.background = 'rgba(210,153,34,0.13)';
                            el.style.color = '#d2992a';
                            el.style.borderColor = '#d2992a';
                        }
                    }
                })
                .catch(e => {
                    el.textContent = '● Agent Offline';
                    el.style.background = 'rgba(248,81,73,0.13)';
                    el.style.color = '#f85149';
                    el.style.borderColor = '#f85149';
                });
        }
        checkHealth();
        setInterval(checkHealth, 15000);

        // Tab switching
        (function() {
            const tabBtns = document.querySelectorAll('.tab-btn');
            const tabPanels = document.querySelectorAll('.tab-content');

            // Restore active tab from localStorage
            const savedTab = localStorage.getItem('activeTab');
            if (savedTab) {
                const savedBtn = document.querySelector('.tab-btn[data-tab="' + savedTab + '"]');
                const savedPanel = document.getElementById('tab-' + savedTab);
                if (savedBtn && savedPanel) {
                    tabBtns.forEach(b => b.classList.remove('active'));
                    tabPanels.forEach(p => p.classList.remove('active'));
                    savedBtn.classList.add('active');
                    savedPanel.classList.add('active');
                }
            }

            tabBtns.forEach(btn => {
                btn.addEventListener('click', function() {
                    const tabId = this.getAttribute('data-tab');

                    tabBtns.forEach(b => b.classList.remove('active'));
                    tabPanels.forEach(p => p.classList.remove('active'));

                    this.classList.add('active');
                    document.getElementById('tab-' + tabId).classList.add('active');

                    localStorage.setItem('activeTab', tabId);
                });
            });
        })();
    </script>
</body>
</html>""")


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/status":
            self._json_response(self._get_status())
            return
        if self.path == "/api/sync":
            # Manual sync from droplet
            _sync_from_droplet("trading_budget.json")
            _sync_from_droplet("trade_history.json")
            _sync_from_droplet("scorecard.json")
            _sync_from_droplet("review_log.json")
            _sync_from_droplet("cycle_logs.json")
            _sync_from_droplet("agent_notes.json")
            _sync_from_droplet("portfolio_state.json")
            self._json_response({"ok": True, "synced": True})
            return
        self._serve_dashboard()

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8") if content_length else ""
        parsed = urlparse(self.path)

        if parsed.path == "/trade":
            params = parse_qs(body)
            amount = params.get("amount", ["cash"])[0]
            budget = set_trading_budget(amount)
            self._json_response({"ok": True, "action": "trade", "budget": budget})
        elif parsed.path == "/stop":
            budget = stop_trading()
            self._json_response({"ok": True, "action": "stop", "budget": budget})
        else:
            self._redirect("/")

    def _get_status(self):
        live = get_live_data()
        budget = get_trading_budget()
        cycles = get_cycle_logs()
        trades = get_trade_history()
        return {
            "balance": live.get("balance", 0),
            "connected": live.get("connected", False),
            "budget": budget,
            "total_cycles": len(cycles),
            "total_trades": len(trades),
        }

    def _serve_dashboard(self):
        # Load local data (synced on POST actions, not every page load)
        _sync_from_droplet("portfolio_state.json")
        live = get_live_data()
        cycles = get_cycle_logs()
        trades = get_trade_history()
        notes = get_agent_notes()
        budget = get_trading_budget()

        balance_cents = live.get("balance", 0)
        portfolio_val = live.get("portfolio_value", 0)
        account_value = balance_cents + portfolio_val
        positions = live.get("positions", [])

        # Calculate PnL from scorecard (settled predictions)
        scorecard = update_scorecard_from_settlements()
        settled_pnl_cents = sum(
            p.get("pnl_cents", 0) for p in scorecard.get("predictions", [])
            if p.get("pnl_cents") is not None
        )

        # Total profit = account value now minus original deposit
        # Use account_value vs initial deposit (stored in budget or fallback to $100)
        initial_deposit_cents = 10000  # $100 initial deposit
        total_pnl_cents = account_value - initial_deposit_cents

        # ROI based on total profit vs total capital at risk (settled + deployed)
        settled_cost_basis = sum(p.get("price", 0) for p in scorecard.get("predictions", []) if p.get("result"))

        # Count open positions using correct field
        open_pos_count = sum(
            1 for p in positions
            if float(p.get("position_fp", p.get("position", 0)) or 0) != 0
        )

        if budget.get("active"):
            budget_status = f"ACTIVE \u2014 ${balance_cents/100:.2f} available cash"
            budget_status_class = "status-active"
            stored_budget = budget.get("budget_cents", 0)
            budget_info = f"(stored cap ${stored_budget/100:.2f}; Kalshi cash is source of truth)"
        else:
            budget_status = "INACTIVE \u2014 agent will not trade"
            budget_status_class = "status-inactive"
            budget_info = ""

        # Build notification
        notification = ""
        if budget.get("active"):
            pnl = budget.get("realized_pnl", 0)
            initial = budget["budget_cents"]
            current_bankroll = initial + pnl
            # Check if bankroll has been deployed (spent)
            if pnl < 0 and abs(pnl) >= initial * 0.5:
                spent = abs(pnl)
                notification = (
                    f'<div class="notification notification-deployed">'
                    f'\u2705 Bankroll deployed! ${spent/100:.2f} of ${initial/100:.2f} has been traded. '
                    f'Positions are live \u2014 waiting for settlement.</div>'
                )
            if current_bankroll <= 0 and len([p for p in positions if p.get("position", 0) != 0]) == 0:
                notification = (
                    '<div class="notification notification-busted">'
                    '\u274c Bankroll busted. All funds have been lost. Set a new bankroll to continue.</div>'
                )
        elif budget.get("deactivated_reason"):
            notification = (
                f'<div class="notification notification-busted">'
                f'\u274c {budget["deactivated_reason"]}</div>'
            )

        # Check for profit
        if budget.get("active") and budget.get("realized_pnl", 0) > 0:
            profit = budget["realized_pnl"]
            notification = (
                f'<div class="notification notification-profit">'
                f'\U0001f4b0 Bankroll is profitable! +${profit/100:.2f} realized gains. '
                f'Total bankroll: ${(budget["budget_cents"] + profit)/100:.2f}</div>'
            )

        # Get scorecard data (already fetched above for PnL)
        wins = scorecard.get("wins", 0)
        losses = scorecard.get("losses", 0)
        pending = scorecard.get("pending", 0)
        total_resolved = wins + losses
        win_rate_pct = f"{wins/total_resolved*100:.0f}%" if total_resolved > 0 else "—"
        win_rate_class = "positive" if total_resolved > 0 and wins >= losses else "negative" if total_resolved > 0 else "neutral"

        # ROI = total profit / all capital allocated to trades (including fees)
        # Capital allocated = what we spent on settled trades + what's currently deployed in open positions + fees paid
        settled_cost_basis = sum(p.get("price", 0) for p in scorecard.get("predictions", []) if p.get("result"))
        fees_estimate = int(settled_cost_basis * 0.048)  # ~4.8% trading fees on settled
        total_capital_allocated = settled_cost_basis + portfolio_val + fees_estimate
        roi_pct = total_pnl_cents / total_capital_allocated * 100 if total_capital_allocated > 0 else 0
        roi_str = f"{roi_pct:+.1f}%" if total_capital_allocated > 0 else "—"
        roi_class = "positive" if roi_pct > 0 else "negative" if roi_pct < 0 else "neutral"

        # Build unified activity feed
        review_log = get_review_log()
        learning_notes = [n for n in notes if n.get("category") in ("winning_pattern", "losing_pattern")]

        html = HTML_PAGE.substitute(
            mode=config.TRADING_MODE,
            mode_upper=config.TRADING_MODE.upper(),
            refresh_time=now_est(),
            notification=notification,
            balance=f"{balance_cents/100:.2f}",
            available_cash=f"{balance_cents/100:.2f}",
            account_value=f"{account_value/100:.2f}",
            total_pnl=f"{total_pnl_cents/100:+.2f}",
            pnl_class="positive" if total_pnl_cents >= 0 else "negative",
            open_positions=open_pos_count,
            total_trades=len(trades),
            win_rate=win_rate_pct,
            win_rate_class=win_rate_class,
            roi=roi_str,
            roi_class=roi_class,
            budget_status=budget_status,
            budget_status_class=budget_status_class,
            budget_info=budget_info,
            positions_table=render_positions_table(positions),
            activity_feed=render_activity_feed(trades, review_log, scorecard.get("predictions", []), learning_notes, cycles),
        )

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())

    def _json_response(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2).encode())

    def _redirect(self, location):
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def log_message(self, format, *args):
        pass

    def handle_one_request(self):
        """Override to suppress BrokenPipeError when browser disconnects."""
        try:
            super().handle_one_request()
        except BrokenPipeError:
            pass


def main():
    print(f"Starting dashboard at http://localhost:{PORT}")
    print(f"Mode: {config.TRADING_MODE}")
    print(f"Press Ctrl+C to stop.\n")

    import socketserver
    socketserver.TCPServer.allow_reuse_address = True
    server = HTTPServer(("0.0.0.0", PORT), DashboardHandler)
    server.allow_reuse_address = True

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
