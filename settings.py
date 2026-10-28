"""
Strategy settings editable from the dashboard.

Saved to settings.json and loaded at start-up AFTER .env, so values changed in the
dashboard win over .env. The 0.5 SOL and 5-trade limits are the field maxima here,
and bot.py enforces them again before any transaction is signed.
"""
import json
import os

GROUPS = ["Position size", "Exits", "Dev", "Holders", "Coin age", "Market cap", "Bundles", "Discord"]

# key, label, group, type, unit, min, max, step, scale (display = value * scale), help
SCHEMA = [
    dict(key="buy_sol", label="Buy size", group="Position size", type="float", unit="SOL", min=0.01, max=0.5,
         step=0.01, help="SOL spent on each launch snipe. Hard limit 0.5."),
    dict(key="copy_buy_sol", label="Copy-trade buy size", group="Position size", type="float", unit="SOL",
         min=0.01, max=0.5, step=0.01, help="SOL spent when copying a tracked wallet. Hard limit 0.5."),
    dict(key="max_open_positions", label="Max open trades", group="Position size", type="int", unit="",
         min=1, max=5, step=1, help="Trades open at once. Hard limit 5."),

    dict(key="take_profit_pct", label="Take profit", group="Exits", type="float", unit="%", min=5, max=2000,
         step=5, help="Sell when up this much. Applies to open trades immediately."),
    dict(key="stop_loss_pct", label="Stop loss", group="Exits", type="float", unit="%", min=1, max=95, step=1,
         help="Sell when down this much. Applies to open trades immediately."),
    dict(key="trailing_stop_pct", label="Trailing stop", group="Exits", type="float", unit="%", min=0, max=90,
         step=1, help="Once in profit, sell if price falls this far from its peak. 0 = off."),
    dict(key="max_hold_seconds", label="Max hold time", group="Exits", type="int", unit="s", min=10, max=86400,
         step=10, help="Sell after this long no matter what."),

    dict(key="max_dev_share", label="Max dev holding", group="Dev", type="float", unit="%", min=0, max=100,
         step=0.5, scale=100, help="Skip if the dev's launch buy is more than this % of supply."),
    dict(key="allow_dev_selling", label="Allow dev selling", group="Dev", type="bool",
         help="Off = skip a coin as soon as its dev sells any."),

    dict(key="min_unique_buyers", label="Min holders (buyers)", group="Holders", type="int", unit="", min=0,
         max=1000, step=1, help="Distinct wallets (not the dev) that bought before the bot buys."),
    dict(key="max_top_buyer_pct", label="Max top buyer share", group="Holders", type="float", unit="%", min=1,
         max=100, step=1, help="Skip if one wallet made more than this % of all buys. 100 = off."),
    dict(key="min_net_inflow_sol", label="Min net inflow", group="Holders", type="float", unit="SOL", min=0,
         max=1000, step=0.1, help="Buys minus sells (SOL) since launch."),

    dict(key="watch_seconds", label="Min coin age", group="Coin age", type="int", unit="s", min=1, max=3600,
         step=1, help="Don't buy before the coin is this old."),
    dict(key="max_age_seconds", label="Max coin age", group="Coin age", type="int", unit="s", min=1, max=3600,
         step=1, help="Stop considering a coin after this age. The bot buys at the first moment between "
                      "min and max age when every rule passes."),

    dict(key="min_mcap_sol", label="Min market cap", group="Market cap", type="float", unit="SOL", min=0,
         max=1_000_000, step=1, help="Launch snipes only."),
    dict(key="max_mcap_sol", label="Max market cap", group="Market cap", type="float", unit="SOL", min=1,
         max=1_000_000, step=1, help="Launch snipes only."),
    dict(key="copy_max_mcap_sol", label="Copy-trade max market cap", group="Market cap", type="float",
         unit="SOL", min=1, max=1_000_000, step=10, help="Don't copy into coins already above this."),

    dict(key="max_bundle_pct", label="Max bundled supply", group="Bundles", type="float", unit="%", min=0,
         max=100, step=0.5, help="Skip if other wallets bought more than this % of supply right at launch "
                                 "(typical of a dev bundling). 100 = off."),
    dict(key="bundle_window_ms", label="Bundle window", group="Bundles", type="int", unit="ms", min=100,
         max=10000, step=100, help="Buys landing within this long after launch count as bundled. "
                                   "Approximate: based on when the bot receives each trade."),

    dict(key="min_social_authors", label="Min Discord callers", group="Discord", type="int", unit="",
         min=0, max=50, step=1, help="Different people who must have posted the coin's address in your "
                                     "Discord servers before the bot will buy it. 0 = off (Discord is "
                                     "still logged and analyzed either way)."),
    dict(key="social_window_min", label="Caller window", group="Discord", type="int", unit="min", min=1,
         max=720, step=5, help="Only count Discord calls from the last this many minutes."),
]
BY_KEY = {f["key"]: f for f in SCHEMA}


