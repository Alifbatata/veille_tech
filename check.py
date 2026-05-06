"""
Inspecteur de modeles Gemini accessibles a la cle API.

USAGE :
    python check.py                  # Liste tous les modeles + quotas + cascade
    python check.py --ping <modele>  # Teste UN modele avec 1 appel minimal (consomme 1 credit)

LIMITATION HONNETE : Google AI Studio NE FOURNIT PAS d'endpoint pour
consulter les credits restants par modele. Le seul moyen de savoir si un
modele a encore du quota est de l'appeler. Le mode --ping permet ce test
ciblé pour eviter de griller le quota a tester les 38 modeles d'un coup.
"""
from __future__ import annotations
import os
import sys
from dataclasses import dataclass

import google.generativeai as genai
from dotenv import load_dotenv

# =============================================================================
# Quotas free tier documentes (source : ai.google.dev/gemini-api/docs/rate-limits
# au 2026-Q1). Ces valeurs sont THEORIQUES — Google les ajuste regulierement.
# Vide pour les modeles preview/non documentes.
# =============================================================================
FREE_TIER_QUOTAS: dict[str, dict[str, str]] = {
    # Tier 1 — Gemini 2.5 stable
    "gemini-2.5-flash":              {"rpm": "10",  "rpd": "250",   "tpm": "250 000"},
    "gemini-2.5-flash-lite":         {"rpm": "15",  "rpd": "1 000", "tpm": "250 000"},
    "gemini-2.5-pro":                {"rpm": "5",   "rpd": "100",   "tpm": "250 000"},

    # Tier 1 bis — Gemini 3.x preview
    "gemini-3-flash-preview":        {"rpm": "?",   "rpd": "?",     "tpm": "?"},
    "gemini-3.1-flash-lite-preview": {"rpm": "?",   "rpd": "?",     "tpm": "?"},
    "gemini-3-pro-preview":          {"rpm": "?",   "rpd": "?",     "tpm": "?"},
    "gemini-3.1-pro-preview":        {"rpm": "?",   "rpd": "?",     "tpm": "?"},

    # Tier 2 — Gemini 2.0
    "gemini-2.0-flash":              {"rpm": "15",  "rpd": "200",   "tpm": "1 000 000"},
    "gemini-2.0-flash-001":          {"rpm": "15",  "rpd": "200",   "tpm": "1 000 000"},
    "gemini-2.0-flash-lite":         {"rpm": "30",  "rpd": "200",   "tpm": "1 000 000"},
    "gemini-2.0-flash-lite-001":     {"rpm": "30",  "rpd": "200",   "tpm": "1 000 000"},

    # Tier 3 — alias latest
    "gemini-flash-latest":           {"rpm": "10",  "rpd": "250",   "tpm": "250 000"},
    "gemini-flash-lite-latest":      {"rpm": "15",  "rpd": "1 000", "tpm": "250 000"},
    "gemini-pro-latest":             {"rpm": "5",   "rpd": "100",   "tpm": "250 000"},

    # Tier 4 — Gemini 1.5
    "gemini-1.5-pro":                {"rpm": "2",   "rpd": "50",    "tpm": "32 000"},
    "gemini-1.5-flash":              {"rpm": "15",  "rpd": "1 500", "tpm": "1 000 000"},
    "gemini-1.5-flash-8b":           {"rpm": "15",  "rpd": "1 500", "tpm": "1 000 000"},

    # Tier 5 — Gemma open-weights (free tier souvent plus genereux)
    "gemma-3-27b-it":                {"rpm": "30",  "rpd": "14 400", "tpm": "15 000"},
    "gemma-3-12b-it":                {"rpm": "30",  "rpd": "14 400", "tpm": "15 000"},
    "gemma-3-9b-it":                 {"rpm": "30",  "rpd": "14 400", "tpm": "15 000"},
    "gemma-3-4b-it":                 {"rpm": "30",  "rpd": "14 400", "tpm": "15 000"},
    "gemma-3-1b-it":                 {"rpm": "30",  "rpd": "14 400", "tpm": "15 000"},
    "gemma-3n-e4b-it":               {"rpm": "30",  "rpd": "14 400", "tpm": "15 000"},
    "gemma-3n-e2b-it":               {"rpm": "30",  "rpd": "14 400", "tpm": "15 000"},
    "gemma-2-27b-it":                {"rpm": "30",  "rpd": "14 400", "tpm": "15 000"},
    "gemma-2-9b-it":                 {"rpm": "30",  "rpd": "14 400", "tpm": "15 000"},
    "gemma-2-2b-it":                 {"rpm": "30",  "rpd": "14 400", "tpm": "15 000"},
    "gemma-4-31b-it":                {"rpm": "?",   "rpd": "?",     "tpm": "?"},
    "gemma-4-26b-a4b-it":            {"rpm": "?",   "rpd": "?",     "tpm": "?"},
}

