import { useState, useEffect, useRef } from 'react';
import {
    createWordList,
    listWordLists,
    getWordList,
    exportWordList,
    markUnknownAsLearning,
    deleteWordList,
    parseWordInput,
    MAX_LIST_WORDS,
} from '../api/wordLists';
import type {
    WordListDetail,
    WordListSummary,
    WordListEntryStatus,
} from '../api/wordLists';

interface Props {
    token: string;
    language: string;
    onClose: () => void;
}

// Status → theme variables only. `ambiguous` uses the warning ramp and
// `unresolved` the danger ramp so the two unbound states are told apart at a
// glance: ambiguous means "we found several senses", unresolved means "we
// found none" — different user actions follow from each.
const STATUS_STYLE: Record<WordListEntryStatus, React.CSSProperties> = {
    known: {
        background: 'var(--color-success-bg)',
        color: 'var(--color-success)',
        border: '1px solid var(--color-success-border)',
    },
    learning: {
        background: 'var(--color-primary-soft)',
        color: 'var(--color-primary-on-soft)',
        border: '1px solid var(--color-border-accent)',
    },
    unknown: {
        background: 'var(--color-surface-sunken)',
        color: 'var(--color-text-muted)',
        border: '1px solid var(--color-border-subtle)',
    },
    unresolved: {
        background: 'var(--color-danger-bg)',
        color: 'var(--color-danger)',
        border: '1px solid var(--color-danger-border)',
    },
    ambiguous: {
        background: 'var(--color-warning-bg)',
        color: 'var(--color-warning)',
        border: '1px solid var(--color-warning-border)',
    },
};

const STATUS_ORDER: WordListEntryStatus[] = [
    'known', 'learning', 'unknown', 'unresolved', 'ambiguous',
];

const STATUS_HELP: Record<WordListEntryStatus, string> = {
    known: 'Already marked as known.',
    learning: 'Currently in your review rotation.',
    unknown: 'In the dictionary, not yet learned.',
    unresolved: 'No dictionary match in this language.',
    ambiguous: 'Several dictionary entries match — we did not guess which one you meant.',
};

// 44px minimum so every control is finger-tappable (mobile polish rules).
const buttonStyle: React.CSSProperties = {
    minHeight: '44px',
    padding: '0 16px',
    borderRadius: '6px',
    border: '1px solid var(--color-border-accent)',
    background: 'var(--color-surface)',
    color: 'var(--color-primary-on-soft)',
    fontSize: '14px',
    fontWeight: 600,
    cursor: 'pointer',
    touchAction: 'manipulation',
};

const primaryButtonStyle: React.CSSProperties = {
    ...buttonStyle,
    background: 'var(--color-primary)',
    color: 'var(--color-primary-text)',
    border: '1px solid var(--color-primary)',
};

const errorStyle: React.CSSProperties = {
    margin: '12px 0 0',
    padding: '10px 12px',
    borderRadius: '6px',
    fontSize: '13px',
    background: 'var(--color-danger-bg)',
    color: 'var(--color-danger)',
    border: '1px solid var(--color-danger-border)',
    overflowWrap: 'anywhere',
};

