from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from ..core.security import BCRYPT_MAX_PASSWORD_BYTES

# ---------------------------------------------------------------------------
# Existing search schemas (unchanged)
# ---------------------------------------------------------------------------


class SearchResult(BaseModel):
    video_id: str
    title: str
    thumbnail_url: str
    language: str
    start_time: float
    start_time_int: int
    content: str
    surface_form: str | None
    match_type: str


class SearchResponse(BaseModel):
    query: str
    results: list[SearchResult]
    total: int


class SuggestionResult(BaseModel):
    word: str
    score: float
    type: str  # 'word' | 'phrase'


class VideoSentence(BaseModel):
    sentence_id: int
    start_time: float
    start_time_int: int
    content: str


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    # Optional invite/registration code (S2). Enforced only when the server sets
    # REGISTRATION_CODE; ignored otherwise. Length-capped to bound the body.
    registration_code: str | None = Field(default=None, max_length=128)

    @field_validator("password")
    @classmethod
    def _password_within_bcrypt_limit(cls, v: str) -> str:
        # bcrypt truncates anything past 72 bytes (S10); reject instead of
        # silently dropping entropy. Measured in BYTES, not len(): accented
        # characters / emoji are multi-byte, so a password well under 72
        # characters can still exceed 72 bytes.
        if len(v.encode("utf-8")) > BCRYPT_MAX_PASSWORD_BYTES:
            raise ValueError(
                f"Password must not exceed {BCRYPT_MAX_PASSWORD_BYTES} bytes "
                "(accented characters and emoji each use multiple bytes)."
            )
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    # max_length is a body-size guard only (S10) — NOT the 72-byte truncation
    # rule. Capping login at 72 bytes would lock out any account whose password
    # predates the register cap; verify_password truncates identically anyway.
    password: str = Field(max_length=1024)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserRead(BaseModel):
    user_id: str
    email: str


# ---------------------------------------------------------------------------
# Word knowledge
# ---------------------------------------------------------------------------


class WordKnowledgeRead(BaseModel):
    item_id: int
    item_type: str  # 'word' | 'phrase' | 'grammar_rule'
    status: str     # 'unknown' | 'learning' | 'known'
    passive_level: int
    active_level: int
    notes: str | None = None
    last_seen: datetime | None = None


class WordStatusUpdate(BaseModel):
    status: Literal["unknown", "learning", "known"]


# ---------------------------------------------------------------------------
# Chat (schemas only — routes/service come in a later step)
# ---------------------------------------------------------------------------


class ReadingStatsResponse(BaseModel):
    video_id: str
    total_lemmas: int
    known: int
    learning: int
    unknown: int
    known_pct: float
    learning_pct: float
    unknown_pct: float


class ChatSessionRead(BaseModel):
    session_id: str
    session_type: str  # 'free' | 'guided'
    target_item_id: int | None = None
    target_item_type: str | None = None
    started_at: datetime


class ChatMessageRead(BaseModel):
    message_id: int
    session_id: str
    role: str   # 'user' | 'assistant'
    content: str
    language_detected: str | None = None
    corrections: list[Any] | None = None
    word_matches: list[Any] | None = None
    evaluation: dict[str, Any] | None = None
    created_at: datetime


# ---------------------------------------------------------------------------
# Sentence matcher
# ---------------------------------------------------------------------------


class MatchRequest(BaseModel):
    # max_length caps unauthenticated spaCy work on the public /sentences/match
    # route (S16): an over-length body is rejected with 422 before the handler
    # runs, so the parser never sees it. 1000 chars covers any real sentence.
    sentence: str = Field(..., max_length=1000)


class MatchPhraseResult(BaseModel):
    dictionary_entry: str
    sentence_phrase: list[str]
    logic: str
    match_type: str
    indices: list[int]
    phrase_id: int | None = None  # populated when canonical is in phrase_table


class PhraseLookupResult(BaseModel):
    phrase_id: int
    canonical: str       # blueprint: "freuen sich über etw."
    surface_form: str    # display form: "sich freuen über"
    phrase_type: str     # 'reflexive_verb' | 'verb_pattern' | 'collocation'
    language: str = "de"


