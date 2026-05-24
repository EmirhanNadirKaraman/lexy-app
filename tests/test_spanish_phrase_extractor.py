"""
#36 — Spanish phrase extractor, first slice (2026-05-23).

`phrase_finder.extract_spanish_logic(doc)` is the first real Spanish phrase
extractor. It is deliberately narrow — two pattern families only:
  1. Reflexive verbs            "me lavo"      -> "lavarse"
  2. Verb + preposition (allowlist) "dependo de …" -> "depender de"
Clitic-attached infinitives, imperatives, subjunctive, idioms and MWEs are
deferred to later slices.

It returns extract_german_logic's dict shape (dictionary_entry /
sentence_phrase / logic / match_type / indices) so pipeline.insert_phrases
consumes it unchanged.

Tests live in the root tests/ tree (phrase_finder lives in subtitle-scraper/,
not the backend). Modules are loaded by explicit file path via importlib.util
— same pattern (and rationale) as tests/test_phrase_dispatcher.py: the repo
root also has a legacy `pipeline.py`, so we never rely on sys.path ordering.

The Spanish spaCy model (es_core_news_sm) is required; the whole module skips
if it isn't installed (`python -m spacy download es_core_news_sm`).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


_SCRAPER = Path(__file__).resolve().parent.parent / "subtitle-scraper"


def _load_by_path(name: str, file_path: Path):
    """Load `file_path` as a module under `name` without touching sys.path
    (re-using the cached module if already loaded this session)."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, file_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


pf = _load_by_path("phrase_finder", _SCRAPER / "phrase_finder.py")


def _load_scraper_pipeline():
    """Load subtitle-scraper/pipeline.py by file path. Snapshot/restore
    sys.path so the scraper's module-init insert doesn't leak across other
    tests (it pushes scraper ahead of root, breaking sibling-import order)."""
    if "subtitle_scraper_pipeline" in sys.modules:
        return sys.modules["subtitle_scraper_pipeline"]
    saved = list(sys.path)
    try:
        return _load_by_path("subtitle_scraper_pipeline", _SCRAPER / "pipeline.py")
    finally:
        sys.path[:] = saved


@pytest.fixture(scope="module")
def es_nlp():
    """Spanish spaCy pipeline. Skip the module if the model isn't installed."""
    spacy = pytest.importorskip("spacy")
    try:
        return spacy.load("es_core_news_sm")
    except OSError as exc:  # model not downloaded on this host
        pytest.skip(f"es_core_news_sm not installed: {exc}")


def _canonicals(es_nlp, sentence: str) -> list[str]:
    """extract_phrases(...,'es') canonical forms for a Spanish sentence."""
    doc = es_nlp(sentence)
    return [p["dictionary_entry"] for p in pf.extract_phrases(doc, "es")]


# ---------------------------------------------------------------------------
# Dispatcher: 'es' is no longer a no-op
# ---------------------------------------------------------------------------

def test_es_extractor_is_registered():
    assert pf._LANGUAGE_EXTRACTORS.get("es") is pf.extract_spanish_logic


def test_es_dispatch_no_longer_empty_for_reflexive(es_nlp):
    """The headline change: a Spanish reflexive sentence now yields a phrase
    (pre-#36 every 'es' call returned [])."""
    assert _canonicals(es_nlp, "Me llamo Ana.") != []


# ---------------------------------------------------------------------------
# Reflexive verbs
#
# NB: sentences are chosen so es_core_news_sm lemmatizes the verb correctly —
# the small model's lemmatizer is noisy (see test_reflexive_detection_is_robust
# _to_sm_lemma_noise below and the report). Detection of the reflexive itself
# is reliable; only the canonical infinitive depends on the lemmatizer.
# ---------------------------------------------------------------------------

def test_reflexive_me_lavo(es_nlp):
    # "lavo" lemmatizes to "lavar" in this fuller context (it mis-lemmatizes to
    # "lavo" in a bare two-word sentence — sm noise).
    assert "lavarse" in _canonicals(es_nlp, "Cada día me lavo la cara.")


def test_reflexive_te_levantas(es_nlp):
    assert "levantarse" in _canonicals(es_nlp, "Te levantas temprano.")


def test_reflexive_se_third_person(es_nlp):
    """A 3rd-person 'se' reflexive that the sm model lemmatizes cleanly."""
    assert "levantarse" in _canonicals(es_nlp, "Ella se levanta muy temprano.")


def test_reflexive_nos_acostamos(es_nlp):
    assert "acostarse" in _canonicals(es_nlp, "Nos acostamos tarde anoche.")


def test_reflexive_me_llamo(es_nlp):
    assert "llamarse" in _canonicals(es_nlp, "Me llamo Ana.")


