# NB: This module's library functions emit no output. The 13 print() calls
# below all live in main() and report results to stdout when the file is run
# as a script (`python phrase_finder.py`) — stdout IS the product for that
# CLI demo, intentionally left as print rather than logging.

import spacy
from functools import lru_cache
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Load German model
nlp = spacy.load("de_core_news_sm")

def load_verb_dictionary(file_path):
    """
    Loads the CSV dictionary where:
    Key: base verb (e.g., 'geben' or 'einladen')
    Value: The full blueprint (e.g., 'geben jdm. etw.')

    When duplicate keys exist, keeps the longer/more detailed value.
    """

    word_map = dict()

    with open(file_path, 'r', encoding='utf-8') as f:
        for line in f.readlines():
            key, value = line.split('\t')
            value = value.strip()  # Remove trailing newlines

            # If key already exists, keep the longer value (more detailed pattern)
            if key in word_map:
                if len(value) > len(word_map[key]):
                    word_map[key] = value
            else:
                word_map[key] = value

    return word_map


_ARTICLES = {'der', 'die', 'das', 'den', 'dem', 'des', 'ein', 'eine', 'einen', 'einem', 'einer', 'eines'}

verb_blueprint_map = load_verb_dictionary(_DATA_DIR / "final_result.txt")

def generate_trigrams(word):
    """
    Generate trigrams from a word.
    Adds padding at the beginning and end to capture edge trigrams.
    """
    if len(word) < 3:
        return set([word])

    padded_word = f"  {word}  "
    trigrams = set()
    for i in range(len(padded_word) - 2):
        trigrams.add(padded_word[i:i+3])
    return trigrams

def _build_trigram_index(dictionary_map):
    """
    Build a trigram inverted index from the dictionary at load time.
    Maps trigram -> list of dict keys whose base word contains that trigram.
    Also pre-computes trigrams for each dict key to avoid regenerating them during lookup.
    Space is O(dict_size * avg_trigrams_per_word) — fixed, never grows with the DB.
    Returns (index, precomputed_trigrams).
    """
    index = {}
    precomputed = {}
    for dict_word in dictionary_map:
        words = dict_word.split()
        if len(words) > 1:
            base_word = words[1] if words[0].lower() in _ARTICLES else words[0]
        else:
            base_word = dict_word
        tgs = frozenset(generate_trigrams(base_word.lower()))
        precomputed[dict_word] = tgs
        for tg in tgs:
            if tg not in index:
                index[tg] = []
            index[tg].append(dict_word)
    return index, precomputed

_trigram_index, _precomputed_trigrams = _build_trigram_index(verb_blueprint_map)

def trigram_similarity(word1, word2):
    """
    Calculate similarity between two words using trigram overlap (Dice coefficient).
    Returns a value between 0 and 1, where 1 is identical.
    """
    trigrams1 = generate_trigrams(word1.lower())
    trigrams2 = generate_trigrams(word2.lower())

    if not trigrams1 or not trigrams2:
        return 0.0

    intersection = len(trigrams1 & trigrams2)
    dice = (2.0 * intersection) / (len(trigrams1) + len(trigrams2))
    return dice

@lru_cache(maxsize=4096)
def find_best_match(target_word, threshold=0.6):
    """
    Find the best matching word from the dictionary using trigram similarity.
    Uses a pre-built inverted index to check only candidate keys that share at
    least one trigram with the target, instead of scanning the full dictionary.
    Results are cached via lru_cache to avoid redundant lookups.
    Returns (best_match, similarity_score) or (None, 0.0) if no match above threshold.
    """
    target_trigrams = generate_trigrams(target_word.lower())

    # Collect only candidates that share at least one trigram
    candidates = set()
    for tg in target_trigrams:
        for dict_word in _trigram_index.get(tg, []):
            candidates.add(dict_word)

    best_match = None
    best_score = 0.0

    for dict_word in candidates:
        # Use pre-computed trigrams instead of regenerating
        dict_trigrams = _precomputed_trigrams[dict_word]
        if not dict_trigrams:
            continue
        intersection = len(target_trigrams & dict_trigrams)
        score = (2.0 * intersection) / (len(target_trigrams) + len(dict_trigrams))

        if score > best_score:
            best_score = score
            best_match = dict_word

    if best_score >= threshold:
        return best_match, best_score
    return None, 0.0

