#!/usr/bin/env python3
"""Redeem settled positions - claim winnings from resolved markets."""

import sys
import json
import asyncio
import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from pathlib import Path

# Add parent to path for lib imports
sys.path.insert(0, str(Path(__file__).parent.parent))

# Load .env file from skill root directory
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from web3 import Web3

from lib.wallet_manager import WalletManager
from lib.gamma_client import GammaClient, Market
from lib.contracts import CONTRACTS, CTF_ABI, POLYGON_CHAIN_ID
from lib.position_storage import PositionStorage


@dataclass
class RedeemResult:
    """Result of a redeem operation."""

    success: bool
    position_id: str
    market_id: str
    question: str
    position: str
    token_count: float
    redeemed_usd: float
    tx_hash: Optional[str] = None
    error: Optional[str] = None


class RedeemExecutor:
    """Detects and redeems settled winning positions."""

    def __init__(self, wallet: WalletManager):
        self.wallet = wallet
        self._gamma = GammaClient()

    def _get_web3(self) -> Web3:
        return Web3(
            Web3.HTTPProvider(
                self.wallet.rpc_url,
                request_kwargs={"timeout": 60, "proxies": {}}
            )
        )

    def _get_ctf_contract(self, w3: Web3):
        return w3.eth.contract(
            address=Web3.to_checksum_address(CONTRACTS["CTF"]),
            abi=CTF_ABI,
        )

    def _get_token_balance(self, w3: Web3, token_id: str) -> int:
        """Get on-chain balance of a conditional token."""
        ctf = self._get_ctf_contract(w3)
        address = Web3.to_checksum_address(self.wallet.address)
        return ctf.functions.balanceOf(address, int(token_id)).call()

    def _is_condition_resolved(self, w3: Web3, condition_id: str) -> bool:
        """Check if a condition has been resolved on-chain via payoutDenominator."""
        ctf = self._get_ctf_contract(w3)
        condition_bytes = bytes.fromhex(
            condition_id[2:] if condition_id.startswith("0x") else condition_id
        )
        try:
            denom = ctf.functions.payoutDenominator(condition_bytes).call()
            return denom > 0
        except Exception:
            return False

    async def scan_redeemable(self) -> list[dict]:
        """Scan all open positions and find ones that are settled and winning."""
        storage = PositionStorage()
        positions = storage.get_open()

        if not positions:
            return []

        w3 = self._get_web3()
        redeemable = []

        for pos in positions:
            try:
                market = await self._gamma.get_market(pos["market_id"])

                if not market.resolved:
                    continue

                # Check if our position is the winning side
                outcome = (market.outcome or "").upper()
                our_side = pos["position"].upper()

                is_winner = False
                if outcome == "YES" and our_side == "YES":
                    is_winner = True
                elif outcome == "NO" and our_side == "NO":
                    is_winner = True

                if not is_winner:
                    # Losing position - mark as resolved with zero value
                    redeemable.append({
                        **pos,
                        "market_outcome": outcome,
                        "is_winner": False,
                        "on_chain_balance": 0,
                        "redeemable_usd": 0,
                    })
                    continue

                # Check on-chain token balance
                token_id = pos.get("token_id", "")
                if not token_id:
                    continue

                balance = self._get_token_balance(w3, token_id)
                if balance == 0:
                    continue

                # Check on-chain condition resolution
                if not self._is_condition_resolved(w3, market.condition_id):
                    continue

                balance_human = balance / 1e6  # USDC.e has 6 decimals

                redeemable.append({
                    **pos,
                    "market_outcome": outcome,
                    "is_winner": True,
                    "on_chain_balance": balance,
                    "redeemable_usd": balance_human,
                    "condition_id": market.condition_id,
                })
            except Exception as e:
                print(f"  Warning: Failed to check {pos['position_id'][:8]}: {e}")
                continue

        return redeemable

    def redeem_position(
        self,
        condition_id: str,
        index_sets: list[int],
    ) -> str:
        """Call CTF.redeemPositions on-chain. Returns tx hash."""
        w3 = self._get_web3()
        address = Web3.to_checksum_address(self.wallet.address)
        account = w3.eth.account.from_key(self.wallet.get_unlocked_key())
        ctf = self._get_ctf_contract(w3)

        condition_bytes = bytes.fromhex(
            condition_id[2:] if condition_id.startswith("0x") else condition_id
        )

        tx = ctf.functions.redeemPositions(
            Web3.to_checksum_address(CONTRACTS["USDC_E"]),
            bytes(32),  # parentCollectionId
            condition_bytes,
            index_sets,
        ).build_transaction({
            "from": address,
            "nonce": w3.eth.get_transaction_count(address),
            "gas": 300000,
            "gasPrice": int(w3.eth.gas_price * 1.1),
            "chainId": POLYGON_CHAIN_ID,
        })

        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        print(f"  Redeem TX submitted: {tx_hash.hex()}")

        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt["status"] != 1:
            raise ValueError(f"Redeem failed: {tx_hash.hex()}")

        print(f"  Redeem confirmed in block {receipt['blockNumber']}")
        return tx_hash.hex()

    async def redeem_all(self, dry_run: bool = False) -> list[RedeemResult]:
        """Scan and redeem all settled winning positions."""
        print("Scanning for redeemable positions...")
        redeemable = await self.scan_redeemable()

        if not redeemable:
            print("No redeemable positions found.")
            return []

        winners = [r for r in redeemable if r["is_winner"]]
        losers = [r for r in redeemable if not r["is_winner"]]

        if losers:
            print(f"\nFound {len(losers)} losing resolved position(s):")
            for pos in losers:
                print(f"  {pos['position_id'][:8]} | {pos['position']} | {pos['question'][:40]} | Outcome: {pos['market_outcome']}")

        if not winners:
            print("\nNo winning positions to redeem.")
            # Still mark losers as resolved
            storage = PositionStorage()
            results = []
            for pos in losers:
                storage.update_status(pos["position_id"], "resolved")
                results.append(RedeemResult(
                    success=True,
                    position_id=pos["position_id"],
                    market_id=pos["market_id"],
                    question=pos["question"],
                    position=pos["position"],
                    token_count=0,
                    redeemed_usd=0,
                    error="Losing position - marked as resolved",
                ))
            return results

        print(f"\nFound {len(winners)} winning position(s) to redeem:")
        for pos in winners:
            print(f"  {pos['position_id'][:8]} | {pos['position']} | ${pos['redeemable_usd']:.2f} | {pos['question'][:40]}")

        if dry_run:
            print("\n[DRY RUN] No transactions submitted.")
            return []

        results = []
        storage = PositionStorage()

        for pos in winners:
            print(f"\nRedeeming {pos['position_id'][:8]}...")
            try:
                # indexSets: [1, 2] for binary markets (YES=1, NO=2)
                tx_hash = self.redeem_position(
                    condition_id=pos["condition_id"],
                    index_sets=[1, 2],
                )

                # Update position status
                storage.update_status(pos["position_id"], "redeemed")
                # Store redeem info in notes
                storage.update_notes(
                    pos["position_id"],
                    f"Redeemed ${pos['redeemable_usd']:.2f} | TX: {tx_hash} | {datetime.now(timezone.utc).isoformat()}"
                )

                results.append(RedeemResult(
                    success=True,
                    position_id=pos["position_id"],
                    market_id=pos["market_id"],
                    question=pos["question"],
                    position=pos["position"],
                    token_count=pos["redeemable_usd"],
                    redeemed_usd=pos["redeemable_usd"],
                    tx_hash=tx_hash,
                ))
                print(f"  Redeemed ${pos['redeemable_usd']:.2f} USDC.e")

            except Exception as e:
                print(f"  Redeem failed: {e}")
                results.append(RedeemResult(
                    success=False,
                    position_id=pos["position_id"],
                    market_id=pos["market_id"],
                    question=pos["question"],
                    position=pos["position"],
                    token_count=pos["redeemable_usd"],
                    redeemed_usd=0,
                    error=str(e),
                ))

        # Also mark losers as resolved
        for pos in losers:
            storage.update_status(pos["position_id"], "resolved")

        return results


