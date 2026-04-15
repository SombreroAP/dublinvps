# Polymarket CLOB API — Research Report for 5m/15m Crypto Sniping Bot

## 1. Authentication flow

Polymarket uses a **two-level auth scheme**:

- **L1 (EIP-712 signature with EOA private key)** — one-time, used only to derive API credentials. Headers: `POLY_ADDRESS`, `POLY_SIGNATURE`, `POLY_TIMESTAMP`, `POLY_NONCE`.
- **L2 (HMAC-SHA256 with `apiKey`/`secret`/`passphrase`)** — every trading request after that. Headers: `POLY_ADDRESS`, `POLY_SIGNATURE`, `POLY_TIMESTAMP`, `POLY_API_KEY`, `POLY_PASSPHRASE`.

In py-clob-client:
```python
client = ClobClient(host, key=PRIVATE_KEY, chain_id=137, signature_type=..., funder=...)
client.set_api_creds(client.create_or_derive_api_creds())
```

**`signature_type` values (this is the landmine — map carefully):**

| ID | Name | Use |
|----|------|-----|
| 0 | EOA | Standalone wallet signing + paying gas itself (fresh MetaMask/hardware key) |
| 1 | POLY_PROXY | Email / Magic Link / Google-login (Polymarket proxy wallet) |
| 2 | POLY_GNOSIS_SAFE | Browser wallet connected through Polymarket (Safe-based proxy, most common retail) |

When `signature_type != 0`, you must also pass `funder=<proxy address>` — the smart-wallet address that holds USDC, distinct from the signing EOA.

**Fresh EOA with USDC on Polygon, never used the UI:** Can trade as `signature_type=0` directly — the EOA is the funder, and the CLOB accepts EIP-712 orders signed by it. **However**, if funds went through the UI/Magic/Google, the proxy wallet deploys lazily on first UI action. Safe rule for a bot: either (a) trade pure-EOA with USDC on the EOA, or (b) log into the UI once to deploy the Safe proxy, then use `signature_type=2` with that proxy as `funder`. **Verify live before production.**

## 2. 5m/15m crypto market structure

- **Slug patterns (DIFFER by duration):**
  - **Hourly / 15m:** `bitcoin-up-or-down-<month>-<day>-<year>-<hh><am|pm>-et` (human-readable)
  - **5m:** `btc-updown-5m-<unix_timestamp>` / `eth-updown-5m-<ts>` / `sol-updown-5m-<ts>` where `<ts>` is round-start unix seconds (always multiple of 300). Example: `https://polymarket.com/event/btc-updown-5m-1775395800`
  - Landing page: `https://polymarket.com/crypto/5M`
