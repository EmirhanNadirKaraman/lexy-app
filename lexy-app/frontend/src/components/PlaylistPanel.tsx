import { useRef, useState, useEffect } from 'react';
import type { PlaylistResult, PlaylistVideo, SearchResult, Suggestion } from '../types';
import { fetchItemRecommendations } from '../api/recommendations';
import { lookupWord, pickSingleOrFirst } from '../api/words';
import { fetchSuggestions } from '../api/suggest';
import { generatePlaylist, PlaylistSolverUnavailableError } from '../api/playlists';
import type { PlaylistAlgorithm } from '../api/playlists';
import { formatDuration } from '../utils/recommendationUtils';
import { LANGUAGE_OPTIONS } from '../config/languages';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface Target {
    item_id: number;
    display_text: string;
}

interface Props {
    token: string;
    language: string;
    onLanguageChange: (lang: string) => void;
    onWatch: (result: SearchResult) => void;
    onClose: () => void;
}

// ---------------------------------------------------------------------------
// PlaylistPanel
// ---------------------------------------------------------------------------

export function PlaylistPanel({ token, language, onLanguageChange, onWatch, onClose }: Props) {
    const [view, setView] = useState<'build' | 'result'>('build');
    const [targets, setTargets] = useState<Target[]>([]);
    const [maxVideos, setMaxVideos] = useState(5);
    // 'greedy' is the default, matching the backend. 'ilp' is opt-in: it runs a
    // solver, so the user asks for it explicitly.
    const [algorithm, setAlgorithm] = useState<PlaylistAlgorithm>('greedy');
    const [loadingRecs, setLoadingRecs] = useState(false);
    const [generating, setGenerating] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [result, setResult] = useState<PlaylistResult | null>(null);

    const [addInput, setAddInput] = useState('');
    const [addError, setAddError] = useState<string | null>(null);
    const [addLoading, setAddLoading] = useState(false);
    const inputRef = useRef<HTMLInputElement>(null);

    function addTarget(t: Target) {
        setTargets(prev => prev.some(x => x.item_id === t.item_id) ? prev : [...prev, t]);
    }

    function removeTarget(item_id: number) {
        setTargets(prev => prev.filter(t => t.item_id !== item_id));
    }

    async function handleLoadRecommended() {
        if (!language) return;
        setLoadingRecs(true);
        try {
            const res = await fetchItemRecommendations(token, language, 'word', 20);
            res.items.forEach(item => addTarget({ item_id: item.item_id, display_text: item.display_text }));
        } catch {
            // silently ignore — user can still add manually
        } finally {
            setLoadingRecs(false);
        }
    }

    async function handleAddWord() {
        const text = addInput.trim();
        if (!text || !language) return;
        setAddLoading(true);
        setAddError(null);
        try {
            // Non-interactive: any plausible match is fine for the playlist
            // builder. The picker UI handles ambiguity elsewhere.
            const resp = await lookupWord(token, text, language);
            const res = pickSingleOrFirst(resp);
            if (!res) {
                setAddError(`"${text}" not found in vocabulary`);
            } else {
                addTarget({ item_id: res.word_id, display_text: res.lemma });
                setAddInput('');
            }
        } catch {
            setAddError('Lookup failed. Try again.');
        } finally {
            setAddLoading(false);
            inputRef.current?.focus();
        }
    }

    async function handleGenerate() {
        if (!language || targets.length === 0) return;
        setGenerating(true);
        setError(null);
        try {
            const res = await generatePlaylist(
                token, targets.map(t => t.item_id), language, maxVideos, algorithm,
            );
            setResult(res);
            setView('result');
        } catch (e) {
            // The optimal planner has an optional backend. Point the user at the
            // fast one rather than showing the operator-facing PuLP message or a
            // generic failure they can do nothing about.
            setError(
                e instanceof PlaylistSolverUnavailableError
                    ? 'Optimal planning is unavailable right now. Switch to Fast and try again.'
                    : 'Failed to generate playlist. Try again.',
            );
        } finally {
            setGenerating(false);
        }
    }

    return (
        <div style={{
            border: '1px solid var(--color-border-accent)',
            borderRadius: '8px',
            padding: 'clamp(14px, 4vw, 20px)',
            background: 'var(--color-surface-muted)',
            marginBottom: '16px',
        }}>
            {/* Header */}
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '14px' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                    {view === 'result' && (
                        <button
                            onClick={() => setView('build')}
                            style={{
                                background: 'none', border: 'none', cursor: 'pointer',
                                color: 'var(--color-primary-on-soft)', fontSize: '14px', fontWeight: 600,
                                padding: '8px 4px', minHeight: '36px',
                                touchAction: 'manipulation',
                            }}
                        >
                            ← Back
                        </button>
                    )}
                    <h2 style={{ margin: 0, fontSize: '16px', color: 'var(--color-text-strong)' }}>
                        {view === 'build' ? 'Build Playlist' : 'Playlist'}
                    </h2>
                </div>
                <button
                    onClick={onClose}
                    style={{
                        background: 'none', border: 'none', fontSize: '20px',
                        cursor: 'pointer', color: 'var(--color-text-muted)',
                        minWidth: '44px', minHeight: '44px',
                        display: 'flex', alignItems: 'center', justifyContent: 'center',
                        padding: 0,
                        touchAction: 'manipulation',
                    }}
                    aria-label="Close"
                    data-testid="playlist-close"
                >
                    ×
                </button>
            </div>

            {view === 'build' && (
                <BuildView
                    language={language}
                    onLanguageChange={onLanguageChange}
                    targets={targets}
                    onRemoveTarget={removeTarget}
                    maxVideos={maxVideos}
                    onMaxVideosChange={setMaxVideos}
                    algorithm={algorithm}
                    onAlgorithmChange={setAlgorithm}
                    addInput={addInput}
                    onAddInputChange={setAddInput}
                    addLoading={addLoading}
                    addError={addError}
                    onAddWord={handleAddWord}
                    inputRef={inputRef}
                    loadingRecs={loadingRecs}
                    onLoadRecommended={handleLoadRecommended}
                    generating={generating}
                    error={error}
                    onGenerate={handleGenerate}
                />
            )}

            {view === 'result' && result && (
                <ResultView result={result} onWatch={onWatch} />
            )}
        </div>
    );
}

