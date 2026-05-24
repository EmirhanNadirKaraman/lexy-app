/**
 * LoginForm — registration invite code (S2) + generic failure (S9).
 *
 *   - the optional invite-code field appears only in register mode
 *   - register() receives the code when entered, undefined when blank
 *   - a backend generic failure surfaces as-is (no enumeration)
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

import { LoginForm } from './LoginForm';
import * as authApi from '../api/auth';

afterEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
});

function openRegister() {
    render(<LoginForm token={null} onLogin={vi.fn()} onLogout={vi.fn()} />);
    fireEvent.click(screen.getByTestId('login-signin-toggle'));
    fireEvent.click(screen.getByText('Register'));
}

describe('LoginForm — registration invite code (S2)', () => {
    it('shows the optional invite-code field only in register mode', () => {
        render(<LoginForm token={null} onLogin={vi.fn()} onLogout={vi.fn()} />);
        fireEvent.click(screen.getByTestId('login-signin-toggle'));
        // Login mode → no code field.
        expect(screen.queryByTestId('login-registration-code')).toBeNull();
        // Register mode → field appears, with an accessible label.
        fireEvent.click(screen.getByText('Register'));
        const code = screen.getByTestId('login-registration-code') as HTMLInputElement;
        expect(code).toBeTruthy();
        expect(code.getAttribute('aria-label')).toMatch(/registration code/i);
    });

    it('sends the invite code to register() when entered', async () => {
        const registerSpy = vi.spyOn(authApi, 'register').mockResolvedValue();
        vi.spyOn(authApi, 'login').mockResolvedValue('tok');
        openRegister();
        fireEvent.change(screen.getByTestId('login-email'), { target: { value: 'a@b.com' } });
        fireEvent.change(screen.getByTestId('login-password'), { target: { value: 'password123' } });
        fireEvent.change(screen.getByTestId('login-registration-code'), { target: { value: 'invite42' } });
        fireEvent.click(screen.getByTestId('login-submit'));
        await waitFor(() => expect(registerSpy).toHaveBeenCalledTimes(1));
        expect(registerSpy).toHaveBeenCalledWith('a@b.com', 'password123', 'invite42');
    });

    it('omits the code (undefined) when the field is left blank', async () => {
        const registerSpy = vi.spyOn(authApi, 'register').mockResolvedValue();
        vi.spyOn(authApi, 'login').mockResolvedValue('tok');
        openRegister();
        fireEvent.change(screen.getByTestId('login-email'), { target: { value: 'a@b.com' } });
        fireEvent.change(screen.getByTestId('login-password'), { target: { value: 'password123' } });
        fireEvent.click(screen.getByTestId('login-submit'));
        await waitFor(() => expect(registerSpy).toHaveBeenCalledTimes(1));
        expect(registerSpy).toHaveBeenCalledWith('a@b.com', 'password123', undefined);
    });

    it('surfaces the backend generic failure message', async () => {
        vi.spyOn(authApi, 'register').mockRejectedValue(
            new Error('Registration could not be completed. Please check your details, or sign in if you already have an account.'),
        );
        openRegister();
        fireEvent.change(screen.getByTestId('login-email'), { target: { value: 'a@b.com' } });
        fireEvent.change(screen.getByTestId('login-password'), { target: { value: 'password123' } });
        fireEvent.click(screen.getByTestId('login-submit'));
        expect(await screen.findByText(/could not be completed/i)).toBeInTheDocument();
    });
});