def get_object_token(child):
    """Maps spaCy dependency labels to jdn./jdm./etw. tokens."""
    # Check if person vs thing
    is_person = child.pos_ in ["PRON", "PROPN"] or child.ent_type_ == "PER"
    
    # da = dative object, oa = accusative object
    if child.dep_ == "da":
        return "jdm." if is_person else "etw."
    if child.dep_ in ["oa", "obj"]:
        return "jdn." if is_person else "etw."
    return "etw."

def extract_german_logic(doc, overrides=None):
    """
    Extract phrases from a pre-computed spaCy doc.
    Pass a doc (from nlp.pipe or nlp(text)) instead of raw text to avoid redundant NLP.

    `overrides` (#39) is accepted for a uniform extractor signature but NOT applied
    here in slice 1 — no German lemma overrides are seeded yet. German behaviour is
    byte-identical to the pre-#39 call. (German lemma overrides can be wired the same
    way as Spanish if a need shows up.)
    """
    _ = overrides  # reserved; see docstring
    result = []
    consumed = set()

    for token in doc:
        if token.i in consumed:
            continue

        # --- VERB MAPPING LOGIC ---
        if token.pos_ in ["VERB", "AUX"] and token.dep_ != "aux":
            indices = [token.i]
            verb_lemma = token.lemma_.lower()
            
            # Handle Separable Prefixes (e.g., 'ein' in 'einladen')
            prefix = ""
            for child in token.children:
                if child.dep_ == "svp":
                    prefix = child.lemma_.lower()
                    indices.append(child.i)
                    consumed.add(child.i)
            
            full_verb = prefix + verb_lemma if prefix else verb_lemma
            
            # Build the "Live" Blueprint from the sentence
            components = [full_verb]
            for child in token.children:
                # Detect reflexive (sich)
                if child.dep_ == "expl:pv" or (child.lemma_.lower() == "sich"):
                    components.append("sich")
                    indices.append(child.i)
                    consumed.add(child.i)
                
                # Detect Objects (jdn/jdm/etw)
                if child.dep_ in ["oa", "da", "obj"]:
                    components.append(get_object_token(child))
                    indices.append(child.i)
                    consumed.add(child.i)

                    # Also include articles and modifiers of the object
                    for grandchild in child.children:
                        if grandchild.dep_ in ["det", "poss", "amod", "nk"]:
                            indices.append(grandchild.i)
                            consumed.add(grandchild.i)
                
                # Detect Prepositions
                if child.pos_ == "ADP":
                    prep = child.lemma_.lower()
                    indices.append(child.i)
                    consumed.add(child.i)
                    # Find the noun inside the prep phrase
                    obj_in_prep = "etw."
                    for grand in child.children:
                        if grand.dep_ in ["nk", "pobj"]:
                            indices.append(grand.i)
                            consumed.add(grand.i)
                            if grand.pos_ in ["PRON", "PROPN"]:
                                obj_in_prep = "jdm."

                            # Also include articles and modifiers of the prepositional object
                            for great_grand in grand.children:
                                if great_grand.dep_ in ["det", "poss", "amod", "nk"]:
                                    indices.append(great_grand.i)
                                    consumed.add(great_grand.i)
                    components.append(f"{prep} {obj_in_prep}")

            # Dictionary Lookup with Trigram Matching
            # First try exact match
            blueprint = verb_blueprint_map.get(full_verb)
            match_info = "exact"

            # If not found, try trigram similarity matching
            if blueprint is None:
                best_match, similarity = find_best_match(full_verb)
                if best_match:
                    blueprint = verb_blueprint_map[best_match]
                    match_info = f"fuzzy ({similarity:.2f}): {best_match}"
                else:
                    blueprint = " ".join(components)
                    match_info = "constructed"

            indices.sort()
            result.append({
                "dictionary_entry": blueprint,
                "sentence_phrase": [doc[i].text for i in indices],
                "logic": " -> ".join(components),
                "match_type": match_info,
                "indices": indices
            })

        # --- NOUN/OTHER COLLECTION ---
        elif token.pos_ == "NOUN":
            indices = [token.i]
            noun_lemma = token.lemma_.lower()

            for child in token.children:
                if child.dep_ in ["det", "poss", "amod", "nk"]:
                    indices.append(child.i)
                    consumed.add(child.i)

            # Dictionary Lookup with Trigram Matching for Nouns
            # First try exact match (without article)
            dictionary_entry = verb_blueprint_map.get(noun_lemma)
            match_info = "exact"

            # If not found, try with common articles
            if dictionary_entry is None:
                for article in ['der', 'die', 'das']:
                    article_form = f"{article} {noun_lemma}"
                    if article_form in verb_blueprint_map:
                        dictionary_entry = verb_blueprint_map[article_form]
                        match_info = "exact (with article)"
                        break

            # If still not found, try trigram similarity matching
            if dictionary_entry is None:
                best_match, similarity = find_best_match(noun_lemma)
                if best_match:
                    dictionary_entry = verb_blueprint_map[best_match]
                    match_info = f"fuzzy ({similarity:.2f}): {best_match}"
                else:
                    dictionary_entry = noun_lemma
                    match_info = "lemma"

            indices.sort()
            result.append({
                "dictionary_entry": dictionary_entry,
                "sentence_phrase": [doc[i].text for i in indices],
                "logic": "NOUN",
                "match_type": match_info,
                "indices": indices
            })

        # --- PRONOUN COLLECTION ---
        elif token.pos_ == "PRON":
            indices = [token.i]
            pronoun_lemma = token.lemma_.lower()

            # Dictionary Lookup with Trigram Matching for Pronouns
            dictionary_entry = verb_blueprint_map.get(pronoun_lemma)
            match_info = "exact"

            if dictionary_entry is None:
                best_match, similarity = find_best_match(pronoun_lemma)
                if best_match:
                    dictionary_entry = verb_blueprint_map[best_match]
                    match_info = f"fuzzy ({similarity:.2f}): {best_match}"
                else:
                    dictionary_entry = pronoun_lemma
                    match_info = "lemma"

            result.append({
                "dictionary_entry": dictionary_entry,
                "sentence_phrase": [doc[i].text for i in indices],
                "logic": "PRON",
                "match_type": match_info,
                "indices": indices
            })

        # --- ADVERB COLLECTION ---
        elif token.pos_ == "ADV":
            indices = [token.i]
            adverb_lemma = token.lemma_.lower()

            # Dictionary Lookup with Trigram Matching for Adverbs
            dictionary_entry = verb_blueprint_map.get(adverb_lemma)
            match_info = "exact"

            if dictionary_entry is None:
                best_match, similarity = find_best_match(adverb_lemma)
                if best_match:
                    dictionary_entry = verb_blueprint_map[best_match]
                    match_info = f"fuzzy ({similarity:.2f}): {best_match}"
                else:
                    dictionary_entry = adverb_lemma
                    match_info = "lemma"

            result.append({
                "dictionary_entry": dictionary_entry,
                "sentence_phrase": [doc[i].text for i in indices],
                "logic": "ADV",
                "match_type": match_info,
                "indices": indices
            })

        # --- ADJECTIVE COLLECTION ---
        elif token.pos_ == "ADJ":
            indices = [token.i]
            adj_lemma = token.lemma_.lower()

            # Dictionary Lookup with Trigram Matching for Adjectives
            dictionary_entry = verb_blueprint_map.get(adj_lemma)
            match_info = "exact"

            if dictionary_entry is None:
                best_match, similarity = find_best_match(adj_lemma)
                if best_match:
                    dictionary_entry = verb_blueprint_map[best_match]
                    match_info = f"fuzzy ({similarity:.2f}): {best_match}"
                else:
                    dictionary_entry = adj_lemma
                    match_info = "lemma"

            result.append({
                "dictionary_entry": dictionary_entry,
                "sentence_phrase": [doc[i].text for i in indices],
                "logic": "ADJ",
                "match_type": match_info,
                "indices": indices
            })

        # --- CATCH-ALL FOR OTHER WORD TYPES ---
        else:
            # Skip punctuation and whitespace
            if token.pos_ in ["PUNCT", "SPACE"]:
                continue

            indices = [token.i]
            word_lemma = token.lemma_.lower()

            # Dictionary Lookup with Trigram Matching
            dictionary_entry = verb_blueprint_map.get(word_lemma)
            match_info = "exact"

            if dictionary_entry is None:
                best_match, similarity = find_best_match(word_lemma)
                if best_match:
                    dictionary_entry = verb_blueprint_map[best_match]
                    match_info = f"fuzzy ({similarity:.2f}): {best_match}"
                else:
                    dictionary_entry = word_lemma
                    match_info = "lemma"

            result.append({
                "dictionary_entry": dictionary_entry,
                "sentence_phrase": [doc[i].text for i in indices],
                "logic": token.pos_,
                "match_type": match_info,
                "indices": indices
            })

    return result