class MatchResponse(BaseModel):
    sentence: str
    phrases: list[MatchPhraseResult]


# ---------------------------------------------------------------------------
# Word lookup
# ---------------------------------------------------------------------------


class WordLookupResult(BaseModel):
    word_id: int
    word: str
    lemma: str
    # `pos` is the spaCy universal POS tag (NOUN, VERB, X, …) — added for
    # W3 (Hole 2) so the disambiguation UI can label competing meanings.
    # Defaults to empty so legacy callers that ignored POS still validate.
    pos: str = ""
    current_status: str | None = None
    passive_level: int = 0
    active_level: int = 0
    passive_due: datetime | None = None
    active_due: datetime | None = None


class WordLookupResponse(BaseModel):
    """
    Discriminated response for `/words/by-text` (W3 / Hole 2).

    - status="not_found":  item=None,           candidates=[]
    - status="single":     item=<single match>, candidates=[same one item]
    - status="ambiguous":  item=None,           candidates=[2..N best matches]

    The shape collapses to single/null for non-interactive callers via the
    `item` field (use `item or (candidates[0] if candidates else None)`);
    the interactive picker reads `candidates` to render the disambiguation.
    """
    status: Literal["not_found", "single", "ambiguous"]
    item: WordLookupResult | None = None
    candidates: list[WordLookupResult] = []


class WordLearnAnywayRequest(BaseModel):
    # W2 / Hole 1: user adopts a word not in word_table. Length cap is
    # generous — German has long compounds (Donaudampfschifffahrtsgesellschaftskapitän
    # is 42 chars) but anything above ~80 is almost certainly a paste mistake.
    text:     str = Field(..., min_length=1, max_length=80)
    language: str = Field(..., min_length=2, max_length=8)


# ---------------------------------------------------------------------------
# SRS (spaced-repetition)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Free chat — input / output
# ---------------------------------------------------------------------------


class ChatSessionCreate(BaseModel):
    session_type: Literal["free"] = "free"
    # Stage 3 (second-language plan, 2026-05-21): the target language for
    # this free-chat session. Defaults to "de" for back-compat with
    # pre-Stage-3 frontends that don't yet send the field; new Spanish
    # sessions send "es".
    language: str = "de"


# ---------------------------------------------------------------------------
# Guided chat — input / output
# ---------------------------------------------------------------------------


class GuidedSessionCreate(BaseModel):
    language: str  # target language code, e.g. "de"
    target_item_id: int | None = None    # override auto-selection (from prep view)
    target_item_type: str | None = None  # 'word' | 'phrase'


class GuidedHints(BaseModel):
    intent_hint: str   # English: what concept to express, no target word
    anchor_hint: str   # German: partial clue — related word or prefix hint
    example:     str   # Complete German sentence using the target word


class GuidedSessionRead(BaseModel):
    session_id: str
    session_type: str           # 'guided'
    target_item_id: int
    target_item_type: str
    target_word: str
    started_at: datetime
    opening_message: ChatMessageRead
    hints: GuidedHints | None = None


class GuidedCompleteRequest(BaseModel):
    hint_level: int = Field(default=0, ge=0, le=3)


class GuidedSessionSummary(BaseModel):
    session_id: str
    target_word: str
    target_item_id: int
    target_item_type: str
    # Deterministic signals
    target_used: bool
    target_counted: bool
    target_counted_count: int
    total_turns: int
    hint_level: int
    sentence_quality: Literal["excellent", "good", "needs_work"]
    # LLM feedback (empty string = nothing to report)
    what_went_well: str
    what_to_improve: str
    corrective_note: str


class ChatSendMessage(BaseModel):
    content: str


class Correction(BaseModel):
    original: str
    corrected: str
    explanation: str


class SendMessageResponse(BaseModel):
    user_message: ChatMessageRead
    assistant_message: ChatMessageRead


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


class ItemFrequency(BaseModel):
    item_id: int
    item_type: str
    word: str | None = None
    event_count: int


class FailedItemStats(BaseModel):
    item_id: int
    item_type: str
    word: str | None = None
    fail_count: int
    last_failed: datetime


