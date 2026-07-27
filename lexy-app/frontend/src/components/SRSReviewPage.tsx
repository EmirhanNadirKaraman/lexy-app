import { useState, useEffect, useCallback } from 'react';
import type { SRSReviewCard, SRSProductionResult } from '../types';
import { getDueCards, skipCard, submitReviewAnswer, submitProductionAnswer } from '../api/srs';
import { LANGUAGE_OPTIONS } from '../config/languages';
import { LemmaFlagButton } from './LemmaFlagButton';

interface Props {
    token: string;
    language: string;
    onLanguageChange: (lang: string) => void;
    onClose: () => void;
}

export function SRSReviewPage({ token, language, onLanguageChange, onClose }: Props) {
    const [cards, setCards]         = useState<SRSReviewCard[]>([]);
    const [index, setIndex]         = useState(0);
    const [revealed, setRevealed]   = useState(false);
    const [loading, setLoading]     = useState(false);
    const [submitting, setSubmitting] = useState(false);
    const [error, setError]         = useState<string | null>(null);
    const [done, setDone]           = useState(false);
    const [reviewed, setReviewed]   = useState(0); // how many answered this session

    // Feedback state: shown after answering, before advancing to the next card.
    // For passive cards: prompt = English gloss, reveal shows German answer_text. Self-graded.
    // For active cards: production result from the LLM evaluator.
    type Feedback = {
        correct:    boolean;
        answerText: string;            // canonical German answer for this card
        direction:  'passive' | 'active';
        // Active-only fields:
        submitted?: string;            // what the user typed
        message?:   string;            // LLM/eval one-sentence verdict
    };
    const [feedback, setFeedback]   = useState<Feedback | null>(null);

    // Active-production state
    const [producedText, setProducedText] = useState('');

    const load = useCallback(async () => {
        if (!language) return;
        setLoading(true);
        setError(null);
        setDone(false);
        setIndex(0);
        setReviewed(0);
        setRevealed(false);
        setFeedback(null);
        setProducedText('');
        try {
            const data = await getDueCards(token, language, 30);
            setCards(data);
            if (data.length === 0) setDone(true);
        } catch {
            setError('Failed to load cards. Try again.');
        } finally {
            setLoading(false);
        }
    }, [token, language]);

    // Load cards when language is set (or changes)
    useEffect(() => { load(); }, [load]);

    const current = cards[index] ?? null;

    // Passive cards: user self-grades after revealing the answer. Active cards
    // either go through handleProduce() below (typed input + LLM eval) or use
    // this same path with correct=false for the "I don't know" button.
    async function handleAnswer(correct: boolean) {
        if (!current || submitting) return;
        setSubmitting(true);
        try {
            await submitReviewAnswer(token, current.card_id, correct);
            setReviewed(r => r + 1);
            setFeedback({
                correct,
                answerText: current.answer_text ?? current.display_text,
                direction: current.direction as 'passive' | 'active',
            });
        } catch {
            setError('Failed to save answer. Try again.');
        } finally {
            setSubmitting(false);
        }
    }

    // Active cards only: type the German answer; backend evaluates.
    async function handleProduce() {
        if (!current || submitting) return;
        const answer = producedText.trim();
        if (!answer) return;
        setSubmitting(true);
        try {
            const result: SRSProductionResult = await submitProductionAnswer(
                token, current.card_id, answer,
            );
            setReviewed(r => r + 1);
            setFeedback({
                correct:    result.correct,
                answerText: result.expected || current.answer_text || current.display_text,
                direction:  'active',
                submitted:  result.submitted,
                message:    result.feedback,
            });
        } catch (e: unknown) {
            setError(e instanceof Error ? e.message : 'Failed to evaluate answer.');
        } finally {
            setSubmitting(false);
        }
    }

    function advance() {
        setFeedback(null);
        setRevealed(false);
        setProducedText('');
        const next = index + 1;
        if (next >= cards.length) {
            setDone(true);
        } else {
            setIndex(next);
        }
    }

    /**
     * W5 / Hole 14: Skip persists the deferral to the backend (`due_date`
     * pushed +1 day) before advancing the local index. Without this the
     * same card reappeared on next refresh because the local skip was
     * UI-only.
     */
    async function handleSkip() {
        if (!current || !token || submitting) return;
        setSubmitting(true);
        setError(null);
        try {
            await skipCard(token, current.card_id);
            advance();
        } catch {
            setError('Failed to skip. Try again.');
        } finally {
            setSubmitting(false);
        }
    }

    // Passive direction (T1.2 / Hole 12): English gloss is on the front
    // (current.prompt_text); reveal shows the German display_text. User then
    // self-grades "I knew it" / "I didn't know it".
    function renderPassiveControls() {
        if (!current) return null;
        if (!revealed) {
            return (
                <button
                    onClick={() => setRevealed(true)}
                    style={{
                        marginTop: '8px', padding: '10px 32px', borderRadius: '6px',
                        border: '1px solid var(--color-border-accent)', background: 'var(--color-primary-soft)',
                        color: 'var(--color-primary-on-soft)', fontSize: '14px', fontWeight: 600,
                        cursor: 'pointer',
                    }}
                >
                    Show answer
                </button>
            );
        }
        return (
            <>
                <div style={{
                    fontSize: '15px', color: 'var(--color-text-strong)', fontWeight: 600,
                    background: 'var(--color-surface-sunken)', border: '1px solid var(--color-border-accent)',
                    borderRadius: '6px', padding: '8px 16px',
                    maxWidth: '100%',
                    overflowWrap: 'anywhere',
                    wordBreak: 'break-word',
                }}>
                    {current.answer_text ?? current.display_text}
                </div>
                {/* Flag the canonical the learner is looking at right now.
                    Words and phrases only — grammar rules carry no lemma, and
                    the backend's item_type is Literal["word","phrase"]. */}
                {(current.item_type === 'word' || current.item_type === 'phrase') && (
                    <div style={{ width: '100%', maxWidth: '340px' }}>
                        <LemmaFlagButton
                            key={current.card_id}
                            token={token}
                            language={language}
                            surfaceForm={current.answer_text ?? current.display_text}
                            observedLemma={current.answer_text ?? current.display_text}
                            itemType={current.item_type}
                            itemId={current.item_id}
                        />
                    </div>
                )}
                <div style={{
                    display: 'flex', gap: '10px', marginTop: '4px',
                    width: '100%', maxWidth: '340px',
                    flexWrap: 'wrap',
                }}>
                    <button
                        data-testid="srs-passive-incorrect"
                        onClick={() => handleAnswer(false)}
                        disabled={submitting}
                        style={{
                            flex: '1 1 140px', minHeight: '44px',
                            padding: '10px 8px', borderRadius: '6px',
                            border: '1px solid var(--color-danger-border)',
                            background: submitting ? 'var(--color-surface-muted)' : 'var(--color-danger-bg)',
                            color: submitting ? 'var(--color-text-subtle)' : 'var(--color-danger)',
                            fontSize: '14px', fontWeight: 600,
                            cursor: submitting ? 'default' : 'pointer',
                            touchAction: 'manipulation',
                        }}
                    >
                        I didn't know it
                    </button>
                    <button
                        data-testid="srs-passive-correct"
                        onClick={() => handleAnswer(true)}
                        disabled={submitting}
                        style={{
                            flex: '1 1 140px', minHeight: '44px',
                            padding: '10px 8px', borderRadius: '6px',
                            border: '1px solid var(--color-success-border)',
                            background: submitting ? 'var(--color-surface-muted)' : 'var(--color-success-bg)',
                            color: submitting ? 'var(--color-text-subtle)' : 'var(--color-success)',
                            fontSize: '14px', fontWeight: 600,
                            cursor: submitting ? 'default' : 'pointer',
                            touchAction: 'manipulation',
                        }}
                    >
                        I knew it ✓
                    </button>
                </div>
            </>
        );
    }

    // Active direction: real production test. User types German; backend evaluates.
    // No "I knew it" self-grade button — active cards must require either typed
    // input or "I don't know" (which routes through the existing /review/{id}).
    function renderActiveControls() {
        if (!current) return null;
        const canSubmit = !!producedText.trim() && !submitting;
        return (
            <>
                <input
                    data-testid="srs-active-input"
                    type="text"
                    value={producedText}
                    onChange={e => setProducedText(e.target.value)}
                    onKeyDown={e => { if (e.key === 'Enter' && canSubmit) handleProduce(); }}
                    placeholder="Type the answer"
                    disabled={submitting}
                    autoFocus
                    // iOS Safari zooms in on any focused input below 16px font.
                    // 16px is the minimum that keeps the zoom-on-focus behaviour
                    // off. Do not lower this without testing on a real device.
                    style={{
                        width: '100%', maxWidth: '340px',
                        minHeight: '44px',
                        padding: '10px 14px', borderRadius: '6px',
                        border: '1px solid var(--color-border-accent)',
                        background: 'var(--color-input-bg)',
                        color: 'var(--color-text)',
                        fontSize: '16px',
                        textAlign: 'center',
                        boxSizing: 'border-box',
                    }}
                />
                <div style={{
                    display: 'flex', gap: '10px', marginTop: '4px',
                    width: '100%', maxWidth: '340px',
                    flexWrap: 'wrap',
                }}>
                    <button
                        data-testid="srs-active-dont-know"
                        onClick={() => handleAnswer(false)}
                        disabled={submitting}
                        style={{
                            flex: '1 1 140px', minHeight: '44px',
                            padding: '10px 8px', borderRadius: '6px',
                            border: '1px solid var(--color-danger-border)',
                            background: submitting ? 'var(--color-surface-muted)' : 'var(--color-danger-bg)',
                            color: submitting ? 'var(--color-text-subtle)' : 'var(--color-danger)',
                            fontSize: '14px', fontWeight: 600,
                            cursor: submitting ? 'default' : 'pointer',
                            touchAction: 'manipulation',
                        }}
                    >
                        I don't know
                    </button>
                    <button
                        data-testid="srs-active-submit"
                        onClick={handleProduce}
                        disabled={!canSubmit}
                        style={{
                            flex: '1 1 140px', minHeight: '44px',
                            padding: '10px 8px', borderRadius: '6px',
                            border: '1px solid var(--color-primary)',
                            background: !canSubmit ? 'var(--color-surface-muted)' : 'var(--color-primary)',
                            color: !canSubmit ? 'var(--color-text-subtle)' : 'var(--color-primary-text)',
                            fontSize: '14px', fontWeight: 600,
                            cursor: !canSubmit ? 'default' : 'pointer',
                            touchAction: 'manipulation',
                        }}
                    >
                        {submitting ? 'Checking…' : 'Submit'}
                    </button>
                </div>
            </>
        );
    }

    const total = cards.length;
    const progress = total > 0 ? Math.round((index / total) * 100) : 0;

    return (
        <div style={{
            border: '1px solid var(--color-border-accent)',
            borderRadius: '8px',
            // Fluid side padding — tight on mobile, comfortable on desktop.
            padding: 'clamp(12px, 4vw, 20px)',
            background: 'var(--color-surface-muted)',
            marginBottom: '16px',
        }}>
            {/* Header */}
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
                <h2 style={{ margin: 0, fontSize: '16px', color: 'var(--color-text-strong)' }}>
                    Review{total > 0 && !done ? ` (${total - index} left)` : ''}
                </h2>
                <button
                    data-testid="srs-close"
                    onClick={onClose}
                    style={{
                        // 44×44 finger-tappable close button.
                        minWidth: '44px', minHeight: '44px',
                        display: 'flex', alignItems: 'center', justifyContent: 'center',
                        background: 'none', border: 'none',
                        fontSize: '20px', cursor: 'pointer', color: 'var(--color-text-muted)',
                        padding: 0,
                        touchAction: 'manipulation',
                    }}
                    aria-label="Close"
                >
                    ×
                </button>
            </div>

            {/* Language + reload controls */}
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap', marginBottom: '14px' }}>
                <select
                    value={language}
                    onChange={e => onLanguageChange(e.target.value)}
                    style={{
                        // 16px keeps iOS from zooming on focus; minHeight 44 for tap.
                        padding: '6px 10px', border: '1px solid var(--color-input-border)',
                        borderRadius: '5px', fontSize: '16px',
                        minHeight: '44px',
                        background: 'var(--color-input-bg)', color: 'var(--color-text)', cursor: 'pointer',
                    }}
                >
                    <option value="">Select language…</option>
                    {LANGUAGE_OPTIONS.map(l => (
                        <option key={l.code} value={l.code}>{l.label}</option>
                    ))}
                </select>
                <button
                    onClick={load}
                    disabled={loading || !language}
                    style={{
                        padding: '6px 14px', minHeight: '44px',
                        border: '1px solid var(--color-border-accent)',
                        borderRadius: '5px', background: 'var(--color-surface)',
                        color: 'var(--color-primary-on-soft)', fontSize: '14px', fontWeight: 600,
                        cursor: loading || !language ? 'not-allowed' : 'pointer',
                        opacity: loading || !language ? 0.5 : 1,
                        touchAction: 'manipulation',
                    }}
                >
                    {loading ? 'Loading…' : 'Reload'}
                </button>
            </div>

            {!language && (
                <p style={{ fontSize: '13px', color: 'var(--color-text-subtle)' }}>Select a language above to start reviewing.</p>
            )}

            {error && (
                <p style={{ fontSize: '13px', color: 'var(--color-danger)', margin: '8px 0' }}>{error}</p>
            )}

            {/* Empty state */}
            {language && !loading && done && reviewed === 0 && cards.length === 0 && (
                <div style={{ textAlign: 'center', padding: '32px 0' }}>
                    <div style={{ fontSize: '32px', marginBottom: '8px' }}>✓</div>
                    <p style={{ fontSize: '15px', fontWeight: 600, color: 'var(--color-success)', margin: '0 0 4px' }}>
                        Nothing due right now
                    </p>
                    <p style={{ fontSize: '13px', color: 'var(--color-text-subtle)', margin: 0 }}>
                        Check back later or mark more words as "learning" to build your review queue.
                    </p>
                </div>
            )}

            {/* Session complete */}
            {language && !loading && done && reviewed > 0 && (
                <div style={{ textAlign: 'center', padding: '32px 0' }}>
                    <div style={{ fontSize: '32px', marginBottom: '8px' }}>✓</div>
                    <p style={{ fontSize: '15px', fontWeight: 600, color: 'var(--color-text-strong)', margin: '0 0 4px' }}>
                        Session complete!
                    </p>
                    <p style={{ fontSize: '13px', color: 'var(--color-text-muted)', margin: '0 0 16px' }}>
                        {reviewed} card{reviewed !== 1 ? 's' : ''} reviewed
                    </p>
                    <button
                        onClick={load}
                        style={{
                            padding: '8px 20px', borderRadius: '6px',
                            border: '1px solid var(--color-border-accent)', background: 'var(--color-surface)',
                            color: 'var(--color-primary-on-soft)', fontSize: '13px', fontWeight: 600, cursor: 'pointer',
                        }}
                    >
                        Check for more
                    </button>
                </div>
            )}

            {/* Review card or feedback panel */}
            {language && !loading && !done && current && (
                <>
                    {/* Progress bar */}
                    <div style={{ marginBottom: '16px' }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '11px', color: 'var(--color-text-subtle)', marginBottom: '4px' }}>
                            <span>{index + 1} of {total}</span>
                            <span>{reviewed} reviewed this session</span>
                        </div>
                        <div style={{ height: '4px', background: 'var(--color-border-accent)', borderRadius: '2px', overflow: 'hidden' }}>
                            <div style={{
                                height: '100%', borderRadius: '2px',
                                background: 'var(--color-primary)',
                                width: `${progress}%`,
                                transition: 'width 0.3s ease',
                            }} />
                        </div>
                    </div>

                    {feedback ? (
                        /* ── Feedback panel ─────────────────────────────── */
                        <div style={{
                            background: feedback.correct ? 'var(--color-success-bg)' : 'var(--color-warning-bg)',
                            border: `1px solid ${feedback.correct ? 'var(--color-success-border)' : 'var(--color-warning-border)'}`,
                            borderRadius: '8px',
                            padding: 'clamp(20px, 5vw, 28px) clamp(16px, 5vw, 24px)',
                            textAlign: 'center',
                            minHeight: '200px',
                            display: 'flex',
                            flexDirection: 'column',
                            alignItems: 'center',
                            justifyContent: 'center',
                            gap: '14px',
                        }}>
                            <div style={{
                                fontSize: 'clamp(22px, 6vw, 28px)',
                                fontWeight: 700,
                                color: feedback.correct ? 'var(--color-success)' : 'var(--color-warning)',
                            }}>
                                {feedback.correct ? '✓ Correct' : '✗ Incorrect'}
                            </div>

                            {/* Answer reveal — always shown so the user sees the target. */}
                            <div
                                data-testid="srs-feedback-answer"
                                style={{
                                    fontSize: '13px',
                                    color: 'var(--color-text)',
                                    background: 'var(--color-surface)',
                                    border: '1px solid var(--color-border)',
                                    borderRadius: '6px',
                                    padding: '8px 16px',
                                    maxWidth: '100%',
                                    overflowWrap: 'anywhere',
                                    wordBreak: 'break-word',
                                }}
                            >
                                <span style={{ color: 'var(--color-text-subtle)', marginRight: '6px' }}>
                                    {/* T1.2: both directions now reveal the German answer.
                                        Passive = the German you should have recalled,
                                        Active  = the German you should have produced. */}
                                    Target:
                                </span>
                                <span style={{ fontWeight: 700, color: 'var(--color-text-strong)' }}>{feedback.answerText}</span>
                            </div>

                            {/* What the user typed (active cards only) */}
                            {feedback.direction === 'active' && feedback.submitted !== undefined && (
                                <div style={{
                                    fontSize: '12px', color: 'var(--color-text-muted)',
                                    maxWidth: '100%',
                                    overflowWrap: 'anywhere',
                                    wordBreak: 'break-word',
                                }}>
                                    You wrote: <span style={{ fontStyle: 'italic' }}>{feedback.submitted}</span>
                                </div>
                            )}

                            {/* Optional LLM verdict */}
                            {feedback.message && (
                                <div style={{
                                    fontSize: '12px', color: 'var(--color-text)',
                                    maxWidth: '420px',
                                    overflowWrap: 'anywhere',
                                    wordBreak: 'break-word',
                                }}>
                                    {feedback.message}
                                </div>
                            )}

                            <button
                                onClick={advance}
                                style={{
                                    marginTop: '4px',
                                    padding: '10px 32px',
                                    minHeight: '44px',
                                    borderRadius: '6px',
                                    border: 'none',
                                    background: 'var(--color-primary)',
                                    color: 'var(--color-primary-text)',
                                    fontSize: '14px',
                                    fontWeight: 600,
                                    cursor: 'pointer',
                                    touchAction: 'manipulation',
                                }}
                            >
                                Continue →
                            </button>
                        </div>
                    ) : (
                        /* ── Review card ────────────────────────────────── */
                        <div style={{
                            background: 'var(--color-surface)',
                            border: '1px solid var(--color-border-accent)',
                            borderRadius: '8px',
                            padding: 'clamp(20px, 5vw, 28px) clamp(16px, 5vw, 24px)',
                            textAlign: 'center',
                            minHeight: '200px',
                            display: 'flex',
                            flexDirection: 'column',
                            alignItems: 'center',
                            justifyContent: 'center',
                            gap: '16px',
                        }}>
                            {/* Direction badge — semantic info/warning palette,
                                kept fixed so passive/active stays recognisable. */}
                            <span style={{
                                fontSize: '10px', fontWeight: 700,
                                textTransform: 'uppercase', letterSpacing: '0.08em',
                                color: current.direction === 'passive' ? '#1565c0' : '#e65100',
                                background: current.direction === 'passive' ? '#e3f2fd' : '#fff3e0',
                                borderRadius: '10px', padding: '2px 8px',
                            }}>
                                {current.direction === 'passive' ? 'Recognition' : 'Production'}
                            </span>

                            {/* Front of the card — prompt_text from the backend.
                                Both directions show the English gloss (T1.2 /
                                Hole 12). Passive reveals the German answer_text
                                for self-grading; active expects the user to
                                type the German into the input below.
                                Fluid font: 24px on phone, 32px on desktop. */}
                            <div style={{
                                fontSize: 'clamp(24px, 7vw, 32px)',
                                fontWeight: 700, color: 'var(--color-text-strong)', lineHeight: 1.2,
                                maxWidth: '100%',
                                overflowWrap: 'anywhere',
                                wordBreak: 'break-word',
                            }}>
                                {current.prompt_text ?? current.display_text}
                            </div>

                            {/* Instruction */}
                            <p style={{ margin: 0, fontSize: '13px', color: 'var(--color-text-muted)' }}>
                                {current.direction === 'passive'
                                    ? 'Recall the answer. Reveal, then self-grade.'
                                    : 'Type the answer for this item.'}
                            </p>

                            {/* Level indicators */}
                            <div style={{ display: 'flex', gap: '14px', fontSize: '11px', color: 'var(--color-text-subtle)', flexWrap: 'wrap', justifyContent: 'center' }}>
                                <span>passive {current.passive_level}</span>
                                <span>active {current.active_level}</span>
                                <span>rep {current.repetitions}</span>
                            </div>

                            {current.direction === 'passive'
                                ? renderPassiveControls()
                                : renderActiveControls()
                            }
                        </div>
                    )}

                    {/* Skip — only shown on the card face, not during feedback.
                        W5 / Hole 14: defers the card on the server (+1 day)
                        instead of just advancing the local index. */}
                    {!feedback && (
                        <div style={{ textAlign: 'right', marginTop: '8px' }}>
                            <button
                                data-testid="srs-skip"
                                disabled={submitting}
                                onClick={handleSkip}
                                style={{
                                    background: 'none', border: 'none',
                                    color: 'var(--color-text-subtle)',
                                    fontSize: '12px',
                                    minHeight: '32px',
                                    padding: '4px 8px',
                                    cursor: submitting ? 'not-allowed' : 'pointer',
                                    touchAction: 'manipulation',
                                }}
                            >
                                {submitting ? 'Skipping…' : 'Skip →'}
                            </button>
                        </div>
                    )}
                </>
            )}
        </div>
    );
}