# =============================================================================
# Tier de qualite pour NOTRE cas d'usage (analyse JSON en francais + scoring).
# Base sur les benchmarks publics + l'experience operationnelle :
# - Suivi d'instructions strictes (mode JSON)
# - Comprehension francophone nuancee
# - Capacite de raisonnement sur 20 articles simultanes
# =============================================================================
QUALITY_TIER: dict[str, str] = {
    # Tier S — qualite maximale, raisonnement profond
    "gemini-2.5-pro":                "S",
    "gemini-3-pro-preview":          "S",
    "gemini-3.1-pro-preview":        "S",
    "gemini-pro-latest":             "S",

    # Tier A — excellent equilibre qualite/cout
    "gemini-2.5-flash":              "A",
    "gemini-3-flash-preview":        "A",
    "gemini-flash-latest":           "A",

    # Tier B — bon equilibre, plus rapide
    "gemini-3.1-flash-lite-preview": "B",
    "gemini-2.5-flash-lite":         "B",
    "gemini-flash-lite-latest":      "B",

    # Tier C — Gemini 2.0 (correct mais legerement vieillissant)
    "gemini-2.0-flash":              "C",
    "gemini-2.0-flash-001":          "C",
    "gemini-1.5-pro":                "C",

    # Tier D — Gemini 2.0 lite + Gemma 27B (open-weights deja moins precis)
    "gemini-2.0-flash-lite":         "D",
    "gemini-2.0-flash-lite-001":     "D",
    "gemma-3-27b-it":                "D",
    "gemma-4-31b-it":                "D",
    "gemma-4-26b-a4b-it":            "D",
    "gemini-1.5-flash":              "D",

    # Tier E — modeles plus petits, moins precis
    "gemma-3-12b-it":                "E",
    "gemma-3-9b-it":                 "E",
    "gemma-2-27b-it":                "E",
    "gemini-1.5-flash-8b":           "E",

    # Tier F — derniers recours
    "gemma-3-4b-it":                 "F",
    "gemma-3-1b-it":                 "F",
    "gemma-3n-e2b-it":               "F",
    "gemma-3n-e4b-it":               "F",
    "gemma-2-9b-it":                 "F",
    "gemma-2-2b-it":                 "F",
}

TIER_COLOR = {"S": "💎", "A": "🥇", "B": "🥈", "C": "🥉", "D": "  ", "E": "  ", "F": "  "}


@dataclass
class ModelInfo:
    name:           str
    input_limit:    int
    output_limit:   int
    methods:        tuple[str, ...]
    version:        str


def _gather_models(api_key: str) -> list[ModelInfo]:
    genai.configure(api_key=api_key)
    out: list[ModelInfo] = []
    for m in genai.list_models():
        methods = tuple(getattr(m, "supported_generation_methods", []) or [])
        if "generateContent" not in methods:
            continue
        name = getattr(m, "name", "").replace("models/", "")
        out.append(ModelInfo(
            name=name,
            input_limit=getattr(m, "input_token_limit", 0) or 0,
            output_limit=getattr(m, "output_token_limit", 0) or 0,
            methods=methods,
            version=getattr(m, "version", "?") or "?",
        ))
    return out


