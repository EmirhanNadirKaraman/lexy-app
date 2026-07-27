// Admin review queue for lemma-correction candidates (#39 P1).
//
// Lists PENDING candidates and lets an admin accept (promotes the corrected
// lemma into `lemma_override`) or reject (records the decision, writes no
// override). Accept may carry an overriding corrected_lemma; when left blank the
// backend falls back to the candidate's own suggested_lemma, and 400s if both
// are empty — surfaced inline on the row.
//
// Rows update IN PLACE after a decision rather than refetching, so a long queue
// doesn't jump under the reviewer's cursor.

import { useCallback, useEffect, useState } from 'react';
import type { CSSProperties } from 'react';

import {
    acceptLemmaCorrection,
    listLemmaCorrections,
    rejectLemmaCorrection,
} from '../api/lemmaCorrections';
import type { LemmaCorrection } from '../api/lemmaCorrections';

interface Props {
    token: string;
    onClose: () => void;
}

const cellLabel: CSSProperties = {
    fontSize: '11px',
    fontWeight: 700,
    letterSpacing: '0.04em',
    textTransform: 'uppercase',
    color: 'var(--color-text-subtle)',
};

const cellValue: CSSProperties = {
    fontSize: '14px',
    color: 'var(--color-text-strong)',
    overflowWrap: 'anywhere',
    wordBreak: 'break-word',
};

const actionBtn = (kind: 'accept' | 'reject' | 'quiet'): CSSProperties => ({
    minHeight: '44px',
    flex: '1 1 110px',
    padding: '10px 12px',
    borderRadius: '6px',
    fontSize: '14px',
    fontWeight: 600,
    cursor: 'pointer',
    touchAction: 'manipulation',
    border: `1px solid ${
        kind === 'accept' ? 'var(--color-success-border)'
            : kind === 'reject' ? 'var(--color-danger-border)'
                : 'var(--color-border-accent)'
    }`,
    background:
        kind === 'accept' ? 'var(--color-success-bg)'
            : kind === 'reject' ? 'var(--color-danger-bg)'
                : 'var(--color-surface)',
    color:
        kind === 'accept' ? 'var(--color-success)'
            : kind === 'reject' ? 'var(--color-danger)'
                : 'var(--color-text)',
});

function Field({ label, value, testId }: { label: string; value: string; testId?: string }) {
    return (
        <div>
            <div style={cellLabel}>{label}</div>
            <div style={cellValue} data-testid={testId}>{value}</div>
        </div>
    );
}