export function WordListsPage({ token, language, onClose }: Props) {
    const [lists, setLists] = useState<WordListSummary[]>([]);
    const [detail, setDetail] = useState<WordListDetail | null>(null);
    const [name, setName] = useState('');
    const [text, setText] = useState('');
    const [creating, setCreating] = useState(false);
    const [marking, setMarking] = useState(false);
    const [createError, setCreateError] = useState<string | null>(null);
    const [loadError, setLoadError] = useState<string | null>(null);
    const [actionError, setActionError] = useState<string | null>(null);
    const [notice, setNotice] = useState<string | null>(null);
    const fileInputRef = useRef<HTMLInputElement>(null);

    useEffect(() => {
        listWordLists(token)
            .then(setLists)
            .catch(e => setLoadError(e instanceof Error ? e.message : 'Unknown error'));
    }, [token]);

    const words = parseWordInput(text);
    const tooMany = words.length > MAX_LIST_WORDS;

    async function handleCreate(e: React.FormEvent) {
        e.preventDefault();
        setCreateError(null);
        setNotice(null);

        // Checked client-side so the user sees the problem before a round-trip;
        // the backend enforces the same cap and returns 422 regardless.
        if (words.length === 0) {
            setCreateError('Add at least one word.');
            return;
        }
        if (tooMany) {
            setCreateError(`Too many words — ${words.length} given, the limit is ${MAX_LIST_WORDS}.`);
            return;
        }

        setCreating(true);
        try {
            const created = await createWordList(
                token, name.trim() || 'Untitled list', language, words,
            );
            setDetail(created);
            setLists(prev => [
                {
                    list_id: created.list_id,
                    name: created.name,
                    language: created.language,
                    description: created.description,
                    created_at: created.created_at,
                    total: created.total,
                },
                ...prev,
            ]);
            setText('');
            setName('');
        } catch (err: unknown) {
            setCreateError(err instanceof Error ? err.message : 'Unknown error');
        } finally {
            setCreating(false);
        }
    }

    function handleFile(e: React.ChangeEvent<HTMLInputElement>) {
        const file = e.target.files?.[0];
        if (!file) return;
        setCreateError(null);
        file.text()
            .then(content => {
                setText(content);
                if (!name.trim()) setName(file.name.replace(/\.txt$/i, ''));
            })
            .catch(() => setCreateError('Could not read that file.'));
        // Allow re-selecting the same file after an edit.
        e.target.value = '';
    }

    async function handleOpen(listId: number) {
        setActionError(null);
        setNotice(null);
        try {
            setDetail(await getWordList(token, listId));
        } catch (err: unknown) {
            setActionError(err instanceof Error ? err.message : 'Unknown error');
        }
    }

    async function handleMarkLearning() {
        if (!detail) return;
        setMarking(true);
        setActionError(null);
        setNotice(null);
        try {
            const result = await markUnknownAsLearning(token, detail.list_id);
            setDetail(await getWordList(token, detail.list_id));
            const skipped = result.skipped_unresolved + result.skipped_ambiguous;
            setNotice(
                `Marked ${result.marked} word${result.marked === 1 ? '' : 's'} as learning.` +
                (skipped > 0 ? ` Skipped ${skipped} that could not be matched to a single dictionary entry.` : ''),
            );
        } catch (err: unknown) {
            setActionError(err instanceof Error ? err.message : 'Unknown error');
        } finally {
            setMarking(false);
        }
    }

    async function handleDownload() {
        if (!detail) return;
        setActionError(null);
        try {
            const content = await exportWordList(token, detail.list_id);
            const url = URL.createObjectURL(new Blob([content], { type: 'text/plain' }));
            const anchor = document.createElement('a');
            anchor.href = url;
            anchor.download = `${detail.name.replace(/[^\w.-]+/g, '_') || 'word-list'}.txt`;
            document.body.appendChild(anchor);
            anchor.click();
            document.body.removeChild(anchor);
            URL.revokeObjectURL(url);
        } catch (err: unknown) {
            setActionError(err instanceof Error ? err.message : 'Unknown error');
        }
    }

    async function handleDelete(listId: number) {
        setActionError(null);
        try {
            await deleteWordList(token, listId);
            setLists(prev => prev.filter(l => l.list_id !== listId));
            if (detail?.list_id === listId) setDetail(null);
        } catch (err: unknown) {
            setActionError(err instanceof Error ? err.message : 'Unknown error');
        }
    }

    const unknownCount = detail?.counts.unknown ?? 0;

    return (
        <div style={{
            background: 'var(--color-surface)', color: 'var(--color-text)', borderRadius: '10px',
            padding: 'clamp(16px, 4vw, 24px)', maxWidth: '720px', margin: '0 auto',
            overflowWrap: 'anywhere', wordBreak: 'break-word',
        }}>
            <div style={{
                display: 'flex', justifyContent: 'space-between',
                alignItems: 'center', marginBottom: '20px',
            }}>
                <h2 style={{ margin: 0, fontSize: '18px', color: 'var(--color-text-strong)' }}>
                    Vocabulary Lists
                </h2>
                <button
                    data-testid="word-lists-close"
                    onClick={onClose}
                    aria-label="Close"
                    style={{
                        minWidth: '44px', minHeight: '44px', display: 'flex',
                        alignItems: 'center', justifyContent: 'center',
                        background: 'none', border: 'none', fontSize: '20px',
                        cursor: 'pointer', color: 'var(--color-text-muted)',
                        padding: 0, touchAction: 'manipulation',
                    }}
                >×</button>
            </div>

            {/* Create */}
            <form onSubmit={handleCreate}>
                <label
                    htmlFor="word-list-name"
                    style={{ display: 'block', fontSize: '13px', color: 'var(--color-text-muted)', marginBottom: '6px' }}
                >
                    List name
                </label>
                <input
                    id="word-list-name"
                    data-testid="word-list-name"
                    value={name}
                    onChange={e => setName(e.target.value)}
                    placeholder="e.g. Goethe B1 vocabulary"
                    style={{
                        width: '100%', boxSizing: 'border-box', minHeight: '44px',
                        padding: '10px 12px', borderRadius: '6px',
                        border: '1px solid var(--color-input-border)',
                        background: 'var(--color-input-bg)', color: 'var(--color-text)',
                        // 16px prevents iOS Safari from zooming on focus.
                        fontSize: '16px',
                    }}
                />

                <label
                    htmlFor="word-list-words"
                    style={{ display: 'block', fontSize: '13px', color: 'var(--color-text-muted)', margin: '14px 0 6px' }}
                >
                    Words — one per line (or comma-separated)
                </label>
                <textarea
                    id="word-list-words"
                    data-testid="word-list-input"
                    value={text}
                    onChange={e => setText(e.target.value)}
                    rows={7}
                    placeholder={'Haus\nStraße\nlaufen'}
                    style={{
                        width: '100%', boxSizing: 'border-box', padding: '10px 12px',
                        borderRadius: '6px', border: '1px solid var(--color-input-border)',
                        background: 'var(--color-input-bg)', color: 'var(--color-text)',
                        fontSize: '16px', fontFamily: 'inherit', resize: 'vertical',
                    }}
                />

                <div style={{
                    display: 'flex', gap: '10px', alignItems: 'center',
                    flexWrap: 'wrap', marginTop: '12px',
                }}>
                    <button type="submit" data-testid="word-list-create" disabled={creating} style={primaryButtonStyle}>
                        {creating ? 'Checking…' : 'Create list'}
                    </button>
                    <button
                        type="button"
                        data-testid="word-list-upload"
                        onClick={() => fileInputRef.current?.click()}
                        style={buttonStyle}
                    >
                        Upload .txt
                    </button>
                    <input
                        ref={fileInputRef}
                        data-testid="word-list-file"
                        type="file"
                        accept=".txt,text/plain"
                        onChange={handleFile}
                        style={{ display: 'none' }}
                    />
                    <span
                        data-testid="word-list-count"
                        style={{
                            fontSize: '13px',
                            color: tooMany ? 'var(--color-danger)' : 'var(--color-text-muted)',
                        }}
                    >
                        {words.length} word{words.length === 1 ? '' : 's'}
                        {tooMany ? ` — limit ${MAX_LIST_WORDS}` : ''}
                    </span>
                </div>

                {createError && (
                    <p data-testid="word-list-create-error" role="alert" style={errorStyle}>{createError}</p>
                )}
            </form>

            {loadError && (
                <p data-testid="word-list-load-error" role="alert" style={errorStyle}>{loadError}</p>
            )}

            {/* Saved lists */}
            {lists.length > 0 && (
                <div style={{ marginTop: '28px' }}>
                    <h3 style={{ fontSize: '14px', margin: '0 0 10px', color: 'var(--color-text-strong)' }}>
                        Your lists
                    </h3>
                    <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gap: '8px' }}>
                        {lists.map(l => (
                            <li
                                key={l.list_id}
                                style={{
                                    display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                                    gap: '10px', flexWrap: 'wrap', padding: '8px 12px',
                                    borderRadius: '6px', background: 'var(--color-surface-sunken)',
                                    border: '1px solid var(--color-border-subtle)',
                                }}
                            >
                                <span style={{ fontSize: '14px' }}>
                                    {l.name}{' '}
                                    <span style={{ color: 'var(--color-text-muted)', fontSize: '12px' }}>
                                        ({l.total} word{l.total === 1 ? '' : 's'}, {l.language})
                                    </span>
                                </span>
                                <span style={{ display: 'flex', gap: '8px' }}>
                                    <button
                                        data-testid={`word-list-open-${l.list_id}`}
                                        onClick={() => handleOpen(l.list_id)}
                                        style={buttonStyle}
                                    >
                                        Open
                                    </button>
                                    <button
                                        data-testid={`word-list-delete-${l.list_id}`}
                                        onClick={() => handleDelete(l.list_id)}
                                        style={{
                                            ...buttonStyle,
                                            color: 'var(--color-danger)',
                                            border: '1px solid var(--color-danger-border)',
                                        }}
                                    >
                                        Delete
                                    </button>
                                </span>
                            </li>
                        ))}
                    </ul>
                </div>
            )}

            {/* Detail */}
            {detail && (
                <div data-testid="word-list-detail" style={{ marginTop: '28px' }}>
                    <h3 style={{ fontSize: '14px', margin: '0 0 10px', color: 'var(--color-text-strong)' }}>
                        {detail.name}
                    </h3>

                    <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', marginBottom: '12px' }}>
                        {STATUS_ORDER.map(status => (
                            <span
                                key={status}
                                data-testid={`word-list-count-${status}`}
                                title={STATUS_HELP[status]}
                                style={{
                                    ...STATUS_STYLE[status], padding: '4px 10px',
                                    borderRadius: '999px', fontSize: '12px', fontWeight: 600,
                                }}
                            >
                                {status} {detail.counts[status] ?? 0}
                            </span>
                        ))}
                    </div>

                    {(detail.counts.ambiguous ?? 0) > 0 && (
                        <p
                            data-testid="word-list-ambiguous-note"
                            style={{
                                margin: '0 0 12px', padding: '10px 12px', fontSize: '13px',
                                lineHeight: 1.4, borderRadius: '6px',
                                background: 'var(--color-warning-bg)',
                                color: 'var(--color-warning)',
                                border: '1px solid var(--color-warning-border)',
                            }}
                        >
                            Some words match several dictionary entries (for example a noun and a
                            verb spelled the same). They are left unassigned rather than guessed,
                            so your progress never attaches to the wrong meaning.
                        </p>
                    )}

                    <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap', marginBottom: '12px' }}>
                        <button
                            data-testid="word-list-mark-learning"
                            onClick={handleMarkLearning}
                            disabled={marking || unknownCount === 0}
                            style={primaryButtonStyle}
                        >
                            {marking ? 'Marking…' : `Mark ${unknownCount} unknown as learning`}
                        </button>
                        <button data-testid="word-list-download" onClick={handleDownload} style={buttonStyle}>
                            Download .txt
                        </button>
                    </div>

                    {notice && (
                        <p
                            data-testid="word-list-notice"
                            style={{
                                ...errorStyle,
                                background: 'var(--color-success-bg)',
                                color: 'var(--color-success)',
                                border: '1px solid var(--color-success-border)',
                            }}
                        >
                            {notice}
                        </p>
                    )}
                    {actionError && (
                        <p data-testid="word-list-action-error" role="alert" style={errorStyle}>{actionError}</p>
                    )}

                    <ul style={{
                        listStyle: 'none', margin: '12px 0 0', padding: 0,
                        display: 'flex', flexWrap: 'wrap', gap: '8px',
                    }}>
                        {detail.entries.map(entry => (
                            <li
                                key={entry.id}
                                data-testid={`word-list-entry-${entry.surface}`}
                                title={STATUS_HELP[entry.status]}
                                style={{
                                    ...STATUS_STYLE[entry.status], padding: '6px 10px',
                                    borderRadius: '6px', fontSize: '14px',
                                }}
                            >
                                {entry.surface}
                                <span style={{ fontSize: '11px', opacity: 0.8, marginLeft: '6px' }}>
                                    {entry.status}
                                </span>
                            </li>
                        ))}
                    </ul>
                </div>
            )}
        </div>
    );
}
