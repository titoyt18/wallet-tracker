"""
Wallet Tracker Solana - v1.2 (message de statut au lancement manuel)
Alertes Telegram sur les achats et ventes de wallets Solana.

MODE=check   -> lancé toutes les 10 min : alerte immédiate sur chaque ACHAT
MODE=summary -> lancé le soir : résumé de la journée (achats + ventes)

Secrets GitHub nécessaires :
HELIUS_API_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, WALLETS (adresses séparées par des virgules)
"""
import html
import json
import os
import time

import requests

HELIUS_KEY = os.environ["HELIUS_API_KEY"].strip()
TG_TOKEN = os.environ["TELEGRAM_TOKEN"].strip()
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"].strip()
WALLETS = [w.strip() for w in os.environ["WALLETS"].split(",") if w.strip()]
MODE = (os.environ.get("MODE") or "check").strip()
MIN_BUY_SOL = float(os.environ.get("MIN_BUY_SOL") or 0)

RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}"
STATE_FILE = "state.json"
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SIG_LIMIT = 50


# ---------- Outils ----------

def rpc(method, params):
    """Appel à l'API Solana (via Helius), avec 3 essais."""
    for attempt in range(3):
        try:
            r = requests.post(
                RPC_URL,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                timeout=30,
            )
            if r.status_code == 429:  # trop de requêtes
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                raise RuntimeError(f"Erreur RPC : {data['error']}")
            return data["result"]
        except requests.RequestException as e:
            print(f"Essai {attempt + 1} échoué : {e}")
            time.sleep(2)
    raise RuntimeError("API Solana indisponible après 3 essais")


