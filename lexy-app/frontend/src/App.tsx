import { useState, useEffect, useRef } from 'react';
import { Routes, Route, NavLink, Outlet, useOutletContext, useNavigate, useLocation, Navigate } from 'react-router-dom';
import { SearchBar } from './components/SearchBar';
import { PlayerView } from './components/PlayerView';
import { LoginForm } from './components/LoginForm';
import { FreeChatPage } from './components/FreeChatPage';
import { GuidedChatPage } from './components/GuidedChatPage';
import { SettingsPanel } from './components/SettingsPanel';
import { PrivacyPage } from './components/PrivacyPage';
import { RecommendationsPanel } from './components/RecommendationsPanel';
import { PlaylistPanel } from './components/PlaylistPanel';
import { BookLibraryPage } from './components/BookLibraryPage';
import { BookReaderPage } from './components/BookReaderPage';
import { ReminderBanner } from './components/ReminderBanner';
import { SRSReviewPage } from './components/SRSReviewPage';
import { ReadingReviewPage } from './components/ReadingReviewPage';
import { ContentRequestPage } from './components/ContentRequestPage';
import { AdminLemmaQueuePage } from './components/AdminLemmaQueuePage';
import { NotificationContainer } from './components/NotificationToast';
import { useNotifications } from './hooks/useNotifications';
import { useSearch } from './hooks/useSearch';
import { useReminders } from './hooks/useReminders';
import { usePreferences } from './hooks/usePreferences';
import { useResolvedTheme } from './hooks/useResolvedTheme';
import { useViewport } from './hooks/useViewport';
import { useIsLemmaAdmin } from './hooks/useIsLemmaAdmin';
import { DEFAULT_LANGUAGE, languageLabel } from './config/languages';
import { getToken } from './auth';
import { AUTH_EXPIRED_EVENT } from './api/_http';
import { ErrorBoundary } from './components/ErrorBoundary';
import type { UserPreferences, UserPreferencesUpdate, ChannelAction, GenreAction } from './api/settings';
import type { SearchResult, BookDocument } from './types';

type AppCtx = {
  token: string | null;
  prefs: UserPreferences;
  savePreferences: (update: UserPreferencesUpdate) => Promise<void>;
  channelAction: (channelId: string, channelName: string, action: ChannelAction) => Promise<void>;
  genreAction: (genre: string, action: GenreAction) => Promise<void>;
  recLanguage: string;
  setRecLanguage: (l: string) => void;
};

function useAppCtx() {
  return useOutletContext<AppCtx>();
}

