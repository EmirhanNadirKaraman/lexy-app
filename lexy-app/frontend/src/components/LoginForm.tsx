import { useState } from 'react';
import { login, register } from '../api/auth';
import { clearStoredEmail, clearToken, getStoredEmail, setStoredEmail, setToken } from '../auth';

interface Props {
    token: string | null;
    onLogin: (token: string) => void;
    onLogout: () => void;
}

type Mode = 'closed' | 'login' | 'register';

export function LoginForm({ token, onLogin, onLogout }: Props) {
    const [mode, setMode] = useState<Mode>('closed');
    const [email, setEmail] = useState('');
    const [password, setPassword] = useState('');
    const [registrationCode, setRegistrationCode] = useState('');
    const [error, setError] = useState<string | null>(null);
    const [loading, setLoading] = useState(false);

    function handleLogout() {
        clearToken();
        clearStoredEmail();
        onLogout();
    }

    // Signed in — show email + sign out
    if (token) {
        return (
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px', fontSize: '13px', flexWrap: 'wrap' }}>
                <span style={{ color: 'var(--color-text-muted)' }}>{getStoredEmail() ?? 'Signed in'}</span>
                <button onClick={handleLogout} style={ghostBtn} data-testid="login-signout">Sign out</button>
            </div>
        );
    }

    // Signed out — show button or inline form
    if (mode === 'closed') {
        return (
            <button onClick={() => setMode('login')} style={outlineBtn} data-testid="login-signin-toggle">Sign in</button>
        );
    }

    async function handleSubmit(e: React.FormEvent) {
        e.preventDefault();
        setError(null);
        setLoading(true);
        try {
            if (mode === 'register') {
                await register(email, password, registrationCode || undefined);
            }
            const newToken = await login(email, password);
            setToken(newToken);
            setStoredEmail(email);
            onLogin(newToken);
            setMode('closed');
            setEmail('');
            setPassword('');
            setRegistrationCode('');
        } catch (err) {
            setError(err instanceof Error ? err.message : 'Failed');
        } finally {
            setLoading(false);
        }
    }

    return (
        <form onSubmit={handleSubmit} style={{ display: 'flex', alignItems: 'center', gap: '6px', flexWrap: 'wrap' }}>
            {/* Mode toggle */}
            <span style={{ fontSize: '12px', color: 'var(--color-text-muted)', display: 'inline-flex', alignItems: 'center', gap: '2px' }}>
                <button type="button" onClick={() => { setMode('login'); setError(null); }}
                    style={{ ...modeToggleBtn, fontWeight: mode === 'login' ? 700 : 400 }}>Login</button>
                {' / '}
                <button type="button" onClick={() => { setMode('register'); setError(null); }}
                    style={{ ...modeToggleBtn, fontWeight: mode === 'register' ? 700 : 400 }}>Register</button>
            </span>

            {/* fontSize: 16px blocks iOS Safari focus-zoom. */}
            <input type="email" placeholder="Email" value={email}
                onChange={e => setEmail(e.target.value)} required style={inputStyle}
                aria-label="Email address"
                autoComplete="email"
                data-testid="login-email" />
            <input type="password" placeholder="Password" value={password}
                onChange={e => setPassword(e.target.value)} required style={inputStyle}
                aria-label="Password"
                autoComplete={mode === 'register' ? 'new-password' : 'current-password'}
                data-testid="login-password" />
            {mode === 'register' && (
                <input type="text" placeholder="Invite code (optional)" value={registrationCode}
                    onChange={e => setRegistrationCode(e.target.value)} style={inputStyle}
                    aria-label="Registration code (optional)"
                    autoComplete="off"
                    data-testid="login-registration-code" />
            )}

            <button type="submit" disabled={loading} style={primaryBtn} data-testid="login-submit">
                {loading ? '…' : mode === 'register' ? 'Create' : 'Sign in'}
            </button>
            <button type="button" onClick={() => { setMode('closed'); setError(null); }} style={closeBtn}
                aria-label="Cancel" data-testid="login-cancel">
                ✕
            </button>

            {error && <span style={{ color: 'var(--color-danger)', fontSize: '12px', width: '100%' }}>{error}</span>}
        </form>
    );
}

const inputStyle: React.CSSProperties = {
    // fontSize: 16px blocks iOS Safari focus-zoom; minHeight 44px = touch target.
    fontSize: '16px', padding: '8px 10px',
    border: '1px solid var(--color-input-border)', borderRadius: '4px',
    background: 'var(--color-input-bg)', color: 'var(--color-text)',
    width: '160px', minHeight: '44px', boxSizing: 'border-box',
};
const ghostBtn: React.CSSProperties = {
    // Header signed-in "Sign out" — secondary chip, 32px touchable.
    background: 'none', border: 'none', cursor: 'pointer',
    fontSize: '13px', color: 'var(--color-text-muted)',
    padding: '6px 10px', minHeight: '32px',
    touchAction: 'manipulation',
};
const modeToggleBtn: React.CSSProperties = {
    // Login/Register text toggles inside the inline form — small text-action.
    background: 'none', border: 'none', cursor: 'pointer',
    fontSize: '13px', color: 'var(--color-text-muted)',
    padding: '6px 4px', minHeight: '32px',
    touchAction: 'manipulation',
};
const outlineBtn: React.CSSProperties = {
    fontSize: '14px', padding: '8px 14px',
    border: '1px solid var(--color-primary)', borderRadius: '4px',
    background: 'none', color: 'var(--color-primary-on-soft)', cursor: 'pointer',
    minHeight: '36px', touchAction: 'manipulation',
};
const primaryBtn: React.CSSProperties = {
    fontSize: '14px', padding: '8px 14px',
    background: 'var(--color-primary)', color: 'var(--color-primary-text)',
    border: 'none', borderRadius: '4px', cursor: 'pointer',
    minHeight: '44px', touchAction: 'manipulation',
};
const closeBtn: React.CSSProperties = {
    background: 'none', border: 'none', cursor: 'pointer',
    fontSize: '14px', color: 'var(--color-text-muted)',
    minWidth: '44px', minHeight: '44px',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    padding: 0,
    touchAction: 'manipulation',
};
