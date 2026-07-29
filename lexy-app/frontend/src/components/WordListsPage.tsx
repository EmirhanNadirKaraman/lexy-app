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
    WordListItemType,
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

// Shown only on resolved entries — an unresolved surface has no catalog row to
// describe. Borrows the surrounding pill's colour via `currentColor` so it
// stays legible on all five status backgrounds without new palette entries.
const TYPE_BADGE: React.CSSProperties = {
    fontSize: '10px',
    fontWeight: 700,
    letterSpacing: '0.03em',
    textTransform: 'uppercase',
    marginLeft: '6px',
    padding: '1px 5px',
    borderRadius: '4px',
    border: '1px solid currentColor',
    opacity: 0.75,
};

/**
 * Entries per page — both what is fetched and what is mounted.
 *
 * The seeded built-in lists hold 4,087 and 5,035 entries. Asking for all of
 * them costs 406 KB / 514 KB of JSON and mounts thousands of DOM nodes
 * synchronously, which is a visible freeze on a phone. Sending this as `limit`
 * bounds both at once: the response is ~22 KB and only a page is ever
 * rendered.
 *
 * Must stay within the backend's `limit` range (1–1000).
 */
const ENTRY_CHUNK = 200;

/**
 * Confirm before marking more than this many words at once.
 *
 * Chosen well below the backend's 500-per-call cap so the dialog fires before
 * the cap ever does: a user who confirms should get their whole request, not a
 * surprise chunk. Small user lists never see it.
 */
const MARK_CONFIRM_THRESHOLD = 200;

const SYSTEM_BADGE: React.CSSProperties = {
    ...TYPE_BADGE,
    marginLeft: '8px',
    opacity: 1,
    color: 'var(--color-accent)',
};

const SYSTEM_HELP =
    'Built-in list — shared with everyone and read-only. Your progress on it is still your own.';

/**
 * Display-only label for a seeded system list.
 *
 * The backend name is the seeding idempotency key (`ON CONFLICT (name) WHERE
 * is_system`), so renaming it there would fork the list on the next seed. Where
 * the stored name undersells the content, override it here instead. "Top German
 * Words" is 46% phrase-typed because column 0 keeps noun articles — `das Haus`
 * binds to a phrase_table collocation, which is correct German (the article is
 * part of the learning unit) but not what "Words" suggests.
 */
const SYSTEM_DISPLAY_NAME: Record<string, string> = {
    'Top German Words': 'Top German Words & Phrases',
};

function displayName(list: { name: string; is_system?: boolean }): string {
    return (list.is_system && SYSTEM_DISPLAY_NAME[list.name]) || list.name;
}

