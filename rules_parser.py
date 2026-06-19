"""Market rules parser - extracts structured settlement criteria from Kalshi rules text."""
import re
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class ParsedRules:
    data_source: str | None = None
    threshold: float | None = None
    operator: str | None = None  # "above", "below", "between", "equal"
    time_window: str | None = None
    time_window_utc: str | None = None
    measurement_method: str | None = None
    raw_text: str = ""
    is_parseable: bool = False


class RulesParser:
    """Extracts structured settlement criteria from market rules text."""

    # Regex patterns for common Kalshi rule formats
    PATTERNS = {
        "temperature": re.compile(
            r"(?:maximum|minimum|high|low)\s+temperature.*?"
            r"(?:above|below|greater than|less than|between)\s*"
            r"(\d+\.?\d*)",
            re.IGNORECASE
        ),
        "crypto_price": re.compile(
            r"(?:Bitcoin|Ethereum|BTC|ETH).*?"
            r"(?:Real-?Time\s+Index|price).*?"
            r"(?:above|below|greater than)\s*"
            r"\$?([\d,]+\.?\d*)",
            re.IGNORECASE
        ),
        "commodity_price": re.compile(
            r"(?:brent|crude|oil|gold|silver).*?"
            r"(?:close|settlement|spot)\s*price.*?"
            r"(?:above|below|greater than)\s*"
            r"\$?([\d,]+\.?\d*)\s*(?:USD|usd)?",
            re.IGNORECASE
        ),
        "sports_outcome": re.compile(
            r"(?:will|if)\s+(.+?)\s+(?:win|beat|defeat|advance)",
            re.IGNORECASE
        ),
        "threshold_generic": re.compile(
            r"(?:above|below|greater than|less than|at least|more than|fewer than)\s*"
            r"\$?([\d,]+\.?\d*)",
            re.IGNORECASE
        ),
    }

    TIME_PATTERNS = {
        "time_edt": re.compile(r"(\d{1,2}(?::\d{2})?\s*(?:AM|PM|am|pm)?\s*(?:EDT|EST|ET))", re.IGNORECASE),
        "time_pdt": re.compile(r"(\d{1,2}(?::\d{2})?\s*(?:AM|PM|am|pm)?\s*(?:PDT|PST|PT))", re.IGNORECASE),
        "time_utc": re.compile(r"(\d{1,2}(?::\d{2})?\s*(?:AM|PM|am|pm)?\s*UTC)", re.IGNORECASE),
        "date": re.compile(r"(?:on|before|by)\s+(\w+\s+\d{1,2},?\s*\d{4}|\w+\s+\d{1,2})", re.IGNORECASE),
    }

    DATA_SOURCE_PATTERNS = [
        re.compile(r"(CF Benchmarks['\s]?\w*\s*(?:Real-?Time\s+)?Index)", re.IGNORECASE),
        re.compile(r"(National Weather Service|NWS)", re.IGNORECASE),
        re.compile(r"recorded at\s+(\w{3,4})\s", re.IGNORECASE),  # Airport codes like DCA
        re.compile(r"(Bureau of Labor Statistics|BLS)", re.IGNORECASE),
        re.compile(r"(ICE Brent|NYMEX|CME)", re.IGNORECASE),
    ]

    OPERATOR_PATTERNS = {
        "above": re.compile(r"\b(?:above|greater than|more than|over|exceeds?)\b", re.IGNORECASE),
        "below": re.compile(r"\b(?:below|less than|fewer than|under)\b", re.IGNORECASE),
        "between": re.compile(r"\b(?:between|range)\b", re.IGNORECASE),
        "equal": re.compile(r"\b(?:exactly|equal to)\b", re.IGNORECASE),
    }

    def parse(self, rules_text: str) -> ParsedRules:
        """Extract structured settlement criteria from rules_primary text."""
        if not rules_text or len(rules_text.strip()) < 10:
            return ParsedRules(raw_text=rules_text or "", is_parseable=False)

        parsed = ParsedRules(raw_text=rules_text)

        # Extract data source
        for pattern in self.DATA_SOURCE_PATTERNS:
            match = pattern.search(rules_text)
            if match:
                parsed.data_source = match.group(1).strip()
                break

        # Extract operator
        for op_name, op_pattern in self.OPERATOR_PATTERNS.items():
            if op_pattern.search(rules_text):
                parsed.operator = op_name
                break

        # Extract threshold using category-specific patterns
        for category, pattern in self.PATTERNS.items():
            match = pattern.search(rules_text)
            if match:
                try:
                    value_str = match.group(1).replace(",", "")
                    parsed.threshold = float(value_str)
                    parsed.is_parseable = True

                    # Extract measurement method for certain categories
                    if "average" in rules_text.lower():
                        parsed.measurement_method = "average"
                    elif "maximum" in rules_text.lower():
                        parsed.measurement_method = "maximum"
                    elif "minimum" in rules_text.lower():
                        parsed.measurement_method = "minimum"
                    elif "close" in rules_text.lower():
                        parsed.measurement_method = "closing price"
                    break
                except (ValueError, IndexError):
                    continue

        # Extract time window
        for time_name, time_pattern in self.TIME_PATTERNS.items():
            match = time_pattern.search(rules_text)
            if match:
                parsed.time_window = match.group(1).strip()
                parsed.time_window_utc = self._convert_to_utc(parsed.time_window)
                break

        # If we got at least a threshold or data source, mark as parseable
        if parsed.threshold is not None or parsed.data_source:
            parsed.is_parseable = True

        return parsed

    def summarize(self, parsed: ParsedRules) -> str:
        """Generate a concise human-readable summary (max 500 chars)."""
        if not parsed.is_parseable:
            # Return truncated raw text
            return parsed.raw_text[:500] if parsed.raw_text else "No rules available"

        parts = []

        if parsed.data_source:
            parts.append(f"Source: {parsed.data_source}")

        if parsed.measurement_method:
            parts.append(f"Measure: {parsed.measurement_method}")

        if parsed.operator and parsed.threshold is not None:
            parts.append(f"Condition: {parsed.operator} {parsed.threshold}")

        if parsed.time_window:
            parts.append(f"Time: {parsed.time_window}")
            if parsed.time_window_utc:
                parts.append(f"(UTC: {parsed.time_window_utc})")

        summary = " | ".join(parts)
        if not summary:
            return parsed.raw_text[:500]

        return summary[:500]

    def is_settlement_window_passed(self, parsed: ParsedRules) -> bool | None:
        """Determine if the measurement time window has already passed."""
        if not parsed.time_window_utc:
            return None

        try:
            # Try to parse the UTC time
            now = datetime.now(timezone.utc)
            # Simple hour extraction
            hour_match = re.search(r"(\d{1,2}):?(\d{2})?\s*UTC", parsed.time_window_utc)
            if hour_match:
                hour = int(hour_match.group(1))
                minute = int(hour_match.group(2) or "0")
                settlement_time = now.replace(hour=hour, minute=minute, second=0)
                return now > settlement_time
        except (ValueError, AttributeError):
            pass

        return None

    def _convert_to_utc(self, time_str: str) -> str | None:
        """Convert a time string with timezone to UTC representation."""
        if not time_str:
            return None

        # EDT = UTC-4, EST = UTC-5, PDT = UTC-7, PST = UTC-8
        offsets = {"EDT": 4, "EST": 5, "ET": 4, "PDT": 7, "PST": 8, "PT": 7}

        for tz_name, offset in offsets.items():
            if tz_name in time_str.upper():
                # Extract hour
                hour_match = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(?:AM|PM|am|pm)?", time_str)
                if hour_match:
                    hour = int(hour_match.group(1))
                    minute = int(hour_match.group(2) or "0")

                    # Handle AM/PM
                    if "PM" in time_str.upper() and hour != 12:
                        hour += 12
                    elif "AM" in time_str.upper() and hour == 12:
                        hour = 0

                    utc_hour = (hour + offset) % 24
                    return f"{utc_hour:02d}:{minute:02d} UTC"

        return None
