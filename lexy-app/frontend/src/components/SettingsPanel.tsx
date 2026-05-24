import { useEffect, useRef, useState } from 'react';
import type { ThemeMode, UserPreferences, UserPreferencesUpdate } from '../api/settings';
import { fetchCategories } from '../api/search';
import { deleteAccount } from '../api/account';

interface Props {
    prefs: UserPreferences;
    onSave: (update: UserPreferencesUpdate) => Promise<void>;
    onClose: () => void;
    token: string;
}

const POPULAR_CHANNELS = [
    'DW Nachrichten', 'Galileo', 'ZDF', 'ARD', 'BBC News',
    'TED', 'Kurzgesagt', 'Y-Kollektiv', 'Arte', 'MDR',
    'Spiegel TV', 'RTL', 'N-TV', 'Phoenix', 'WDR',
];

function parseList(s: string): string[] {
    return s.split(',').map(x => x.trim()).filter(Boolean);
}
function formatList(arr: string[]): string {
    return arr.join(', ');
}

function TagInput({
    value,
    onChange,
    placeholder,
    suggestions,
    presets,
    presetLabel,
}: {
    value: string;
    onChange: (v: string) => void;
    placeholder: string;
    suggestions: string[];
    presets: string[];
    presetLabel: string;
}) {
    const [inputText, setInputText] = useState('');
    const [showSuggestions, setShowSuggestions] = useState(false);
    const inputRef = useRef<HTMLInputElement>(null);

    const current = parseList(value);

    function addTag(tag: string) {
        const trimmed = tag.trim();
        if (!trimmed || current.includes(trimmed)) return;
        onChange(formatList([...current, trimmed]));
        setInputText('');
        setShowSuggestions(false);
    }

    function removeTag(tag: string) {
        onChange(formatList(current.filter(t => t !== tag)));
    }

    function handleKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
        if ((e.key === 'Enter' || e.key === ',') && inputText.trim()) {
            e.preventDefault();
            addTag(inputText);
        } else if (e.key === 'Backspace' && !inputText && current.length > 0) {
            removeTag(current[current.length - 1]);
        }
    }

    const filtered = suggestions.filter(s =>
        inputText && s.toLowerCase().includes(inputText.toLowerCase()) && !current.includes(s),
    );

    return (
        <div>
            {/* Preset chips */}
            <div style={{ marginBottom: '8px' }}>
                <span style={{ fontSize: '11px', color: 'var(--color-text-subtle)', fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.04em', display: 'block', marginBottom: '5px' }}>
                    {presetLabel}
                </span>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '5px' }}>
                    {presets.map(p => {
                        const active = current.includes(p);
                        return (
                            <button
                                key={p}
                                type="button"
                                onClick={() => active ? removeTag(p) : addTag(p)}
                                style={{
                                    // Preset chips: compact but ≥32px tall for tap.
                                    minHeight: '32px',
                                    padding: '6px 12px', fontSize: '13px', borderRadius: '12px',
                                    border: active ? '1px solid var(--color-primary)' : '1px solid var(--color-border)',
                                    background: active ? 'var(--color-primary-soft)' : 'var(--color-surface)',
                                    color: active ? 'var(--color-primary-on-soft)' : 'var(--color-text-muted)',
                                    cursor: 'pointer', fontWeight: active ? 600 : 400,
                                    touchAction: 'manipulation',
                                }}
                            >
                                {active ? '✓ ' : ''}{p}
                            </button>
                        );
                    })}
                </div>
            </div>

            {/* Selected tags + input */}
            <div style={{ position: 'relative' }}>
                <div
                    onClick={() => inputRef.current?.focus()}
                    style={{
                        display: 'flex', flexWrap: 'wrap', gap: '5px', alignItems: 'center',
                        padding: '6px 8px', border: '1px solid var(--color-input-border)', borderRadius: '6px',
                        minHeight: '38px', cursor: 'text', background: 'var(--color-input-bg)',
                    }}
                >
                    {current.map(tag => (
                        <span key={tag} style={{
                            display: 'inline-flex', alignItems: 'center', gap: '3px',
                            padding: '2px 8px', background: 'var(--color-primary-soft)', borderRadius: '10px',
                            fontSize: '12px', color: 'var(--color-primary-on-soft)',
                        }}>
                            {tag}
                            <button
                                type="button"
                                onClick={e => { e.stopPropagation(); removeTag(tag); }}
                                style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--color-primary-on-soft)', fontSize: '13px', lineHeight: 1, padding: '0 1px' }}
                            >
                                ×
                            </button>
                        </span>
                    ))}
                    <input
                        data-testid="tag-input-field"
                        ref={inputRef}
                        type="text"
                        value={inputText}
                        placeholder={current.length === 0 ? placeholder : ''}
                        onChange={e => { setInputText(e.target.value); setShowSuggestions(true); }}
                        onKeyDown={handleKeyDown}
                        onBlur={() => setTimeout(() => setShowSuggestions(false), 150)}
                        onFocus={() => setShowSuggestions(true)}
                        // fontSize: 16px blocks iOS Safari focus-zoom.
                        style={{ border: 'none', outline: 'none', flex: 1, minWidth: '100px', fontSize: '16px', background: 'transparent', color: 'var(--color-text)' }}
                    />
                </div>

                {showSuggestions && filtered.length > 0 && (
                    <ul style={{
                        position: 'absolute', top: '100%', left: 0, right: 0,
                        background: 'var(--color-surface)', border: '1px solid var(--color-input-border)', borderTop: 'none',
                        borderRadius: '0 0 5px 5px', margin: 0, padding: 0, listStyle: 'none',
                        zIndex: 300, maxHeight: '160px', overflowY: 'auto',
                        boxShadow: 'var(--shadow-card)',
                    }}>
                        {filtered.map(s => (
                            <li key={s}
                                onMouseDown={() => addTag(s)}
                                style={{ padding: '8px 12px', cursor: 'pointer', fontSize: '13px', color: 'var(--color-text)' }}
                            >
                                {s}
                            </li>
                        ))}
                    </ul>
                )}
            </div>
            <span style={{ fontSize: '11px', color: 'var(--color-text-subtle)', marginTop: '3px', display: 'block' }}>
                Click presets or type and press Enter
            </span>
        </div>
    );
}

