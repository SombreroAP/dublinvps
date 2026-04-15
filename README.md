# Polymarket Crypto Sniper

Late-round sniping bot for Polymarket 5m/15m BTC/ETH/SOL Up/Down markets. Targets
QuantVPS Dublin for sub-ms latency to Polymarket's London-hosted CLOB.

## Status

Phase 1 (paper trading). No live orders. Logs hypothetical signals to `paper_trades.jsonl`.

## Local quickstart

```bash
python3.11 -m venv .venv
source .venv/bin/activate         # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env              # edit; private key only needed for live mode
python -m src.main
```

## Dublin VPS deploy

```bash
# On the VPS (Ubuntu 22.04+):
git clone <repo> /opt/sniper
cd /opt/sniper
sudo bash deploy/bootstrap.sh
# Edit /opt/sniper/.env, then re-run bootstrap, then:
sudo systemctl start sniper
journalctl -u sniper -f
```

## Architecture

- `src/feeds/binance.py` — bookTicker WebSocket for BTC/ETH/SOL mid prices
- `src/polymarket/gamma.py` — discover active 5m/15m markets via Gamma API
- `src/polymarket/clob.py` — order book reader (py-clob-client wrapper)
- `src/strategy/sniper.py` — edge calc + paper signal logger
- `src/main.py` — async entry point

## Decisions log

See `docs/polymarket-api-research.md`. Key locked decisions:

- Wallet: pure EOA (`signature_type=0`)
- Edge gate: ≥ 2.5% (1.80% taker fee + 0.7% buffer)
- Entry window: T-45s to T-5s before settlement
- Settlement source: Chainlink Data Streams (5m/15m markets)

## Phase 2 TODO (before going live)

- Subscribe to Chainlink Data Streams for true round-open price (vs current Binance approximation in `main.py`)
- Wire `py-clob-client.post_order` execution path
- Backtest from `paper_trades.jsonl` to validate edge threshold
- Measure Dublin → CLOB p50/p99 latency under load
- Confirm fresh-EOA trading works without any Polymarket UI interaction