class InteractedItemStats(BaseModel):
    item_id: int
    item_type: str
    word: str | None = None
    total_interactions: int
    last_seen: datetime


# ---------------------------------------------------------------------------
# Playlist generation
# ---------------------------------------------------------------------------


class PlaylistGenerateRequest(BaseModel):
    item_ids: list[int] = Field(..., min_length=1, max_length=200)
    item_type: Literal["word"] = "word"   # extend to "phrase" when supported
    language: str
    max_videos: int = Field(default=10, ge=1, le=50)
    # algorithm: Literal["greedy"] = "greedy"   # add "ilp" here when implemented


class PlaylistVideoEntry(BaseModel):
    video_id: str
    title: str
    thumbnail_url: str
    language: str
    start_time: float
    start_time_int: int
    content: str
    covered_item_ids: list[int]
    covered_count: int


class PlaylistCoverage(BaseModel):
    target_count: int
    covered_count: int
    coverage_pct: float
    uncovered_item_ids: list[int]
    video_count: int


class PlaylistResult(BaseModel):
    videos: list[PlaylistVideoEntry]
    coverage: PlaylistCoverage


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------


class SentenceRecommendation(BaseModel):
    sentence_id: int
    content: str
    video_id: str
    video_title: str
    thumbnail_url: str
    start_time: float
    start_time_int: int
    unknown_count: int
    due_count: int       # SRS passive cards due in this sentence
    priority_count: int  # high-frequency unknown items present
    score: float


class SentenceRecommendationsResponse(BaseModel):
    sentences: list[SentenceRecommendation]
    target_unknown: int
    total: int


class VideoRecommendation(BaseModel):
    video_id: str
    title: str
    thumbnail_url: str
    language: str
    duration: float
    start_time: float
    start_time_int: int
    priority_score: float    # sum of prioritization scores for covered items
    covered_item_ids: list[int]
    covered_count: int
    score: float
    channel_id:   str | None = None
    channel_name: str | None = None
    category:     str | None = None


class VideoRecommendationsResponse(BaseModel):
    videos: list[VideoRecommendation]
    target_item_count: int    # total prioritized items used as ranking targets
    reason: str | None = None  # e.g. "no_target_items" for new users


class PrioritizedItemRead(BaseModel):
    """
    Not yet exposed via API endpoint — schema added now so the future
    GET /api/v1/recommendations/items endpoint is one thin router file away.
    signals dict is intentionally omitted from the API model (internal only).
    """
    item_id:   int
    item_type: str
    score:     float
    reasons:   list[str]


class ItemRecommendation(BaseModel):
    item_id:        int
    item_type:      str            # 'word' | 'phrase' | 'grammar_rule'
    score:          float
    display_text:   str
    secondary_text: str | None = None
    current_status: str | None = None   # None = no knowledge row yet
    passive_level:  int = 0
    active_level:   int = 0
    due_date:       datetime | None = None
    signals:        dict[str, float]    # is_due, mistake_recency, freq_rank, is_learning
    reasons:        list[str]           # from explain_signals()


class ItemRecommendationsResponse(BaseModel):
    items:     list[ItemRecommendation]
    item_type: str
    language:  str
    total:     int


# ---------------------------------------------------------------------------
# Insights
# ---------------------------------------------------------------------------


class InsightItem(BaseModel):
    item_id: int
    item_type: str                  # 'word' | 'phrase'
    display_text: str
    secondary_text: str | None = None
    score: float
    reasons: list[str]
    signals: dict[str, float]       # is_due, mistake_recency, freq_rank, is_learning
    extra: dict[str, Any] = {}      # card-type specific: event_count / fail_count / last_failed


class InsightCard(BaseModel):
    card_type: str                  # 'frequent_unknowns' | 'recent_mistakes'
    title: str
    explanation: str
    items: list[InsightItem]


class InsightCardsResponse(BaseModel):
    cards: list[InsightCard]
    language: str


class GrammarRuleRef(BaseModel):
    """Compact grammar rule reference included in prep view responses."""
    rule_id: int
    slug: str
    title: str
    rule_type: str
    short_explanation: str
    pattern_hint: str | None = None


