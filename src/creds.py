"""Secure credential access for signal-trading via authd surrogates.

Bot tokens live in the Secure Vault (custom.telegram-signal-bot,
custom.discord-signal-bot). They are never readable as plaintext: authd hands
out ``hsurr:`` surrogates, and the egress proxy swaps them for the real secret
on approved outbound requests.

QUIRK (2026-09-29, verified live): do NOT percent-encode the surrogate into
the URL. The bundled ``url_with_surrogate_path_segment()`` helper quotes it
(``hsurr:`` -> ``hsurr%3A``), which breaks the proxy's replacement -- Telegram
then 404s as if the token were invalid. Substitute the RAW surrogate string
into the URL template instead.
"""

import sys

_DC_PATH = "/opt/hatch/skills/skill-creator/bin"


def _dc():
    if _DC_PATH not in sys.path:
        sys.path.insert(0, _DC_PATH)
    import dynamic_credentials
    return dynamic_credentials


def _surrogate(credential_name):
    dc = _dc()
    entry = dc.dynamic_credential_entry(credential_name)
    surr = str(entry.get("surrogate", "")).strip()
    if not surr.startswith("hsurr:"):
        raise RuntimeError(f"authd returned no surrogate for {credential_name}")
    return dc, surr


def telegram_bot_url(template):
    """Fill a ``https://api.telegram.org/bot{}/<method>`` template.

    The surrogate is substituted RAW (unquoted) -- see module docstring.
    """
    dc, surr = _surrogate("custom.telegram-signal-bot")
    dc.ensure_allowed_url(template, ["api.telegram.org"])
    if "{}" not in template:
        raise RuntimeError("telegram_bot_url template must contain {}")
    return template.replace("{}", surr, 1)


def discord_auth_headers():
    """Headers for Discord Bot API calls: ``Authorization: Bot <surrogate>``.

    The ``Bot `` prefix is Discord's scheme; the proxy replaces the surrogate
    substring wherever it appears in the approved request.
    """
    dc, surr = _surrogate("custom.discord-signal-bot")
    return {"Authorization": f"Bot {surr}"}