// ---------------------------------------------------------------------------
// BuildView
// ---------------------------------------------------------------------------

interface BuildViewProps {
    language: string;
    onLanguageChange: (lang: string) => void;
    targets: Target[];
    onRemoveTarget: (id: number) => void;
    maxVideos: number;
    onMaxVideosChange: (n: number) => void;
    algorithm: PlaylistAlgorithm;
    onAlgorithmChange: (a: PlaylistAlgorithm) => void;
    addInput: string;
    onAddInputChange: (s: string) => void;
    addLoading: boolean;
    addError: string | null;
    onAddWord: () => void;
    // React 19 widened useRef's return type to RefObject<T | null>; mirror it here.
    inputRef: React.RefObject<HTMLInputElement | null>;
    loadingRecs: boolean;
    onLoadRecommended: () => void;
    generating: boolean;
    error: string | null;
    onGenerate: () => void;
}

function BuildView({
    language, onLanguageChange,
    targets, onRemoveTarget,
    maxVideos, onMaxVideosChange,
    algorithm, onAlgorithmChange,
    addInput, onAddInputChange, addLoading, addError, onAddWord, inputRef,
    loadingRecs, onLoadRecommended,
    generating, error, onGenerate,
}: BuildViewProps) {
    const inputStyle: React.CSSProperties = {
        // fontSize: 16px blocks iOS Safari focus-zoom; 44px = touch target.
        padding: '8px 10px',
        border: '1px solid var(--color-input-border)',
        borderRadius: '5px',
        fontSize: '16px',
        background: 'var(--color-input-bg)',
        color: 'var(--color-text)',
        minHeight: '44px',
        boxSizing: 'border-box',
    };

    // Autocomplete suggestions for word input
    const [suggestions, setSuggestions] = useState<Suggestion[]>([]);
    const [showDropdown, setShowDropdown] = useState(false);
    const [activeIdx, setActiveIdx] = useState(-1);
    const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const abortRef = useRef<AbortController | null>(null);

    useEffect(() => {
        if (debounceRef.current) clearTimeout(debounceRef.current);
        if (!addInput.trim() || !language) {
            setSuggestions([]);
            setShowDropdown(false);
            return;
        }
        setSuggestions([{ word: addInput.trim(), score: 1, type: 'word' }]);
        setShowDropdown(true);
        debounceRef.current = setTimeout(async () => {
            abortRef.current?.abort();
            const ctrl = new AbortController();
            abortRef.current = ctrl;
            try {
                const data = await fetchSuggestions(addInput.trim(), language, ctrl.signal);
                const base: Suggestion = { word: addInput.trim(), score: 1, type: 'word' };
                setSuggestions([base, ...data.filter(s => s.word !== addInput.trim())]);
                setShowDropdown(true);
                setActiveIdx(-1);
            } catch { /* abort or network */ }
        }, 200);
        return () => { if (debounceRef.current) clearTimeout(debounceRef.current); };
    }, [addInput, language]);

    function selectSuggestion(word: string) {
        onAddInputChange(word);
        setSuggestions([]);
        setShowDropdown(false);
        setActiveIdx(-1);
        inputRef.current?.focus();
    }

    function handleKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            setActiveIdx(i => Math.min(i + 1, suggestions.length - 1));
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            setActiveIdx(i => Math.max(i - 1, -1));
        } else if (e.key === 'Enter') {
            e.preventDefault();
            if (activeIdx >= 0 && suggestions[activeIdx]) {
                selectSuggestion(suggestions[activeIdx].word);
            } else {
                onAddWord();
                setShowDropdown(false);
            }
        } else if (e.key === 'Escape') {
            setShowDropdown(false);
        }
    }

    return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
            {/* Language */}
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
                <select
                    value={language}
                    onChange={e => onLanguageChange(e.target.value)}
                    style={{ ...inputStyle, cursor: 'pointer' }}
                >
                    <option value="">Select language…</option>
                    {LANGUAGE_OPTIONS.map(l => (
                        <option key={l.code} value={l.code}>{l.label}</option>
                    ))}
                </select>

                <button
                    onClick={onLoadRecommended}
                    disabled={!language || loadingRecs}
                    style={{
                        // ≥36px secondary action — sits beside the language select.
                        padding: '8px 14px',
                        borderRadius: '5px',
                        border: '1px solid var(--color-border-accent)',
                        background: 'var(--color-surface)',
                        color: 'var(--color-primary-on-soft)',
                        fontSize: '13px',
                        fontWeight: 600,
                        cursor: !language || loadingRecs ? 'not-allowed' : 'pointer',
                        opacity: !language || loadingRecs ? 0.5 : 1,
                        minHeight: '36px',
                        touchAction: 'manipulation',
                    }}
                >
                    {loadingRecs ? 'Loading…' : 'Add recommended words'}
                </button>
            </div>

            {/* Word search with autocomplete */}
            <div style={{ position: 'relative' }}>
                <div style={{ display: 'flex', gap: '6px' }}>
                    <input
                        ref={inputRef}
                        style={{ ...inputStyle, flex: 1, minWidth: 0 }}
                        placeholder="Search words to add…"
                        value={addInput}
                        onChange={e => { onAddInputChange(e.target.value); }}
                        onKeyDown={handleKeyDown}
                        onBlur={() => setTimeout(() => setShowDropdown(false), 150)}
                        onFocus={() => suggestions.length > 0 && setShowDropdown(true)}
                        disabled={!language || addLoading}
                    />
                    <button
                        onClick={() => { onAddWord(); setShowDropdown(false); }}
                        disabled={!addInput.trim() || !language || addLoading}
                        style={{
                            padding: '10px 18px',
                            borderRadius: '5px',
                            border: 'none',
                            background: 'var(--color-primary)',
                            color: 'var(--color-primary-text)',
                            fontSize: '14px',
                            fontWeight: 600,
                            cursor: !addInput.trim() || !language || addLoading ? 'not-allowed' : 'pointer',
                            opacity: !addInput.trim() || !language || addLoading ? 0.5 : 1,
                            flexShrink: 0,
                            minHeight: '44px',
                            minWidth: '64px',
                            touchAction: 'manipulation',
                        }}
                        data-testid="playlist-add"
                    >
                        {addLoading ? '…' : 'Add'}
                    </button>
                </div>
                {addError && (
                    <p style={{ margin: '4px 0 0', fontSize: '12px', color: 'var(--color-danger)' }}>{addError}</p>
                )}

                {showDropdown && suggestions.length > 0 && (
                    <ul style={{
                        position: 'absolute', top: '100%', left: 0, right: '62px',
                        background: 'var(--color-surface)', border: '1px solid var(--color-input-border)', borderTop: 'none',
                        borderRadius: '0 0 5px 5px', margin: 0, padding: 0, listStyle: 'none',
                        zIndex: 200, maxHeight: '180px', overflowY: 'auto',
                        boxShadow: 'var(--shadow-card)',
                    }}>
                        {suggestions.map((s, i) => (
                            <li key={s.word}
                                onMouseDown={() => selectSuggestion(s.word)}
                                onMouseEnter={() => setActiveIdx(i)}
                                style={{
                                    padding: '9px 12px', cursor: 'pointer', fontSize: '14px',
                                    background: i === activeIdx ? 'var(--color-primary-soft)' : 'var(--color-surface)',
                                    color: 'var(--color-text)',
                                    display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                                }}
                            >
                                <span>{s.word}</span>
                                <span style={{ display: 'flex', gap: '6px', alignItems: 'center' }}>
                                    {s.type === 'phrase' && (
                                        <span style={{ fontSize: '10px', background: 'var(--color-warning-bg)', color: 'var(--color-warning)', borderRadius: '3px', padding: '1px 4px' }}>phrase</span>
                                    )}
                                    <span style={{ fontSize: '11px', color: 'var(--color-text-subtle)' }}>{Math.round(s.score * 100)}%</span>
                                </span>
                            </li>
                        ))}
                    </ul>
                )}
            </div>

            {/* Target chips */}
            {targets.length > 0 && (
                <div>
                    <p style={{ margin: '0 0 6px', fontSize: '11px', fontWeight: 700, color: 'var(--color-text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                        Targets ({targets.length})
                    </p>
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px' }}>
                        {targets.map(t => (
                            <span
                                key={t.item_id}
                                style={{
                                    display: 'inline-flex',
                                    alignItems: 'center',
                                    gap: '4px',
                                    padding: '3px 8px 3px 10px',
                                    background: 'var(--color-primary-soft)',
                                    borderRadius: '12px',
                                    fontSize: '13px',
                                    color: 'var(--color-primary-on-soft)',
                                    fontWeight: 500,
                                }}
                            >
                                {t.display_text}
                                <button
                                    onClick={() => onRemoveTarget(t.item_id)}
                                    style={{
                                        background: 'none',
                                        border: 'none',
                                        cursor: 'pointer',
                                        color: 'var(--color-primary-on-soft)',
                                        fontSize: '14px',
                                        lineHeight: 1,
                                        padding: '0 2px',
                                    }}
                                    aria-label={`Remove ${t.display_text}`}
                                >
                                    ×
                                </button>
                            </span>
                        ))}
                    </div>
                </div>
            )}

            {/* Max videos */}
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                <label style={{ fontSize: '13px', color: 'var(--color-text)', fontWeight: 600, whiteSpace: 'nowrap' }}>
                    Max videos
                </label>
                <input
                    type="number"
                    min={1}
                    max={10}
                    value={maxVideos}
                    onChange={e => onMaxVideosChange(Math.max(1, Math.min(10, parseInt(e.target.value, 10) || 1)))}
                    style={{ ...inputStyle, width: '64px' }}
                />
            </div>

            {/* Planner: greedy (default) vs optimal ILP. Copy is user-facing —
                "Fast"/"Optimal" rather than the algorithm names. */}
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
                <label
                    htmlFor="playlist-algorithm"
                    style={{ fontSize: '13px', color: 'var(--color-text)', fontWeight: 600, whiteSpace: 'nowrap' }}
                >
                    Planning
                </label>
                <select
                    id="playlist-algorithm"
                    data-testid="playlist-algorithm"
                    value={algorithm}
                    onChange={e => onAlgorithmChange(e.target.value as PlaylistAlgorithm)}
                    style={{ ...inputStyle, cursor: 'pointer' }}
                >
                    <option value="greedy">Fast</option>
                    <option value="ilp">Optimal (slower)</option>
                </select>
                <span style={{ fontSize: '12px', color: 'var(--color-text-subtle)' }}>
                    {algorithm === 'ilp'
                        ? 'Covers the most words possible within your video limit.'
                        : 'Good results, returns instantly.'}
                </span>
            </div>

            {error && (
                <p style={{ margin: 0, fontSize: '13px', color: 'var(--color-danger)' }}>{error}</p>
            )}

            {/* Generate */}
            <div>
                <button
                    onClick={onGenerate}
                    disabled={!language || targets.length === 0 || generating}
                    style={{
                        padding: '12px 24px',
                        background: 'var(--color-primary)',
                        color: 'var(--color-primary-text)',
                        border: 'none',
                        borderRadius: '6px',
                        fontSize: '14px',
                        fontWeight: 600,
                        cursor: !language || targets.length === 0 || generating ? 'not-allowed' : 'pointer',
                        opacity: !language || targets.length === 0 || generating ? 0.5 : 1,
                        minHeight: '44px',
                        touchAction: 'manipulation',
                    }}
                    data-testid="playlist-generate"
                >
                    {generating ? 'Generating…' : 'Generate playlist'}
                </button>
                {!language && (
                    <span style={{ marginLeft: '10px', fontSize: '12px', color: 'var(--color-text-subtle)' }}>Select a language first</span>
                )}
                {language && targets.length === 0 && (
                    <span style={{ marginLeft: '10px', fontSize: '12px', color: 'var(--color-text-subtle)' }}>Add at least one word</span>
                )}
            </div>
        </div>
    );
}