const TYPE_HELP: Record<WordListItemType, string> = {
    word: 'Single word — tracked in the word catalog.',
    phrase: 'Phrase or verb pattern — tracked in the phrase catalog.',
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
const listStyle: React.CSSProperties = {
    listStyle: 'none', margin: 0, padding: 0, display: 'grid', gap: '8px',
};

const listRowStyle: React.CSSProperties = {
    display: 'flex', justifyContent: 'space-between', alignItems: 'center',
    gap: '10px', flexWrap: 'wrap', padding: '8px 12px',
    borderRadius: '6px', background: 'var(--color-surface-sunken)',
    border: '1px solid var(--color-border-subtle)',
};

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
    const [loadingMore, setLoadingMore] = useState(false);
    const [moreError, setMoreError] = useState<string | null>(null);
    const [name, setName] = useState('');
    const [text, setText] = useState('');
    const [creating, setCreating] = useState(false);
    const [marking, setMarking] = useState(false);
    const [createError, setCreateError] = useState<string | null>(null);
    const [loadError, setLoadError] = useState<string | null>(null);
    const [actionError, setActionError] = useState<string | null>(null);
    const [notice, setNotice] = useState<string | null>(null);

    /**
     * Swap the open list and reset its paging state.
     *
     * Every `setDetail` that *replaces* a list goes through here, so a stale
     * "loading" spinner or a failed-page error can never outlive the list it
     * belongs to — opening another list, refreshing after mark-learning, and
     * closing all reset together. Resetting at each call site instead would
     * work until the next one forgets.
     *
     * `handleShowMore` is the one deliberate exception: it *extends* the open
     * list rather than replacing it, so it updates `entries` in place and must
     * not clear the state it is itself driving.
     *
     * `inFlightPagesRef` is deliberately NOT reset here — a request that is
     * still running is still running, and dropping its key would let its exact
     * duplicate be issued on the way back to this list. It drains itself.
     */
    function showDetail(next: WordListDetail | null) {
        setDetail(next);
        openListIdRef.current = next?.list_id ?? null;
        setLoadingMore(false);
        setMoreError(null);
    }
    const fileInputRef = useRef<HTMLInputElement>(null);
    /**
     * Which list is open right now, readable from an in-flight callback.
     *
     * `handleShowMore` awaits a fetch; by the time it settles the user may have
     * opened a different list. The `detail` in its closure is the old one, so
     * the ref is what tells a late response that its list is gone and it must
     * not write loading/error state over the new list's.
     */
    const openListIdRef = useRef<number | null>(null);
    /**
     * Keys of the page requests currently in flight — see `handleShowMore`.
     *
     * A set rather than a single slot, and deliberately NOT cleared by
     * `showDetail`. Both follow from the same requirement: block a duplicate
     * request for the same list and offset, without ever blocking a *different*
     * one. A single slot would be overwritten the moment a second list started
     * paging, so switching away and back while the first request still hangs
     * would let its exact duplicate through; clearing on list switch would do
     * the same. Each request only ever removes its own key, so the set drains
     * itself and every other pending page keeps its guard.
     */
    const inFlightPagesRef = useRef<Set<string>>(new Set());

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
            showDetail(created);
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
            showDetail(await getWordList(token, listId, { limit: ENTRY_CHUNK, offset: 0 }));
        } catch (err: unknown) {
            setActionError(err instanceof Error ? err.message : 'Unknown error');
        }
    }

    /**
     * Fetch the next page of entries and append it.
     *
     * Offset is the number of entries already loaded, so pages line up with the
     * backend's positional slice however many times this runs, and the slice
     * being positional is why ambiguous and unresolved entries arrive in their
     * place rather than being filtered out of a page.
     *
     * The response's own `total`/`counts` are ignored. They are whole-list on
     * every page, so keeping the ones the list was opened with costs nothing
     * and guarantees the badges never disagree with each other because two
     * pages happened to observe different moments.
     */
    async function handleShowMore() {
        if (!detail) return;
        const listId = detail.list_id;
        const offset = detail.entries.length;
        // Keyed on what the request actually asks for, and held in a ref rather
        // than in `loadingMore`: two clicks in one frame both read the
        // pre-click render's closure, where that state is still false. The
        // disabled attribute is what stops that in practice — this is the guard
        // that holds if a commit lags behind the second click, and it is scoped
        // so an unrelated list's pending page can never block this one.
        const key = `${listId}:${offset}:${ENTRY_CHUNK}`;
        if (inFlightPagesRef.current.has(key)) return;
        inFlightPagesRef.current.add(key);
        setLoadingMore(true);
        setMoreError(null);
        try {
            const page = await getWordList(token, listId, { limit: ENTRY_CHUNK, offset });
            setDetail(prev =>
                // A page for a list the user has navigated away from must not
                // be appended to whichever list is open now.
                prev && prev.list_id === listId
                    ? { ...prev, entries: [...prev.entries, ...page.entries] }
                    : prev,
            );
        } catch (err: unknown) {
            // Entries already loaded stay mounted — a failed page costs the
            // user nothing they had, and the control stays available to retry.
            if (openListIdRef.current === listId) {
                setMoreError(err instanceof Error ? err.message : 'Unknown error');
            }
        } finally {
            // Only ever this request's own key.
            inFlightPagesRef.current.delete(key);
            // A page for a list that is no longer open must not clear the
            // spinner belonging to the list that is.
            if (openListIdRef.current === listId) setLoadingMore(false);
        }
    }

    async function handleMarkLearning() {
        if (!detail) return;
        // Confirm before a bulk add. Marking is irreversible in practice —
        // auto-promotion is one-way and there is no un-mark — so a built-in
        // list with thousands of unknown words is a click away from burying
        // the user's review queue. Small lists are unaffected.
        if (unknownCount > MARK_CONFIRM_THRESHOLD) {
            const ok = window.confirm(
                `This will add ${unknownCount} words to your reviews. Continue?`,
            );
            if (!ok) return;
        }
        setMarking(true);
        setActionError(null);
        setNotice(null);
        try {
            const result = await markUnknownAsLearning(token, detail.list_id);
            // Back to the first page: statuses have changed list-wide, so
            // re-reading only the pages already loaded would show a mix.
            showDetail(await getWordList(token, detail.list_id, { limit: ENTRY_CHUNK, offset: 0 }));
            const skipped = result.skipped_unresolved + result.skipped_ambiguous;
            const remaining = result.remaining ?? 0;
            setNotice(
                `Marked ${result.marked} word${result.marked === 1 ? '' : 's'} as learning.` +
                (skipped > 0 ? ` Skipped ${skipped} that could not be matched to a single dictionary entry.` : '') +
                // The backend caps each call, so say plainly that the work is
                // unfinished and that clicking again continues it.
                (result.capped ? ` ${remaining} remaining — click again to continue.` : ''),
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
            if (detail?.list_id === listId) showDetail(null);
        } catch (err: unknown) {
            setActionError(err instanceof Error ? err.message : 'Unknown error');
        }
    }

    const unknownCount = detail?.counts.unknown ?? 0;

    // `total` is whole-list on every page; `entries` is only what has been
    // fetched. Comparing the two is what makes the footer say "200 of 5,035"
    // instead of the "200 of 200" a paged response would otherwise produce.
    const hasMore = detail !== null && detail.entries.length < detail.total;

    // Split on the backend's `is_system` flag. Hiding Delete for these is a
    // courtesy — the guarantee is server-side (a system list has no owner, so
    // the ownership-filtered delete can never match it and answers 404).
    const systemLists = lists.filter(l => l.is_system);
    const myLists = lists.filter(l => !l.is_system);

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

            {/* Built-in lists — shared, read-only. Rendered first because a new
                user has nothing of their own yet, so this is the only thing on
                the page that gives them somewhere to start. */}
            {systemLists.length > 0 && (
                <div data-testid="word-list-system-section" style={{ marginTop: '28px' }}>
                    <h3 style={{ fontSize: '14px', margin: '0 0 4px', color: 'var(--color-text-strong)' }}>
                        Built-in lists
                    </h3>
                    <p style={{ margin: '0 0 10px', fontSize: '12px', color: 'var(--color-text-muted)' }}>
                        Shared, read-only vocabulary lists. Your progress on them is your own.
                    </p>
                    <ul style={listStyle}>
                        {systemLists.map(l => (
                            <li key={l.list_id} style={listRowStyle}>
                                <span style={{ fontSize: '14px' }}>
                                    {displayName(l)}
                                    <span
                                        data-testid={`word-list-system-badge-${l.list_id}`}
                                        title={SYSTEM_HELP}
                                        style={SYSTEM_BADGE}
                                    >
                                        Built-in
                                    </span>{' '}
                                    <span style={{ color: 'var(--color-text-muted)', fontSize: '12px' }}>
                                        ({l.total} word{l.total === 1 ? '' : 's'}, {l.language})
                                    </span>
                                </span>
                                {/* No Delete: the backend refuses it (404) for a system
                                    list, so offering the control would only produce an
                                    error. Open/export/mark-learning all still work. */}
                                <span style={{ display: 'flex', gap: '8px' }}>
                                    <button
                                        data-testid={`word-list-open-${l.list_id}`}
                                        onClick={() => handleOpen(l.list_id)}
                                        style={buttonStyle}
                                    >
                                        Open
                                    </button>
                                </span>
                            </li>
                        ))}
                    </ul>
                </div>
            )}

            {/* Saved lists */}
            {(myLists.length > 0 || systemLists.length > 0) && (
                <div style={{ marginTop: '28px' }}>
                    <h3 style={{ fontSize: '14px', margin: '0 0 10px', color: 'var(--color-text-strong)' }}>
                        Your lists
                    </h3>
                    {myLists.length === 0 && (
                        <p
                            data-testid="word-list-mine-empty"
                            style={{ margin: '0 0 10px', fontSize: '13px', color: 'var(--color-text-muted)' }}
                        >
                            You haven't created a list yet. Paste or upload one above.
                        </p>
                    )}
                    <ul style={listStyle}>
                        {myLists.map(l => (
                            <li key={l.list_id} style={listRowStyle}>
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
                        {displayName(detail)}
                        {detail.is_system && (
                            <span
                                data-testid="word-list-detail-system-badge"
                                title={SYSTEM_HELP}
                                style={SYSTEM_BADGE}
                            >
                                Built-in
                            </span>
                        )}
                    </h3>

                    {detail.is_system && (
                        <p
                            data-testid="word-list-detail-system-note"
                            style={{
                                margin: '0 0 12px', fontSize: '13px', lineHeight: 1.4,
                                color: 'var(--color-text-muted)',
                            }}
                        >
                            This is a built-in list, shared with everyone and read-only. Marking
                            words as learning still records progress on your account only.
                        </p>
                    )}

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
                                {entry.item_id !== null && (
                                    <span
                                        data-testid={`word-list-type-${entry.surface}`}
                                        title={TYPE_HELP[entry.item_type]}
                                        style={TYPE_BADGE}
                                    >
                                        {entry.item_type}
                                    </span>
                                )}
                                <span style={{ fontSize: '11px', opacity: 0.8, marginLeft: '6px' }}>
                                    {entry.status}
                                </span>
                            </li>
                        ))}
                    </ul>

                    {/* Only while entries remain unfetched. A list that fits in
                        one page shows no extra chrome at all, and the control
                        disappears once the last page lands. Deliberately no
                        "Show all": that would put the whole 514 KB payload —
                        and the freeze — one click away. */}
                    {hasMore && (
                        <div
                            data-testid="word-list-more"
                            style={{
                                display: 'flex', alignItems: 'center', gap: '10px',
                                flexWrap: 'wrap', marginTop: '12px',
                            }}
                        >
                            <span style={{ fontSize: '12px', color: 'var(--color-text-muted)' }}>
                                Showing {detail.entries.length.toLocaleString()} of{' '}
                                {detail.total.toLocaleString()} entries
                            </span>
                            <button
                                data-testid="word-list-show-more"
                                onClick={handleShowMore}
                                disabled={loadingMore}
                                aria-busy={loadingMore}
                                style={buttonStyle}
                            >
                                {loadingMore ? 'Loading…' : 'Show more'}
                            </button>
                        </div>
                    )}

                    {/* Outside the block above so a failure is still readable if
                        the last page is the one that failed. */}
                    {moreError && (
                        <p data-testid="word-list-more-error" role="alert" style={errorStyle}>
                            {moreError}
                        </p>
                    )}
                </div>
            )}
        </div>
    );
}