function CandidateRow({
    candidate, token, onReviewed,
}: {
    candidate: LemmaCorrection;
    token: string;
    onReviewed: (updated: LemmaCorrection) => void;
}) {
    const [correctedLemma, setCorrectedLemma] = useState('');
    const [busy, setBusy] = useState<null | 'accept' | 'reject'>(null);
    const [error, setError] = useState<string | null>(null);

    const reviewed = candidate.status !== 'pending';

    async function run(action: 'accept' | 'reject') {
        setBusy(action);
        setError(null);
        try {
            const result = action === 'accept'
                ? await acceptLemmaCorrection(token, candidate.candidate_id, correctedLemma)
                : await rejectLemmaCorrection(token, candidate.candidate_id);
            onReviewed(result.candidate);
        } catch (e) {
            setError(e instanceof Error ? e.message : `Could not ${action} this report.`);
        } finally {
            setBusy(null);
        }
    }

    return (
        <li
            data-testid={`lemma-candidate-${candidate.candidate_id}`}
            style={{
                listStyle: 'none',
                border: '1px solid var(--color-border)',
                borderRadius: '8px',
                padding: 'clamp(12px, 3vw, 16px)',
                marginBottom: '12px',
                background: 'var(--color-surface)',
                opacity: reviewed ? 0.75 : 1,
            }}
        >
            <div style={{
                display: 'flex', justifyContent: 'space-between',
                alignItems: 'baseline', gap: '8px', flexWrap: 'wrap',
                marginBottom: '10px',
            }}>
                <span style={{ fontSize: '13px', color: 'var(--color-text-muted)' }}>
                    #{candidate.candidate_id} · {candidate.language}
                    {candidate.item_type ? ` · ${candidate.item_type}` : ''}
                </span>
                <span
                    data-testid={`lemma-report-count-${candidate.candidate_id}`}
                    style={{
                        fontSize: '12px', fontWeight: 700,
                        padding: '3px 8px', borderRadius: '999px',
                        background: 'var(--color-primary-soft)',
                        color: 'var(--color-primary-on-soft)',
                    }}
                >
                    {candidate.report_count} report{candidate.report_count === 1 ? '' : 's'}
                </span>
            </div>

            <div style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))',
                gap: '10px',
                marginBottom: '12px',
            }}>
                <Field
                    label="Surface form"
                    value={candidate.surface_form}
                    testId={`lemma-surface-${candidate.candidate_id}`}
                />
                <Field
                    label="Observed lemma"
                    value={candidate.observed_lemma}
                    testId={`lemma-observed-${candidate.candidate_id}`}
                />
                <Field
                    label="Suggested lemma"
                    value={candidate.suggested_lemma || '—'}
                    testId={`lemma-suggested-${candidate.candidate_id}`}
                />
                <Field
                    label="Context"
                    value={candidate.context_text || '—'}
                    testId={`lemma-context-${candidate.candidate_id}`}
                />
            </div>

            {reviewed ? (
                <div
                    data-testid={`lemma-status-${candidate.candidate_id}`}
                    style={{
                        fontSize: '14px', fontWeight: 600,
                        color: candidate.status === 'accepted'
                            ? 'var(--color-success)'
                            : 'var(--color-text-muted)',
                    }}
                >
                    {candidate.status === 'accepted' ? 'Accepted — override written.' : 'Rejected.'}
                </div>
            ) : (
                <>
                    <label
                        htmlFor={`corrected-${candidate.candidate_id}`}
                        style={{ ...cellLabel, display: 'block', marginBottom: '4px' }}
                    >
                        Override the suggestion (optional)
                    </label>
                    <input
                        id={`corrected-${candidate.candidate_id}`}
                        data-testid={`lemma-corrected-input-${candidate.candidate_id}`}
                        type="text"
                        value={correctedLemma}
                        onChange={e => setCorrectedLemma(e.target.value)}
                        placeholder={candidate.suggested_lemma || 'Correct lemma'}
                        style={{
                            width: '100%', boxSizing: 'border-box',
                            minHeight: '44px', padding: '10px 12px',
                            fontSize: '16px', borderRadius: '6px',
                            border: '1px solid var(--color-input-border)',
                            background: 'var(--color-input-bg)',
                            color: 'var(--color-text)',
                            marginBottom: '10px',
                        }}
                    />

                    {error && (
                        <div
                            data-testid={`lemma-row-error-${candidate.candidate_id}`}
                            style={{
                                fontSize: '13px', padding: '8px 10px',
                                borderRadius: '6px', marginBottom: '10px',
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
                            data-testid={`lemma-accept-${candidate.candidate_id}`}
                            type="button"
                            onClick={() => run('accept')}
                            disabled={busy !== null}
                            style={actionBtn('accept')}
                        >
                            {busy === 'accept' ? 'Accepting…' : 'Accept'}
                        </button>
                        <button
                            data-testid={`lemma-reject-${candidate.candidate_id}`}
                            type="button"
                            onClick={() => run('reject')}
                            disabled={busy !== null}
                            style={actionBtn('reject')}
                        >
                            {busy === 'reject' ? 'Rejecting…' : 'Reject'}
                        </button>
                    </div>
                </>
            )}
        </li>
    );
}

export function AdminLemmaQueuePage({ token, onClose }: Props) {
    const [candidates, setCandidates] = useState<LemmaCorrection[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);

    const load = useCallback(async () => {
        setLoading(true);
        setError(null);
        try {
            setCandidates(await listLemmaCorrections(token));
        } catch (e) {
            setError(e instanceof Error ? e.message : 'Failed to load review queue');
        } finally {
            setLoading(false);
        }
    }, [token]);

    useEffect(() => { void load(); }, [load]);

    // Replace the row in place rather than refetching — keeps scroll position and
    // lets the reviewer see what they just decided.
    const handleReviewed = useCallback((updated: LemmaCorrection) => {
        setCandidates(prev => prev.map(c =>
            c.candidate_id === updated.candidate_id ? updated : c
        ));
    }, []);

    return (
        <div style={{
            maxWidth: '820px', margin: '0 auto',
            padding: 'clamp(12px, 4vw, 20px)',
            color: 'var(--color-text)',
        }}>
            <div style={{
                display: 'flex', justifyContent: 'space-between',
                alignItems: 'center', gap: '8px', flexWrap: 'wrap',
                marginBottom: '4px',
            }}>
                <h2 style={{ margin: 0, fontSize: '20px' }}>Lemma corrections</h2>
                <button
                    type="button"
                    onClick={onClose}
                    aria-label="Close"
                    style={{
                        width: '44px', height: '44px',
                        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                        background: 'none', border: 'none',
                        color: 'var(--color-text-muted)',
                        fontSize: '20px', cursor: 'pointer',
                        touchAction: 'manipulation',
                    }}
                >
                    ✕
                </button>
            </div>

            <p style={{ fontSize: '13px', color: 'var(--color-text-muted)', marginTop: 0 }}>
                Accepting writes a <code>lemma_override</code> that the extractor trusts from then on.
                Rejecting records the decision and changes nothing.
            </p>

            {loading && (
                <p data-testid="lemma-queue-loading" style={{ fontSize: '14px', color: 'var(--color-text-subtle)' }}>
                    Loading…
                </p>
            )}

            {error && (
                <div
                    data-testid="lemma-queue-error"
                    style={{
                        fontSize: '14px', padding: '10px 12px', borderRadius: '6px',
                        border: '1px solid var(--color-danger-border)',
                        background: 'var(--color-danger-bg)',
                        color: 'var(--color-danger)',
                    }}
                >
                    {error}
                </div>
            )}

            {!loading && !error && candidates.length === 0 && (
                <p data-testid="lemma-queue-empty" style={{ fontSize: '14px', color: 'var(--color-text-muted)' }}>
                    Nothing pending. Reports from learners will show up here.
                </p>
            )}

            <ul style={{ padding: 0, margin: '16px 0 0' }}>
                {candidates.map(c => (
                    <CandidateRow
                        key={c.candidate_id}
                        candidate={c}
                        token={token}
                        onReviewed={handleReviewed}
                    />
                ))}
            </ul>
        </div>
    );
}