// ---------------------------------------------------------------------------
// ResultView
// ---------------------------------------------------------------------------

function ResultView({ result, onWatch }: { result: PlaylistResult; onWatch: (r: SearchResult) => void }) {
    const { coverage, videos } = result;

    return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
            {/* Coverage summary */}
            <div style={{ background: 'var(--color-surface)', border: '1px solid var(--color-border-accent)', borderRadius: '6px', padding: '12px 14px' }}>
                <div style={{ marginBottom: '6px' }}>
                    <div style={{ display: 'flex', height: '6px', borderRadius: '3px', overflow: 'hidden', background: 'var(--color-border)' }}>
                        <div style={{ width: `${coverage.coverage_pct}%`, background: 'var(--color-primary)', transition: 'width 0.4s' }} />
                    </div>
                </div>
                <div style={{ fontSize: '13px', color: 'var(--color-text)' }}>
                    <strong>{coverage.covered_count} of {coverage.target_count}</strong> target words covered
                    {' '}<span style={{ color: 'var(--color-text-muted)' }}>({coverage.coverage_pct}%)</span>
                    {' '}across <strong>{coverage.video_count}</strong> video{coverage.video_count !== 1 ? 's' : ''}
                </div>
                {coverage.uncovered_item_ids.length > 0 && (
                    <p style={{ margin: '4px 0 0', fontSize: '12px', color: 'var(--color-text-muted)' }}>
                        {coverage.uncovered_item_ids.length} word{coverage.uncovered_item_ids.length !== 1 ? 's' : ''} not found in any video
                    </p>
                )}
                {videos.length === 0 && (
                    <p style={{ margin: '4px 0 0', fontSize: '13px', color: 'var(--color-text-subtle)' }}>
                        No videos found for these words. Try different targets or a different language.
                    </p>
                )}
            </div>

            {/* Video cards */}
            {videos.map((v, i) => (
                <PlaylistVideoCard key={v.video_id} video={v} position={i + 1} onWatch={onWatch} />
            ))}
        </div>
    );
}

