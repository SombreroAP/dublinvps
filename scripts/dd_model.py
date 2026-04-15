"""Phase 3: Fit a logistic regression to features → side picked.

Uses scikit-learn if available, else a simple manual gradient-descent LR.

Reports:
  - Coefficient per feature (sign + magnitude)
  - In-sample accuracy
  - Held-out (last 20%) accuracy — chronological split
  - Side-pick correlation with actual round outcome (would the model have won?)
"""
from __future__ import annotations

import json
import statistics
import subprocess
from pathlib import Path

DATA = Path("/opt/sniper/data")
FEATURES = DATA / "dd_features.jsonl"

FEATURE_NAMES = ["mom_1m_bps", "mom_5m_bps", "vol_1m_bps", "hour_utc", "day_of_week"]


def fetch_outcome(slug: str) -> str | None:
    """UP / DOWN / None"""
    try:
        out = subprocess.check_output([
            "curl", "-s", "-H", "User-Agent: Mozilla/5.0",
            f"https://gamma-api.polymarket.com/events?slug={slug}",
        ], timeout=8)
        d = json.loads(out)
        if not d:
            return None
        m = d[0].get("markets", [{}])[0]
        if not m.get("closed"):
            return None
        op = json.loads(m.get("outcomePrices") or "[]")
        if op == ["1", "0"]:
            return "UP"
        if op == ["0", "1"]:
            return "DOWN"
    except Exception:
        return None
    return None


def standardize(vals: list[float]) -> tuple[list[float], float, float]:
    mu = statistics.mean(vals)
    sd = statistics.pstdev(vals) or 1.0
    return [(v - mu) / sd for v in vals], mu, sd


def fit_lr(X: list[list[float]], y: list[int], n_iter: int = 2000, lr: float = 0.05
            ) -> tuple[list[float], float]:
    """Manual logistic regression via gradient descent. y in {0,1}."""
    n_feat = len(X[0]) if X else 0
    w = [0.0] * n_feat
    b = 0.0
    n = len(X)
    for _ in range(n_iter):
        gw = [0.0] * n_feat
        gb = 0.0
        for i in range(n):
            z = b + sum(w[j] * X[i][j] for j in range(n_feat))
            # sigmoid clipped
            if z > 30:
                p = 1.0
            elif z < -30:
                p = 0.0
            else:
                from math import exp
                p = 1.0 / (1.0 + exp(-z))
            err = p - y[i]
            for j in range(n_feat):
                gw[j] += err * X[i][j]
            gb += err
        for j in range(n_feat):
            w[j] -= lr * gw[j] / n
        b -= lr * gb / n
    return w, b


def predict(w: list[float], b: float, x: list[float]) -> float:
    from math import exp
    z = b + sum(w[j] * x[j] for j in range(len(w)))
    z = max(-30, min(30, z))
    return 1.0 / (1.0 + exp(-z))


def main() -> None:
    rows = [json.loads(l) for l in FEATURES.open()]
    # Keep rows where all features are present
    rows = [r for r in rows if all(r.get(f) is not None for f in FEATURE_NAMES)]
    print(f"Usable rows: {len(rows)}")

    # Sort chronologically
    rows.sort(key=lambda r: r["round_start"])

    # Per-feature univariate analysis: do Up vs Down picks differ?
    print("\n=== Univariate (mean by side) ===")
    print(f"{'feature':<15} {'Up mean':>10} {'Down mean':>10} {'diff':>10}  {'sig?':>6}")
    for f in FEATURE_NAMES:
        ups = [r[f] for r in rows if r["side"] == "Up"]
        downs = [r[f] for r in rows if r["side"] == "Down"]
        if not ups or not downs:
            continue
        mu_up = statistics.mean(ups)
        mu_dn = statistics.mean(downs)
        sd_up = statistics.pstdev(ups) or 0.001
        sd_dn = statistics.pstdev(downs) or 0.001
        # Welch's t-statistic
        t = (mu_up - mu_dn) / ((sd_up**2 / len(ups) + sd_dn**2 / len(downs)) ** 0.5)
        sig = "***" if abs(t) > 2.5 else "**" if abs(t) > 1.96 else ""
        print(f"{f:<15} {mu_up:>+10.3f} {mu_dn:>+10.3f} {mu_up-mu_dn:>+10.3f}  t={t:+.2f} {sig}")

    # Build X, y
    raw_X = [[r[f] for f in FEATURE_NAMES] for r in rows]
    y = [1 if r["side"] == "Up" else 0 for r in rows]

    # Standardize per-feature
    cols = list(zip(*raw_X))
    X_std_cols = []
    means, sds = [], []
    for col in cols:
        s, mu, sd = standardize(list(col))
        X_std_cols.append(s)
        means.append(mu); sds.append(sd)
    X_std = list(map(list, zip(*X_std_cols)))

    # Train/test chronological split 80/20
    split = int(len(X_std) * 0.8)
    X_train, X_test = X_std[:split], X_std[split:]
    y_train, y_test = y[:split], y[split:]
    print(f"\nTrain n={len(X_train)}, Test n={len(X_test)}")

    w, b = fit_lr(X_train, y_train)
    print("\n=== Logistic regression coefficients ===")
    print(f"{'feature':<15} {'coef (std units)':>18}")
    for f, wj in zip(FEATURE_NAMES, w):
        print(f"{f:<15} {wj:>+18.4f}")
    print(f"intercept     : {b:+.4f}")

    # Train accuracy
    train_preds = [1 if predict(w, b, x) > 0.5 else 0 for x in X_train]
    test_preds = [1 if predict(w, b, x) > 0.5 else 0 for x in X_test]
    train_acc = sum(p == y for p, y in zip(train_preds, y_train)) / max(1, len(y_train))
    test_acc = sum(p == y for p, y in zip(test_preds, y_test)) / max(1, len(y_test))
    print(f"\nTrain accuracy (vs DD's pick): {train_acc:.1%}")
    print(f"Test  accuracy (vs DD's pick): {test_acc:.1%}")
    print(f"Baseline (always-Up):           {sum(y_test)/max(1,len(y_test)):.1%}")

    # Now: would the MODEL'S picks beat random against ACTUAL outcomes?
    print("\n=== Backtesting model's picks vs actual round outcomes (test set) ===")
    print("Fetching outcomes... (may take a minute)")
    test_rows = rows[split:]
    wins = losses = pending = 0
    pl_units = 0.0  # +1 per win, -1 per loss (as if 1c shots paid 100c)
    for x, pred, r in zip(X_test, test_preds, test_rows):
        outcome = fetch_outcome(r["slug"])
        if outcome is None:
            pending += 1
            continue
        model_side = "Up" if pred == 1 else "Down"
        won = (model_side == "Up" and outcome == "UP") or (model_side == "Down" and outcome == "DOWN")
        if won:
            wins += 1
            pl_units += 1
        else:
            losses += 1
            pl_units -= 1
    print(f"Model picks: {wins}W / {losses}L / {pending} pending")
    if wins + losses:
        print(f"Model win rate: {wins/(wins+losses):.1%}  (vs 50% random)")
        print(f"Net unit P&L (each pick = 1 unit): {pl_units:+.0f}")


if __name__ == "__main__":
    main()
