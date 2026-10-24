from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from pathlib import Path

import httpx
from defusedxml.common import DefusedXmlException
from dotenv import load_dotenv
from pydantic import ValidationError

from .config import load_config, validate_credentials
from .demo import demo
from .http import USER_AGENT, ServiceError
from .pipeline import run
from .store import Store


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
    sub.add_parser("pending", help="List unresolved email attempts without revealing addresses")
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
            print(f"Jev: {config.jev.backend} / {config.jev.model}")
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
        elif args.command in {"pending", "resolve"}:
            with Store(config.state_dir) as store:
                if args.command == "pending":
                    print(json.dumps(store.pending(), indent=2))
                else:
                    store.resolve(args.delivery_id, args.outcome)
                    print("Delivery resolved.")
        else:
            with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=60) as client:
                result = run(config, client, dry_run=args.dry_run)
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