function Layout() {
  const [token, setToken] = useState<string | null>(getToken);
  const [recLanguage, setRecLanguage] = useState(() => localStorage.getItem('recLanguage') ?? '');
  const { prefs, savePreferences, channelAction, genreAction } = usePreferences(token);
  const { notifications, dismiss: dismissNotification } = useNotifications(token);
  const { summary: reminderSummary, showBanner: showReminderBanner, dismissBanner } =
    useReminders(token, prefs.reminders_enabled);
  const navigate = useNavigate();
  // T1.3: theme_mode (system|light|dark) is the source of truth; the
  // resolver follows `prefers-color-scheme` when mode === 'system' so iOS /
  // macOS dark switches at runtime without re-saving.
  const resolvedTheme = useResolvedTheme(prefs.theme_mode);
  const { isMobile } = useViewport();
  // Probe-based: `is_admin` is never sent to the client, so we ask the admin
  // endpoint whether it answers. Hides the nav link only — `require_admin` on
  // the routes themselves is the real gate.
  const isLemmaAdmin = useIsLemmaAdmin(token);

  // Apply theme via the [data-theme] attribute on <html>. CSS variables in
  // index.css flip when this changes, so component inline styles using
  // `var(--color-*)` auto-update without React re-renders. (#20)
  useEffect(() => {
    document.documentElement.dataset.theme = resolvedTheme;
    return () => { delete document.documentElement.dataset.theme; };
  }, [resolvedTheme]);

  // Centralised auth-expiry handler: api/_http.ts dispatches AUTH_EXPIRED_EVENT
  // whenever any request returns 401 (including the new detail='token_expired'
  // branch from backend/core/deps.py). _http.ts has already cleared the stored
  // token/email by the time this fires; we just sync React state and bounce
  // the user back to home so any protected page they're on re-renders empty.
  useEffect(() => {
    function onAuthExpired() {
      setToken(null);
      navigate('/');
    }
    window.addEventListener(AUTH_EXPIRED_EVENT, onAuthExpired);
    return () => window.removeEventListener(AUTH_EXPIRED_EVENT, onAuthExpired);
  }, [navigate]);

  const ctx: AppCtx = { token, prefs, savePreferences, channelAction, genreAction, recLanguage, setRecLanguage };

  const nl = ({ isActive }: { isActive: boolean }) => ({
    // Mobile: 36px min height keeps the top-nav row chunky enough to tap on iOS.
    padding: '8px 14px', borderRadius: '6px',
    border: '1px solid var(--color-border-accent)',
    background: isActive ? 'var(--color-primary-soft)' : 'var(--color-surface)',
    color: isActive ? 'var(--color-primary-on-soft)' : 'var(--color-primary-on-soft)',
    fontSize: '13px', fontWeight: 600 as const, cursor: 'pointer', textDecoration: 'none',
    display: 'inline-flex', alignItems: 'center',
    minHeight: '36px',
    touchAction: 'manipulation' as const,
  });

  const nlReview = ({ isActive }: { isActive: boolean }) => ({
    ...nl({ isActive }),
    border: '1px solid var(--color-success-border)',
    color: 'var(--color-success)',
    background: isActive ? 'var(--color-success-bg)' : 'var(--color-surface)',
  });

  return (
    <>
      <NotificationContainer notifications={notifications} onDismiss={dismissNotification} />
      <div style={{
        maxWidth: '900px',
        margin: '0 auto',
        // Mobile (#27a): tighter side gutters; add safe-area top inset so a
        // future iOS Capacitor wrap doesn't draw under the notch. The
        // breakpoint is duplicated as a literal in useViewport — keep them
        // aligned with --bp-md in index.css.
        padding: isMobile ? '12px 8px' : '24px 16px',
        paddingTop: isMobile
          ? 'calc(12px + var(--safe-top))'
          : 'calc(24px + var(--safe-top))',
        fontFamily: 'sans-serif',
        background: 'var(--color-bg)',
        minHeight: '100vh',
        color: 'var(--color-text)',
      }}>

        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px', flexWrap: 'wrap', gap: '10px' }}>
          <NavLink to="/" style={{ textDecoration: 'none', color: 'inherit' }}>
            <h1 style={{ fontSize: '24px', margin: 0 }}>Lexy Clone</h1>
          </NavLink>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
            {token && <NavLink to="/for-you" style={nl}>For You</NavLink>}
            {token && <NavLink to="/playlist" style={nl}>Playlist</NavLink>}
            {token && <NavLink to="/books" style={nl}>Books</NavLink>}
            {token && <NavLink to="/review" style={nlReview}>Review</NavLink>}
            {token && <NavLink to="/add-content" style={nl}>+ Add Content</NavLink>}
            {token && <NavLink to="/settings" style={nl}>Settings</NavLink>}
            {token && isLemmaAdmin && (
              <NavLink to="/admin/lemma-corrections" style={nl}>Lemma Queue</NavLink>
            )}
            <LoginForm
              token={token}
              onLogin={t => { setToken(t); navigate('/'); }}
              onLogout={() => { setToken(null); navigate('/'); }}
            />
          </div>
        </div>

        {token && showReminderBanner && reminderSummary && (
          <ReminderBanner
            summary={reminderSummary}
            onDismiss={dismissBanner}
            onOpenRecs={() => { dismissBanner(); navigate('/for-you'); }}
          />
        )}

        <ErrorBoundary>
          <Outlet context={ctx} />
        </ErrorBoundary>

        <footer
          style={{
            marginTop: '24px',
            paddingTop: '12px',
            borderTop: '1px solid var(--color-border-subtle)',
            fontSize: '12px',
            color: 'var(--color-text-muted)',
            textAlign: 'center',
          }}
        >
          <NavLink to="/privacy" style={{ color: 'var(--color-text-muted)', textDecoration: 'none' }}>
            Privacy
          </NavLink>
        </footer>
      </div>
    </>
  );
}