# ---------------------------------------------------------------------------
# Spanish phrase extractor — slice 1 (#36, 2026-05-23) + slice 2 (2026-05-24)
# ---------------------------------------------------------------------------
#
# Conservative, pattern-based Spanish phrase extraction. Three families:
#   1. Finite reflexive verbs          "me lavo"        -> "lavarse"
#   2. Verb + preposition (allowlist)  "dependo de …"   -> "depender de"
#   3. Clitic-attached infinitives     "quiero lavarme" -> "lavarse"  (slice 2)
# Slice 2 also broadened the verb+prep allowlist and the prep-attachment search
# (now matches `mark` deps, e.g. "consiste en practicar").
#
# Reflexive+preposition combos ("acordarse de") are handled via `_ES_REFLEXIVE_PREP`
# for BOTH finite forms (block 1a) and clitic-attached infinitives (block 3a,
# "quiero acordarme de…"). Still deferred (model-bound or needs a new pattern):
# imperatives ("lávate" — es_core_news_sm doesn't tag it as a verb), subjunctive,
# idioms, MWEs.
#
# Output shape is IDENTICAL to extract_german_logic so pipeline.insert_phrases
# consumes it unchanged: each dict carries dictionary_entry / sentence_phrase /
# logic / match_type / indices. Spanish has no blueprint dictionary (that's
# German-only, loaded from final_result.txt), so the canonical form is
# CONSTRUCTED from the sentence — `dictionary_entry` doubles as the
# phrase_blueprint lookup_key and is deduped downstream via ON CONFLICT.