async def cmd_scan(args):
    """Scan for redeemable positions (no execution)."""
    wallet = WalletManager()
    if not wallet.is_unlocked:
        print("Error: No wallet configured")
        print("Set POLYCLAW_PRIVATE_KEY environment variable.")
        return 1

    try:
        executor = RedeemExecutor(wallet)
        redeemable = await executor.scan_redeemable()

        if not redeemable:
            print("No settled positions found.")
            return 0

        winners = [r for r in redeemable if r["is_winner"]]
        losers = [r for r in redeemable if not r["is_winner"]]

        if winners:
            print(f"\nWinning positions ready to redeem ({len(winners)}):")
            print(f"{'ID':<10} {'Side':<4} {'Amount':>10} {'Market'}")
            print("-" * 70)
            total = 0
            for pos in winners:
                print(f"{pos['position_id'][:8]:<10} {pos['position']:<4} ${pos['redeemable_usd']:>8.2f} {pos['question'][:40]}")
                total += pos["redeemable_usd"]
            print("-" * 70)
            print(f"Total redeemable: ${total:.2f} USDC.e")
            print(f"\nRun 'polyclaw redeem execute' to claim.")

        if losers:
            print(f"\nLosing resolved positions ({len(losers)}):")
            for pos in losers:
                print(f"  {pos['position_id'][:8]} | {pos['position']} | {pos['question'][:40]} | Outcome: {pos['market_outcome']}")

        if args.json:
            print("\nJSON:")
            print(json.dumps(redeemable, indent=2, default=str))

        return 0
    finally:
        wallet.lock()