// ---- Pages ----

type HomeNavState = {
  recResult?: SearchResult;
  guidedTarget?: { itemId: number; itemType: string };
  showChat?: 'guided';
  searchTerm?: string;
};

function HomePage() {
  const { token } = useAppCtx();
  // Audit #7: corpus search is auth-only. Show a friendly prompt for
  // logged-out users instead of letting them hit a 401 wall mid-typing.
  // The Layout's top-right LoginForm provides the sign-in path. Authed
  // body is in HomePageAuthed so useSearch + downstream hooks only run
  // when a token actually exists (no rules-of-hooks divergence across
  // login state).
  if (!token) {
    return (
      <section
        aria-labelledby="home-signed-out-heading"
        style={{ padding: 'clamp(24px, 6vw, 48px) 0', textAlign: 'center' }}
      >
        <h2
          id="home-signed-out-heading"
          style={{ margin: 0, fontSize: 'clamp(20px, 4vw, 26px)', color: 'var(--color-text-strong)' }}
        >
          Sign in to start learning
        </h2>
        <p style={{
          margin: '12px auto 0',
          maxWidth: '520px',
          fontSize: '15px',
          color: 'var(--color-text-muted)',
          lineHeight: 1.5,
        }}>
          Search videos in context, build your vocabulary, and practice
          recognition + production with spaced repetition. Use the sign-in
          button at the top of the page to get started.
        </p>
      </section>
    );
  }
  return <HomePageAuthed />;
}

