"""One-shot repair of gas_token on CLMM liquidity events (CORR-104).

Rows written before ARCH-054 unified the chain -> native-gas-token map carry a wrong
gas_token: the add/remove handlers' old ternary left it NULL off solana/ethereum (and a
row inserted CONFIRMED is never re-polled, so the NULL is permanent), and the transaction
poller's old 6-entry dict wrote "UNKNOWN" for base, arbitrum and polygon. The write paths
are already correct; this script repairs the rows they left behind.

The repair itself lives in GatewayCLMMRepository.backfill_liquidity_gas_tokens() so it is
testable; this module is only the entry point that opens a session and reports.

Safe to re-run: a repaired row no longer matches the filter, so a second run is a no-op.
Rows whose chain still resolves to "UNKNOWN" are left untouched and reported, so an
unmapped chain surfaces instead of being papered over.

Usage:
    conda run --no-capture-output -n hummingbot-api python -m scripts.backfill_gas_tokens --dry-run
    conda run --no-capture-output -n hummingbot-api python -m scripts.backfill_gas_tokens
"""
import argparse
import asyncio
import logging
import sys

from config import settings
from database.connection import AsyncDatabaseManager
from database.repositories.gateway_clmm_repository import GatewayCLMMRepository

logger = logging.getLogger("backfill_gas_tokens")


async def backfill(dry_run: bool = False) -> dict:
    """Run the repair against the configured database, committing unless dry_run."""
    db_manager = AsyncDatabaseManager(settings.database.url)
    try:
        async with db_manager.get_session_context() as session:
            repo = GatewayCLMMRepository(session)
            report = await repo.backfill_liquidity_gas_tokens()
            if dry_run:
                # Discard the pending UPDATEs; the session context would commit them.
                await session.rollback()
        return report
    finally:
        await db_manager.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change without writing")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    report = asyncio.run(backfill(dry_run=args.dry_run))

    prefix = "[dry run] " if args.dry_run else ""
    logger.info("%sgas_token repaired on %d liquidity event(s)", prefix, report["fixed"])
    if report["unresolved"]:
        logger.warning(
            "%s%d event(s) left untouched: no native gas token is mapped for %s",
            prefix, report["unresolved"], ", ".join(report["unresolved_networks"]),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