class GrammarRuleDetail(BaseModel):
    """Full grammar rule info returned by GET /insights/grammar/{slug}."""
    rule_id: int
    slug: str
    title: str
    rule_type: str
    short_explanation: str
    pattern_hint: str | None = None
    long_explanation: str | None = None   # None = not yet generated


class GrammarRuleExplainResponse(BaseModel):
    slug: str
    long_explanation: str


class PrepViewData(BaseModel):
    item_id: int
    item_type: str
    display_text: str
    translation: str
    grammar_structure: str | None = None
    grammar_explanation: str
    example: str | None = None
    templates: list[str] = []
    has_examples: bool
    linked_grammar_rules: list[GrammarRuleRef] = []


class GenerateExamplesRequest(BaseModel):
    item_id: int
    item_type: str
    language: str


class GenerateExamplesResponse(BaseModel):
    example: str
    templates: list[str]


# ---------------------------------------------------------------------------
# Settings / preferences
# ---------------------------------------------------------------------------

import re as _re


def _hex_color(v: object) -> object:
    """Validator: accept None (optional field) or a 6-digit hex color string."""
    if v is not None and not _re.match(r'^#[0-9a-fA-F]{6}$', str(v)):
        raise ValueError('must be a 6-digit hex color, e.g. "#388e3c"')
    return v


class ReminderSummary(BaseModel):
    srs_due_count:       int
    reading_due_count:   int
    learning_item_count: int
    total_due:           int
    has_anything_due:    bool


class UserPreferences(BaseModel):
    liked_categories:       list[str]        = []
    disliked_categories:    list[str]        = []
    liked_genres:           list[str]        = []  # alias for liked_categories (frontend compat)
    disliked_genres:        list[str]        = []  # alias for disliked_categories (frontend compat)
    liked_channels:         list[str]        = []
    followed_channels:      list[str]        = []
    disliked_channels:      list[str]        = []
    channel_names:          dict[str, str]   = {}
    passive_reps_for_known: int              = 3
    active_reps_for_known:  int              = 5
    known_word_color:       str              = "#388e3c"
    learning_word_color:    str              = "#f57c00"
    unknown_word_color:     str              = "#d32f2f"
    reminders_enabled:      bool             = True
    theme_mode:             Literal["system", "light", "dark"] = "system"
    dark_mode:              bool             = False
    auto_mark_known:        bool             = False


class UserPreferencesUpdate(BaseModel):
    """
    All fields are optional — absent fields keep their current stored value.
    This gives PATCH-style merge semantics while using a PUT endpoint.
    """
    liked_channels:         list[str] | None = None
    followed_channels:      list[str] | None = None
    disliked_channels:      list[str] | None = None
    channel_names:          dict[str, str] | None = None
    passive_reps_for_known: int | None = Field(default=None, ge=1, le=20)
    active_reps_for_known:  int | None = Field(default=None, ge=1, le=20)
    known_word_color:       str | None = None
    learning_word_color:    str | None = None
    unknown_word_color:     str | None = None
    reminders_enabled:      bool | None = None
    theme_mode:             Literal["system", "light", "dark"] | None = None
    dark_mode:              bool | None = None
    auto_mark_known:        bool | None = None
    liked_genres:           list[str] | None = None  # alias for liked_categories (frontend compat)
    disliked_genres:        list[str] | None = None  # alias for disliked_categories (frontend compat)

    @field_validator("known_word_color", "learning_word_color", "unknown_word_color", mode="before")
    @classmethod
    def validate_hex_color(cls, v: object) -> object:
        return _hex_color(v)


class ChannelPreferenceRequest(BaseModel):
    channel_id:   str
    channel_name: str
    action:       Literal["follow", "like", "dislike", "clear"]


class CategoryPreferenceRequest(BaseModel):
    category: str
    action:   Literal["like", "dislike", "clear"]


class FollowedChannelVideosResponse(BaseModel):
    videos: list[VideoRecommendation]
    total:  int


# ---------------------------------------------------------------------------
# Book reading mode
# ---------------------------------------------------------------------------