function HomePageAuthed() {
  const { token, prefs, recLanguage } = useAppCtx();
  const { terms, query, addTerm, removeTerm, results, total, loading, error, hasMore, loadMore } = useSearch(recLanguage || DEFAULT_LANGUAGE);
  const [resultIdx, setResultIdx] = useState(0);
  const [showChat, setShowChat] = useState<'free' | 'guided' | null>(null);
  const [recResult, setRecResult] = useState<SearchResult | null>(null);
  const [guidedTarget, setGuidedTarget] = useState<{ itemId: number; itemType: string } | null>(null);
  const location = useLocation();
  const navigate = useNavigate();
  const processedState = useRef(false);

  useEffect(() => {
    if (processedState.current) return;
    const state = location.state as HomeNavState | null;
    if (!state) return;
    processedState.current = true;
    if (state.recResult) setRecResult(state.recResult);
    if (state.guidedTarget) setGuidedTarget(state.guidedTarget);
    if (state.showChat) setShowChat(state.showChat);
    if (state.searchTerm) addTerm(state.searchTerm);
    window.history.replaceState(null, '');
  }, []);

  useEffect(() => { setResultIdx(0); setShowChat(null); setRecResult(null); setGuidedTarget(null); }, [query]);
  useEffect(() => { setShowChat(null); }, [resultIdx]);

  const currentResult = results[resultIdx] ?? null;
  const activeResult = recResult ?? currentResult;

  const handleNext = () => {
    if (resultIdx === results.length - 1 && hasMore) loadMore();
    setResultIdx(i => Math.min(i + 1, total - 1));
  };
  const handlePrev = () => setResultIdx(i => Math.max(0, i - 1));

  const wordColors = {
    known:    { color: prefs.known_word_color },
    learning: { color: prefs.learning_word_color },
    unknown:  { color: prefs.unknown_word_color },
  };

  return (
    <>
      <SearchBar
        terms={terms}
        onAddTerm={addTerm}
        onRemoveTerm={removeTerm}
        loading={loading}
        language={recLanguage || undefined}
      />
      {error && <p style={{ color: 'red', marginTop: '12px' }}>{error}</p>}

      {loading && !currentResult && (
        <p style={{ color: '#888', marginTop: '24px', textAlign: 'center' }}>Searching…</p>
      )}
      {!loading && query && results.length === 0 && (
        <p style={{ color: '#888', marginTop: '24px', textAlign: 'center' }}>No results found.</p>
      )}

      {activeResult && (
        <>
          {query && currentResult && !recResult && (
            <p
              data-testid="home-result-banner"
              style={{ margin: '20px 0 14px', fontSize: '22px', lineHeight: 1.4, color: '#1a237e' }}
            >
              Pronunciation of{' '}
              <strong style={{ color: '#c0392b' }}>{query}</strong>{' '}
              in {languageLabel(currentResult.language)}{' '}
              <span style={{ color: '#666', fontSize: '18px' }}>({resultIdx + 1} of {total}):</span>
            </p>
          )}
          <PlayerView
            result={activeResult}
            query={query}
            token={token}
            canPrev={!recResult && resultIdx > 0}
            canNext={!recResult && resultIdx < total - 1}
            onPrev={handlePrev}
            onNext={handleNext}
            wordColors={wordColors}
            passiveMax={prefs.passive_reps_for_known}
            activeMax={prefs.active_reps_for_known}
          />
          {token && !showChat && (
            <div style={{ display: 'flex', gap: '8px', marginTop: '12px', flexWrap: 'wrap' }}>
              <button onClick={() => setShowChat('free')} style={{ padding: '10px 20px', borderRadius: '6px', border: '1px solid #c5cae9', background: '#fff', color: '#1a237e', fontSize: '14px', fontWeight: 600, cursor: 'pointer', minHeight: '44px', touchAction: 'manipulation' }} data-testid="home-free-chat">
                Free Chat
              </button>
              <button onClick={() => setShowChat('guided')} style={{ padding: '10px 20px', borderRadius: '6px', border: '1px solid #ffe082', background: '#fff', color: '#e65100', fontSize: '14px', fontWeight: 600, cursor: 'pointer', minHeight: '44px', touchAction: 'manipulation' }} data-testid="home-guided-practice">
                Guided Practice
              </button>
            </div>
          )}
          {token && showChat === 'free' && (
            <FreeChatPage result={activeResult} token={token} onClose={() => setShowChat(null)} />
          )}
          {token && showChat === 'guided' && (
            <GuidedChatPage
              result={activeResult}
              token={token}
              targetItemId={guidedTarget?.itemId}
              targetItemType={guidedTarget?.itemType}
              onClose={() => { setShowChat(null); setGuidedTarget(null); }}
              onSessionComplete={() => { setShowChat(null); setGuidedTarget(null); navigate('/for-you'); }}
            />
          )}
        </>
      )}
    </>
  );
}

function ForYouPage() {
  const { token, prefs, recLanguage, setRecLanguage, channelAction, genreAction } = useAppCtx();
  const navigate = useNavigate();
  if (!token) return <Navigate to="/" />;

  const wordColors = {
    known:    { color: prefs.known_word_color },
    learning: { color: prefs.learning_word_color },
    unknown:  { color: prefs.unknown_word_color },
  };
  const blank = (lang: string): SearchResult => ({
    video_id: '', title: '', thumbnail_url: '', language: lang,
    start_time: 0, start_time_int: 0, content: '', surface_form: null, match_type: 'recommendation',
  });

  return (
    <RecommendationsPanel
      token={token}
      language={recLanguage}
      onLanguageChange={lang => { setRecLanguage(lang); localStorage.setItem('recLanguage', lang); }}
      onWatch={result => navigate('/', { state: { recResult: result } })}
      onPractice={lang => navigate('/', { state: { recResult: blank(lang), showChat: 'guided' } })}
      onPracticeItem={(itemId, itemType, lang) => navigate('/', { state: { recResult: blank(lang), guidedTarget: { itemId, itemType }, showChat: 'guided' } })}
      onPracticeSentence={result => navigate('/', { state: { recResult: result, showChat: 'guided' } })}
      onSearch={term => navigate('/', { state: { searchTerm: term } })}
      onClose={() => navigate('/')}
      onOpenBooks={() => navigate('/books')}
      wordColors={wordColors}
      passiveMax={prefs.passive_reps_for_known}
      prefs={prefs}
      onChannelAction={channelAction}
      onGenreAction={genreAction}
    />
  );
}