export function SettingsPanel({ prefs, onSave, onClose, token }: Props) {
    const [categories, setCategories] = useState<string[]>([]);
    const [likedGenres, setLikedGenres]     = useState('');
    const [likedChannels, setLikedChannels] = useState('');
    const [passiveReps, setPassiveReps]     = useState(prefs.passive_reps_for_known);
    const [activeReps, setActiveReps]       = useState(prefs.active_reps_for_known);
    const [knownColor, setKnownColor]             = useState(prefs.known_word_color);
    const [learningColor, setLearningColor]       = useState(prefs.learning_word_color);
    const [unknownColor, setUnknownColor]         = useState(prefs.unknown_word_color);
    const [remindersEnabled, setRemindersEnabled] = useState(prefs.reminders_enabled);
    const [themeMode, setThemeMode]               = useState<ThemeMode>(prefs.theme_mode);
    const [autoMarkKnown, setAutoMarkKnown]       = useState(prefs.auto_mark_known);
    const [saved, setSaved]       = useState(false);
    const [saveError, setSaveError] = useState(false);

    // Account deletion: two-step (confirm) so a misclick can't nuke the user.
    const [deleteConfirm, setDeleteConfirm] = useState(false);
    const [deleting, setDeleting]           = useState(false);
    const [deleteError, setDeleteError]     = useState<string | null>(null);
    // S3: password re-auth — the destructive delete now requires the current
    // password so a stolen bearer token can't delete the account on its own.
    const [deletePassword, setDeletePassword] = useState('');

    async function handleDeleteAccount(): Promise<void> {
        setDeleting(true);
        setDeleteError(null);
        try {
            await deleteAccount(token, deletePassword);
            // deleteAccount() already cleared local auth + dispatched
            // 'auth:expired'. Layout listens for that event and resets
            // token state + navigates back to '/'. No further action here.
        } catch (e) {
            setDeleting(false);
            setDeleteError((e as Error).message || 'Delete failed');
        }
    }

    const syncingFromProps = useRef(false);
    const savedTimerRef    = useRef<ReturnType<typeof setTimeout> | null>(null);
    // Holds values that need to be saved (set on every user change, cleared on successful save)
    const pendingSave = useRef<UserPreferencesUpdate | null>(null);
    // Always up-to-date ref so the unmount flush doesn't use a stale closure
    const onSaveRef = useRef(onSave);
    useEffect(() => { onSaveRef.current = onSave; }, [onSave]);

    useEffect(() => {
        fetchCategories().then(setCategories).catch(() => {});
    }, []);

    // Sync from parent prefs without triggering auto-save
    useEffect(() => {
        syncingFromProps.current = true;
        setLikedGenres(prefs.liked_genres.join(', '));
        setLikedChannels(prefs.liked_channels.join(', '));
        setPassiveReps(prefs.passive_reps_for_known);
        setActiveReps(prefs.active_reps_for_known);
        setKnownColor(prefs.known_word_color);
        setLearningColor(prefs.learning_word_color);
        setUnknownColor(prefs.unknown_word_color);
        setRemindersEnabled(prefs.reminders_enabled);
        setThemeMode(prefs.theme_mode);
        setAutoMarkKnown(prefs.auto_mark_known);
        const t = setTimeout(() => { syncingFromProps.current = false; }, 0);
        return () => clearTimeout(t);
    }, [prefs]);

    // Auto-save 600ms after any change; unmount flush below ensures the save
    // still fires if the panel is closed before the debounce completes.
    useEffect(() => {
        if (syncingFromProps.current) return;
        const values: UserPreferencesUpdate = {
            liked_genres:           parseList(likedGenres),
            liked_channels:         parseList(likedChannels),
            passive_reps_for_known: passiveReps,
            active_reps_for_known:  activeReps,
            known_word_color:       knownColor,
            learning_word_color:    learningColor,
            unknown_word_color:     unknownColor,
            reminders_enabled:      remindersEnabled,
            theme_mode:             themeMode,
            auto_mark_known:        autoMarkKnown,
        };
        pendingSave.current = values;
        const timer = setTimeout(() => {
            pendingSave.current = null;
            onSaveRef.current(values).then(() => {
                setSaved(true);
                setSaveError(false);
                if (savedTimerRef.current) clearTimeout(savedTimerRef.current);
                savedTimerRef.current = setTimeout(() => setSaved(false), 2000);
            }).catch(() => {
                setSaveError(true);
            });
        }, 600);
        return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [likedGenres, likedChannels, passiveReps, activeReps, knownColor, learningColor, unknownColor, remindersEnabled, themeMode, autoMarkKnown]);

    // Flush any pending save when the panel closes (timer was cancelled by cleanup above)
    useEffect(() => {
        return () => {
            if (pendingSave.current) {
                onSaveRef.current(pendingSave.current).catch(() => {});
            }
        };
    }, []);

    // Build channel suggestions from all known channels in prefs
    const knownChannelNames = [
        ...POPULAR_CHANNELS,
        ...Object.values(prefs.channel_names ?? {}),
        ...(prefs.followed_channels ?? []).map(id => (prefs.channel_names ?? {})[id]).filter(Boolean),
    ];
    const uniqueChannels = [...new Set(knownChannelNames)];

    const field: React.CSSProperties = { display: 'flex', flexDirection: 'column', gap: '6px', marginBottom: '16px' };
    const label: React.CSSProperties = { fontSize: '13px', fontWeight: 600, color: 'var(--color-text)' };
    // fontSize: 16px blocks iOS Safari focus-zoom. Applied to all number/text
    // inputs in this panel via the `input` helper. minHeight: 44 for tap.
    const input: React.CSSProperties = {
        padding: '8px 10px',
        border: '1px solid var(--color-input-border)',
        borderRadius: '5px',
        fontSize: '16px',
        minHeight: '44px',
        background: 'var(--color-input-bg)',
        color: 'var(--color-text)',
        boxSizing: 'border-box',
    };
    const sectionHeader: React.CSSProperties = { fontSize: '12px', fontWeight: 700, color: 'var(--color-text-subtle)', textTransform: 'uppercase' as const, letterSpacing: '0.05em', margin: '0 0 12px' };
    const checkboxLabel: React.CSSProperties = { fontSize: '13px', color: 'var(--color-text)', cursor: 'pointer' };

    return (
        <div
            data-testid="settings-panel"
            style={{
                border: '1px solid var(--color-border)',
                borderRadius: '8px',
                // Fluid side padding so 320–375px viewports keep more content room.
                padding: 'clamp(14px, 4vw, 24px)',
                background: 'var(--color-surface-muted)',
                marginBottom: '16px',
            }}
        >
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '20px' }}>
                <h2 style={{ margin: 0, fontSize: '16px', color: 'var(--color-text-strong)' }}>Preferences</h2>
                <button
                    data-testid="settings-close"
                    onClick={onClose}
                    style={{
                        // 44×44 finger-tappable close.
                        minWidth: '44px', minHeight: '44px',
                        display: 'flex', alignItems: 'center', justifyContent: 'center',
                        background: 'none', border: 'none', fontSize: '20px',
                        cursor: 'pointer', color: 'var(--color-text-muted)',
                        padding: 0,
                        touchAction: 'manipulation',
                    }}
                    aria-label="Close settings"
                >×</button>
            </div>

            {/* Recommendations */}
            <p style={{ ...sectionHeader, margin: '0 0 12px' }}>
                Recommendations
            </p>

            <div style={field}>
                <label style={label}>Liked genres</label>
                <TagInput
                    value={likedGenres}
                    onChange={setLikedGenres}
                    placeholder="e.g. News, Comedy…"
                    suggestions={categories}
                    presets={categories}
                    presetLabel="Quick picks"
                />
            </div>

            <div style={field}>
                <label style={label}>Liked channels</label>
                <TagInput
                    value={likedChannels}
                    onChange={setLikedChannels}
                    placeholder="e.g. DW Nachrichten…"
                    suggestions={uniqueChannels}
                    presets={POPULAR_CHANNELS.slice(0, 8)}
                    presetLabel="Popular channels"
                />
            </div>

            {/* SRS */}
            <p style={{ ...sectionHeader, margin: '18px 0 12px' }}>
                Spaced Repetition
            </p>

            <div style={{ display: 'flex', gap: '16px', flexWrap: 'wrap' }}>
                <div style={field}>
                    <label style={label}>Passive reps for "known"</label>
                    <input
                        data-testid="settings-passive-reps"
                        style={{ ...input, width: '88px' }} type="number" min={1} max={20}
                        value={passiveReps}
                        onChange={e => setPassiveReps(Math.max(1, Math.min(20, parseInt(e.target.value, 10) || 1)))} />
                </div>
                <div style={field}>
                    <label style={label}>Active reps for "known"</label>
                    <input
                        data-testid="settings-active-reps"
                        style={{ ...input, width: '88px' }} type="number" min={1} max={20}
                        value={activeReps}
                        onChange={e => setActiveReps(Math.max(1, Math.min(20, parseInt(e.target.value, 10) || 1)))} />
                </div>
            </div>

            {/* Word colors */}
            <p style={{ ...sectionHeader, margin: '18px 0 12px' }}>
                Word Colors
            </p>
            <div style={{ display: 'flex', gap: '20px', flexWrap: 'wrap', marginBottom: '16px' }}>
                {[
                    { label: 'Known',    value: knownColor,    onChange: setKnownColor },
                    { label: 'Learning', value: learningColor, onChange: setLearningColor },
                    { label: 'Unknown',  value: unknownColor,  onChange: setUnknownColor },
                ].map(({ label: lbl, value, onChange }) => (
                    <div key={lbl} style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '4px' }}>
                        <input type="color" value={value} onChange={e => onChange(e.target.value)}
                            aria-label={`${lbl} word colour`}
                            // 44×44 — color pickers need a finger-target too.
                            style={{ width: '44px', height: '44px', border: '1px solid var(--color-input-border)', borderRadius: '4px', cursor: 'pointer', padding: '2px', touchAction: 'manipulation' }} />
                        <span style={{ fontSize: '12px', color: 'var(--color-text-muted)', fontWeight: 600 }}>{lbl}</span>
                        <span style={{ fontSize: '11px', color: value, fontWeight: 700 }}>Aa</span>
                    </div>
                ))}
            </div>

            {/* Reminders */}
            <p style={{ ...sectionHeader, margin: '18px 0 10px' }}>
                Reminders
            </p>
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '10px' }}>
                <input id="reminders-toggle" type="checkbox" checked={remindersEnabled}
                    onChange={e => setRemindersEnabled(e.target.checked)}
                    style={{ width: '16px', height: '16px', cursor: 'pointer' }} />
                <label htmlFor="reminders-toggle" style={checkboxLabel}>
                    Show banner when reviews are due
                </label>
            </div>

            {/* Reading */}
            <p style={{ ...sectionHeader, margin: '18px 0 10px' }}>
                Reading
            </p>
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '10px' }}>
                <input id="auto-mark-toggle" type="checkbox" checked={autoMarkKnown}
                    onChange={e => setAutoMarkKnown(e.target.checked)}
                    style={{ width: '16px', height: '16px', cursor: 'pointer' }} />
                <label htmlFor="auto-mark-toggle" style={checkboxLabel}>
                    Auto-mark words as known when finishing a page
                </label>
            </div>

            {/* Appearance */}
            <p style={{ ...sectionHeader, margin: '18px 0 10px' }}>
                Appearance
            </p>
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '18px', flexWrap: 'wrap' }}>
                <label htmlFor="theme-mode-select" style={checkboxLabel}>
                    Theme
                </label>
                <select
                    id="theme-mode-select"
                    data-testid="theme-mode-select"
                    value={themeMode}
                    onChange={e => {
                        const next = e.target.value as ThemeMode;
                        setThemeMode(next);
                        // Apply data-theme immediately so the UI flips without
                        // waiting for the 600ms auto-save → prefs round-trip.
                        // App.tsx Layout's useResolvedTheme effect will re-apply
                        // the same value once prefs updates; idempotent.
                        // For "system", we resolve via prefers-color-scheme so
                        // the optimistic flip matches what Layout will compute.
                        if (typeof document !== 'undefined') {
                            let resolved: 'light' | 'dark';
                            if (next === 'light') resolved = 'light';
                            else if (next === 'dark') resolved = 'dark';
                            else {
                                const mql = typeof window !== 'undefined' && window.matchMedia
                                    ? window.matchMedia('(prefers-color-scheme: dark)')
                                    : null;
                                resolved = mql?.matches ? 'dark' : 'light';
                            }
                            document.documentElement.dataset.theme = resolved;
                        }
                    }}
                    style={{ ...input, minWidth: '140px', flex: '0 1 auto' }}
                >
                    <option value="system">System</option>
                    <option value="light">Light</option>
                    <option value="dark">Dark</option>
                </select>
            </div>

            <div style={{ height: '20px' }}>
                {saved      && <span style={{ fontSize: '12px', color: 'var(--color-success)' }}>Saved</span>}
                {saveError  && <span style={{ fontSize: '12px', color: 'var(--color-danger)' }}>Save failed — check your connection</span>}
            </div>

            {/* Account — destructive zone. Two-step confirm so a misclick can't
                delete the user. */}
            <p style={{ ...sectionHeader, margin: '24px 0 10px', color: 'var(--color-danger)' }}>
                Account
            </p>
            <div style={{
                border: '1px solid var(--color-danger-border)',
                background: 'var(--color-danger-bg)',
                borderRadius: '6px',
                padding: '12px 14px',
                marginBottom: '10px',
            }}>
                <p style={{ margin: '0 0 8px', fontSize: '13px', color: 'var(--color-text)', lineHeight: 1.45 }}>
                    Deleting your account is permanent. Your learning history,
                    SRS cards, uploaded books, reading selections, chat
                    sessions, and saved preferences will be removed.
                    Shared catalog data (words, channels, videos) is kept.
                </p>
                <p style={{ margin: '0 0 10px', fontSize: '12px', color: 'var(--color-text-muted)' }}>
                    See the <a href="/privacy" style={{ color: 'var(--color-primary)' }}>privacy policy</a> for what is stored and how to contact us.
                </p>

                {!deleteConfirm ? (
                    <button
                        data-testid="account-delete-start"
                        onClick={() => { setDeleteError(null); setDeletePassword(''); setDeleteConfirm(true); }}
                        style={{
                            padding: '10px 16px',
                            minHeight: '44px',
                            border: '1px solid var(--color-danger)',
                            background: 'var(--color-surface)',
                            color: 'var(--color-danger)',
                            borderRadius: '5px',
                            fontSize: '14px',
                            fontWeight: 600,
                            cursor: 'pointer',
                            touchAction: 'manipulation',
                        }}
                    >
                        Delete account
                    </button>
                ) : (
                    <div>
                        <label
                            htmlFor="account-delete-password"
                            style={{
                                display: 'block', fontSize: '13px', fontWeight: 600,
                                color: 'var(--color-text)', marginBottom: '6px',
                            }}
                        >
                            Enter your password to confirm
                        </label>
                        <input
                            id="account-delete-password"
                            data-testid="account-delete-password"
                            type="password"
                            autoComplete="current-password"
                            value={deletePassword}
                            onChange={e => setDeletePassword(e.target.value)}
                            disabled={deleting}
                            style={{
                                display: 'block', width: '100%', maxWidth: '280px',
                                minHeight: '44px', padding: '8px 10px', marginBottom: '10px',
                                border: '1px solid var(--color-border)', borderRadius: '5px',
                                background: 'var(--color-surface)', color: 'var(--color-text)',
                                fontSize: '14px', boxSizing: 'border-box',
                            }}
                        />
                        <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
                            <button
                                data-testid="account-delete-confirm"
                                onClick={handleDeleteAccount}
                                disabled={deleting || !deletePassword}
                                style={{
                                    padding: '10px 16px',
                                    minHeight: '44px',
                                    border: 'none',
                                    background: 'var(--color-danger)',
                                    color: '#fff',
                                    borderRadius: '5px',
                                    fontSize: '14px',
                                    fontWeight: 700,
                                    cursor: (deleting || !deletePassword) ? 'not-allowed' : 'pointer',
                                    touchAction: 'manipulation',
                                    opacity: (deleting || !deletePassword) ? 0.6 : 1,
                                }}
                            >
                                {deleting ? 'Deleting…' : 'Yes, permanently delete'}
                            </button>
                            <button
                                data-testid="account-delete-cancel"
                                onClick={() => { setDeleteConfirm(false); setDeletePassword(''); }}
                                disabled={deleting}
                                style={{
                                    padding: '10px 16px',
                                    minHeight: '44px',
                                    border: '1px solid var(--color-border)',
                                    background: 'var(--color-surface)',
                                    color: 'var(--color-text)',
                                    borderRadius: '5px',
                                    fontSize: '14px',
                                    fontWeight: 600,
                                    cursor: 'pointer',
                                    touchAction: 'manipulation',
                                }}
                            >
                                Cancel
                            </button>
                        </div>
                    </div>
                )}
                {deleteError && (
                    <p style={{ margin: '8px 0 0', fontSize: '12px', color: 'var(--color-danger)' }}>
                        {deleteError}
                    </p>
                )}
            </div>
        </div>
    );
}
