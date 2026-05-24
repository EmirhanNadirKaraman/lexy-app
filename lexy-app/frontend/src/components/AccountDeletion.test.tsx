/**
 * Account-deletion UI in SettingsPanel + privacy page route.
 *
 * Covers:
 *   - The two-step confirm flow (start button → confirm/cancel).
 *   - deleteAccount() is called only after the second click.
 *   - A failed delete surfaces the error and does NOT navigate.
 *   - Mobile-safe sizing (>=44px target).
 *   - PrivacyPage renders the headline + the contact email.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

import { SettingsPanel } from './SettingsPanel';
import { PrivacyPage } from './PrivacyPage';
import * as searchApi from '../api/search';
import * as accountApi from '../api/account';
import { PREFERENCE_DEFAULTS } from '../api/settings';

beforeEach(() => {
    vi.spyOn(searchApi, 'fetchCategories').mockResolvedValue([]);
});

afterEach(() => {
    vi.restoreAllMocks();
});

function renderSettings(overrides: Partial<Parameters<typeof SettingsPanel>[0]> = {}) {
    return render(
        <SettingsPanel
            prefs={PREFERENCE_DEFAULTS}
            onSave={vi.fn().mockResolvedValue(undefined)}
            onClose={() => {}}
            token="test-token"
            {...overrides}
        />,
    );
}

describe('SettingsPanel — account deletion', () => {
    it('shows the start button by default, no confirm UI', () => {
        renderSettings();
        expect(screen.getByTestId('account-delete-start')).toBeInTheDocument();
        expect(screen.queryByTestId('account-delete-confirm')).not.toBeInTheDocument();
    });

    it('clicking start reveals confirm + cancel, hides start', () => {
        renderSettings();
        fireEvent.click(screen.getByTestId('account-delete-start'));
        expect(screen.getByTestId('account-delete-confirm')).toBeInTheDocument();
        expect(screen.getByTestId('account-delete-cancel')).toBeInTheDocument();
        expect(screen.queryByTestId('account-delete-start')).not.toBeInTheDocument();
    });

    it('cancel returns to the start button without calling the API', () => {
        const spy = vi.spyOn(accountApi, 'deleteAccount').mockResolvedValue();
        renderSettings();
        fireEvent.click(screen.getByTestId('account-delete-start'));
        fireEvent.click(screen.getByTestId('account-delete-cancel'));
        expect(screen.getByTestId('account-delete-start')).toBeInTheDocument();
        expect(spy).not.toHaveBeenCalled();
    });

    it('confirm sends the token and typed password to deleteAccount', async () => {
        const spy = vi.spyOn(accountApi, 'deleteAccount').mockResolvedValue();
        renderSettings({ token: 'tok-XYZ' });
        fireEvent.click(screen.getByTestId('account-delete-start'));
        fireEvent.change(screen.getByTestId('account-delete-password'), { target: { value: 'hunter2' } });
        fireEvent.click(screen.getByTestId('account-delete-confirm'));
        await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
        expect(spy).toHaveBeenCalledWith('tok-XYZ', 'hunter2');
    });

    it('confirm step shows a labelled password field and disables confirm until filled', () => {
        renderSettings();
        fireEvent.click(screen.getByTestId('account-delete-start'));

        const pw = screen.getByLabelText(/enter your password/i) as HTMLInputElement;
        expect(pw).toBeInTheDocument();
        expect(pw.type).toBe('password');

        const confirm = screen.getByTestId('account-delete-confirm') as HTMLButtonElement;
        expect(confirm.disabled).toBe(true);          // no password yet
        fireEvent.change(pw, { target: { value: 'pw' } });
        expect(confirm.disabled).toBe(false);         // enabled once filled
    });

    it('start button is at least 44px high and confirm button reads "Yes, permanently delete"', () => {
        renderSettings();
        const start = screen.getByTestId('account-delete-start') as HTMLButtonElement;
        expect(parseInt(start.style.minHeight, 10)).toBeGreaterThanOrEqual(44);

        fireEvent.click(start);
        const confirm = screen.getByTestId('account-delete-confirm') as HTMLButtonElement;
        expect(confirm.textContent).toContain('Yes, permanently delete');
        expect(parseInt(confirm.style.minHeight, 10)).toBeGreaterThanOrEqual(44);
    });

    it('surfaces an error and keeps the confirm UI when deleteAccount fails', async () => {
        vi.spyOn(accountApi, 'deleteAccount').mockRejectedValue(
            new Error('Incorrect password. Please try again.'),
        );
        renderSettings();
        fireEvent.click(screen.getByTestId('account-delete-start'));
        fireEvent.change(screen.getByTestId('account-delete-password'), { target: { value: 'wrong' } });
        fireEvent.click(screen.getByTestId('account-delete-confirm'));
        const errorNode = await screen.findByText(/Incorrect password/i);
        expect(errorNode).toBeInTheDocument();
        // Confirm UI still present so the user can retry or cancel (auth NOT cleared).
        expect(screen.getByTestId('account-delete-confirm')).toBeInTheDocument();
    });
});

describe('PrivacyPage', () => {
    it('renders the headline, the placeholder contact, and key sections', () => {
        render(<PrivacyPage />);
        expect(screen.getByText('Privacy Policy')).toBeInTheDocument();
        // Placeholder must be obvious (angle brackets) so it can't ship as a
        // real contact. Render is intentionally a <code> block, not a mailto.
        expect(screen.getByText(/<YOUR_REAL_PRIVACY_EMAIL_BEFORE_LAUNCH>/)).toBeInTheDocument();
        expect(screen.getByText(/Account deletion/i)).toBeInTheDocument();
        // localStorage disclosure paragraph (item 2)
        expect(screen.getByText(/Browser storage/i)).toBeInTheDocument();
        expect(screen.getByText(/auth_token/)).toBeInTheDocument();
        expect(screen.getByText(/auth_email/)).toBeInTheDocument();
    });
});