def tg(text):
    """Envoie un message Telegram."""
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={
            "chat_id": TG_CHAT,
            "text": text[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    if not r.ok:
        print("Erreur Telegram :", r.text)


def short(addr):
    return f"{addr[:4]}…{addr[-4:]}"


def fmt_usd(x):
    if not x:
        return "?"
    x = float(x)
    if x >= 1e9:
        return f"{x / 1e9:.2f} Md$"
    if x >= 1e6:
        return f"{x / 1e6:.2f} M$"
    if x >= 1e3:
        return f"{x / 1e3:.1f} k$"
    return f"{x:.0f} $"


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    state.setdefault("last_sig", {})
    state.setdefault("day", [])
    return state


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


_token_cache = {}


def token_info(mint):
    """Nom, prix et market cap du token via Dexscreener (gratuit, sans clé)."""
    if mint in _token_cache:
        return _token_cache[mint]
    info = {"symbol": short(mint), "mcap": None, "url": f"https://dexscreener.com/solana/{mint}"}
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}", timeout=20)
        pairs = [
            p for p in (r.json().get("pairs") or [])
            if p.get("chainId") == "solana" and p.get("baseToken", {}).get("address") == mint
        ]
        if pairs:
            best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
            info["symbol"] = best["baseToken"].get("symbol") or info["symbol"]
            info["mcap"] = best.get("marketCap") or best.get("fdv")
    except Exception as e:
        print("Dexscreener indisponible :", e)
    _token_cache[mint] = info
    return info


# ---------- Analyse d'une transaction ----------

def parse_trade(tx, wallet):
    """Retourne un achat/vente détecté, ou None si ce n'est pas un trade."""
    if not tx or tx.get("meta") is None or tx["meta"].get("err"):
        return None
    meta = tx["meta"]
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
    if wallet not in keys:
        return None
    i = keys.index(wallet)

    sol = (meta["postBalances"][i] - meta["preBalances"][i]) / 1e9

    deltas = {}
    for sign, field in ((-1, "preTokenBalances"), (1, "postTokenBalances")):
        for b in meta.get(field) or []:
            if b.get("owner") != wallet:
                continue
            amount = float(b["uiTokenAmount"].get("uiAmountString") or 0)
            deltas[b["mint"]] = deltas.get(b["mint"], 0) + sign * amount

    sol += deltas.pop(WSOL, 0)
    usdc = deltas.pop(USDC, 0)
    deltas = {m: d for m, d in deltas.items() if abs(d) > 0}
    if not deltas:
        return None

    mint, amount = max(deltas.items(), key=lambda kv: abs(kv[1]))
    if amount > 0 and (sol < -0.001 or usdc < -0.01):
        side = "buy"
    elif amount < 0 and (sol > 0.001 or usdc > 0.01):
        side = "sell"
    else:
        return None  # simple transfert, pas un trade

    return {
        "side": side,
        "mint": mint,
        "amount": abs(amount),
        "sol": round(abs(sol), 4) if abs(sol) > 0.001 else 0,
        "usdc": round(abs(usdc), 2) if abs(usdc) > 0.01 else 0,
        "time": tx.get("blockTime"),
    }


def fmt_price_paid(t):
    parts = []
    if t["sol"]:
        parts.append(f"{t['sol']:.2f} SOL")
    if t["usdc"]:
        parts.append(f"{t['usdc']:.0f} USDC")
    return " + ".join(parts) or "?"


def fmt_buy(t, info):
    sym = html.escape(info["symbol"])
    return (
        f"🟢 <b>ACHAT</b> · wallet <code>{short(t['wallet'])}</code>\n"
        f"Token : <b>${sym}</b>\n"
        f"Montant : {fmt_price_paid(t)}\n"
        f"Market cap : {fmt_usd(info['mcap'])}\n"
        f"<a href=\"{info['url']}\">Dexscreener</a> · "
        f"<a href=\"https://solscan.io/tx/{t['sig']}\">Transaction</a>"
    )


# ---------- Modes ----------

def check(state):
    manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    report = []
    for w in WALLETS:
        sigs = rpc("getSignaturesForAddress", [w, {"limit": SIG_LIMIT}])
        if not sigs:
            report.append(f"⚠️ <code>{short(w)}</code> : aucune transaction trouvée (adresse à vérifier)")
            continue
        last = state["last_sig"].get(w)

        if last is None:  # premier lancement : on démarre à partir de maintenant
            state["last_sig"][w] = sigs[0]["signature"]
            tg(f"✅ Tracker activé pour <code>{short(w)}</code>. Les prochains achats seront signalés ici.")
            continue

        new = []
        for s in sigs:
            if s["signature"] == last:
                break
            new.append(s)

        for s in reversed(new):  # du plus ancien au plus récent
            if s.get("err"):
                continue
            try:
                tx = rpc("getTransaction", [s["signature"], {
                    "encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 1,
                    "commitment": "confirmed",
                }])
                t = parse_trade(tx, w)
            except Exception as e:  # une transaction illisible ne bloque plus tout
                print(f"Transaction ignorée {s['signature'][:8]}… : {e}")
                continue
            if not t:
                continue
            t["wallet"] = w
            t["sig"] = s["signature"]
            info = token_info(t["mint"])
            t["symbol"] = info["symbol"]
            state["day"].append(t)

            if t["side"] == "buy" and (t["sol"] >= MIN_BUY_SOL or t["usdc"] > 0):
                tg(fmt_buy(t, info))
            time.sleep(0.3)

        state["last_sig"][w] = sigs[0]["signature"]
        print(f"{short(w)} : {len(new)} nouvelle(s) transaction(s)")

        day = [t for t in state["day"] if t["wallet"] == w]
        n_buy = sum(1 for t in day if t["side"] == "buy")
        n_sell = sum(1 for t in day if t["side"] == "sell")
        report.append(
            f"<code>{short(w)}</code> : {len(new)} nouvelle(s) transaction(s)\n"
            f"Depuis le dernier résumé : 🟢 {n_buy} achat(s) · 🔴 {n_sell} vente(s)"
        )

    if manual:
        tg("🤖 <b>Tracker en marche</b>\n" + "\n\n".join(report))


def summary(state):
    trades = state["day"]
    if not trades:
        tg("🌙 <b>Résumé du jour</b>\nAucun trade détecté aujourd'hui.")
        return

    lines = ["🌙 <b>Résumé du jour</b>"]
    for w in sorted({t["wallet"] for t in trades}):
        wt = [t for t in trades if t["wallet"] == w]
        buys = [t for t in wt if t["side"] == "buy"]
        sells = [t for t in wt if t["side"] == "sell"]
        sol_out = sum(t["sol"] for t in buys)
        sol_in = sum(t["sol"] for t in sells)

        lines.append(f"\n<b>Wallet {short(w)}</b>")
        lines.append(f"🟢 {len(buys)} achat(s) · {sol_out:.2f} SOL dépensés")
        lines.append(f"🔴 {len(sells)} vente(s) · {sol_in:.2f} SOL récupérés")
        lines.append(f"Solde SOL du jour : {sol_in - sol_out:+.2f} SOL")

        per_token = {}
        for t in wt:
            d = per_token.setdefault(t["symbol"], {"buy": 0.0, "sell": 0.0})
            d[t["side"]] += t["sol"]
        top = sorted(per_token.items(), key=lambda kv: kv[1]["buy"] + kv[1]["sell"], reverse=True)[:10]
        for sym, d in top:
            lines.append(f"• ${html.escape(sym)} : achat {d['buy']:.2f} / vente {d['sell']:.2f} SOL")

    lines.append("\n<i>Les ventes de tokens achetés avant aujourd'hui gonflent le solde du jour.</i>")
    tg("\n".join(lines))
    state["day"] = []


def main():
    if not WALLETS:
        raise SystemExit("Le secret WALLETS est vide.")
    state = load_state()
    try:
        if MODE == "summary":
            summary(state)
        else:
            check(state)
    finally:
        save_state(state)


if __name__ == "__main__":
    main()