function PlaylistPage() {
  const { token, recLanguage, setRecLanguage } = useAppCtx();
  const navigate = useNavigate();
  if (!token) return <Navigate to="/" />;
  return (
    <PlaylistPanel
      token={token}
      language={recLanguage}
      onLanguageChange={lang => { setRecLanguage(lang); localStorage.setItem('recLanguage', lang); }}
      onWatch={result => navigate('/', { state: { recResult: result } })}
      onClose={() => navigate('/')}
    />
  );
}

function BooksPage() {
  const { token, prefs, recLanguage } = useAppCtx();
  const [activeBook, setActiveBook] = useState<BookDocument | null>(null);
  const navigate = useNavigate();
  if (!token) return <Navigate to="/" />;
  if (activeBook) {
    return (
      <BookReaderPage
        token={token}
        doc={activeBook}
        onClose={() => setActiveBook(null)}
        autoMarkKnown={prefs.auto_mark_known}
      />
    );
  }
  return (
    <BookLibraryPage
      token={token}
      onOpen={setActiveBook}
      onClose={() => navigate('/')}
      defaultLanguage={recLanguage || undefined}
      onOpenReadingReview={() => navigate('/reading-review')}
    />
  );
}

function ReviewPage() {
  const { token, recLanguage, setRecLanguage } = useAppCtx();
  const navigate = useNavigate();
  if (!token) return <Navigate to="/" />;
  return (
    <SRSReviewPage
      token={token}
      language={recLanguage}
      onLanguageChange={lang => { setRecLanguage(lang); localStorage.setItem('recLanguage', lang); }}
      onClose={() => navigate('/')}
    />
  );
}

function ReadingReviewRoute() {
  const { token } = useAppCtx();
  const navigate = useNavigate();
  if (!token) return <Navigate to="/" />;
  return <ReadingReviewPage token={token} onClose={() => navigate('/books')} />;
}

function AddContentPage() {
  const { token } = useAppCtx();
  const navigate = useNavigate();
  if (!token) return <Navigate to="/" />;
  return <ContentRequestPage token={token} onClose={() => navigate('/')} />;
}

// Reachable directly by URL on purpose — the nav link is hidden for non-admins,
// but the server's require_admin is what actually enforces access. A non-admin
// who navigates here sees the queue's inline error, not a fake client-side
// "forbidden" screen that would imply the client is the gate.
function AdminLemmaQueueRoute() {
  const { token } = useAppCtx();
  const navigate = useNavigate();
  if (!token) return <Navigate to="/" />;
  return <AdminLemmaQueuePage token={token} onClose={() => navigate('/')} />;
}

function SettingsPage() {
  const { token, prefs, savePreferences } = useAppCtx();
  const navigate = useNavigate();
  if (!token) return <Navigate to="/" />;
  return <SettingsPanel prefs={prefs} onSave={savePreferences} onClose={() => navigate('/')} token={token} />;
}

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Layout />}>
        <Route index element={<HomePage />} />
        <Route path="for-you" element={<ForYouPage />} />
        <Route path="playlist" element={<PlaylistPage />} />
        <Route path="books" element={<BooksPage />} />
        <Route path="review" element={<ReviewPage />} />
        <Route path="reading-review" element={<ReadingReviewRoute />} />
        <Route path="add-content" element={<AddContentPage />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="privacy" element={<PrivacyPage />} />
        <Route path="admin/lemma-corrections" element={<AdminLemmaQueueRoute />} />
      </Route>
    </Routes>
  );
}
