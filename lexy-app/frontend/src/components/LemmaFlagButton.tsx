// Learner-facing "this looks wrong" flag for a canonical / lemma (#39 P1).
//
// Renders a small link-weight button next to a displayed canonical form. Opening
// it reveals a compact form: an optional suggested lemma and an optional note.
// Submitting POSTs to /api/v1/lemma-corrections, which creates a PENDING
// candidate — it never changes extraction truth on its own. An admin promotes it
// (or doesn't) from the review queue.
//
// Deliberately low-stakes UI: flagging is a signal, not an edit, so success is
// quiet and the control collapses back to its resting state.

import { useState } from 'react';
import type { CSSProperties } from 'react';

import { flagLemma } from '../api/lemmaCorrections';

interface Props {
    token: string;
    language: string;
    /** The form as it appeared to the learner. */
    surfaceForm: string;
    /** The lemma/canonical we're asking about — usually the same string. */
    observedLemma: string;
    /** Only 'word' and 'phrase' carry lemmas; grammar rules must not render this. */
    itemType?: 'word' | 'phrase';
    itemId?: number;
    sentenceId?: number;
}

const labelStyle: CSSProperties = {
    display: 'block',
    fontSize: '12px',
    fontWeight: 600,
    color: 'var(--color-text-muted)',
    marginBottom: '4px',
};

// 16px font is an iOS guard: anything smaller makes Safari zoom on focus.
const inputStyle: CSSProperties = {
    width: '100%',
    boxSizing: 'border-box',
    minHeight: '44px',
    padding: '10px 12px',
    fontSize: '16px',
    borderRadius: '6px',
    border: '1px solid var(--color-input-border)',
    background: 'var(--color-input-bg)',
    color: 'var(--color-text)',
};

export function LemmaFlagButton({
    token, language, surfaceForm, observedLemma, itemType, itemId, sentenceId,
}: Props) {
    const [open, setOpen] = useState(false);
    const [suggested, setSuggested] = useState('');
    const [note, setNote] = useState('');
    const [submitting, setSubmitting] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [done, setDone] = useState(false);

    function reset() {
        setSuggested('');
        setNote('');
        setError(null);
        setSubmitting(false);
    }

    async function handleSubmit() {
        setSubmitting(true);
        setError(null);
        try {
            await flagLemma(token, {
                language,
                surface_form: surfaceForm,
                observed_lemma: observedLemma,
                suggested_lemma: suggested,
                context_text: note,
                item_type: itemType,
                item_id: itemId,
                sentence_id: sentenceId,
            });
            // Success is quiet: collapse the form, leave a small acknowledgement.
            setOpen(false);
            setDone(true);
            reset();
        } catch (e) {
            // Keep the form OPEN so the user doesn't lose what they typed.
            setError(e instanceof Error ? e.message : 'Could not send the report.');
            setSubmitting(false);
        }
    }

    if (done) {
        return (
            <span
                data-testid="lemma-flag-done"
                style={{ fontSize: '12px', color: 'var(--color-success)', fontWeight: 600 }}
            >
                Thanks — sent for review.
            </span>
        );
    }

    if (!open) {
        return (
            <button
                data-testid="lemma-flag-open"
                type="button"
                onClick={() => setOpen(true)}
                style={{
                    minHeight: '32px',
                    padding: '4px 8px',
                    background: 'none',
                    border: 'none',
                    color: 'var(--color-text-subtle)',
                    fontSize: '12px',
                    textDecoration: 'underline',
                    cursor: 'pointer',
                    touchAction: 'manipulation',
                }}
            >
                Looks wrong?
            </button>
        );
    }

    return (
        <div
            data-testid="lemma-flag-form"
            style={{
                width: '100%',
                textAlign: 'left',
                padding: '12px',
                borderRadius: '6px',
                border: '1px solid var(--color-border)',
                background: 'var(--color-surface-muted)',
                display: 'flex',
                flexDirection: 'column',
                gap: '10px',
            }}
        >
            <div style={{ fontSize: '13px', color: 'var(--color-text)' }}>
                Reporting <strong>{observedLemma}</strong> as the dictionary form of{' '}
                <strong>{surfaceForm}</strong>.
            </div>

            <div>
                <label htmlFor="lemma-flag-suggested" style={labelStyle}>
                    Correct form (optional)
                </label>
                <input
                    id="lemma-flag-suggested"
                    data-testid="lemma-flag-suggested"
                    type="text"
                    value={suggested}
                    onChange={e => setSuggested(e.target.value)}
                    placeholder="e.g. ducharse"
                    style={inputStyle}
                />
            </div>

            <div>
                <label htmlFor="lemma-flag-note" style={labelStyle}>
                    What looks wrong? (optional)
                </label>
                <input
                    id="lemma-flag-note"
                    data-testid="lemma-flag-note"
                    type="text"
                    value={note}
                    onChange={e => setNote(e.target.value)}
                    placeholder="Anything that helps a reviewer"
                    style={inputStyle}
                />
            </div>

            {error && (
                <div
                    data-testid="lemma-flag-error"
                    style={{
                        fontSize: '13px',
                        padding: '8px 10px',
                        borderRadius: '6px',
                        border: '1px solid var(--color-danger-border)',
                        background: 'var(--color-danger-bg)',
                        color: 'var(--color-danger)',
                        overflowWrap: 'anywhere',
                    }}
                >
                    {error}
                </div>
            )}

            <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
                <button
                    data-testid="lemma-flag-submit"
                    type="button"
                    onClick={handleSubmit}
                    disabled={submitting}
                    style={{
                        flex: '1 1 120px',
                        minHeight: '44px',
                        borderRadius: '6px',
                        border: '1px solid var(--color-primary)',
                        background: submitting ? 'var(--color-surface-muted)' : 'var(--color-primary)',
                        color: submitting ? 'var(--color-text-subtle)' : 'var(--color-primary-text)',
                        fontSize: '14px',
                        fontWeight: 600,
                        cursor: submitting ? 'default' : 'pointer',
                        touchAction: 'manipulation',
                    }}
                >
                    {submitting ? 'Sending…' : 'Send report'}
                </button>
                <button
                    data-testid="lemma-flag-cancel"
                    type="button"
                    onClick={() => { setOpen(false); reset(); }}
                    disabled={submitting}
                    style={{
                        flex: '1 1 100px',
                        minHeight: '44px',
                        borderRadius: '6px',
                        border: '1px solid var(--color-border-accent)',
                        background: 'var(--color-surface)',
                        color: 'var(--color-text)',
                        fontSize: '14px',
                        cursor: submitting ? 'default' : 'pointer',
                        touchAction: 'manipulation',
                    }}
                >
                    Cancel
                </button>
            </div>
        </div>
    );
}