// ---------------------------------------------------------------------------
// PlaylistVideoCard
// ---------------------------------------------------------------------------

function PlaylistVideoCard({
    video, position, onWatch,
}: {
    video: PlaylistVideo;
    position: number;
    onWatch: (r: SearchResult) => void;
}) {
    const result: SearchResult = {
        video_id:       video.video_id,
        title:          video.title,
        thumbnail_url:  video.thumbnail_url,
        language:       video.language,
        start_time:     video.start_time,
        start_time_int: video.start_time_int,
        content:        video.content,
        surface_form:   null,
        match_type:     'playlist',
    };

    return (
        <div style={{
            display: 'flex',
            gap: '12px',
            background: 'var(--color-surface)',
            border: '1px solid var(--color-border-accent)',
            borderRadius: '8px',
            overflow: 'hidden',
            alignItems: 'stretch',
        }}>
            {/* Position number */}
            <div style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                width: '32px',
                flexShrink: 0,
                background: 'var(--color-surface-sunken)',
                color: 'var(--color-primary-on-soft)',
                fontSize: '13px',
                fontWeight: 700,
            }}>
                {position}
            </div>

            {/* Thumbnail — solid black so the YouTube poster fades in cleanly. */}
            <div style={{ position: 'relative', width: '120px', flexShrink: 0, background: '#000' }}>
                <img
                    src={video.thumbnail_url}
                    alt={video.title}
                    style={{ width: '100%', height: '100%', objectFit: 'cover', opacity: 0.9, display: 'block' }}
                />
                <span style={{
                    position: 'absolute', bottom: '4px', right: '4px',
                    background: 'rgba(0,0,0,0.75)', color: '#fff',
                    padding: '1px 4px', borderRadius: '3px', fontSize: '10px',
                }}>
                    {formatDuration(video.start_time)}
                </span>
            </div>

            {/* Info */}
            <div style={{ flex: 1, padding: '10px 10px 10px 0', display: 'flex', flexDirection: 'column', gap: '4px', minWidth: 0 }}>
                <div style={{
                    fontSize: '14px',
                    fontWeight: 600,
                    color: 'var(--color-text)',
                    lineHeight: 1.3,
                    display: '-webkit-box',
                    WebkitLineClamp: 2,
                    WebkitBoxOrient: 'vertical' as const,
                    overflow: 'hidden',
                }}>
                    {video.title}
                </div>
                <span style={{
                    display: 'inline-block',
                    padding: '2px 8px',
                    background: 'var(--color-primary-soft)',
                    color: 'var(--color-primary-on-soft)',
                    borderRadius: '10px',
                    fontSize: '11px',
                    fontWeight: 600,
                    alignSelf: 'flex-start',
                }}>
                    {video.covered_count} word{video.covered_count !== 1 ? 's' : ''}
                </span>
                <div style={{ marginTop: 'auto' }}>
                    <button
                        onClick={() => onWatch(result)}
                        style={{
                            // ≥36px tappable in playlist video row.
                            padding: '8px 16px',
                            background: 'var(--color-primary)',
                            color: 'var(--color-primary-text)',
                            border: 'none',
                            borderRadius: '5px',
                            fontSize: '13px',
                            fontWeight: 600,
                            cursor: 'pointer',
                            minHeight: '36px',
                            touchAction: 'manipulation',
                        }}
                    >
                        Watch
                    </button>
                </div>
            </div>
        </div>
    );
}
