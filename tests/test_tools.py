"""Tests sans réseau des outils de apipriceindex-mcp (0.2.0).

Lancement : python3 tests/test_tools.py — aucune dépendance (le SDK `mcp`
n'est pas importé : la logique vit dans des fonctions pures).

Ce que le test tient :
- les trois outils de 0.1 répondent comme avant (mêmes clés, même tri) ;
- search_models : filtres prix maximal, capacités, vitesse minimale ;
  une capacité inconnue est une erreur, pas un filtre ignoré ;
- list_deals : genres, « finit dans N jours », « no end date » écrit en clair,
  gratuits d'abord ;
- offers_table : colonnes choisies, moins cher d'abord, sources de la vitesse
  et de la disponibilité, crédit Epoch quand la note est là ;
- aucune note sans son crédit ; index injoignable = erreur, jamais de cache.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import apipriceindex_mcp as a  # noqa: E402

ECHECS = []


def verifie(ok, msg):
    if not ok:
        ECHECS.append(msg)


AS_OF = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
DEMAIN = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
LOIN = (datetime.now(timezone.utc) + timedelta(days=40)).strftime("%Y-%m-%d")
INTEL = {"eci": 154.8, "score": 155, "ci_low": 152.1, "ci_high": 156.9, "rank": 29, "ranked_models": 111}


def offre(i, prov, inp, out, **kw):
    m = {"id": f"{prov}:{i}", "name": i, "base_model": i, "provider": {"id": prov, "name": prov.title()},
         "context_window": 131072, "open_weights": True, "capabilities": ["text"],
         "pricing": {"input_per_mtok": inp, "output_per_mtok": out, "cached_input_per_mtok": None,
                     "verified_at": "2026-10-21", "source_url": f"https://{prov}.example/pricing", "discount": 0}}
    m.update(kw)
    return m


INDEX = {"as_of": AS_OF, "models": [
    offre("fast-1", "alpha", 0.1, 0.3, capabilities=["text", "function-calling"], served={"tools": True, "reasoning": True},
          speed={"tokens_per_s_p50": 250.0, "first_token_ms_p50": 300}, availability={"uptime_24h_pct": 99.9},
          intelligence=INTEL),
    offre("fast-1", "beta", 0.05, 0.2, speed={"tokens_per_s_p50": 80.0, "first_token_ms_p50": 900}, intelligence=INTEL),
    offre("slow-2", "gamma", 2.0, 8.0, capabilities=["text", "vision"]),
    {"id": "delta:unpriced", "name": "unpriced", "base_model": "unpriced", "provider": {"id": "delta", "name": "Delta"},
     "pricing": None},
]}
DEALS = {"as_of": AS_OF, "deals": [
    {"type": "free_model", "title": "Fast 1, free on OpenRouter", "model": "Fast 1", "provider": {"name": "OpenRouter"},
     "expires_at": DEMAIN, "source_url": "https://openrouter.ai/x", "verified_at": "2026-10-21"},
    {"type": "free_model", "title": "Other, free at Host", "model": "Other", "provider": {"name": "Host"},
     "expires_at": None, "source_url": "https://host.example", "verified_at": "2026-10-21"},
    {"type": "free_credits", "title": "$5 free credits", "provider": {"name": "Cred"}, "value_usd": 5,
     "expires_at": None, "source_url": "https://cred.example"},
    {"type": "discount", "title": "Half price until later", "provider": {"name": "Big"}, "discount_pct": 50,
     "expires_at": LOIN, "source_url": "https://big.example"},
], "api_discounts": [{"id": "beta:fast-1", "base_model": "fast-1", "provider": {"name": "Beta"}, "discount_pct": 40.0,
                      "input_per_mtok": 0.05, "output_per_mtok": 0.2, "source_url": "https://or.example"}]}

a._fetch = lambda url: DEALS if url.endswith("deals.json") else INDEX

# 1. Outils de 0.1 : mêmes clés, même tri (prix vérifié d'abord, puis id)
r = a._search("fast")
verifie([m["id"] for m in r["matches"]] == ["alpha:fast-1", "beta:fast-1"], f"tri de search_models : {r['matches']}")
verifie(all(k in r["matches"][0] for k in ("id", "name", "provider", "context_window", "input_usd_per_mtok",
                                           "output_usd_per_mtok", "open_weights")), "clés 0.1 de search_models")
verifie("attribution" in r and "index_as_of" in r, "attribution et as_of")
r = a._price("alpha:fast-1")
verifie(r["pricing"]["input_per_mtok"] == 0.1 and r.get("intelligence_credit"), f"get_price + crédit : {r}")
verifie("error" in a._price("nope:none"), "modèle inconnu : erreur")
r = a._estimate(10, 1, 0, None)
verifie([e["id"] for e in r["estimates"]][0] == "beta:fast-1", "estimate_cost : le moins cher d'abord")

# 2. Filtres de search_models
r = a._search("", max_price=0.5)
verifie({m["id"] for m in r["matches"]} == {"alpha:fast-1", "beta:fast-1"}, f"max_price : {r['matches']}")
r = a._search("", capabilities=["tools", "reasoning"])
verifie([m["id"] for m in r["matches"]] == ["alpha:fast-1"], f"capacités : {r['matches']}")
r = a._search("", capabilities=["vision"])
verifie([m["id"] for m in r["matches"]] == ["gamma:slow-2"], f"vision : {r['matches']}")
r = a._search("", min_speed=100)
verifie([m["id"] for m in r["matches"]] == ["alpha:fast-1"] and r.get("speed_source"), f"min_speed : {r}")
verifie("error" in a._search("", capabilities=["teleport"]), "capacité inconnue : erreur")
verifie(a._search("slow").get("intelligence_credit") is None, "crédit Epoch sans note")

# 3. list_deals
r = a._deals("free")
verifie([d["title"] for d in r["deals"]] == ["Fast 1, free on OpenRouter", "Other, free at Host"], f"gratuits : {r}")
verifie(r["deals"][1]["expires_at"] == "no end date", "« no end date » en clair")
r = a._deals("all", ending_within_days=2)
verifie([d["title"] for d in r["deals"]] == ["Fast 1, free on OpenRouter"], f"finit dans 2 jours : {r['deals']}")
r = a._deals("discount")
verifie(len(r["deals"]) == 2 and r["deals"][0]["title"] == "Half price until later", f"remises : {r['deals']}")
verifie(a._deals("credits")["total"] == 1, "crédits")
verifie("error" in a._deals("lottery"), "genre inconnu : erreur")

# 4. offers_table
r = a._offers("fast-1", ["input", "speed", "uptime"])
verifie([o["provider"] for o in r["offers"]] == ["Beta", "Alpha"], f"moins cher d'abord : {r['offers']}")
verifie(r["offers"][1]["speed"] == 250.0 and r["offers"][1]["uptime"] == 99.9 and r["offers"][0]["uptime"] is None,
        f"colonnes : {r['offers']}")
verifie(r.get("speed_source") and r.get("availability_source") and r.get("intelligence_credit"), "sources et crédit")
verifie("error" in a._offers("fast-1", ["mood"]), "colonne inconnue : erreur")
verifie("error" in a._offers("nothing-like-this"), "modèle inconnu : erreur")

# 5. Index injoignable : erreur, pas de cache resservi
import urllib.request  # noqa: E402
real = urllib.request.urlopen
urllib.request.urlopen = lambda *x, **k: (_ for _ in ()).throw(OSError("down"))
try:
    import importlib
    b = importlib.reload(a)
    try:
        b._fetch("https://example.invalid/all.json")
        verifie(False, "index injoignable : pas d'erreur")
    except b.IndexUnavailable:
        pass
finally:
    urllib.request.urlopen = real

if ECHECS:
    print("\n".join("✗ " + e for e in ECHECS))
    sys.exit(1)
print("✓ apipriceindex-mcp : outils 0.1 inchangés, filtres, deals, tableau d'offres, sources et crédit Epoch tenus")