class BookDocumentRead(BaseModel):
    doc_id: str
    user_id: str
    title: str
    filename: str
    total_pages: int | None = None
    language: str
    source_type: str   # 'pdf_text' | 'pdf_scan' | 'mixed' | 'unknown'
    status: str        # 'pending' | 'processing' | 'ready' | 'error'
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class BookPageSummary(BaseModel):
    page_id: int
    page_number: int
    is_scanned: bool
    has_image: bool
    block_count: int
    sentence_count: int | None = None


class SentenceCountUpdate(BaseModel):
    sentence_count: int


class StoredBlockToken(BaseModel):
    """A single token as stored server-side with a stable UUID."""
    token_id: str
    text: str
    is_word: bool


class BookBlockRead(BaseModel):
    block_id: int
    block_index: int
    block_type: str       # 'text' | 'ignored'
    bbox_x0: float | None = None
    bbox_y0: float | None = None
    bbox_x1: float | None = None
    bbox_y1: float | None = None
    ocr_text: str | None = None
    clean_text: str | None = None
    corrected_text: str | None = None
    correction_status: str            # 'none' | 'suggested' | 'approved' | 'rejected'
    ocr_confidence: float | None = None
    is_header_footer: bool
    user_text_override: str | None = None
    display_text: str                 # computed: override > approved correction > clean_text
    tokens: list[StoredBlockToken] = []  # Server-managed token list with stable IDs


class BookPageDetail(BaseModel):
    page_id: int
    page_number: int
    is_scanned: bool
    has_image: bool
    width_pt: float | None = None
    height_pt: float | None = None
    blocks: list[BookBlockRead]


class BlockPatchRequest(BaseModel):
    block_type: str | None = None          # 'text' | 'ignored'
    user_text_override: str | None = None  # empty string clears the override
    correction_status: str | None = None   # 'approved' | 'rejected'


class LLMRepairResponse(BaseModel):
    block_id: int
    ocr_text: str | None
    corrected_text: str
    correction_status: str  # 'suggested'


# ---------------------------------------------------------------------------
# Interactive reading — selections (custom learning units)
# ---------------------------------------------------------------------------


class ReadingSelectionAnchor(BaseModel):
    block_id: int
    token_id: str       # UUID — replaced token_index for stability across edits
    surface: str


class ReadingSelectionCreate(BaseModel):
    canonical: str                          # lowercase surface text
    surface_text: str                       # exact surface form, tokens in order
    sentence_text: str                      # full block text (context container)
    anchors: list[ReadingSelectionAnchor] = []
    note: str | None = None


class ReadingSelectionRead(BaseModel):
    selection_id: str
    doc_id: str
    canonical: str
    surface_text: str
    sentence_text: str
    anchors: list[ReadingSelectionAnchor]
    note: str | None
    status: str
    review_count: int = 0
    next_review_at: datetime | None = None
    created_at: datetime


class ReadingSelectionPatch(BaseModel):
    note: str | None = None
    status: str | None = None


class ReviewRequest(BaseModel):
    outcome: Literal["got_it", "still_learning", "mastered"]


class DueSelectionItem(BaseModel):
    """Compact selection returned by the cross-book due-items endpoint."""
    selection_id: str
    doc_id: str
    doc_title: str
    canonical: str
    surface_text: str
    sentence_text: str
    note: str | None
    review_count: int
    next_review_at: datetime | None
    created_at: datetime


# ---------------------------------------------------------------------------
# SRS review
# ---------------------------------------------------------------------------


class SRSReviewCard(BaseModel):
    card_id:       int
    item_id:       int
    item_type:     str   # 'word' | 'phrase' | 'grammar_rule'
    direction:     str   # 'passive' | 'active'
    due_date:      datetime
    repetitions:   int
    passive_level: int
    active_level:  int
    display_text:  str
    # For passive cards: prompt is the German item, answer is the English gloss
    # (user tries to recognize). For active cards: prompt is the English gloss,
    # answer is the German item (user tries to produce). Grammar rules use the
    # rule title as the prompt and the short_explanation as the answer.
    prompt_text:   str
    answer_text:   str