def _print_main_table(models: list[ModelInfo]) -> None:
    print("\n" + "=" * 120)
    print(f"  {len(models)} MODELES ACCESSIBLES A TA CLE API")
    print("=" * 120)
    print(f"  {'Tier':<5} {'Modele':<40} {'Input':>10} {'Output':>8} {'RPM':>5} {'RPD':>8} {'TPM':>12}")
    print("-" * 120)

    # Tri par tier de qualite (S < A < B...) puis alpha
    def key(m: ModelInfo) -> tuple[str, str]:
        return (QUALITY_TIER.get(m.name, "Z"), m.name)
    for m in sorted(models, key=key):
        tier = QUALITY_TIER.get(m.name, "?")
        emoji = TIER_COLOR.get(tier, "  ")
        q = FREE_TIER_QUOTAS.get(m.name, {"rpm": "?", "rpd": "?", "tpm": "?"})
        in_lim = f"{m.input_limit:,}" if m.input_limit else "?"
        out_lim = f"{m.output_limit:,}" if m.output_limit else "?"
        print(f"  {emoji}{tier:<3} {m.name:<40} {in_lim:>10} {out_lim:>8} "
              f"{q['rpm']:>5} {q['rpd']:>8} {q['tpm']:>12}")
    print()


def _print_explanation() -> None:
    print("=" * 120)
    print("  LEGENDE")
    print("=" * 120)
    print("  Tier de qualite (mes recommandations pour notre cas d'usage : analyse JSON en francais)")
    print("    💎 S = qualite maximale (Pro) — quotas free tres restreints")
    print("    🥇 A = excellent equilibre (Flash) — recommande pour le scoring batch")
    print("    🥈 B = bon equilibre (Flash Lite)")
    print("    🥉 C = correct (Gemini 2.0 stable, 1.5 Pro)")
    print("       D = Gemma 27B + Gemini 2.0 lite (open-weights, suivi instructions moins strict)")
    print("       E = modeles plus legers")
    print("       F = derniers recours, modeles 1-4B")
    print()
    print("  RPM = Requests Per Minute (free tier documente)")
    print("  RPD = Requests Per Day  (free tier documente)")
    print("  TPM = Tokens Per Minute (free tier documente)")
    print()
    print("  ⚠️  IMPORTANT : ces quotas sont THEORIQUES. Google AI Studio NE FOURNIT PAS")
    print("     d'API pour consulter le credit RESTANT. Le seul moyen de tester reste l'appel.")
    print()
    print("  Cascade actuelle effective (configurable dans src/ai_filter.py:_MODEL_PREFERENCE) :")
    print("    Choix : garder Flash en premier pour la COHERENCE du scoring sur 31 batchs.")
    print("    Mettre Pro en premier griller son quota en 1-2 batchs et casserait l'homogeneite.")
    print("    => le PRO est utilise UNIQUEMENT pour le resume executif final (1 seul appel).")
    print()


def _ping_one(api_key: str, model_name: str) -> None:
    """Teste un modele specifique avec un appel minimal (consomme 1 credit)."""
    print(f"\n--- PING : {model_name} ---")
    print("⚠️  Cet appel CONSOMME 1 credit du modele teste.")
    genai.configure(api_key=api_key)
    try:
        model = genai.GenerativeModel(model_name)
        resp = model.generate_content(
            "Reponds OK et rien d'autre.",
            generation_config=genai.GenerationConfig(max_output_tokens=10, temperature=0.0),
        )
        if resp.candidates:
            text = resp.text.strip()[:100]
            print(f"  ✅ {model_name} repond : '{text}'")
        else:
            print(f"  ⚠️  {model_name} : reponse vide (peut-etre filtree par safety)")
    except Exception as exc:
        msg = str(exc)[:200]
        print(f"  ❌ {model_name} : {type(exc).__name__} — {msg}")


def main() -> int:
    load_dotenv()
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("❌ GEMINI_API_KEY introuvable dans .env")
        return 1

    if len(sys.argv) >= 3 and sys.argv[1] == "--ping":
        _ping_one(api_key, sys.argv[2])
        return 0

    models = _gather_models(api_key)
    _print_main_table(models)
    _print_explanation()

    print("=" * 120)
    print("  USAGE AVANCE")
    print("=" * 120)
    print("  python check.py --ping gemini-2.5-flash      # tester un modele specifique")
    print("  python check.py --ping gemini-3-pro-preview  # idem, consomme 1 credit")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