# One-click profiles for the editor. Values are in INTERNAL units (same as Config fields) and only the keys
# listed are changed. They stay inside the hard limits (0.5 SOL per trade, 5 open trades): every value is
# validated by test_strategy.py against SCHEMA. "balanced" is the shipped default; "aggressive" trades more
# often (looser entry rules, full-size copy trades) and lets winners run further. It is a judgment call, not a
# backtested result: more trades also means more fees and more rugs, so try it in PAPER mode first.
PRESETS = {
    "balanced": dict(buy_sol=0.5, copy_buy_sol=0.5, max_open_positions=5, take_profit_pct=60, stop_loss_pct=25,
                     trailing_stop_pct=20, max_hold_seconds=300, max_dev_share=0.10, allow_dev_selling=False,
                     min_unique_buyers=8, max_top_buyer_pct=100, min_net_inflow_sol=1.0, watch_seconds=20,
                     max_age_seconds=20, min_mcap_sol=30, max_mcap_sol=120, copy_max_mcap_sol=400,
                     max_bundle_pct=100, bundle_window_ms=1500),
    "aggressive": dict(buy_sol=0.5, copy_buy_sol=0.5, max_open_positions=5, take_profit_pct=100, stop_loss_pct=25,
                       trailing_stop_pct=25, max_hold_seconds=900, max_dev_share=0.30, allow_dev_selling=False,
                       min_unique_buyers=5, max_top_buyer_pct=100, min_net_inflow_sol=0.5, watch_seconds=1,
                       max_age_seconds=600, min_mcap_sol=30, max_mcap_sol=1000, copy_max_mcap_sol=600,
                       max_bundle_pct=20, bundle_window_ms=1500),
}
BACKUP_NAME = "settings.before-aggressive.json"      # written when the aggressive profile is applied to a file


def preset_display(values: dict) -> dict:
    """Internal values -> display units, the way the editor shows them."""
    out = {}
    for k, v in values.items():
        f = BY_KEY.get(k)
        if not f:
            continue
        if f["type"] == "bool":
            out[k] = bool(v)
        else:
            d = v * f.get("scale", 1)
            out[k] = round(d, 6) if f["type"] == "float" else int(d)
    return out


def presets_for(settings_path: str) -> dict:
    """What the editor can load: the built-in profiles, plus the settings saved before aggressive was applied."""
    out = {"balanced": preset_display(PRESETS["balanced"]), "aggressive": preset_display(PRESETS["aggressive"])}
    here = os.path.dirname(os.path.abspath(settings_path))
    prev = load(os.path.join(here, BACKUP_NAME))
    if prev:
        out["previous"] = preset_display(prev)
    res = research_suggestion(os.path.join(here, RESEARCH_FILE))
    if res:
        out["research"] = preset_display(res)
    return out


RESEARCH_FILE = "research_result.json"             # written by backtest/sweep.py
RESEARCH_KEYS = {"take_profit_pct", "stop_loss_pct", "trailing_stop_pct", "max_hold_seconds",
                 "min_mcap_sol", "max_mcap_sol"}   # the only settings research may suggest (never size or limits)