class SRSAnswerRequest(BaseModel):
    correct: bool


class SRSAnswerResponse(BaseModel):
    card_id: int
    success: bool


class SRSProductionRequest(BaseModel):
    # The German answer the learner typed for an active card. Empty strings are
    # treated as incorrect by the evaluator.
    answer: str


class SRSProductionResponse(BaseModel):
    card_id:   int
    correct:   bool
    expected:  str    # the target_text the user should have produced
    submitted: str    # echoed back; useful for the feedback panel
    feedback:  str    # one-sentence explanation


class SRSSkipResponse(BaseModel):
    # W5 / Hole 14: skip defers a card by `review_service.SKIP_DEFER_DAYS`.
    # No body — the deferred due_date is the only state change.
    card_id:  int
    due_date: datetime


class TranslateRequest(BaseModel):
    sentence: str
    language: str


class TranslateResponse(BaseModel):
    translation: str


class ExplainRequest(BaseModel):
    selection: str
    sentence: str
    language: str


class ExplainResponse(BaseModel):
    explanation: str


# ---------------------------------------------------------------------------
# Lemma correction candidates (#39 slice 3A) — user flags for bad lemmas.
# Signal only; never mutates lemma_override. See docs/LEMMA_OVERRIDE_WORKFLOW.md.
# ---------------------------------------------------------------------------


class LemmaCorrectionCreate(BaseModel):
    language: str = Field(min_length=1, max_length=16)
    surface_form: str = Field(min_length=1, max_length=200)
    observed_lemma: str = Field(min_length=1, max_length=200)
    # `null`/missing/blank all mean "no suggested correction"; normalized to ''
    # below so the table's NOT-NULL-DEFAULT-'' convention (migration 034) stays
    # invisible to callers. Same for context_text.
    suggested_lemma: str | None = Field(default=None, max_length=200)
    context_text: str | None = Field(default=None, max_length=1000)
    item_type: Literal["word", "phrase"] | None = None
    item_id: int | None = None
    sentence_id: int | None = None

    @field_validator("language", "surface_form", "observed_lemma")
    @classmethod
    def _strip_required(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v

    @model_validator(mode="after")
    def _normalize_optionals(self) -> "LemmaCorrectionCreate":
        # None / missing(default) / blank → '' (the "absent" sentinel matching
        # the NOT NULL DEFAULT '' columns). A model_validator runs even when the
        # field used its default — a field_validator would SKIP the default case,
        # letting a missing field reach SQL as NULL.
        self.suggested_lemma = (self.suggested_lemma or "").strip()
        self.context_text = (self.context_text or "").strip()
        return self


class LemmaCorrectionRead(BaseModel):
    candidate_id: int
    language: str
    surface_form: str
    observed_lemma: str
    suggested_lemma: str
    context_text: str
    item_type: str | None = None
    source: str
    status: str
    report_count: int
    created_at: datetime
    updated_at: datetime


# Admin review (#39 slice 3B) — accept promotes to lemma_override, reject does not.


class LemmaCorrectionAccept(BaseModel):
    # When omitted/blank, the candidate's own suggested_lemma is promoted; if
    # that's also blank the accept is rejected (400 nothing_to_promote).
    corrected_lemma: str | None = Field(default=None, max_length=200)
    review_note: str | None = Field(default=None, max_length=1000)


class LemmaCorrectionReject(BaseModel):
    review_note: str | None = Field(default=None, max_length=1000)


class LemmaOverrideRead(BaseModel):
    id: int
    language: str
    observed_lemma: str
    corrected_lemma: str
    source: str
    status: str


class LemmaCorrectionReviewResult(BaseModel):
    candidate: LemmaCorrectionRead
    override: LemmaOverrideRead | None = None  # None for reject


class LemmaCorrectionAdjudication(BaseModel):
    # Dry-run LLM adjudication proposal (#39 slice 3C). ADVISORY ONLY — returned
    # as an API response, never persisted, never mutates lemma_override. An admin
    # still decides via accept/reject (3B).
    candidate_id: int
    decision: Literal["accept", "reject", "needs_review"]
    proposed_corrected_lemma: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
