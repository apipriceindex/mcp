#!/usr/bin/env python3
"""
apipriceindex-mcp — serveur MCP de l'API Price Index.

Expose l'index ouvert (apipriceindex.com, CC BY 4.0) à tout agent compatible
MCP. Outils : chercher un modèle (avec filtres prix, capacités, vitesse),
obtenir son prix vérifié (avec source et date), estimer la facture mensuelle
d'un workload ; depuis 0.2.0, lister les deals du moment (gratuits, remises,
crédits), rendre le tableau des offres d'un modèle aux colonnes choisies,
comparer deux à quatre modèles, trouver le moins cher à un niveau de la note
Epoch, et lister les baisses, hausses et nouveaux modèles sur N jours.

Règles maison :
- La donnée est lue en direct sur /api/all.json (cache mémoire 15 min).
- Index injoignable → erreur explicite, JAMAIS de dernière valeur servie
  en silence.
- Index périmé (as_of > 14 j) → on répond quand même, avec avertissement.
- Pas de prix caché publié → aucune remise inventée (décision partagée avec
  le calculateur du site et llm-cost-diff).
- Chaque réponse porte l'attribution CC BY et l'as_of de l'index ; une réponse
  qui porte la note Epoch porte aussi son crédit (Epoch AI, CC BY 4.0).
- Vitesse et disponibilité : toujours avec leur source (OpenRouter) et leur
  fenêtre (médiane sur 30 min, 24 h), jamais de p95 ni de taux d'erreur.

Une seule dépendance : le SDK `mcp`. La logique métier est dans des fonctions
pures (_search/_price/_estimate) testables sans le SDK.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import datetime, timezone

DATA_URL = os.environ.get("APIPRICEINDEX_URL", "https://apipriceindex.com/api/all.json")
DEALS_URL = os.environ.get("APIPRICEINDEX_DEALS_URL", DATA_URL.rsplit("/", 1)[0] + "/deals.json")
HISTORY_URL = os.environ.get("APIPRICEINDEX_HISTORY_URL", DATA_URL.rsplit("/", 1)[0] + "/pricing-history.csv")
CACHE_TTL_S = 15 * 60
STALE_DAYS = 14
SEARCH_LIMIT = 20
RANK_LIMIT = 25
DEALS_LIMIT = 50
ATTRIBUTION = "API Price Index (https://apipriceindex.com) — CC BY 4.0"
INTEL_CREDIT = ("Intelligence: Epoch Capabilities Index by Epoch AI (https://epoch.ai/eci), "
                "CC BY 4.0 — credit Epoch AI when you reuse it")
SPEED_NOTE = "Speed: OpenRouter, median of the last 30 minutes before the daily read"
UPTIME_NOTE = "Availability: OpenRouter, share of successful requests over 24 hours"

_cache: dict = {}


class IndexUnavailable(RuntimeError):
    pass


def _fetch(url: str, as_text: bool = False):
    """JSON (ou texte) avec cache 15 min par URL. Lève IndexUnavailable si la
    source est morte — le cache expiré n'est pas resservi : mieux vaut une erreur
    qu'un prix faux."""
    now = time.monotonic()
    hit = _cache.get(url)
    if hit is not None and now - hit[0] < CACHE_TTL_S:
        return hit[1]
    req = urllib.request.Request(url, headers={"User-Agent": "apipriceindex-mcp/0.2"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8")
            data = raw if as_text else json.loads(raw)
    except Exception as e:  # réseau, HTTP, JSON : même verdict — indisponible
        raise IndexUnavailable(
            f"Price index unreachable ({url}): {e}. "
            "No cached data served — check https://apipriceindex.com/health.json"
        ) from e
    _cache[url] = (now, data)
    return data


def _fetch_index() -> dict:
    return _fetch(DATA_URL)


def _fetch_deals() -> dict:
    return _fetch(DEALS_URL)


def _fetch_history() -> str:
    return _fetch(HISTORY_URL, as_text=True)


def _meta(index: dict) -> dict:
    """Bloc commun à toutes les réponses : attribution + fraîcheur + alerte éventuelle."""
    meta = {"attribution": ATTRIBUTION, "index_as_of": index.get("as_of")}
    try:
        dt = datetime.strptime(index.get("as_of") or "", "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        age_d = (datetime.now(timezone.utc) - dt).days
        if age_d > STALE_DAYS:
            meta["warning"] = (
                f"Index is {age_d} days old — prices may be outdated. "
                "Check https://apipriceindex.com/health.json"
            )
    except ValueError:
        pass
    return meta


def _intel(m: dict) -> int | None:
    return ((m.get("intelligence") or {}).get("score"))


def _tps(m: dict) -> float | None:
    return ((m.get("speed") or {}).get("tokens_per_s_p50"))


def _blended(m: dict) -> float | None:
    p = m.get("pricing") or {}
    if p.get("input_per_mtok") is None or p.get("output_per_mtok") is None:
        return None
    return p["input_per_mtok"] + p["output_per_mtok"]


def _brief(m: dict) -> dict:
    p = m.get("pricing") or {}
    out = {
        "id": m["id"],
        "name": m.get("name"),
        "provider": (m.get("provider") or {}).get("name"),
        "context_window": m.get("context_window"),
        "input_usd_per_mtok": p.get("input_per_mtok"),
        "output_usd_per_mtok": p.get("output_per_mtok"),
        "open_weights": m.get("open_weights"),
    }
    # Ajouts 0.2.0 : seulement quand la donnée existe (aucun champ vide inventé).
    if p.get("cached_input_per_mtok") is not None:
        out["cached_input_usd_per_mtok"] = p["cached_input_per_mtok"]
    if (p.get("discount") or 0) > 0:
        out["discount_pct"] = round(p["discount"] * 100)
    if _tps(m) is not None:
        out["output_tokens_per_s_p50"] = _tps(m)
    if _intel(m) is not None:
        out["intelligence_eci"] = _intel(m)
    return out


# Capacités : ce que l'hôte déclare (registre + OpenRouter), jamais mesuré par nous.
_CAPS = {
    "tools": lambda m: "function-calling" in (m.get("capabilities") or []) or bool((m.get("served") or {}).get("tools")),
    "reasoning": lambda m: "reasoning" in (m.get("capabilities") or []) or bool((m.get("served") or {}).get("reasoning")),
    "vision": lambda m: "vision" in (m.get("capabilities") or []),
    "json": lambda m: "json-mode" in (m.get("capabilities") or []) or bool((m.get("served") or {}).get("structured_outputs")),
}
_CAPS["image"] = _CAPS["vision"]
_CAPS["structured_outputs"] = _CAPS["json"]


def _search(query: str, provider: str | None = None, min_context: int | None = None,
            open_weights: bool | None = None, max_price: float | None = None,
            capabilities: list[str] | None = None, min_speed: float | None = None) -> dict:
    index = _fetch_index()
    q = (query or "").strip().lower()
    caps = [c.strip().lower() for c in (capabilities or []) if c and c.strip()]
    unknown = [c for c in caps if c not in _CAPS]
    if unknown:
        return {"error": f"Unknown capability: {', '.join(unknown)}.",
                "hint": f"Use any of: {', '.join(sorted(_CAPS))}.", **_meta(index)}
    hits = []
    for m in index.get("models", []):
        hay = " ".join(str(x).lower() for x in (
            m.get("id"), m.get("name"), m.get("base_model"),
            (m.get("provider") or {}).get("name")) if x)
        if q and q not in hay:
            continue
        if provider and provider.strip().lower() != (m.get("provider") or {}).get("id", "").lower():
            continue
        if min_context is not None and (m.get("context_window") or 0) < min_context:
            continue
        if open_weights is not None and bool(m.get("open_weights")) != open_weights:
            continue
        if max_price is not None and (_blended(m) is None or _blended(m) > max_price):
            continue
        if any(not _CAPS[c](m) for c in caps):
            continue
        if min_speed is not None and (_tps(m) is None or _tps(m) < min_speed):
            continue
        hits.append(m)
    # Modèles avec prix vérifié d'abord : c'est eux qu'on peut vraiment servir.
    hits.sort(key=lambda m: (m.get("pricing") is None, m["id"]))
    shown = hits[:SEARCH_LIMIT]
    out = {
        "matches": [_brief(m) for m in shown],
        "total_matches": len(hits),
        "note": "Use the `id` field with get_price, estimate_cost or offers_table."
                + (" max_price is input + output per million tokens." if max_price is not None else ""),
        **_meta(index),
    }
    if any(_tps(m) is not None for m in shown):
        out["speed_source"] = SPEED_NOTE
    if any(_intel(m) is not None for m in shown):
        out["intelligence_credit"] = INTEL_CREDIT
    return out


def _price(model_id: str) -> dict:
    index = _fetch_index()
    m = next((x for x in index.get("models", []) if x.get("id") == model_id), None)
    if m is None:
        return {
            "error": f"'{model_id}' is not tracked by the index. No price invented.",
            "hint": "Find valid ids with search_models, or browse "
                    "https://apipriceindex.com/use-this-data/",
            **_meta(index),
        }
    p = m.get("pricing")
    if not p:
        return {
            "error": f"'{model_id}' is tracked but has no verified price yet.",
            "page": m.get("page"),
            **_meta(index),
        }
    out = {
        "id": m["id"],
        "name": m.get("name"),
        "provider": (m.get("provider") or {}).get("name"),
        "pricing": p,  # input/output/cached_input per Mtok, currency, confidence, verified_at, source_url
        "context_window": m.get("context_window"),
        "max_output": m.get("max_output"),
        "license": m.get("license"),
        "open_weights": m.get("open_weights"),
        "page": m.get("page"),
        **_meta(index),
    }
    # Ajouts 0.2.0, chacun avec sa source, seulement quand la donnée existe.
    if m.get("speed"):
        out["speed"] = m["speed"]
    if m.get("availability"):
        out["availability"] = m["availability"]
    if m.get("served"):
        out["served"] = m["served"]
    if m.get("intelligence"):
        out["intelligence"] = m["intelligence"]
        out["intelligence_credit"] = INTEL_CREDIT
    return out


def _monthly_cost(p: dict, input_mtok: float, output_mtok: float, cache_pct: float) -> tuple[float, bool]:
    """Même arithmétique que le calculateur du site et cost_diff.py.
    Renvoie (coût mensuel USD, remise cache réellement appliquée ?)."""
    cached = p.get("cached_input_per_mtok")
    c = min(100.0, max(0.0, cache_pct)) / 100.0
    if cached is None:
        c = 0.0  # pas de prix caché publié → aucune remise inventée
    cost = (input_mtok * (1 - c) * p["input_per_mtok"]
            + input_mtok * c * (cached if cached is not None else p["input_per_mtok"])
            + output_mtok * p["output_per_mtok"])
    return cost, c > 0


def _estimate(input_mtok: float, output_mtok: float, cache_pct: float = 0.0,
              models: list[str] | None = None) -> dict:
    index = _fetch_index()
    by_id = {m["id"]: m for m in index.get("models", [])}
    if models:
        unknown = [i for i in models if i not in by_id]
        if unknown:
            return {
                "error": f"Not tracked by the index: {', '.join(unknown)}. No price invented.",
                "hint": "Find valid ids with search_models.",
                **_meta(index),
            }
        pool = [by_id[i] for i in models]
    else:
        pool = list(by_id.values())
    lines = []
    for m in pool:
        p = m.get("pricing") or {}
        if p.get("input_per_mtok") is None or p.get("output_per_mtok") is None:
            continue  # catalogue entier : on saute les non-pricés au lieu d'échouer
        cost, cache_applied = _monthly_cost(p, input_mtok, output_mtok, cache_pct)
        lines.append({
            "id": m["id"],
            "name": m.get("name"),
            "provider": (m.get("provider") or {}).get("name"),
            "monthly_usd": round(cost, 2),
            "cache_discount_applied": cache_applied,
            "price_verified_at": p.get("verified_at"),
            "page": m.get("page"),
        })
    lines.sort(key=lambda l: l["monthly_usd"])
    truncated = models is None and len(lines) > RANK_LIMIT
    return {
        "workload": {"input_mtok": input_mtok, "output_mtok": output_mtok, "cache_pct": cache_pct},
        "estimates": lines[:RANK_LIMIT] if truncated else lines,
        "total_priced_models": len(lines),
        "note": "Cost ranking only — this index does not judge model quality. "
                "Cache discount applied only where the provider publishes a cached-input price.",
        **_meta(index),
    }


# --------------------------------- deals (0.2.0) ------------------------------
_KIND = {"free": {"free_model"}, "discount": {"discount"},
         "credits": {"free_credits", "free_tier", "no_cc_required"}}


def _deals(kind: str = "all", ending_within_days: int | None = None) -> dict:
    data = _fetch_deals()
    kind = (kind or "all").strip().lower()
    if kind not in ("all", *_KIND):
        return {"error": f"Unknown kind '{kind}'.", "hint": "Use all, free, discount or credits.",
                "attribution": ATTRIBUTION}
    types = set().union(*_KIND.values()) if kind == "all" else _KIND[kind]
    today = datetime.now(timezone.utc).date()
    rows = []
    for d in data.get("deals", []):
        if d.get("type") not in types:
            continue
        exp = d.get("expires_at")
        if ending_within_days is not None:
            if not exp:
                continue
            try:
                left = (datetime.strptime(exp[:10], "%Y-%m-%d").date() - today).days
            except ValueError:
                continue
            if left < 0 or left > ending_within_days:
                continue
        row = {k: d.get(k) for k in ("type", "title", "model", "base_model", "served_by", "value_usd",
                                     "discount_pct", "limits", "conditions", "starts_at", "source_url",
                                     "verified_at", "page") if d.get(k) not in (None, [], "")}
        row["provider"] = (d.get("provider") or {}).get("name")
        row["expires_at"] = exp or "no end date"
        rows.append(row)
    if kind in ("all", "discount") and ending_within_days is None:
        for a in data.get("api_discounts", []):
            rows.append({"type": "discount", "title": f"-{(a.get('discount_pct') or 0):g}% at {(a.get('provider') or {}).get('name')}",
                         "base_model": a.get("base_model"), "id": a.get("id"),
                         "provider": (a.get("provider") or {}).get("name"), "discount_pct": a.get("discount_pct"),
                         "input_usd_per_mtok": a.get("input_per_mtok"), "output_usd_per_mtok": a.get("output_per_mtok"),
                         "list_input_usd_per_mtok": a.get("list_input_per_mtok"),
                         "list_output_usd_per_mtok": a.get("list_output_per_mtok"),
                         "expires_at": "no end date", "source_url": a.get("source_url"),
                         "verified_at": a.get("verified_at"), "page": a.get("page")})
    order = {"free_model": 0, "discount": 1}
    rows.sort(key=lambda r: (order.get(r["type"], 2),
                             r["expires_at"] if r["expires_at"] != "no end date" else "9999",
                             -(r.get("discount_pct") or 0)))
    return {
        "deals": rows[:DEALS_LIMIT],
        "total": len(rows),
        "note": "expires_at 'no end date' means the host publishes none. Free endpoints carry rate limits "
                "and can end without notice; each deal links its official source.",
        "attribution": ATTRIBUTION,
        "deals_as_of": data.get("as_of"),
    }


# ------------------------------ offers_table (0.2.0) ---------------------------
_COLUMNS = {
    "input": lambda m: (m.get("pricing") or {}).get("input_per_mtok"),
    "output": lambda m: (m.get("pricing") or {}).get("output_per_mtok"),
    "cache": lambda m: (m.get("pricing") or {}).get("cached_input_per_mtok"),
    "discount": lambda m: round((m.get("pricing") or {}).get("discount", 0) * 100) or None,
    "speed": lambda m: _tps(m),
    "first_token": lambda m: (m.get("speed") or {}).get("first_token_ms_p50"),
    "uptime": lambda m: (m.get("availability") or {}).get("uptime_24h_pct"),
    "context": lambda m: m.get("context_window"),
    "verified_at": lambda m: (m.get("pricing") or {}).get("verified_at"),
}
_UNITS = {"input": "USD per million tokens", "output": "USD per million tokens",
          "cache": "USD per million cached input tokens", "discount": "% off the host's list price",
          "speed": "output tokens per second, median (OpenRouter, 30 min)",
          "first_token": "milliseconds to first token, median (OpenRouter, 30 min)",
          "uptime": "% successful requests over 24 h (OpenRouter)", "context": "tokens",
          "verified_at": "date the price was checked"}
DEFAULT_COLUMNS = ["input", "output", "cache", "speed", "uptime", "verified_at"]


def _offers(model: str, columns: list[str] | None = None) -> dict:
    index = _fetch_index()
    cols = [c.strip().lower() for c in (columns or DEFAULT_COLUMNS) if c and c.strip()]
    bad = [c for c in cols if c not in _COLUMNS]
    if bad:
        return {"error": f"Unknown column: {', '.join(bad)}.", "hint": f"Use any of: {', '.join(_COLUMNS)}.",
                **_meta(index)}
    q = (model or "").strip().lower()
    models = index.get("models", [])
    seed = next((m for m in models if m.get("id", "").lower() == q), None) \
        or next((m for m in models if (m.get("base_model") or "").lower() == q), None) \
        or next((m for m in models if q and q in str(m.get("name", "")).lower()), None)
    if seed is None:
        return {"error": f"'{model}' is not tracked by the index.",
                "hint": "Find the model with search_models; pass its id or base model.", **_meta(index)}
    base = seed.get("base_model")
    offers = [m for m in models if base and m.get("base_model") == base] or [seed]
    offers.sort(key=lambda m: (_blended(m) is None, _blended(m) or 0))
    rows = [{"provider": (m.get("provider") or {}).get("name"), "id": m["id"],
             **{c: _COLUMNS[c](m) for c in cols}} for m in offers]
    out = {
        "model": base or seed["id"],
        "offers": rows,
        "columns": {c: _UNITS[c] for c in cols},
        "note": "Cheapest input + output first; null means the host publishes no such figure "
                "or OpenRouter has too little traffic to measure it.",
        **_meta(index),
    }
    if "speed" in cols or "first_token" in cols:
        out["speed_source"] = SPEED_NOTE
    if "uptime" in cols:
        out["availability_source"] = UPTIME_NOTE
    intel = next((m.get("intelligence") for m in offers if m.get("intelligence")), None)
    if intel:
        out["intelligence"] = {k: intel.get(k) for k in ("eci", "score", "ci_low", "ci_high", "rank", "ranked_models")}
        out["intelligence_credit"] = INTEL_CREDIT
    return out


# --------------------------- résolution d'un modèle ---------------------------
def _find(models: list[dict], name: str) -> dict | None:
    """Une offre du modèle demandé : id exact, puis modèle de base, puis nom."""
    q = (name or "").strip().lower()
    if not q:
        return None
    return (next((m for m in models if m.get("id", "").lower() == q), None)
            or next((m for m in models if (m.get("base_model") or "").lower() == q), None)
            or next((m for m in models if q in str(m.get("name", "")).lower()), None))


def _family(models: list[dict], seed: dict) -> list[dict]:
    base = seed.get("base_model")
    return [m for m in models if base and m.get("base_model") == base] or [seed]


def _best(offers: list[dict], key, high: bool = False):
    vals = [(key(m), m) for m in offers if key(m) is not None]
    if not vals:
        return None, None
    v, m = (max if high else min)(vals, key=lambda x: x[0])
    return v, (m.get("provider") or {}).get("name")


# ------------------------------ compare_models (0.2.0) -------------------------
def _compare(models: list[str], input_mtok: float | None = None, output_mtok: float | None = None,
             cache_pct: float = 0.0) -> dict:
    index = _fetch_index()
    pool = index.get("models", [])
    names = [n for n in (models or []) if n and n.strip()]
    if not 2 <= len(names) <= 4:
        return {"error": "Give two to four models.", "hint": "Find ids with search_models.", **_meta(index)}
    missing = [n for n in names if _find(pool, n) is None]
    if missing:
        return {"error": f"Not tracked by the index: {', '.join(missing)}. No price invented.",
                "hint": "Find valid ids with search_models.", **_meta(index)}
    cols = []
    for n in names:
        seed = _find(pool, n)
        fam = [m for m in _family(pool, seed) if _blended(m) is not None]
        price = lambda k: (lambda m: (m.get("pricing") or {}).get(k))  # noqa: E731
        inp, inp_h = _best(fam, price("input_per_mtok"))
        out, out_h = _best(fam, price("output_per_mtok"))
        cac, cac_h = _best(fam, price("cached_input_per_mtok"))
        tps, tps_h = _best(fam, _tps, high=True)
        intel = next((m.get("intelligence") for m in fam + [seed] if m.get("intelligence")), None)
        col = {
            "model": seed.get("base_model") or seed["id"],
            "name": seed.get("name"),
            "providers": len(fam),
            "lowest_input": {"usd_per_mtok": inp, "provider": inp_h},
            "lowest_output": {"usd_per_mtok": out, "provider": out_h},
            "lowest_cache": {"usd_per_mtok": cac, "provider": cac_h},
            "fastest": {"output_tokens_per_s_p50": tps, "provider": tps_h},
            "context_window": max([m.get("context_window") or 0 for m in fam + [seed]]) or None,
            "license": seed.get("license"),
            "open_weights": seed.get("open_weights"),
            "intelligence_eci": (intel or {}).get("score"),
        }
        if input_mtok is not None and output_mtok is not None and fam:
            costs = [(_monthly_cost(m["pricing"], input_mtok, output_mtok, cache_pct)[0], m) for m in fam]
            c, m = min(costs, key=lambda x: x[0])
            col["monthly_usd"] = {"usd": round(c, 2), "provider": (m.get("provider") or {}).get("name")}
        cols.append(col)
    best = {}
    for label, get, high in (("lowest_input", lambda c: c["lowest_input"]["usd_per_mtok"], False),
                             ("lowest_output", lambda c: c["lowest_output"]["usd_per_mtok"], False),
                             ("lowest_cache", lambda c: c["lowest_cache"]["usd_per_mtok"], False),
                             ("fastest", lambda c: c["fastest"]["output_tokens_per_s_p50"], True),
                             ("intelligence_eci", lambda c: c["intelligence_eci"], True),
                             ("monthly_usd", lambda c: (c.get("monthly_usd") or {}).get("usd"), False)):
        vals = [(get(c), c["model"]) for c in cols if get(c) is not None]
        if vals:
            best[label] = (max if high else min)(vals, key=lambda x: x[0])[1]
    out = {"models": cols, "best": best,
           "note": "Lowest price across each model's providers, with the provider that gives it. "
                   "Cost only — the index does not judge quality beyond the Epoch score.",
           **_meta(index)}
    if any(c["fastest"]["output_tokens_per_s_p50"] is not None for c in cols):
        out["speed_source"] = SPEED_NOTE
    if any(c["intelligence_eci"] is not None for c in cols):
        out["intelligence_credit"] = INTEL_CREDIT
    if input_mtok is not None and output_mtok is not None:
        out["workload"] = {"input_mtok": input_mtok, "output_mtok": output_mtok, "cache_pct": cache_pct}
    return out


# ---------------------------- cheapest_at_level (0.2.0) ------------------------
def _at_level(min_score: float | None = None, as_good_as: str | None = None,
              open_weights: bool | None = None, limit: int = 5) -> dict:
    index = _fetch_index()
    pool = index.get("models", [])
    ref = None
    if as_good_as:
        seed = _find(pool, as_good_as)
        ref = next((m for m in _family(pool, seed) if m.get("intelligence")), None) if seed else None
        if ref is None:
            return {"error": f"'{as_good_as}' has no Epoch score in the index.",
                    "hint": "Pass min_score instead, or another model.", **_meta(index)}
        min_score = ref["intelligence"]["score"]
    if min_score is None:
        return {"error": "Give min_score (an Epoch Capabilities Index score) or as_good_as (a model).",
                **_meta(index)}
    bases: dict[str, list[dict]] = {}
    for m in pool:
        sc = _intel(m)
        if sc is None or sc < min_score or _blended(m) is None:
            continue
        if open_weights is not None and bool(m.get("open_weights")) != open_weights:
            continue
        bases.setdefault(m.get("base_model") or m["id"], []).append(m)
    rows = []
    for base, offers in bases.items():
        c = min(offers, key=_blended)
        it = c["intelligence"]
        rows.append({"model": base, "name": c.get("name"), "intelligence_eci": it.get("score"),
                     "eci_interval": [it.get("ci_low"), it.get("ci_high")],
                     "cheapest": {"id": c["id"], "provider": (c.get("provider") or {}).get("name"),
                                  "input_usd_per_mtok": c["pricing"]["input_per_mtok"],
                                  "output_usd_per_mtok": c["pricing"]["output_per_mtok"],
                                  "blended_usd_per_mtok": round(_blended(c), 4),
                                  "verified_at": c["pricing"].get("verified_at")},
                     "providers": len(offers), "page": c.get("page")})
    rows.sort(key=lambda r: r["cheapest"]["blended_usd_per_mtok"])
    out = {"min_score": min_score, "models": rows[:max(1, min(limit, 25))], "total": len(rows),
           "note": "Models whose Epoch Capabilities Index point score reaches min_score, cheapest first "
                   "(lowest input + output price per million tokens among their providers). Scores carry "
                   "an interval: models a few points apart may not differ.",
           "intelligence_credit": INTEL_CREDIT, **_meta(index)}
    if ref is not None:
        out["reference"] = {"model": ref.get("base_model") or ref["id"], "intelligence_eci": min_score,
                            "cheapest_blended_usd_per_mtok": round(min((_blended(m) for m in _family(pool, ref)
                                                                         if _blended(m) is not None), default=0), 4)}
    return out


# ------------------------------ price_changes (0.2.0) --------------------------
def _changes(days: int = 7, direction: str = "all", include_new_models: bool = True) -> dict:
    index = _fetch_index()
    direction = (direction or "all").strip().lower()
    if direction not in ("all", "drops", "rises"):
        return {"error": f"Unknown direction '{direction}'.", "hint": "Use all, drops or rises.", **_meta(index)}
    days = max(1, min(int(days or 7), 90))
    start = (datetime.now(timezone.utc).date().toordinal() - days)
    series: dict[str, list[tuple[str, float, float]]] = {}
    lines = _fetch_history().splitlines()
    head = lines[0].split(",") if lines else []
    ix = {h: i for i, h in enumerate(head)}
    for line in lines[1:]:
        f = line.split(",")
        try:
            series.setdefault(f[ix["id"]], []).append(
                (f[ix["date"]], float(f[ix["input_usd_per_mtok"]]), float(f[ix["output_usd_per_mtok"]])))
        except (KeyError, IndexError, ValueError):
            continue
    by_id = {m["id"]: m for m in index.get("models", [])}
    moves = []
    for mid, pts in series.items():
        pts.sort()
        before = [p for p in pts if datetime.strptime(p[0], "%Y-%m-%d").date().toordinal() < start]
        if not before:
            continue  # apparu pendant la période : nouveau, pas un changement
        ref, cur = before[-1], pts[-1]
        b0, b1 = ref[1] + ref[2], cur[1] + cur[2]
        if b0 <= 0 or abs(b1 - b0) < 1e-12:
            continue
        pct = (b1 - b0) / b0 * 100
        if (direction == "drops" and pct >= 0) or (direction == "rises" and pct <= 0):
            continue
        m = by_id.get(mid, {})
        moves.append({"id": mid, "model": m.get("base_model"), "provider": (m.get("provider") or {}).get("name"),
                      "changed_on": cur[0], "before": {"input": ref[1], "output": ref[2]},
                      "now": {"input": cur[1], "output": cur[2]}, "change_pct": round(pct, 1),
                      "reprices_often": bool((m.get("pricing") or {}).get("reprices_often")),
                      "page": m.get("page")})
    moves.sort(key=lambda x: x["change_pct"] if direction != "rises" else -x["change_pct"])
    out = {"days": days, "direction": direction, "price_changes": moves[:RANK_LIMIT], "total_changes": len(moves),
           "note": "Change on input + output USD per million tokens, from the last recorded price before the period "
                   "to today's. Full series: https://apipriceindex.com/api/pricing-history.csv",
           **_meta(index)}
    if include_new_models:
        new: dict[str, list[dict]] = {}
        for m in index.get("models", []):
            rd = m.get("release_date")
            try:
                ok = rd and datetime.strptime(rd[:10], "%Y-%m-%d").date().toordinal() >= start
            except ValueError:
                ok = False
            if ok and _blended(m) is not None:
                new.setdefault(m.get("base_model") or m["id"], []).append(m)
        rows = []
        for base, offers in new.items():
            c = min(offers, key=_blended)
            rows.append({"model": base, "name": c.get("name"), "release_date": c.get("release_date"),
                         "providers": len(offers), "cheapest": {"provider": (c.get("provider") or {}).get("name"),
                         "input_usd_per_mtok": c["pricing"]["input_per_mtok"],
                         "output_usd_per_mtok": c["pricing"]["output_per_mtok"]}, "page": c.get("page")})
        rows.sort(key=lambda r: r["release_date"] or "", reverse=True)
        out["new_models"] = rows[:RANK_LIMIT]
    return out


def build_server():
    """Construit le serveur MCP (import différé : la logique reste testable sans SDK).
    SDK mcp >= 2 (MCPServer, ex-FastMCP)."""
    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer("apipriceindex")

    @mcp.tool()
    def search_models(query: str, provider: str | None = None, min_context: int | None = None,
                      open_weights: bool | None = None, max_price: float | None = None,
                      capabilities: list[str] | None = None, min_speed: float | None = None) -> dict:
        """Search the API Price Index for LLM API endpoints by name, id or provider.

        Returns matching models with their `id` (use it with get_price /
        estimate_cost / offers_table), provider, context window and USD prices
        per million tokens. Optional filters: provider slug (e.g. 'openai'),
        minimum context window, open-weights only, max_price (input + output
        USD per million tokens), capabilities (any of tools, reasoning, vision,
        json — as declared by the host), min_speed (output tokens per second,
        OpenRouter median over 30 min). An empty query lists everything that
        passes the filters.
        """
        return _search(query, provider, min_context, open_weights, max_price, capabilities, min_speed)

    @mcp.tool()
    def get_price(model_id: str) -> dict:
        """Get the verified price of one model (USD per million tokens).

        Returns input/output/cached-input prices with verification date,
        cross-check confidence and source URL. Prices are re-checked daily,
        from each provider's own price list or from OpenRouter's per-provider
        data; source_url says which. Data: apipriceindex.com, CC BY 4.0.
        """
        return _price(model_id)

    @mcp.tool()
    def estimate_cost(input_mtok: float, output_mtok: float, cache_pct: float = 0,
                      models: list[str] | None = None) -> dict:
        """Estimate the monthly USD bill of a workload and rank models by cost.

        Give monthly volumes in millions of tokens (input and output) and an
        optional share of input served from cache (0-100). Ranks the given
        model ids — or the whole catalog — from cheapest to most expensive.
        Cost only: this tool does not rank model quality.
        """
        return _estimate(input_mtok, output_mtok, cache_pct, models)

    @mcp.tool()
    def list_deals(kind: str = "all", ending_within_days: int | None = None) -> dict:
        """List today's LLM API deals: models free to call right now, discounts,
        free credits and free tiers.

        kind: all, free, discount or credits. ending_within_days: only deals
        whose host-published end date falls within that many days (e.g. 2 for
        "what ends tomorrow?"). Every deal carries its end date ("no end date"
        when the host gives none), its official source and the date it was
        checked. Free models are read every morning from OpenRouter, host pages
        twice a day.
        """
        return _deals(kind, ending_within_days)

    @mcp.tool()
    def offers_table(model: str, columns: list[str] | None = None) -> dict:
        """Every host serving one model, as a table with the columns you choose.

        model: a model id, base model or name (find it with search_models).
        columns: any of input, output, cache, discount, speed, first_token,
        uptime, context, verified_at (default: input, output, cache, speed,
        uptime, verified_at). Rows are sorted cheapest first. Speed and uptime
        come from OpenRouter with their window; prices from each host's source.
        """
        return _offers(model, columns)

    @mcp.tool()
    def compare_models(models: list[str], input_mtok: float | None = None, output_mtok: float | None = None,
                       cache_pct: float = 0) -> dict:
        """Compare two to four models side by side.

        For each: lowest input, output and cached-input price with the provider
        that gives it, number of providers, fastest provider (OpenRouter median),
        context window, license, Epoch Capabilities Index score; `best` names the
        winner of each row. Give monthly volumes (millions of tokens) to add the
        monthly cost at each model's cheapest provider.
        """
        return _compare(models, input_mtok, output_mtok, cache_pct)

    @mcp.tool()
    def cheapest_at_level(min_score: float | None = None, as_good_as: str | None = None,
                          open_weights: bool | None = None, limit: int = 5) -> dict:
        """Cheapest models that reach a given intelligence level.

        Level = Epoch Capabilities Index score (Epoch AI, CC BY 4.0): pass
        min_score, or as_good_as a model to use its score. Returns the cheapest
        models at or above it (lowest input + output price among their
        providers), optionally open weights only.
        """
        return _at_level(min_score, as_good_as, open_weights, limit)

    @mcp.tool()
    def price_changes(days: int = 7, direction: str = "all", include_new_models: bool = True) -> dict:
        """Price drops and rises over the last N days, and the models released in that time.

        direction: all, drops or rises. Each change compares the last recorded
        price before the period with today's (input + output per million
        tokens). New models come with their cheapest provider.
        """
        return _changes(days, direction, include_new_models)

    return mcp


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
