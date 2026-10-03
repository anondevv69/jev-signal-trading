"""Per-token intelligence gathering. PAPER ADVISORY ONLY.

Every source is fail-open: a timeout, HTTP error, or unknown chain skips that
check and records what was unavailable in the ``unavailable`` list. Missing
safety data is reported as UNKNOWN (caution) -- never as a pass.

Live-verified 2026-09-30 from the sandbox:
  - DexScreener ``/latest/dex/tokens/{addr}``: cross-chain pair lookup, one
    call. ``/tokens/v1/{chain}/{addr}`` also works (chain slugs include
    "robinhood" for Robinhood Chain, "ethereum", "base", "solana", ...).
    Response: list of pairs with priceUsd, priceChange{m5,h1,h6,h24},
    txns{m5,h1,h6,h24:{buys,sells}}, volume{h24,...}, liquidity{usd}, fdv.
  - honeypot.is v2 ``/v2/IsHoneypot?address=&chainID=``: works for major EVM
    chains (1, 56, 8453, 42161, 137). Chain 4663 (Robinhood) -> "Invalid
    chain": NOT supported, so Robinhood tokens always report safety unknown.
  - Robinhood blockscout ``/api?module=contract&action=getcontractcreation``:
    works with browser-like Origin/Referer headers (else 403). Returns
    contractCreator + contractFactory. Responses may be truncated.
"""

import time

import requests

UA = {"User-Agent": "fren-signal-trading/1.0"}
TIMEOUT = 15

# DexScreener chainId -> honeypot.is chainID (only chains honeypot.is supports)
HONEYPOT_CHAIN_IDS = {
    "ethereum": 1,
    "bsc": 56,
    "base": 8453,
    "arbitrum": 42161,
    "polygon": 137,
}

ROBINHOOD_BLOCKSCOUT = "https://robinhoodchain.blockscout.com/api"
BLOCKSCOUT_HEADERS = {
    **UA,
    "Origin": "https://robinhoodchain.blockscout.com",
    "Referer": "https://robinhoodchain.blockscout.com/",
}


def _get_json(url, headers=None, params=None, timeout=TIMEOUT):
    """GET -> parsed JSON, or None on any failure. Never raises."""
    try:
        r = requests.get(url, headers=headers or UA, params=params,
                         timeout=timeout)
        if not r.ok:
            return None
        return r.json()
    except Exception:
        return None


def _best_pair(pairs):
    """Pick the pair with the most USD liquidity."""
    best, best_liq = None, -1.0
    for p in pairs or []:
        try:
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
        except (TypeError, ValueError):
            liq = 0.0
        if liq > best_liq:
            best, best_liq = p, liq
    return best


def dexscreener(chain, address):
    """Market/momentum intel. Returns dict or None if unavailable."""
    address = (address or "").strip()
    if not address:
        return None
    if chain == "solana":
        data = _get_json(
            f"https://api.dexscreener.com/tokens/v1/solana/{address}")
        pairs = data if isinstance(data, list) else []
    else:
        # cross-chain lookup: one call, works for any EVM address
        data = _get_json(
            f"https://api.dexscreener.com/latest/dex/tokens/{address}")
        pairs = (data or {}).get("pairs", []) if isinstance(data, dict) else []
    pair = _best_pair(pairs)
    if not pair:
        return None
    txns = pair.get("txns") or {}
    h24 = txns.get("h24") or {}
    pc = pair.get("priceChange") or {}
    vol = pair.get("volume") or {}
    liq = pair.get("liquidity") or {}
    base = pair.get("baseToken") or {}
    try:
        price = float(pair.get("priceUsd")) if pair.get("priceUsd") else None
    except (TypeError, ValueError):
        price = None
    return {
        "resolved_chain": pair.get("chainId"),
        "symbol": base.get("symbol"),
        "name": base.get("name"),
        "price_usd": price,
        "price_change": {k: pc.get(k) for k in ("m5", "h1", "h6", "h24")},
        "volume_24h": vol.get("h24"),
        "buys_24h": h24.get("buys"),
        "sells_24h": h24.get("sells"),
        "liquidity_usd": liq.get("usd"),
        "fdv": pair.get("fdv"),
        "pair_created_at_ms": pair.get("pairCreatedAt"),
        "pairs_count": len(pairs),
    }