def test_reflexive_detection_is_robust_to_sm_lemma_noise(es_nlp):
    """Documents a known limitation: es_core_news_sm mis-lemmatizes some verbs
    ('ducha' -> 'duchaber'), so the canonical is imperfect — but the reflexive
    is still DETECTED (an 'es_reflexive' phrase is emitted). Production quality
    here is bounded by the lemmatizer; see the report's known-limitations note."""
    doc = es_nlp("Se ducha por la mañana.")
    phrases = pf.extract_phrases(doc, "es")
    assert any(p["match_type"] == "es_reflexive" for p in phrases), (
        "reflexive 'se ducha' should be detected even if its canonical lemma is noisy"
    )


def test_non_reflexive_object_clitic_excluded(es_nlp):
    """Person/number agreement rejects a non-reflexive object clitic:
    'me ve' = 'sees me' (ve is 3rd person, clitic me is 1st) -> NOT 'verse'."""
    assert "verse" not in _canonicals(es_nlp, "Él me ve.")


# ---------------------------------------------------------------------------
# Verb + preposition (allowlist)
# ---------------------------------------------------------------------------

def test_verbprep_depender_de(es_nlp):
    assert "depender de" in _canonicals(es_nlp, "Dependo de mis padres.")


def test_verbprep_pensar_en(es_nlp):
    assert "pensar en" in _canonicals(es_nlp, "Pienso en ti.")


def test_verbprep_hablar_de(es_nlp):
    assert "hablar de" in _canonicals(es_nlp, "Hablamos de la película.")


def test_verbprep_not_in_allowlist_excluded(es_nlp):
    """A verb+prep pair NOT on the allowlist must not be emitted — e.g.
    'vivir en' is a real collocation but deliberately not listed in v1."""
    assert "vivir en" not in _canonicals(es_nlp, "Vivo en Madrid.")


# ---------------------------------------------------------------------------
# Dedup + output-shape compatibility
# ---------------------------------------------------------------------------

def test_duplicates_in_one_sentence_are_deduped(es_nlp):
    doc = es_nlp("Me lavo y me lavo otra vez.")
    phrases = pf.extract_phrases(doc, "es")
    lavarse = [p for p in phrases if p["dictionary_entry"] == "lavarse"]
    assert len(lavarse) == 1, f"expected one 'lavarse', got {len(lavarse)}"


def test_output_dict_shape_matches_german_contract(es_nlp):
    """Each Spanish phrase dict must carry exactly the keys insert_phrases
    reads, with compatible types — no German-only extras, none missing."""
    doc = es_nlp("Dependo de mis padres.")
    phrases = pf.extract_phrases(doc, "es")
    assert phrases
    required = {"dictionary_entry", "sentence_phrase", "logic", "match_type", "indices"}
    for p in phrases:
        assert set(p.keys()) == required, f"unexpected keys: {set(p.keys())}"
        assert isinstance(p["dictionary_entry"], str)
        assert isinstance(p["sentence_phrase"], list)
        assert all(isinstance(t, str) for t in p["sentence_phrase"])
        assert isinstance(p["logic"], str)
        assert isinstance(p["match_type"], str)
        assert isinstance(p["indices"], list)
        assert all(isinstance(i, int) for i in p["indices"])


# ---------------------------------------------------------------------------
# Other languages unaffected
# ---------------------------------------------------------------------------

def test_fr_still_returns_empty(es_nlp):
    """French has no extractor — dispatch still returns [] (using any doc)."""
    assert pf.extract_phrases(es_nlp("Je m'appelle Ana."), "fr") == []


def test_de_dispatch_still_equals_extract_german_logic():
    """German path unchanged: extract_phrases(doc,'de') == extract_german_logic.
    Uses phrase_finder's eagerly-loaded German model (no extra install)."""
    de_doc = pf.nlp("Ich lade meine Freunde zum Essen ein.")
    assert pf.extract_phrases(de_doc, "de") == pf.extract_german_logic(de_doc)


# ---------------------------------------------------------------------------
# Pipeline compatibility — insert_phrases consumes Spanish phrases, no crash
# ---------------------------------------------------------------------------

class _FakeCursor:
    """Minimal psycopg2-cursor stand-in. mogrify() returns bytes so the
    `b",".join(cursor.mogrify(...))` concatenation in insert_phrases works;
    the blueprint-id SELECT is answered from its own params."""

    def __init__(self):
        self.executed: list[str] = []
        self._fetch: list[tuple] = []

    def mogrify(self, template, args):
        t = template.encode() if isinstance(template, str) else template
        return t + b" :: " + repr(tuple(args)).encode()

    def execute(self, sql, params=None):
        s = sql.decode() if isinstance(sql, (bytes, bytearray)) else sql
        self.executed.append(s)
        if "SELECT lookup_key, blueprint_id" in s and params:
            # Resolve every requested lookup_key to a synthetic id.
            self._fetch = [(bp, i) for i, bp in enumerate(params, start=1)]

    def fetchall(self):
        return self._fetch