# Reflexive clitic -> (verb Person, allowed verb Numbers | None=any). Requiring
# person/number agreement between the clitic and its finite verb rejects
# non-reflexive object clitics — e.g. "me ve" ("sees me"): ve is 3rd person,
# the clitic "me" is 1st -> no match.
_ES_REFLEXIVE_CLITICS = {
    "me":  ("1", {"Sing"}),
    "te":  ("2", {"Sing"}),
    "se":  ("3", None),
    "nos": ("1", {"Plur"}),
    "os":  ("2", {"Plur"}),
}

# Allowlisted NON-reflexive verb+preposition collocations (verb lemma,
# preposition). Kept conservative on purpose — only emit for a known pair, never
# guess. Reflexive+preposition collocations ("acordarse de", "enamorarse de",
# "convertirse en", "quejarse de") are a DIFFERENT pattern that must yield a
# reflexive canonical ("acordarse de", not "acordar de"); do NOT add those here
# (a finite "me acuerdo de ti" would emit the wrong "acordar de"). They get
# their own set + canonical builder in a later slice (see TODO #36).
_ES_VERB_PREP = {
    # slice 1
    ("depender", "de"), ("pensar", "en"), ("hablar", "de"), ("soñar", "con"),
    ("esperar", "a"), ("tratar", "de"), ("ayudar", "a"), ("aprender", "a"),
    ("empezar", "a"), ("acabar", "de"),
    # slice 2 (each has a test in tests/test_spanish_phrase_extractor.py)
    ("confiar", "en"), ("consistir", "en"), ("creer", "en"),
    ("jugar", "a"), ("salir", "de"), ("llegar", "a"),
}

