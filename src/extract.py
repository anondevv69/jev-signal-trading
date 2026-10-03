"""Regex extraction of token candidates from chat messages.

Finds:
  - EVM contract addresses: 0x + 40 hex chars          -> chain "evm"
  - Solana mints: 32-44 char base58 (no 0/O/I/l)        -> chain "solana"
  - Cashtags: $TICKER (letter-first, 2-11 chars)        -> symbol hint only
  - DexScreener / Photon / DEXTools / GeckoTerminal URLs -> contract + chain hint

Returns a normalized candidate list per message:
    {"chain": ..., "address": ..., "symbol": ..., "source": ...}

Chain is a coarse family ("evm" / "solana"); finer chain resolution is v2.
Everything here is pure and network-free. Run `python extract.py` for the self-test.
"""

import re

EVM_RE = re.compile(r"0x[0-9a-fA-F]{40}\b")
# base58 without 0/O/I/l; bounded so we don't slice longer blobs
SOL_RE = re.compile(
    r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])"
)
CASHTAG_RE = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,10})\b")
URL_RE = re.compile(r"https?://[^\s)>\]]+")

DEXSCREENER_RE = re.compile(
    r"dexscreener\.com/([a-z0-9-]+)/([1-9A-HJ-NP-Za-km-z]{32,44}|0x[0-9a-fA-F]{40})",
    re.IGNORECASE,
)
GECKO_RE = re.compile(
    r"geckoterminal\.com/([a-z0-9-]+)/pools/([1-9A-HJ-NP-Za-km-z]{32,44}|0x[0-9a-fA-F]{40})",
    re.IGNORECASE,
)
DEXTOOLS_RE = re.compile(
    r"dextools\.io/(?:app/[a-z]+/)?(?:[a-z-]+/)?pair-explorer/(0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})",
    re.IGNORECASE,
)
PHOTON_RE = re.compile(
    r"photon(?:-sol)?\.tinyastro\.io/[^\s]*?([1-9A-HJ-NP-Za-km-z]{32,44})"
)

# dexscreener/geckoterminal path slug -> coarse chain family
CHAIN_SLUGS = {
    "solana": "solana",
    "ethereum": "evm",
    "base": "evm",
    "bsc": "evm",
    "arbitrum": "evm",
    "polygon": "evm",
    "optimism": "evm",
    "avalanche": "evm",
    "unichain": "evm",
    "worldchain": "evm",
    "linea": "evm",
    "blast": "evm",
}


def _chain_for_address(addr: str, hint: str = "") -> str:
    if hint in ("solana", "evm"):
        return hint
    if addr.startswith("0x"):
        return "evm"
    return "solana"


def extract_candidates(text: str):
    """Extract token candidates from a message. Pure function."""
    if not text:
        return []
    found = {}  # (chain, address) -> candidate

    def add(chain, address, symbol="", source="text"):
        key = (chain, address)
        if key not in found:
            found[key] = {"chain": chain, "address": address,
                          "symbol": symbol, "source": source}
        elif symbol and not found[key]["symbol"]:
            found[key]["symbol"] = symbol

    # 1. URLs first (they carry chain hints); strip them from text afterwards
    #    so the same address isn't double-counted as a bare mention.
    work = text
    for m in URL_RE.finditer(text):
        url = m.group(0)
        dm = DEXSCREENER_RE.search(url)
        if dm:
            slug, addr = dm.group(1).lower(), dm.group(2)
            add(CHAIN_SLUGS.get(slug, _chain_for_address(addr)), addr, source="dexscreener")
            continue
        gm = GECKO_RE.search(url)
        if gm:
            slug, addr = gm.group(1).lower(), gm.group(2)
            add(CHAIN_SLUGS.get(slug, _chain_for_address(addr)), addr, source="geckoterminal")
            continue
        tm = DEXTOOLS_RE.search(url)
        if tm:
            add(_chain_for_address(tm.group(1)), tm.group(1), source="dextools")
            continue
        pm = PHOTON_RE.search(url)
        if pm:
            add("solana", pm.group(1), source="photon")
            continue
    work = URL_RE.sub(" ", work)

    # 2. Bare addresses
    for m in EVM_RE.finditer(work):
        add("evm", m.group(0))
    # Mask EVM matches before the Solana pass: the "0" in "0x" does not trip
    # SOL_RE's lookbehind, so without this the tail of an EVM address
    # (x + 32-43 hex chars) gets extracted as a phantom Solana mint.
    work = EVM_RE.sub(" ", work)
    for m in SOL_RE.finditer(work):
        add("solana", m.group(0))

    # 3. Cashtags -> attach as symbol hint to any address candidate in the message
    cashtags = CASHTAG_RE.findall(work)
    if cashtags and found:
        hint = cashtags[0].upper()
        for cand in found.values():
            if not cand["symbol"]:
                cand["symbol"] = hint

    return list(found.values())


def self_test():
    cases = [
        # (message, expected [(chain, address)])
        ("ape in 0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984",  # UNI
         [("evm", "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984")]),
        ("check this sol mint 7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU",
         [("solana", "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU")]),
        ("$BONK looking strong today", []),
        ("$WIF 0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984 moon",
         [("evm", "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984")]),
        ("https://dexscreener.com/solana/7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU",
         [("solana", "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU")]),
        ("https://dexscreener.com/ethereum/0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984",
         [("evm", "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984")]),
        ("no tokens here, just chatting about the game", []),
        # words containing l/o must not match base58
        ("hello world this is a normal longish sentence with words", []),
    ]
    fails = 0
    for text, expected in cases:
        got = [(c["chain"], c["address"]) for c in extract_candidates(text)]
        if sorted(got) != sorted(expected):
            fails += 1
            print(f"FAIL: {text!r}\n  expected {expected}\n  got      {got}")
    # cashtag symbol hint
    c = extract_candidates("$WIF 0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984")
    assert c and c[0]["symbol"] == "WIF", f"cashtag hint failed: {c}"
    # dedupe: same address twice -> one candidate
    c = extract_candidates("0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984 "
                           "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984")
    assert len(c) == 1, f"dedupe failed: {c}"
    print(f"{len(cases)} cases, {fails} failures")
    return fails == 0


if __name__ == "__main__":
    raise SystemExit(0 if self_test() else 1)