def research_suggestion(path: str) -> dict:
    """The settings backtest/sweep.py found to hold up on unseen data (empty when none did)."""
    try:
        data = json.load(open(path, encoding="utf-8"))
        sug = data["suggested_settings"] if data.get("held_up_on_unseen_data") and not data.get("synthetic") else {}
    except (OSError, ValueError, KeyError, AttributeError, TypeError):
        return {}
    out = {}
    for k, v in (sug or {}).items():
        f = BY_KEY.get(k)
        if k in RESEARCH_KEYS and f and isinstance(v, (int, float)) and not isinstance(v, bool) \
                and f["min"] <= v * f.get("scale", 1) <= f["max"]:
            out[k] = int(v) if f["type"] == "int" else float(v)
    return out


def display(cfg, f):
    v = getattr(cfg, f["key"])
    if f["type"] == "bool":
        return bool(v)
    v = v * f.get("scale", 1)
    return round(v, 6) if f["type"] == "float" else int(v)


def schema_with_values(cfg, settings_path: str = "") -> dict:
    out = {"groups": GROUPS, "fields": [{**f, "value": display(cfg, f)} for f in SCHEMA]}
    if settings_path:
        out["presets"] = presets_for(settings_path)
    return out


def validate(cfg, updates: dict) -> tuple[dict, list[str]]:
    """Display-unit values from the UI -> internal values. Returns (clean, errors)."""
    clean, errors = {}, []
    for key, raw in (updates or {}).items():
        f = BY_KEY.get(key)
        if not f:
            errors.append(f"unknown setting: {key}")
            continue
        try:
            if f["type"] == "bool":
                if not isinstance(raw, bool):
                    raise ValueError
                clean[key] = raw
                continue
            v = float(raw)
            if v != v or v in (float("inf"), float("-inf")):
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"{f['label']}: not a valid value")
            continue
        if not (f["min"] <= v <= f["max"]):
            errors.append(f"{f['label']}: must be between {f['min']:g} and {f['max']:g}{f['unit'] and ' ' + f['unit']}")
            continue
        if f["type"] == "int":
            v = int(round(v))
        clean[key] = v / f.get("scale", 1) if f["type"] == "float" else v
    # cross-field rules, judged on the values that would result
    get = lambda k: clean.get(k, getattr(cfg, k))
    if get("min_mcap_sol") >= get("max_mcap_sol"):
        errors.append("Min market cap must be below max market cap")
    if get("watch_seconds") > get("max_age_seconds"):
        errors.append("Min coin age can't be above max coin age")
    return clean, errors


def apply(cfg, clean: dict) -> list[str]:
    """Set values on the live config. Returns human-readable change notes."""
    notes = []
    for k, v in clean.items():
        f = BY_KEY[k]
        old = getattr(cfg, k)
        if old == v:
            continue
        setattr(cfg, k, v)
        if f["type"] == "bool":
            notes.append(f"{f['label']}: {'on' if v else 'off'}")
        else:
            s = f.get("scale", 1)
            notes.append(f"{f['label']}: {old * s:g} → {v * s:g}{f['unit'] and ' ' + f['unit']}")
    return notes


def load(path: str) -> dict:
    """Internal values saved earlier (bad or unknown entries are ignored)."""
    if not os.path.exists(path):
        return {}
    try:
        data = json.load(open(path, encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    out = {}
    for k, v in data.items():
        f = BY_KEY.get(k)
        if not f:
            continue
        if f["type"] == "bool" and isinstance(v, bool):
            out[k] = v
        elif f["type"] != "bool" and isinstance(v, (int, float)) and not isinstance(v, bool):
            disp = v * f.get("scale", 1)
            if f["min"] <= disp <= f["max"]:
                out[k] = int(v) if f["type"] == "int" else float(v)
    return out


def save(path: str, cfg):
    data = {f["key"]: getattr(cfg, f["key"]) for f in SCHEMA}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)