def test_insert_phrases_writes_spanish_rows_without_crashing(es_nlp):
    """insert_phrases(..., 'es') on a Spanish doc with a first-slice phrase
    must attempt the phrase_blueprint + sentence_to_phrase INSERTs and not
    raise. (Pre-#36 Spanish was a no-op; this proves the full extract ->
    insert path is wired.)"""
    scraper = _load_scraper_pipeline()
    doc = es_nlp("Me llamo Ana.")
    # Sanity: the doc actually produces a phrase, else the test is vacuous.
    assert pf.extract_phrases(doc, "es")

    cur = _FakeCursor()
    scraper.insert_phrases(cur, [1], [doc], language="es")

    assert any("phrase_blueprint" in s for s in cur.executed), (
        "phrase_blueprint INSERT should fire for a Spanish phrase"
    )
    assert any("sentence_to_phrase" in s for s in cur.executed), (
        "sentence_to_phrase INSERT should fire for a Spanish phrase"
    )


# ---------------------------------------------------------------------------
# #39 — lemma overrides patch bad spaCy lemmas
# ---------------------------------------------------------------------------

def test_override_fixes_bad_reflexive_canonical(es_nlp):
    """Headline #39 assertion: spaCy lemmatizes 'ducha' -> 'duchaber', so the
    raw canonical is wrong (`duchaberse`); an override {duchaber: duchar} makes
    it the correct `ducharse`."""
    doc = es_nlp("Se ducha por la mañana.")
    # Without override the broken lemma yields a wrong canonical (not 'ducharse').
    assert "ducharse" not in _canonicals(es_nlp, "Se ducha por la mañana.")
    # With the override applied, the canonical is corrected.
    fixed = [p["dictionary_entry"]
             for p in pf.extract_phrases(doc, "es", {"duchaber": "duchar"})]
    assert "ducharse" in fixed


def test_override_does_not_touch_unrelated_lemmas(es_nlp):
    """An irrelevant override must not change a correctly-lemmatized verb."""
    doc = es_nlp("Te levantas temprano.")
    out = [p["dictionary_entry"]
           for p in pf.extract_phrases(doc, "es", {"duchaber": "duchar"})]
    assert "levantarse" in out


def test_override_none_is_pre_39_behaviour(es_nlp):
    """No overrides == trust spaCy — back-compat for callers that don't load
    the table (e.g. the backend matcher today)."""
    doc = es_nlp("Te levantas temprano.")
    assert pf.extract_phrases(doc, "es") == pf.extract_phrases(doc, "es", None)


def test_load_lemma_overrides_reads_active_context_free_rows():
    """pipeline.load_lemma_overrides → {observed: corrected} for active,
    context-free rows; queries the right language with the right filters."""
    scraper = _load_scraper_pipeline()

    class _Cur:
        def __init__(self):
            self.query = None
            self.params = None

        def execute(self, query, params=None):
            self.query, self.params = query, params

        def fetchall(self):
            return [("duchaber", "duchar"), ("duchir", "duchar")]

    cur = _Cur()
    out = scraper.load_lemma_overrides(cur, "es")
    assert out == {"duchaber": "duchar", "duchir": "duchar"}
    assert cur.params == ("es",)
    assert "status = 'active'" in cur.query
    assert "surface_form IS NULL" in cur.query


def test_insert_phrases_applies_overrides_to_canonical(es_nlp):
    """End-to-end through the scraper insert path: with an override loaded, the
    phrase_blueprint INSERT carries the corrected canonical 'ducharse', never
    the broken 'duchaberse'."""
    scraper = _load_scraper_pipeline()
    doc = es_nlp("Se ducha por la mañana.")
    cur = _FakeCursor()
    scraper.insert_phrases(cur, [1], [doc], language="es",
                           overrides={"duchaber": "duchar"})
    joined = " ".join(cur.executed)
    assert "ducharse" in joined, "corrected canonical should reach the INSERT"
    assert "duchaberse" not in joined, "the broken canonical must not be written"


# ---------------------------------------------------------------------------
# Slice 2 — clitic-attached reflexive infinitives ("quiero lavarme" -> lavarse)
# ---------------------------------------------------------------------------

def test_clitic_infinitive_lavarme(es_nlp):
    assert "lavarse" in _canonicals(es_nlp, "Quiero lavarme.")


def test_clitic_infinitive_levantarme(es_nlp):
    assert "levantarse" in _canonicals(es_nlp, "Voy a levantarme temprano.")