async def cmd_execute(args):
    """Execute redemption of all settled winning positions."""
    wallet = WalletManager()
    if not wallet.is_unlocked:
        print("Error: No wallet configured")
        print("Set POLYCLAW_PRIVATE_KEY environment variable.")
        return 1

    try:
        executor = RedeemExecutor(wallet)
        results = await executor.redeem_all(dry_run=args.dry_run)

        if not results:
            return 0

        print("\n" + "=" * 60)
        print("Redemption Summary:")
        succeeded = [r for r in results if r.success and r.redeemed_usd > 0]
        failed = [r for r in results if not r.success]
        resolved = [r for r in results if r.success and r.redeemed_usd == 0]

        if succeeded:
            total = sum(r.redeemed_usd for r in succeeded)
            print(f"  Redeemed: {len(succeeded)} position(s) for ${total:.2f} USDC.e")
        if resolved:
            print(f"  Resolved (losing): {len(resolved)} position(s)")
        if failed:
            print(f"  Failed: {len(failed)} position(s)")
            for r in failed:
                print(f"    {r.position_id[:8]}: {r.error}")

        if args.json:
            from dataclasses import asdict
            print("\nJSON:")
            print(json.dumps([asdict(r) for r in results], indent=2))

        return 0
    finally:
        wallet.lock()


def main():
    parser = argparse.ArgumentParser(description="Redeem settled positions")
    parser.add_argument("--json", action="store_true", help="JSON output")
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Scan
    scan_parser = subparsers.add_parser("scan", help="Scan for redeemable positions")

    # Execute
    exec_parser = subparsers.add_parser("execute", help="Redeem all settled winning positions")
    exec_parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be redeemed without executing"
    )

    args = parser.parse_args()

    if args.command == "scan":
        return asyncio.run(cmd_scan(args))
    elif args.command == "execute":
        return asyncio.run(cmd_execute(args))
    else:
        # Default to scan
        args.json = False
        return asyncio.run(cmd_scan(args))


if __name__ == "__main__":
    sys.exit(main() or 0)