# Allowlisted REFLEXIVE verb+preposition collocations (bare verb lemma, prep).
# These are deliberately SEPARATE from `_ES_VERB_PREP` above and must NOT be
# merged into it: the canonical is the *reflexive* form ("acordarse de"), built
# as `f"{lemma}se {prep}"`, not "acordar de". They fire only when the finite
# verb also carries an agreeing reflexive clitic (see extract_spanish_logic
# block 1); a matching combo SUPPRESSES the bare reflexive ("acordarse") for
# that token, since the collocation is the real learning unit. (The bare
# reflexive still surfaces from prep-less occurrences elsewhere — phrase_table
# dedups across the corpus.)
_ES_REFLEXIVE_PREP = {
    ("acordar", "de"),    # acordarse de
    ("enamorar", "de"),   # enamorarse de
    ("quejar", "de"),     # quejarse de
    ("preocupar", "por"), # preocuparse por
    ("olvidar", "de"),    # olvidarse de
}


def _es_clitic_agrees(verb, person, numbers):
    """True if finite `verb`'s morphology agrees with a reflexive clitic of the
    given person/number. Permissive when the model didn't tag Person (rare) so
    valid reflexives on under-analysed tokens aren't silently dropped."""
    vp = verb.morph.get("Person")
    if not vp:
        return True
    if person not in vp:
        return False
    if numbers is None:
        return True
    vn = verb.morph.get("Number")
    if not vn:
        return True
    return any(n in numbers for n in vn)


def _es_prep_candidates(verb):
    """Prepositions syntactically attached to `verb`: direct ADP children, plus
    ADP markers heading the verb's oblique/object/clausal complements. The
    grandchild dep is `case` for noun objects ('dependo de mis padres' — 'de'
    cases the obl noun 'padres') and `mark` for infinitive complements
    ('consiste en practicar' — 'en' marks the xcomp verb 'practicar'). Emission
    is still gated by the allowlist, so widening the dep set only finds more
    candidate preps, never invents matches."""
    out = []
    for child in verb.children:
        if child.pos_ == "ADP":
            out.append((child.lemma_.lower(), child.i))
        else:
            for grand in child.children:
                if grand.pos_ == "ADP" and grand.dep_ in ("case", "mark"):
                    out.append((grand.lemma_.lower(), grand.i))
    return out