def test_clitic_infinitive_ducharme(es_nlp):
    # The base infinitive lemmatizes correctly here ('duchar'), unlike the finite
    # 'ducha'->'duchaber' — so this path needs no override.
    assert "ducharse" in _canonicals(es_nlp, "Necesito ducharme.")


def test_clitic_infinitive_acostarme(es_nlp):
    assert "acostarse" in _canonicals(es_nlp, "Puedo acostarme tarde.")


def test_non_reflexive_infinitive_not_marked_reflexive(es_nlp):
    """A plain infinitive with no enclitic ('comer') must NOT become reflexive.
    Guards the false-positive surface of the clitic-stripping rule."""
    out = _canonicals(es_nlp, "Quiero comer.")
    assert "comerse" not in out
    assert all(not c.endswith("se") for c in out)


# ---------------------------------------------------------------------------
# Slice 2 — broadened verb+preposition allowlist
# ---------------------------------------------------------------------------

def test_verbprep_confiar_en(es_nlp):
    assert "confiar en" in _canonicals(es_nlp, "Confío en ti.")


def test_verbprep_consistir_en(es_nlp):
    # 'en' attaches as a `mark` on the infinitive complement — exercises the
    # widened prep-attachment search added in slice 2.
    assert "consistir en" in _canonicals(es_nlp, "Consiste en practicar.")


def test_verbprep_creer_en(es_nlp):
    assert "creer en" in _canonicals(es_nlp, "Creo en ti.")


def test_verbprep_jugar_a(es_nlp):
    assert "jugar a" in _canonicals(es_nlp, "Juego a fútbol.")


def test_verbprep_salir_de(es_nlp):
    assert "salir de" in _canonicals(es_nlp, "Salimos de la casa.")


def test_verbprep_llegar_a(es_nlp):
    assert "llegar a" in _canonicals(es_nlp, "Llega a Madrid mañana.")


# ---------------------------------------------------------------------------
# Slice 2 — deferred patterns (regression guards)
# ---------------------------------------------------------------------------

def test_imperative_reflexive_currently_unsupported(es_nlp):
    """es_core_news_sm tags 'Lávate.' as a non-verb, so reflexive imperatives
    extract nothing today. Deferred; this pins the documented state so a future
    change that starts extracting them flags the slice for review."""
    assert _canonicals(es_nlp, "Lávate.") == []


def test_reflexive_prep_combo_emits_combo(es_nlp):
    """'Me acuerdo de ti.' now yields the reflexive+preposition collocation
    'acordarse de' (the precise learning unit). The bare reflexive 'acordarse'
    is suppressed for this token, and 'acordar de' must NEVER be emitted —
    'acordar' is intentionally absent from the verb+prep allowlist."""
    out = _canonicals(es_nlp, "Me acuerdo de ti.")
    assert "acordarse de" in out
    assert "acordarse" not in out      # bare suppressed in the combo sentence
    assert "acordar de" not in out     # wrong (non-reflexive) canonical, never emitted


@pytest.mark.parametrize("sentence, combo, bare", [
    ("Me acuerdo de mi abuela.",  "acordarse de",   "acordarse"),
    ("Se enamora de ella.",       "enamorarse de",  "enamorarse"),
    ("Se queja de todo.",         "quejarse de",    "quejarse"),
    ("Se preocupa por su madre.", "preocuparse por", "preocuparse"),
    ("Me olvido de las llaves.",  "olvidarse de",   "olvidarse"),
])
def test_reflexive_prep_combos_emit_reflexive_canonical(es_nlp, sentence, combo, bare):
    """Each finite reflexive+prep verb yields its reflexive collocation, not the
    bare reflexive (suppressed here) nor the non-reflexive verb+prep form."""
    out = _canonicals(es_nlp, sentence)
    assert combo in out
    assert bare not in out                              # bare suppressed in the combo sentence
    assert combo.replace("se ", " ", 1) not in out     # e.g. "acordar de" never emitted


def test_reflexive_without_prep_still_emits_bare(es_nlp):
    """Suppression is per-token: a reflexive+prep verb used WITHOUT its
    preposition still yields the bare reflexive (the combo only wins when the
    prep is actually present)."""
    out = _canonicals(es_nlp, "Se queja constantemente.")
    assert "quejarse" in out
    assert "quejarse de" not in out


def test_reflexive_prep_combo_match_type(es_nlp):
    """The combo carries the new 'es_reflexive_prep' match_type."""
    phrases = pf.extract_phrases(es_nlp("Me acuerdo de mi abuela."), "es")
    combo = [p for p in phrases if p["dictionary_entry"] == "acordarse de"]
    assert combo and combo[0]["match_type"] == "es_reflexive_prep"
