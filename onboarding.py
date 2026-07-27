"""
onboarding.py
-------------
MVP vocabulary onboarding for new users of the German language-learning pipeline.

Replaces hardcoded seed lists with a structured module that maps a user's
self-reported proficiency level to a curated, frequency-ranked lemma set
and writes it to the UserKnowledgeStore.

MVP onboarding flow
-------------------
1. Ask the user to self-report their approximate German level:
       "Have you studied German before?  Never / A bit (A1) / Some (A2) / Intermediate (B1)"
2. Call VocabularyOnboarding.seed_from_level(user_id, tier, store).
3. Optionally show the user a short word sample from the next tier up and
   let them call mark_known() for items they already recognise.
4. The pipeline's i+1 filter can now fire on real subtitle content.

This is intentionally not a quiz engine.  Level self-reporting is fast and
accurate enough to bootstrap the filter.  Individual mis-calibration corrects
itself organically: an over-seeded user sees the filter fire too easily and
quickly exhausts the obvious targets; an under-seeded user sees fewer matches
initially.  Both stabilise after the first few subtitle files.

Frequency tier design
---------------------
Tiers are cumulative: seeding A2 covers everything in A1 plus A2-specific
vocabulary.  The tier boundaries are approximate; the goal is not linguistic
precision but a seed set large enough that the i+1 filter can fire reliably.

  COMPLETE_BEGINNER  No seed.  The i+1 filter will rarely fire until the
                     user encounters enough vocabulary through raw exposure.
                     Use only for a fully cold start.

  A1   ~130 lemmas   Core modals and auxiliaries, the most-used full verbs,
                     essential prepositions and conjunctions, high-frequency
                     adverbs, basic nouns and adjectives.

  A2   ~300 lemmas   Everything in A1 plus numbers (as words), colours, body
                     parts, transport, food, family, and everyday abstract
                     vocabulary.

  B1   ~500 lemmas   Everything in A2 plus abstract/separable verbs, discourse
                     markers, and society/work nouns.

Safety contract
---------------
seed_from_level() and seed_from_lemmas() never demote a unit: if the user
already has a record at or above the requested state (e.g. KNOWN_ACTIVE from
SRS review), it is left untouched.  Only units currently below the target
state are updated.

Limitations
-----------
- The embedded lemma lists are hand-curated starting points.  For production,
  replace or supplement them with frequency data derived from a verified German
  subtitle corpus (e.g. SUBTLEX-DE, OpenSubtitles-DE).
- Self-reporting is imprecise at level boundaries.  Both over- and
  under-seeding self-correct over time via exposure and SRS review.
- Separable-verb keys (e.g. "anfangen", "aufmachen") must match the combined
  form produced by LearningUnitExtractor.  Verify against real spaCy output
  before extending the B1 list.
- Lemmas in learning_units._SKIP_LEMMAS (e.g. "ich", "doch") are silently
  dropped at seed time: they are never extraction targets, so seeding them
  has no effect and clutters the store.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional

from learning_units import LearningUnit, LearningUnitType, _SKIP_LEMMAS
from user_knowledge import KnowledgeState, UserKnowledgeStore


# ---------------------------------------------------------------------------
# Level tier enum
# ---------------------------------------------------------------------------

class LevelTier(str, Enum):
    """
    Self-reported proficiency tier.

    Used to select a pre-built vocabulary seed set.  Values match the
    CEFR labels A1–B1 informally; there is no A0 or B2+ in MVP.
    """
    COMPLETE_BEGINNER = "complete_beginner"
    A1 = "a1"
    A2 = "a2"
    B1 = "b1"


# ---------------------------------------------------------------------------
# Frequency-ranked lemma sets
#
# Storage strategy: each constant holds only the *additions* for that tier.
# get_tier_lemmas() computes the cumulative union.  This makes it easy to
# audit what changes between tiers and to extend individual tiers later.
#
# All strings are lowercase canonical lemma keys, matching LearningUnit.key
# (which enforces lowercase in __post_init__).
# ---------------------------------------------------------------------------

# Tier 1 — A1
# Core auxiliaries, most-used full verbs, essential prepositions and
# conjunctions, high-frequency adverbs, basic nouns and adjectives.
_A1_LEMMAS: frozenset[str] = frozenset({
    # --- Core auxiliaries and modals ---
    "sein", "haben", "werden",
    "können", "müssen", "wollen", "sollen", "dürfen", "mögen",

    # --- Most-used full verbs ---
    "gehen", "kommen", "machen", "sagen", "sehen", "wissen",
    "denken", "finden", "nehmen", "geben", "stehen", "liegen",
    "sitzen", "essen", "trinken", "schlafen", "fahren", "lesen",
    "hören", "sprechen", "lernen", "kaufen", "heißen", "wohnen",
    "bleiben", "laufen", "schreiben", "arbeiten", "spielen",
    "bringen", "brauchen", "suchen", "fragen", "verstehen",
    "helfen", "zeigen", "leben", "legen", "stellen",

    # --- Determiners and articles ---
    "der", "die", "das", "ein", "kein",
    "mein", "dein", "unser",
    "dieser", "jeder", "alle",

    # --- Prepositions ---
    "in", "auf", "mit", "von", "zu", "an", "für", "aus",
    "bei", "nach", "über", "unter", "durch", "ohne",
    "gegen", "um", "bis", "seit",

    # --- Conjunctions and subordinators ---
    "und", "oder", "aber", "dass", "wenn", "weil", "als",
    "ob", "wie", "so", "also", "dann", "deshalb",

    # --- High-frequency adverbs ---
    # Note: "ja", "nein", "doch", "schon", "noch", "mal" are in
    # _SKIP_LEMMAS and are filtered at seed time; do not add them here.
    "nicht", "auch", "nur", "sehr", "hier", "da",
    "jetzt", "heute", "immer", "wieder", "ganz", "wirklich",
    "natürlich", "vielleicht", "manchmal", "oft", "mehr",
    "genug", "fast", "gar", "nun",

    # --- Reflexive / indefinite pronoun ---
    # "ich", "du", "er", "sie", etc. are in _SKIP_LEMMAS; include only
    # pronouns that CAN appear as learning-unit targets.
    "man", "sich",

    # --- Basic adjectives ---
    "gut", "schlecht", "groß", "klein", "neu", "alt",
    "jung", "lang", "kurz", "schön", "heiß", "kalt", "warm",
    "richtig", "falsch", "schwer", "leicht", "wichtig",

    # --- Core nouns ---
    "tag", "zeit", "mann", "frau", "kind", "haus", "jahr",
    "hand", "stadt", "land", "welt", "leben", "weg", "arbeit",
    "schule", "geld", "buch", "frage", "name", "wasser",
})

# Tier 2 — A2 additions
# Numbers, colours, body, transport, food, family, everyday abstract vocab.
_A2_ADDITIONAL_LEMMAS: frozenset[str] = frozenset({
    # --- More verbs ---
    "erklären", "erzählen", "kennen", "glauben", "antworten",
    "treffen", "reisen", "warten", "öffnen", "schließen",
    "beginnen", "vergessen", "erinnern", "vorstellen",
    "anfangen", "aufmachen", "ankommen", "vorbereiten",
    "waschen", "zahlen", "holen", "reden", "wünschen",
    "teilen", "versuchen", "hoffen", "benutzen", "verlassen",
    "passieren", "verlieren", "verkaufen", "bezahlen",
    "empfehlen", "gehören", "meinen", "mitnehmen",

    # --- Numbers as words ---
    "eins", "zwei", "drei", "vier", "fünf",
    "sechs", "sieben", "acht", "neun", "zehn",
    "zwanzig", "hundert", "tausend",

    # --- Colours ---
    "rot", "blau", "grün", "gelb", "schwarz", "weiß", "grau", "braun",

    # --- More adjectives ---
    "einfach", "schwierig", "billig", "teuer", "müde", "glücklich",
    "traurig", "ruhig", "laut", "frisch", "sauber", "schmutzig",
    "offen", "fertig", "nötig", "pünktlich", "nett", "toll",
    "gesund", "krank", "hungrig", "durstig", "frei", "beliebt",

    # --- Time ---
    "morgen", "gestern", "abend", "nacht", "woche", "monat",
    "minute", "stunde", "sekunde",

    # --- Family and people ---
    "vater", "mutter", "bruder", "schwester", "eltern",
    "freundin", "kollege", "lehrer", "schüler", "chef",

    # --- Transport and places ---
    "auto", "bus", "zug", "fahrrad", "flugzeug", "bahnhof",
    "straße", "platz", "ort", "hotel", "restaurant",

    # --- Food and drink ---
    "brot", "milch", "fleisch", "kaffee", "tee", "obst", "gemüse",

    # --- Body ---
    "kopf", "arm", "bein", "auge", "ohr", "mund",

    # --- Everyday nouns ---
    "büro", "telefon", "brief", "gast", "raum", "tisch",
    "plan", "idee", "problem", "thema", "wort", "sprache",
    "unterschied", "ergebnis", "beispiel", "möglichkeit", "situation",

    # --- Determiners and pronouns ---
    "beide", "einige", "welch", "solch",

    # --- More adverbs and discourse connectives ---
    "trotzdem", "außerdem", "deswegen", "endlich", "plötzlich",
    "sofort", "bereits", "ungefähr", "zusammen", "allein",
    "meistens", "leider", "eigentlich", "wenigstens", "kaum",
})

# Tier 3 — B1 additions
# Abstract verbs, discourse markers, society/work/everyday nouns, adjectives.
_B1_ADDITIONAL_LEMMAS: frozenset[str] = frozenset({
    # --- Abstract and separable verbs ---
    "entscheiden", "entwickeln", "erwarten", "erlauben",
    "fördern", "verändern", "verbessern", "vorschlagen",
    "darstellen", "beschreiben", "erreichen", "nachdenken",
    "aufhören", "bestehen", "erscheinen", "entstehen",
    "erkennen", "berichten", "betonen", "bemerken", "beachten",
    "leiten", "lösen", "führen", "gelten", "nennen",
    "steigen", "stimmen", "tragen", "unterscheiden",
    "verhindern", "zunehmen", "abnehmen", "gelingen",
    "scheitern", "bewegen", "weitermachen", "vorgehen",
    "anbieten", "aufbauen", "ausgehen",

    # --- Society, politics, work ---
    "bereich", "bevölkerung", "bildung", "einfluss", "entwicklung",
    "erfahrung", "gesellschaft", "gesetz", "grund", "information",
    "interesse", "lage", "lösung", "meinung", "nachricht",
    "ordnung", "politik", "recht", "regierung", "sicherheit",
    "system", "verantwortung", "verhältnis", "wirtschaft",
    "wirkung", "zusammenhang", "entscheidung", "fähigkeit",
    "folge", "grenze", "inhalt", "kosten", "leistung",
    "preis", "produkt", "programm", "projekt", "qualität",
    "reaktion", "regel", "stärke", "ursache", "verbindung",
    "zustand", "angebot", "vorteil", "nachteil", "aufgabe",
    "erklärung", "bedeutung", "einheit", "kraft",
    "struktur", "ziel", "zweck", "rahmen", "niveau",

    # --- More adjectives ---
    "aktuell", "allgemein", "ähnlich", "bedeutend", "bekannt",
    "bereit", "deutlich", "direkt", "entsprechend", "erfolgreich",
    "gemeinsam", "genau", "gewiss", "international", "möglich",
    "national", "offiziell", "persönlich", "politisch", "praktisch",
    "sozial", "stark", "typisch", "wirtschaftlich", "wissenschaftlich",
    "öffentlich", "zusätzlich", "wesentlich", "besondere",

    # --- Discourse markers and adverbs ---
    "allerdings", "anscheinend", "dagegen", "daher", "damals",
    "dabei", "damit", "darüber", "davon", "dazu", "gegenüber",
    "jedoch", "letztlich", "möglicherweise", "offenbar",
    "schließlich", "seitdem", "sicherlich", "stattdessen",
    "tatsächlich", "überall", "übrigens", "weitgehend",
    "zumindest", "zunächst", "zwar", "immerhin",
    "einerseits", "andererseits", "inzwischen", "jedenfalls",
    "letztendlich", "scheinbar", "offensichtlich",
})

# Map from tier to its own delta set — used by get_tier_lemmas().
_TIER_DELTAS: dict[LevelTier, frozenset[str]] = {
    LevelTier.COMPLETE_BEGINNER: frozenset(),
    LevelTier.A1:                _A1_LEMMAS,
    LevelTier.A2:                _A2_ADDITIONAL_LEMMAS,
    LevelTier.B1:                _B1_ADDITIONAL_LEMMAS,
}

# Ordered progression, lowest to highest.
_TIER_ORDER: tuple[LevelTier, ...] = (
    LevelTier.COMPLETE_BEGINNER,
    LevelTier.A1,
    LevelTier.A2,
    LevelTier.B1,
)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OnboardingResult:
    """
    Summary of a completed onboarding seed operation.

    Attributes:
        user_id:       The user who was seeded.
        seeded_count:  Number of units actually written to the store.
                       Units the user had already progressed past the seed
                       state are excluded from this count.
        skipped_count: Units omitted because the existing state was already
                       at or above the target (no demotion).
        state:         The KnowledgeState units were set to.
        tier:          The LevelTier used, or None for custom-list seeding.
    """
    user_id: str
    seeded_count: int
    skipped_count: int
    state: KnowledgeState
    tier: Optional[LevelTier] = None


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class VocabularyOnboarding:
    """
    Seeds a new user's knowledge store from a frequency-ranked vocabulary tier
    or a custom lemma list.

    All write methods are non-destructive: units the user has already
    progressed past the target state are left untouched.

    Example usage::

        from onboarding import LevelTier, VocabularyOnboarding
        from user_knowledge import UserKnowledgeStore

        store = UserKnowledgeStore()
        onboarding = VocabularyOnboarding()

        # Option A: level-based (primary flow)
        result = onboarding.seed_from_level("user-42", LevelTier.A2, store)
        print(f"Seeded {result.seeded_count} words for {result.tier.value} learner.")

        # Option B: user marks additional items as known
        onboarding.mark_known("user-42", ["urlaub", "buchung", "reise"], store)

        # Option C: seed from a custom external list
        onboarding.seed_from_lemmas("user-42", my_frequency_list, store)
    """

    # ------------------------------------------------------------------
    # Primary API
    # ------------------------------------------------------------------

    def seed_from_level(
        self,
        user_id: str,
        tier: LevelTier,
        store: UserKnowledgeStore,
        state: KnowledgeState = KnowledgeState.KNOWN_PASSIVE,
    ) -> OnboardingResult:
        """
        Seed vocabulary for a user based on their self-reported level tier.

        Seeds all lemmas up to and including *tier* as *state*.  Lemmas the
        user has already reached or surpassed *state* for are left untouched.

        Args:
            user_id: Opaque user identifier.
            tier:    Self-reported proficiency level.
            store:   The UserKnowledgeStore to write to.
            state:   KnowledgeState to assign to seeded units.
                     Default KNOWN_PASSIVE: units will not appear as i+1
                     targets but can still be reinforced via exposure.

        Returns:
            OnboardingResult with counts of seeded and skipped units.
        """
        lemma_keys = self.get_tier_lemmas(tier)
        return self.seed_from_lemmas(user_id, lemma_keys, store, state=state, tier=tier)

    def seed_from_lemmas(
        self,
        user_id: str,
        lemma_keys: Iterable[str],
        store: UserKnowledgeStore,
        state: KnowledgeState = KnowledgeState.KNOWN_PASSIVE,
        tier: Optional[LevelTier] = None,
    ) -> OnboardingResult:
        """
        Seed an arbitrary collection of lemma keys as known for a user.

        Useful for integrating an external frequency list or for testing.
        Silently skips lemmas that are in _SKIP_LEMMAS (they can never be
        extraction targets) and units the user has already reached or
        surpassed *state* for.

        Args:
            user_id:    Opaque user identifier.
            lemma_keys: Iterable of lowercase lemma strings.
            store:      The UserKnowledgeStore to write to.
            state:      KnowledgeState to assign to newly-seeded units.
            tier:       Optional tier tag for the returned result summary.

        Returns:
            OnboardingResult with counts of seeded and skipped units.
        """
        to_seed: list[LearningUnit] = []
        skipped = 0

        for raw_key in lemma_keys:
            key = raw_key.strip().lower()
            if not key or key in _SKIP_LEMMAS:
                continue
            unit = _make_lemma_unit(key)
            current_state = store.get_state(user_id, unit)
            if current_state >= state:
                skipped += 1
            else:
                to_seed.append(unit)

        store.seed_known_units(user_id, to_seed, state=state)
        return OnboardingResult(
            user_id=user_id,
            seeded_count=len(to_seed),
            skipped_count=skipped,
            state=state,
            tier=tier,
        )

    def mark_known(
        self,
        user_id: str,
        lemma_keys: Iterable[str],
        store: UserKnowledgeStore,
    ) -> OnboardingResult:
        """
        Mark specific lemma keys as KNOWN_PASSIVE for a user.

        Intended for the "I already know these words" step of onboarding:
        show the user a sample word list and let them flag items above their
        seeded tier.  Sets the state to KNOWN_PASSIVE (same as the default
        seed state).

        Args:
            user_id:    Opaque user identifier.
            lemma_keys: Lemmas the user has indicated they already know.
            store:      The UserKnowledgeStore to write to.

        Returns:
            OnboardingResult summarising what was updated.
        """
        return self.seed_from_lemmas(
            user_id,
            lemma_keys,
            store,
            state=KnowledgeState.KNOWN_PASSIVE,
        )

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    @staticmethod
    def get_tier_lemmas(tier: LevelTier) -> frozenset[str]:
        """
        Return the cumulative lemma set for *tier*.

        Cumulative means: A2 returns all A1 lemmas plus A2 additions.
        COMPLETE_BEGINNER returns an empty frozenset.

        Args:
            tier: The target proficiency level.

        Returns:
            frozenset of lowercase lemma key strings.
        """
        result: set[str] = set()
        for t in _TIER_ORDER:
            result |= _TIER_DELTAS[t]
            if t == tier:
                break
        return frozenset(result)

    @staticmethod
    def tier_size(tier: LevelTier) -> int:
        """Return the number of lemmas in the cumulative set for *tier*."""
        return len(VocabularyOnboarding.get_tier_lemmas(tier))

    @staticmethod
    def sample_tier_lemmas(
        tier: LevelTier,
        n: int,
        above_tier: Optional[LevelTier] = None,
    ) -> list[str]:
        """
        Return up to *n* lemma keys from the delta above *above_tier*.

        Useful for building a "do you know these words?" word-check screen.
        For example, to show a sample of A2 words to an A1 user:

            sample = VocabularyOnboarding.sample_tier_lemmas(
                tier=LevelTier.A2, n=10, above_tier=LevelTier.A1
            )

        Args:
            tier:       The tier whose delta to sample from.
            n:          Maximum number of lemmas to return.
            above_tier: If given, return only lemmas in *tier*'s delta
                        (i.e. not already in *above_tier*).  If None,
                        samples from the full cumulative set.

        Returns:
            Sorted list of up to *n* lemma strings.
        """
        if above_tier is not None:
            pool = _TIER_DELTAS.get(tier, frozenset())
        else:
            pool = VocabularyOnboarding.get_tier_lemmas(tier)
        return sorted(pool)[:n]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _make_lemma_unit(key: str) -> LearningUnit:
    """
    Build a LearningUnit for a raw lemma key.

    Uses the key itself as the display_form.  When the user later sees this
    unit in a real utterance, the display_form from the live spaCy extraction
    will take precedence, so the cosmetic difference (capitalisation of nouns
    etc.) has no downstream effect.
    """
    return LearningUnit(
        unit_type=LearningUnitType.LEMMA,
        key=key,
        display_form=key,
    )


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

def _demo() -> None:
    from user_knowledge import UserKnowledgeStore, KnowledgeFilterPolicy, ExposurePolicy

    store = UserKnowledgeStore(
        filter_policy=KnowledgeFilterPolicy(min_known_state=KnowledgeState.KNOWN_PASSIVE),
        exposure_policy=ExposurePolicy(auto_advance=True, exposures_to_unlock=5),
    )
    onboarding = VocabularyOnboarding()
    USER = "demo_user"

    # ------------------------------------------------------------------
    # 1. Show tier sizes
    # ------------------------------------------------------------------
    print("─" * 60)
    print("  TIER SIZES  (cumulative)")
    print("─" * 60)
    for tier in _TIER_ORDER:
        size = onboarding.tier_size(tier)
        print(f"  {tier.value:<20} {size:>4} lemmas")

    # ------------------------------------------------------------------
    # 2. Seed from level
    # ------------------------------------------------------------------
    print("\n" + "─" * 60)
    print("  SEEDING LEVEL: A2")
    print("─" * 60)

    result = onboarding.seed_from_level(USER, LevelTier.A2, store)
    print(f"\n  Seeded : {result.seeded_count}")
    print(f"  Skipped: {result.skipped_count}  (already at or above {result.state.name})")
    summary = store.get_summary(USER)
    print(f"  Store summary: { {s.name: n for s, n in summary.items() if n > 0} }")

    # ------------------------------------------------------------------
    # 3. Re-seed: existing records must not be demoted
    # ------------------------------------------------------------------
    print("\n" + "─" * 60)
    print("  RE-SEED SAFETY CHECK")
    print("─" * 60)

    # Manually advance one unit to MASTERED
    from learning_units import LearningUnit, LearningUnitType
    mastered_unit = LearningUnit(LearningUnitType.LEMMA, "gehen", "gehen")
    store.set_state(USER, mastered_unit, KnowledgeState.MASTERED)
    print("\n  Set 'gehen' to MASTERED manually.")

    # Re-seed A2 — 'gehen' should stay MASTERED
    result2 = onboarding.seed_from_level(USER, LevelTier.A2, store)
    state_after = store.get_state(USER, mastered_unit)
    print(f"  After re-seed: 'gehen' state = {state_after.name}")
    print(f"  Re-seed skipped {result2.skipped_count} already-known unit(s).")

    # ------------------------------------------------------------------
    # 4. User marks additional known words
    # ------------------------------------------------------------------
    print("\n" + "─" * 60)
    print("  USER MARKS ADDITIONAL KNOWN WORDS")
    print("─" * 60)

    extra = ["urlaub", "buchung", "strandurlaub", "abflug"]
    result3 = onboarding.mark_known(USER, extra, store)
    print(f"\n  User-marked: {extra}")
    print(f"  Seeded: {result3.seeded_count}  Skipped: {result3.skipped_count}")

    # ------------------------------------------------------------------
    # 5. Sample word check for "do you already know these?"
    # ------------------------------------------------------------------
    print("\n" + "─" * 60)
    print("  WORD-CHECK SAMPLE  (A2 words shown to an A1 user)")
    print("─" * 60)

    sample = onboarding.sample_tier_lemmas(LevelTier.A2, n=10, above_tier=LevelTier.A1)
    print(f"\n  Sample: {sample}")

    # ------------------------------------------------------------------
    # 6. Final summary
    # ------------------------------------------------------------------
    print("\n" + "─" * 60)
    print("  FINAL KNOWLEDGE SUMMARY")
    print("─" * 60)

    final_summary = store.get_summary(USER)
    print()
    for state, count in final_summary.items():
        if count > 0:
            bar = "█" * min(count, 40)
            print(f"  {state.label():<22} {bar}  ({count})")


if __name__ == "__main__":
    _demo()