def extract_spanish_logic(doc, overrides=None):
    """Spanish phrase extractor (finite reflexives + reflexive+preposition combos
    + allowlisted verb+preposition + clitic-attached reflexive infinitives).
    Returns extract_german_logic's dict shape. `doc` must be a Spanish spaCy Doc;
    the caller owns model selection.

    `overrides` (#39) is an optional {observed_lemma: corrected_lemma} map that
    patches spaCy lemmatizer errors (e.g. `duchaber`→`duchar`) before the
    canonical is built. Applied at the single point where the verb lemma is
    read, so both the reflexive (`lemma + "se"`) and verb+prep canonicals use
    the corrected lemma. `None`/empty trusts spaCy."""
    overrides = overrides or {}
    result = []
    # Dedup canonicals within this doc. NB: the pipeline passes one sentence per
    # doc, so this is effectively per-sentence; a multi-sentence doc would dedup
    # across sentences too (acceptable for v1).
    seen = set()

    def _emit(canonical, indices, match_type, logic):
        if canonical in seen:
            return
        seen.add(canonical)
        idx = sorted(set(indices))
        result.append({
            "dictionary_entry": canonical,
            "sentence_phrase": [doc[i].text for i in idx],
            "logic": logic,
            "match_type": match_type,
            "indices": idx,
        })

    for token in doc:
        if token.pos_ != "VERB":
            continue
        verb_lemma = token.lemma_.lower()
        # #39: patch known spaCy lemmatizer errors before building any canonical.
        verb_lemma = overrides.get(verb_lemma, verb_lemma)

        # 1. Reflexive verbs: finite verb + an agreeing reflexive clitic child.
        reflexive_child = None
        for child in token.children:
            if child.pos_ != "PRON":
                continue
            spec = _ES_REFLEXIVE_CLITICS.get(child.text.lower())
            if spec is None:
                continue
            person, numbers = spec
            if _es_clitic_agrees(token, person, numbers):
                reflexive_child = child
                break  # at most one reflexive clitic per finite verb
        if reflexive_child is not None:
            # lemma is the bare infinitive ("lavar"); guard the rare case where
            # the model already returns the reflexive lemma.
            base = verb_lemma if verb_lemma.endswith("se") else f"{verb_lemma}se"
            # 1a. Reflexive + preposition combo ("acordarse de") — the precise
            #     collocation. When it matches, it SUPPRESSES the bare reflexive
            #     for THIS token (bare still surfaces from prep-less occurrences
            #     elsewhere; phrase_table dedups across the corpus).
            combo = False
            for prep_lemma, prep_i in _es_prep_candidates(token):
                if (verb_lemma, prep_lemma) in _ES_REFLEXIVE_PREP:
                    combo = True
                    _emit(f"{base} {prep_lemma}",
                          [token.i, reflexive_child.i, prep_i],
                          "es_reflexive_prep",
                          f"{verb_lemma} + {reflexive_child.text.lower()} + {prep_lemma} (reflexive+prep)")
                    break
            if not combo:
                _emit(base, [token.i, reflexive_child.i], "es_reflexive",
                      f"{verb_lemma} + {reflexive_child.text.lower()} (reflexive)")

        # 2. Verb + preposition from the allowlist.
        for prep_lemma, prep_i in _es_prep_candidates(token):
            if (verb_lemma, prep_lemma) in _ES_VERB_PREP:
                _emit(f"{verb_lemma} {prep_lemma}", [token.i, prep_i],
                      "es_verb_prep", f"{verb_lemma} -> {prep_lemma}")

        # 3. Clitic-attached reflexive infinitive ("quiero lavarme" -> "lavarse"),
        #    plus reflexive+preposition on that infinitive ("quiero acordarme de"
        #    -> "acordarse de", slice 4).
        #    spaCy keeps the enclitic fused into one VERB token (VerbForm=Inf)
        #    whose surface ends in the clitic. Recover the base by stripping that
        #    suffix — the spaCy lemma here is the quirky "lavar yo", so we use the
        #    surface, not the lemma. (The override is applied to the recovered
        #    base; block 1's finite path applies it to the spaCy lemma instead —
        #    two override application points, one per recovery method.)
        if "Inf" in token.morph.get("VerbForm"):
            surface = token.text.lower()
            # Longest clitic first: 'nos' before 'os', else 'lavarnos' strips to
            # 'lavarn', fails the ends-in-'r' check, and the match is lost.
            for clitic in ("nos", "me", "te", "se", "os"):
                if surface.endswith(clitic):
                    base = surface[: -len(clitic)]
                    if base.endswith("r"):  # a real Spanish infinitive base
                        base = overrides.get(base, base)
                        # 3a. Reflexive + preposition on the infinitive (slice 4)
                        #     — same priority/suppression rule as the finite path
                        #     (block 1a): the combo wins, the bare reflexive is
                        #     skipped for this token. The infinitival "a"
                        #     ("voy a acordarme…") is filtered for free — only
                        #     (base, prep) pairs in _ES_REFLEXIVE_PREP emit.
                        combo = False
                        for prep_lemma, prep_i in _es_prep_candidates(token):
                            if (base, prep_lemma) in _ES_REFLEXIVE_PREP:
                                combo = True
                                _emit(f"{base}se {prep_lemma}",
                                      [token.i, prep_i],
                                      "es_reflexive_prep",
                                      f"{base} + -{clitic} + {prep_lemma} (clitic infinitive + prep)")
                                break
                        if not combo:
                            _emit(f"{base}se", [token.i], "es_reflexive_infinitive",
                                  f"{base} + -{clitic} (clitic infinitive)")
                    break

    return result


