"""Public, unauthenticated source snapshots; no assertion of strict session eligibility."""

from datetime import datetime, timezone
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path
import argparse
import time

import requests

from validate_live import BA, write_json


SOURCES = (
    ("primary_faq", "https://apihub.primary.com.ar/#faq12", ("24x7",)),
    ("a3_hours", "https://a3mercados.com.ar/info-de-mercado/datos-de-mercado", ("RFX20",)),
    ("remarkets_status", "https://remarkets.primary.ventures/", ("mantenimiento", "banner")),
    ("national_calendar", "https://www.argentina.gob.ar/jefatura/feriados-nacionales-2026", ("2026", "Octubre", "octubre")),
    ("current_calendar", "https://www.argentina.gob.ar/feriados", ("2026", "Octubre", "octubre")),
    ("legacy_calendar", "https://www.argentina.gob.ar/interior/feriados-nacionales-2026", ()),
)


class Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def collect(output):
    output.mkdir(parents=True, exist_ok=False)
    now = datetime.now(timezone.utc)
    evidence = {
        "collected_at_utc": now.isoformat(), "unix_time_seconds": time.time(),
        "collected_at_buenos_aires": now.astimezone(BA).isoformat(),
        "weekday_number_buenos_aires": now.astimezone(BA).weekday(),
        "clock_method": "Machine datetime.now(timezone.utc), fixed UTC-03 BA conversion; not locale formatting. Machine clock not independently synchronized/verified.",
        "sources": [],
        "demo_policy": "REMARKETS is a 24/7 test environment: you can submit order requests outside production trading hours. Testing may work better during trading hours. Availability does not guarantee acceptance, execution, liquidity, fresh quotes or uninterrupted service.",
        "limitations": [
            "24x7 common-scenario testing is not a date-specific RFX20 live/production trading-session confirmation.",
            "A3 production schedule is not a demo availability guarantee or exchange holiday calendar.",
            "REMARKETS maintenance banner is service-status caution, not proof every API endpoint is unavailable.",
            "National holidays are not an A3 exchange calendar; calendar page may require client JavaScript. No date-specific trading-day or REMARKETS session assertion supplied.",
            "Strict session remains BLOCKED; any separately opted-in run measures demo observations only.",
        ],
    }
    for name, url, needles in SOURCES:
        item = {"name": name, "url": url, "requested_at_utc": datetime.now(timezone.utc).isoformat()}
        try:
            response = requests.get(url, timeout=(10, 30), allow_redirects=False)
            item["http_status"] = response.status_code
            item["body_sha256"] = sha256(response.content).hexdigest()
            # Public source bodies only. Never save cookies or response headers.
            with (output / (name + ".html")).open("xb") as f:
                f.write(response.content)
            parser = Text()
            parser.feed(response.content.decode("utf-8", errors="replace"))
            text = " ".join(" ".join(parser.parts).split())
            excerpts = []
            for needle in needles:
                index = text.casefold().find(needle.casefold())
                if index >= 0:
                    excerpts.append({"needle": needle, "text": text[max(0, index - 180):index + 500]})
            item["excerpts"] = excerpts
            item["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
        except requests.RequestException as exc:
            item["error_class"] = type(exc).__name__
        evidence["sources"].append(item)
    write_json(output / "source_evidence.json", evidence)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    collect(parser.parse_args().output_dir)
