from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from datetime import time
from pathlib import Path

import httpx
from defusedxml.common import DefusedXmlException
from dotenv import load_dotenv
from pydantic import ValidationError

from .clients import fingerprint
from .config import ArxivSettings, load_config, recipients, validate_credentials
from .demo import demo
from .http import USER_AGENT, ServiceError
from .operations import (
    actions_summary,
    assess_institutions,
    inspect_metadata,
    notify_failure,
    persist_outbox,
    test_delivery,
)
from .pipeline import run
from .store import Store, recipient_key


def daily_time(value: str) -> time:
    try:
        parsed = time.fromisoformat(value)
        if len(value) != 5 or parsed.tzinfo is not None:
            raise ValueError
        return parsed
    except ValueError:
        raise argparse.ArgumentTypeError("Use local time HH:MM, e.g. 09:30") from None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Jev → DeepSeek → email daily arXiv digest")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="Validate configuration without network calls")
    check.add_argument(
        "--credentials", action="store_true", help="Also require delivery credentials"
    )
    sub.add_parser("demo", help="Generate a fictional offline email preview; no keys needed")
    execute = sub.add_parser("run", help="Fetch, screen, summarize, and email")
    execute.add_argument(
        "--dry-run", action="store_true", help="Use real APIs; do not send or mark read"
    )
    execute.add_argument(
        "--persist-outbox",
        action="store_true",
        help="Persist the pending delivery with the trusted Actions state helper before SMTP",
    )
    execute.add_argument(
        "--daily-after",
        type=daily_time,
        metavar="HH:MM",
        help="Automatic daily delivery after local HH:MM; skip recipients already sent today",
    )
    execute.add_argument(
        "--recovery-days",
        type=int,
        choices=range(1, 32),
        metavar="1-31",
        help="Explicit API submission-window recovery; does not reset announcement checkpoints",
    )
    sub.add_parser("pending", help="List unresolved email attempts without revealing addresses")
    institutions = sub.add_parser(
        "institutions", help="Show/export learned institution scores and aliases"
    )
    institutions.add_argument(
        "--assess", nargs="+", help="Review 1-5 names with DeepSeek; no email"
    )
    inspect = sub.add_parser(
        "inspect", help="Verify metadata for 1-5 papers; uses APIs, never emails"
    )
    inspect.add_argument("paper_ids", nargs="+")
    inspect.add_argument("--refresh", action="store_true", help="Bypass metadata cache")
    delivery_test = sub.add_parser(
        "test-email", help="Explicit SMTP test; no models or paper-history changes"
    )
    delivery_test.add_argument("--persist-outbox", action="store_true")
    sub.add_parser(
        "status", help="Show latest health and API usage; write Actions summary if available"
    )
    notify = sub.add_parser(
        "notify-failure", help="Send optional failure alert; no model API calls"
    )
    notify.add_argument("--run-url", required=True)
    sub.add_parser(
        "reset-checkpoints",
        help="After manual gap recovery: clear source checkpoints; keep sent history",
    )
    resolve = sub.add_parser(
        "resolve", help="Resolve an uncertain email attempt after checking inbox"
    )
    resolve.add_argument("delivery_id")
    resolve.add_argument("--outcome", choices=["sent", "retry"], required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        config_path = args.config.resolve()
        load_dotenv(config_path.parent / ".env", override=False)
        config = load_config(config_path)
        if args.command == "check":
            if args.credentials:
                validate_credentials(config, send=True)
            print("Configuration valid. No network requests made.")
            print(f"Categories: {', '.join(config.arxiv.categories)}")
            print(f"Discovery source: {config.arxiv.source}")
            print(f"Decision model: {config.jev.backend} / {config.jev.model} (System One)")
            print(f"Summary model: {config.deepseek.model}; up to {config.selection.top_k} papers")
            if config.ranking.enabled:
                print(
                    f"Ranking: {1 - config.ranking.institution_weight:.0%} relevance + "
                    f"{config.ranking.institution_weight:.0%} institution; "
                    f"institution minimum {config.ranking.min_institution_score:.0%}; "
                    f"unknown metadata: {config.ranking.unknown_policy}"
                )
        elif args.command == "demo":
            print(f"Offline demo: {demo(config) / 'digest.html'}")
        elif args.command == "status":
            print(json.dumps(actions_summary(config), ensure_ascii=False, indent=2))
        elif args.command == "notify-failure":
            print(f"Failure alerts sent: {notify_failure(config, args.run_url)}")
        elif args.command == "inspect":
            with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=60) as client:
                result = inspect_metadata(config, client, args.paper_ids, refresh=args.refresh)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "test-email":
            result = test_delivery(
                config,
                outbox_hook=(lambda: persist_outbox(config_path.parent))
                if args.persist_outbox
                else None,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "reset-checkpoints":
            addresses = recipients(config)
            if not addresses:
                raise ValueError("Set EMAIL_TO before resetting source checkpoints")
            with Store(config.state_dir) as store:
                for address in addresses:
                    for category in config.arxiv.categories:
                        source = fingerprint(
                            {
                                "categories": [category],
                                "cross_lists": config.arxiv.include_cross_lists,
                                "mode": "latest",
                            }
                        )
                        store.reset_checkpoint(recipient_key(address), source)
            print(
                "Configured announcement checkpoints cleared. Sent and reviewed history preserved."
            )
        elif args.command == "institutions":
            if args.assess:
                with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=60) as client:
                    result = assess_institutions(config, client, args.assess)
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                with Store(config.state_dir) as store:
                    print(json.dumps(store.export_institutions(), ensure_ascii=False, indent=2))
        elif args.command in {"pending", "resolve"}:
            with Store(config.state_dir) as store:
                if args.command == "pending":
                    print(json.dumps(store.pending(), indent=2))
                else:
                    store.resolve(args.delivery_id, args.outcome)
                    print("Delivery resolved.")
        else:
            if args.recovery_days:
                if args.recovery_days > config.retention.cache_days:
                    raise ValueError("Recovery days must not exceed retention.cache_days")
                config.arxiv = ArxivSettings.model_validate(
                    {
                        **config.arxiv.model_dump(),
                        "source": "api",
                        "catch_up": False,
                        "lookback_days": args.recovery_days,
                    }
                )
            with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=60) as client:
                result = run(
                    config,
                    client,
                    dry_run=args.dry_run,
                    daily_after=args.daily_after,
                    outbox_hook=(lambda: persist_outbox(config_path.parent))
                    if args.persist_outbox
                    else None,
                )
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ValidationError as exc:
        # Do not echo configuration values, which could contain accidental secrets.
        errors = [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]
        logging.error("Invalid configuration: %s", "; ".join(errors))
    except (ServiceError, ValueError, OSError, sqlite3.Error, DefusedXmlException) as exc:
        logging.error("%s", exc)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