# ---------------------------------------------------------------------------
# Language-gated dispatcher (Stage 1 of second-language plan, 2026-05-21;
# Spanish first-slice extractor registered 2026-05-23, #36)
# ---------------------------------------------------------------------------
#
# `extract_german_logic` encodes German-specific morphology (separable
# prefixes, Akk/Dat alignment, reflexive sich); `extract_spanish_logic` is a
# narrow first slice for Spanish (reflexives + allowlisted verb+prep). Any
# OTHER language code still returns an empty list — the scraper writes
# word_table + sentence rows as normal but skips phrase rows. Callers don't
# need to special-case.

_LANGUAGE_EXTRACTORS = {
    "de": extract_german_logic,
    "es": extract_spanish_logic,
}


def extract_phrases(doc, language, overrides=None):
    """Dispatch phrase extraction by content language.

    German routes to `extract_german_logic` (byte-identical behaviour
    to the pre-Stage-1 call site); Spanish ('es') routes to
    `extract_spanish_logic` (first slice, #36). Any other language returns
    an empty list so the caller can iterate normally and the scraper writes
    zero phrase rows for it.

    `doc` is a spaCy Doc; we accept it without inspecting language so
    the caller is the source of truth (matches scraper detected_lang,
    matcher request language, chat session language).

    `overrides` (#39) is an optional {observed_lemma: corrected_lemma} map the
    caller loads from the `lemma_override` table (scraper:
    `pipeline.load_lemma_overrides`). It patches spaCy lemmatizer errors before
    canonicals are built. `None`/empty means "trust spaCy" — the pre-#39
    behaviour, so existing call sites are unaffected.
    """
    extractor = _LANGUAGE_EXTRACTORS.get(language)
    if extractor is None:
        return []
    return extractor(doc, overrides)


def get_words_array(text):
    """
    Extract words from text and return as a simple array.
    Returns list of dictionary entries (lemmas).
    """
    doc = nlp(text)
    analysis = extract_german_logic(doc)
    return [item['dictionary_entry'] for item in analysis]

def get_words_detailed_array(text):
    """
    Extract words from text and return as an array with details.
    Returns list of [word, lemma, pos_type, indices] tuples.
    """
    doc = nlp(text)
    analysis = extract_german_logic(doc)
    words_array = []
    for item in analysis:
        word = ' '.join(item['sentence_phrase'])
        lemma = item['dictionary_entry']
        pos = item['logic']
        indices = item['indices']
        words_array.append([word, lemma, pos, indices])
    return words_array


def main():
    # --- TEST ---
    sentence = "Ich lade morgen meine Freunde zum Essen ein"

    # Simple array output
    print("SIMPLE ARRAY:")
    words = get_words_array(sentence)
    print(words)
    print()

    # Detailed array output
    print("DETAILED ARRAY:")
    words_detailed = get_words_detailed_array(sentence)
    for word, lemma, pos, indices in words_detailed:
        print(f"['{word}', '{lemma}', '{pos}', {indices}]")
    print()

    # Full analysis (original)
    print("FULL ANALYSIS:")
    doc = nlp(sentence)
    analysis = extract_german_logic(doc)
    for item in analysis:
        print(f"Phrase:  {' '.join(item['sentence_phrase'])}")
        print(f"Logic:   {item['logic']}")
        print(f"Dict:    {item['dictionary_entry']}")
        print(f"Indices: {item['indices']}")
        if 'match_type' in item:
            print(f"Match:   {item['match_type']}")
        print()


if __name__ == '__main__': 
    main()