- **Discovery:** Gamma API. No dedicated short-duration endpoint; filter events. Start: `GET https://gamma-api.polymarket.com/events?tag_id=<crypto_tag>&active=true&closed=false&limit=...` sorted/filtered by `endDate`. Tag IDs via `GET /tags`. Practical loop: poll Gamma every N seconds for events whose `endDate` is within the next hour and whose slug matches `^(bitcoin|ethereum|solana)-up-or-down-`.
- **Token IDs:** Each market is a single binary condition with **two ERC-1155 outcome tokens** — YES `token_id` and NO `token_id`, in the market's `tokens` array. `condition_id` is the parent market; `token_id` is what order endpoints take. Order books are per `token_id`.
- **Good reference:** [txbabaxyz/polyrec](https://github.com/txbabaxyz/polyrec) — working code aggregating Gamma + Chainlink + Binance for 15m BTC.

## 3. Settlement mechanics — CRITICAL

- **Hourly BTC Up/Down:** resolves on the **Binance BTC/USDT 1h candle**. Outcome = (close ≥ open) → Up, else Down. The open/close shown at the top of the market graph is the resolution source.
- **15-minute BTC/ETH Up/Down:** uses **Chainlink price feeds** for instant automated settlement, not UMA. Distributed via Polymarket's RTDS stream (`wss://ws-live-data.polymarket.com`).
- **5-minute markets:** CONFIRMED — **Chainlink Data Streams** (not standard Price Feeds) at `data.chain.link/streams/btc-usd` (and ETH/SOL equivalents). Open = single snapshot at round start timestamp. Close = single snapshot at round end timestamp (5 min later). Resolves **Up** if close ≥ open, else Down (ties go Up). "Instant settlement" — automated resolver fires at round end, seconds not minutes.
- **UMA vs automated resolver:** UMA is fallback/dispute backstop. For short-duration markets, automated Chainlink/Binance-derived resolver pays out within seconds-to-minutes of candle close. UMA blocks payout for hours-to-days on dispute. Bot should treat "resolved but not yet paid" as normal.
- **Time-to-payout:** typically under a minute for 15m — verify empirically.

## 4. Order placement

- **REST base:** `https://clob.polymarket.com`. Primary endpoint: `POST /order` (via `client.post_order`).
- **Order types:** `GTC`, `GTD`, `FOK`, `FAK`, plus `post_only`. Market orders = FOK/FAK with USDC amount on buys / share size on sells.
- **Tick size:** `{0.1, 0.01, 0.001, 0.0001}` per market. `client.get_tick_size(token_id)`. Can tighten dynamically on one-sided books. Price range `[tick, 1 - tick]`.
- **Fees (2026):** **probability-based dynamic fee** — scales with proximity to p=0.50 (max uncertainty). For **Crypto**, published taker fee ~**1.80%** on the global platform early 2026. Makers pay **$0** + daily rebate (~20–25% of counterparty fee). **US CFTC-regulated exchange is separate:** 0.30% taker / 0.20% maker rebate. **1.80% taker on crypto is a huge drag for 5m sniping** — you want to be maker-side or have big edge.
- **Rate limits:** docs vague on exact numbers; `POST /order` typically sub-second. **Measure live.**
- **WebSocket:**
  - `wss://ws-subscriptions-clob.polymarket.com/ws/market` — public orderbook/trades/tick-size
  - `wss://ws-subscriptions-clob.polymarket.com/ws/user` — authed, order lifecycle (`MATCHED → CONFIRMED`)
  - `wss://ws-live-data.polymarket.com` — RTDS (Chainlink feeds)
  - All require PING every 10s.

## 5. py-clob-client specifics

- **Package:** `pip install py-clob-client`
- **Python:** Requires 3.9+ → **3.11 fine**.
- **Key classes:** `ClobClient`, `OrderArgs` (limit), `MarketOrderArgs` (market — USDC on BUY), `OrderType` enum.
- **YES market-buy example:**

```python
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType

client = ClobClient(
    host="https://clob.polymarket.com",
    key=PRIVATE_KEY,
    chain_id=137,
    signature_type=2,      # 0=EOA, 1=Magic proxy, 2=Safe/browser proxy
    funder=PROXY_ADDRESS,
)
client.set_api_creds(client.create_or_derive_api_creds())

mo = MarketOrderArgs(
    token_id=YES_TOKEN_ID,
    amount=25.0,     # USDC notional on BUY
    side="BUY",
    order_type=OrderType.FOK,
)
signed = client.create_market_order(mo)
resp = client.post_order(signed, OrderType.FOK)
```

Examples in [`py-clob-client/examples/`](https://github.com/Polymarket/py-clob-client): `market_buy_order.py`, `post_only_order.py`, `GTD_order.py`, `create_api_key.py`, `derive_api_key.py`, `get_orderbook.py`.

## Deployment target: QuantVPS Dublin

- Ubuntu 22.04 or 24.04 LTS likely. Install Python 3.11 via `deadsnakes` PPA if not default.
- Polymarket CLOB/Gamma/WebSockets: verify Dublin egress reaches all three hosts with low RTT before committing.
- Firewall: outbound HTTPS/WSS only — no inbound ports needed.
- Process supervision: `systemd` unit running the bot as a non-root user. `journalctl` for logs.
- Secrets: `.env` file owned by bot user, `chmod 600`. Private key NEVER in git.
- NTP: must be tightly synced — settlement timing is second-sensitive. `chrony` over `systemd-timesyncd`.
- Time zone: keep server in UTC; convert Polymarket's ET slugs in code.
- Consider a **read-only/paper-trade build deployed first** to measure actual Dublin→Polymarket latency with live data before any funded key touches the box.

## Items to verify live before prod

1. Does a fresh EOA with USDC on Polygon trade without any UI interaction? (sig_type 0)
2. Exact crypto fee at current market probability — dynamic curve means 1.80% is nominal.
3. CLOB rate limits and `post_order` p50/p99 latency from Dublin.
4. Time-to-payout distribution, especially UMA-dispute edge cases.
5. Tick size observed on live 5m/15m markets (likely 0.01 active, 0.001 near expiry on one-sided books).
6. Whether our price feed (Binance/Coinbase) matches Chainlink Data Stream snapshots closely enough — any systematic lag = the edge source. If Chainlink is slower than Binance, we have foresight; if faster, we're behind.

## Strategic decisions (locked in)

- **Wallet:** Magic-proxy (`signature_type=1`) — user already has funded Polymarket account via email/Google login. Bot uses the Magic-issued EOA private key to sign, with the proxy address as `funder`. (Original plan was pure EOA but switched to reuse existing funded account.)
- **Execution:** taker-capable with a hard edge gate. Require edge ≥ 2.5% (1.80% fee + 0.7% slippage/latency buffer) before firing. If live data shows threshold rarely hits, pivot to maker resting orders placed earlier in the round.
- **Settlement clock:** 5m markets resolve on Chainlink Data Stream snapshots at exact round boundaries. Our edge = comparing live Binance/Coinbase price at T-30s to the Chainlink snapshot Polymarket will use at T+0. Monitor both feeds; the lag between them is where the mispricing lives.

## Sources

- [Polymarket docs (CLOB intro)](https://docs.polymarket.com/developers/CLOB/introduction)
- [Polymarket docs (order types)](https://docs.polymarket.com/developers/CLOB/orders/orders)
- [Polymarket WebSocket docs](https://docs.polymarket.com/developers/CLOB/websocket/wss-overview)
- [Polymarket Gamma API (get-markets)](https://docs.polymarket.com/developers/gamma-markets-api/get-markets)
- [py-clob-client GitHub](https://github.com/Polymarket/py-clob-client)
- [polyrec — 15m BTC dashboard](https://github.com/txbabaxyz/polyrec)
- [Castle Crypto — 15-min Up/Down launch](https://castlecrypto.gg/news/polymarket-unveils-rapid-fire-15-minute-up-down-crypto-price-bets/)
- [CryptoRank — Chainlink integration for 15m](https://cryptorank.io/news/feed/c875b-polymarkets-15-minute-up-down)
- [Prediction Hunt — 2026 fee guide](https://www.predictionhunt.com/blog/polymarket-fees-complete-guide)
- [QuantJourney — dynamic fee curve](https://quantjourney.substack.com/p/understanding-the-polymarket-fee)
- [Polymarket US fee schedule](https://www.polymarketexchange.com/fees-hours.html)