def honeypot_check(address, resolved_chain):
    """Contract safety for supported EVM chains.

    Returns {"checked": bool, ...}. checked=False means UNKNOWN, not safe.
    """
    out = {"checked": False, "is_honeypot": None, "buy_tax": None,
           "sell_tax": None, "note": ""}
    chain_id = HONEYPOT_CHAIN_IDS.get(resolved_chain or "")
    if not chain_id:
        out["note"] = (f"safety API does not support chain "
                       f"'{resolved_chain}' (unknown = caution, not a pass)")
        return out
    data = _get_json(
        "https://api.honeypot.is/v2/IsHoneypot",
        params={"address": address, "chainID": chain_id})
    if not data or not isinstance(data, dict) or "honeypotResult" not in data:
        out["note"] = "honeypot.is lookup failed or token unknown"
        return out
    out["checked"] = True
    out["is_honeypot"] = bool(
        (data.get("honeypotResult") or {}).get("isHoneypot"))
    sim = data.get("simulationResult") or {}
    out["buy_tax"] = sim.get("buyTax")
    out["sell_tax"] = sim.get("sellTax")
    out["note"] = "checked via honeypot.is"
    return out


def deployer_info(address, resolved_chain):
    """Deployer basics. v1: Robinhood Chain via blockscout only.

    Returns {"checked": bool, "address":..., "factory":..., "tx_count":...,
             "wallet_age_days":..., "note":...}.
    """
    out = {"checked": False, "address": None, "factory": None,
           "tx_count": None, "wallet_age_days": None, "note": ""}
    if resolved_chain != "robinhood":
        out["note"] = ("deployer lookup only implemented for Robinhood Chain "
                       "(blockscout); other chains stubbed")
        return out
    data = _get_json(
        ROBINHOOD_BLOCKSCOUT,
        headers=BLOCKSCOUT_HEADERS,
        params={"module": "contract", "action": "getcontractcreation",
                "contractaddresses": address},
        timeout=20,
    )
    results = (data or {}).get("result") or []
    if not results:
        out["note"] = "blockscout: no contract creation record found"
        return out
    creator = results[0].get("contractCreator")
    out["checked"] = True
    out["address"] = creator
    out["factory"] = results[0].get("contractFactory")
    if not creator:
        out["note"] = "blockscout: creation record had no creator"
        return out
    # wallet footprint: tx count + age from tx list (ascending)
    txs = _get_json(
        ROBINHOOD_BLOCKSCOUT,
        headers=BLOCKSCOUT_HEADERS,
        params={"module": "account", "action": "txlist", "address": creator,
                "startblock": 0, "endblock": 99999999, "sort": "asc"},
        timeout=25,
    )
    txlist = (txs or {}).get("result") or []
    if isinstance(txlist, list) and txlist:
        out["tx_count"] = len(txlist)
        try:
            first_ts = int(txlist[0].get("timeStamp", 0))
            if first_ts > 0:
                out["wallet_age_days"] = round(
                    (time.time() - first_ts) / 86400.0, 1)
        except (TypeError, ValueError):
            pass
        out["note"] = "deployer resolved via Robinhood blockscout"
    else:
        out["note"] = "deployer resolved; wallet tx history unavailable"
    return out


def get_intel(chain, address):
    """Full intel bundle for one token. Never raises; gaps are explicit."""
    now = int(time.time())
    intel = {
        "chain": chain, "address": address,
        "resolved_chain": None, "symbol": None, "name": None,
        "price_usd": None, "price_change": {},
        "volume_24h": None, "buys_24h": None, "sells_24h": None,
        "liquidity_usd": None, "fdv": None, "pair_created_at_ms": None,
        "safety": {"checked": False, "note": "not attempted"},
        "deployer": {"checked": False, "note": "not attempted"},
        "unavailable": [],
        "fetched_ts": now,
    }
    try:
        mkt = dexscreener(chain, address)
    except Exception:
        mkt = None
    if mkt:
        intel.update({k: mkt[k] for k in (
            "resolved_chain", "symbol", "name", "price_usd", "price_change",
            "volume_24h", "buys_24h", "sells_24h", "liquidity_usd", "fdv",
            "pair_created_at_ms")})
    else:
        intel["unavailable"].append("dexscreener: no pairs found")

    rc = intel["resolved_chain"]
    try:
        intel["safety"] = honeypot_check(address, rc)
    except Exception:
        intel["safety"] = {"checked": False, "note": "safety check errored"}
    if not intel["safety"]["checked"]:
        intel["unavailable"].append("safety: " + intel["safety"]["note"])

    try:
        intel["deployer"] = deployer_info(address, rc)
    except Exception:
        intel["deployer"] = {"checked": False, "note": "deployer lookup errored"}
    if not intel["deployer"]["checked"]:
        intel["unavailable"].append("deployer: " + intel["deployer"]["note"])

    return intel


def safety_flagged(intel, max_tax_pct=10.0):
    """True only on positive danger signals. Unknown safety -> False (but
    decide.py treats unknown as caution in the Noul state, not as a pass)."""
    s = intel.get("safety") or {}
    if not s.get("checked"):
        return False
    if s.get("is_honeypot"):
        return True
    for tax in (s.get("buy_tax"), s.get("sell_tax")):
        try:
            if tax is not None and float(tax) > max_tax_pct:
                return True
        except (TypeError, ValueError):
            pass
    return False
