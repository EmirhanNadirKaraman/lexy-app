/**
 * WordListsPage tests — vocabulary list upload/download.
 *
 * Covers:
 *   - paste flow posts the parsed words
 *   - .txt upload parses words into the textarea
 *   - all five status counts render, and unresolved/ambiguous surfaces are visible
 *   - ambiguous gets its own explanation, distinct from unresolved
 *   - mark-as-learning calls the endpoint and refreshes
 *   - download triggers the export endpoint
 *   - empty and oversized lists are refused client-side with an inline error
 *   - server errors surface inline
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';

import { WordListsPage } from './WordListsPage';
import type { WordListDetail } from '../api/wordLists';

type FetchCall = { url: string; init: RequestInit | undefined };
let calls: FetchCall[] = [];

function installFetch(handler: (url: string, init: RequestInit | undefined) => Response | Promise<Response>) {
    const stub = vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
        const url = typeof input === 'string' ? input : input.toString();
        calls.push({ url, init });
        return handler(url, init);
    });
    vi.stubGlobal('fetch', stub);
    return stub;
}

function jsonResponse(body: unknown, status = 200): Response {
    return new Response(JSON.stringify(body), {
        status,
        headers: { 'Content-Type': 'application/json' },
    });
}

/**
 * A promise the test resolves by hand.
 *
 * Lets a fetch stay in flight across several `fireEvent.click`s, which is the
 * only way to observe the in-flight window that the duplicate-request guard
 * exists to close.
 */
function deferred<T>() {
    let resolve!: (value: T) => void;
    const promise = new Promise<T>(res => { resolve = res; });
    return { promise, resolve };
}

function makeDetail(overrides: Partial<WordListDetail> = {}): WordListDetail {
    return {
        list_id: 1,
        name: 'My list',
        language: 'de',
        description: null,
        created_at: '2026-07-27T10:00:00Z',
        total: 4,
        counts: { known: 1, learning: 0, unknown: 1, unresolved: 1, ambiguous: 1 },
        entries: [
            { id: 1, surface: 'Haus', item_id: 10, item_type: 'word', status: 'known' },
            { id: 2, surface: 'Straße', item_id: 11, item_type: 'word', status: 'unknown' },
            { id: 3, surface: 'Blorptzk', item_id: null, item_type: 'word', status: 'unresolved' },
            { id: 4, surface: 'Bank', item_id: null, item_type: 'word', status: 'ambiguous' },
        ],
        ...overrides,
    };
}

function renderPage() {
    return render(<WordListsPage token="tok" language="de" onClose={() => {}} />);
}

/** Empty index, then whatever POST/GET the test cares about. */
function installIndexThen(handler: (url: string, init: RequestInit | undefined) => Response) {
    installFetch((url, init) => {
        if (url.includes('/word-lists') && (!init || init.method === undefined) && !url.match(/word-lists\/\d/)) {
            return jsonResponse([]);
        }
        return handler(url, init);
    });
}

beforeEach(() => {
    calls = [];
});

afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
});

describe('WordListsPage', () => {
    it('posts the parsed words from a pasted list', async () => {
        installIndexThen(() => jsonResponse(makeDetail(), 201));
        renderPage();

        fireEvent.change(screen.getByTestId('word-list-name'), { target: { value: 'Goethe B1' } });
        fireEvent.change(screen.getByTestId('word-list-input'), {
            target: { value: 'Haus\nStraße\n\nlaufen' },
        });
        fireEvent.click(screen.getByTestId('word-list-create'));

        await waitFor(() => expect(screen.getByTestId('word-list-detail')).toBeTruthy());

        const post = calls.find(c => c.init?.method === 'POST');
        expect(post).toBeTruthy();
        const body = JSON.parse(post!.init!.body as string);
        // Blank line dropped, order preserved, language passed through.
        expect(body.words).toEqual(['Haus', 'Straße', 'laufen']);
        expect(body.name).toBe('Goethe B1');
        expect(body.language).toBe('de');
    });

    it('splits comma-separated input and shows a live count', async () => {
        installIndexThen(() => jsonResponse(makeDetail(), 201));
        renderPage();

        fireEvent.change(screen.getByTestId('word-list-input'), {
            target: { value: 'Haus, Straße; laufen' },
        });

        expect(screen.getByTestId('word-list-count').textContent).toContain('3 words');
    });

    it('parses an uploaded .txt into the input', async () => {
        installIndexThen(() => jsonResponse(makeDetail(), 201));
        renderPage();

        const file = new File(['Haus\nStraße\nlaufen\n'], 'goethe.txt', { type: 'text/plain' });
        // jsdom's File.text() is not always implemented — provide it explicitly.
        Object.defineProperty(file, 'text', { value: async () => 'Haus\nStraße\nlaufen\n' });

        fireEvent.change(screen.getByTestId('word-list-file'), { target: { files: [file] } });

        await waitFor(() =>
            expect((screen.getByTestId('word-list-input') as HTMLTextAreaElement).value)
                .toContain('Straße'),
        );
        expect(screen.getByTestId('word-list-count').textContent).toContain('3 words');
        // Filename (without extension) becomes the default list name.
        expect((screen.getByTestId('word-list-name') as HTMLInputElement).value).toBe('goethe');
    });

    it('renders all five counts and shows unresolved + ambiguous surfaces', async () => {
        installIndexThen(() => jsonResponse(makeDetail(), 201));
        renderPage();

        fireEvent.change(screen.getByTestId('word-list-input'), { target: { value: 'Haus' } });
        fireEvent.click(screen.getByTestId('word-list-create'));

        await waitFor(() => expect(screen.getByTestId('word-list-detail')).toBeTruthy());

        expect(screen.getByTestId('word-list-count-known').textContent).toContain('1');
        expect(screen.getByTestId('word-list-count-learning').textContent).toContain('0');
        expect(screen.getByTestId('word-list-count-unknown').textContent).toContain('1');
        expect(screen.getByTestId('word-list-count-unresolved').textContent).toContain('1');
        expect(screen.getByTestId('word-list-count-ambiguous').textContent).toContain('1');

        // The words themselves stay visible — not silently dropped from the list.
        expect(screen.getByTestId('word-list-entry-Blorptzk')).toBeTruthy();
        expect(screen.getByTestId('word-list-entry-Bank')).toBeTruthy();
    });

    it('explains ambiguous entries separately from unresolved ones', async () => {
        installIndexThen(() => jsonResponse(makeDetail(), 201));
        renderPage();

        fireEvent.change(screen.getByTestId('word-list-input'), { target: { value: 'Bank' } });
        fireEvent.click(screen.getByTestId('word-list-create'));

        await waitFor(() => expect(screen.getByTestId('word-list-ambiguous-note')).toBeTruthy());
        expect(screen.getByTestId('word-list-ambiguous-note').textContent)
            .toMatch(/several dictionary entries/i);
    });

    it('omits the ambiguous note when nothing is ambiguous', async () => {
        const detail = makeDetail({
            counts: { known: 1, learning: 0, unknown: 1, unresolved: 0, ambiguous: 0 },
            entries: [{ id: 1, surface: 'Haus', item_id: 10, item_type: 'word', status: 'known' }],
        });
        installIndexThen(() => jsonResponse(detail, 201));
        renderPage();

        fireEvent.change(screen.getByTestId('word-list-input'), { target: { value: 'Haus' } });
        fireEvent.click(screen.getByTestId('word-list-create'));

        await waitFor(() => expect(screen.getByTestId('word-list-detail')).toBeTruthy());
        expect(screen.queryByTestId('word-list-ambiguous-note')).toBeNull();
    });

    it('calls the mark-unknown-learning endpoint and reports what was skipped', async () => {
        installIndexThen((url, init) => {
            if (url.includes('mark-unknown-learning')) {
                return jsonResponse({
                    list_id: 1, marked: 1, marked_item_ids: [11],
                    skipped_unresolved: 1, skipped_ambiguous: 1,
                });
            }
            if (init?.method === 'POST') return jsonResponse(makeDetail(), 201);
            return jsonResponse(makeDetail());
        });
        renderPage();

        fireEvent.change(screen.getByTestId('word-list-input'), { target: { value: 'Haus' } });
        fireEvent.click(screen.getByTestId('word-list-create'));
        await waitFor(() => expect(screen.getByTestId('word-list-detail')).toBeTruthy());

        fireEvent.click(screen.getByTestId('word-list-mark-learning'));

        await waitFor(() => expect(screen.getByTestId('word-list-notice')).toBeTruthy());
        expect(calls.some(c => c.url.includes('/mark-unknown-learning') && c.init?.method === 'POST')).toBe(true);
        const notice = screen.getByTestId('word-list-notice').textContent ?? '';
        expect(notice).toContain('Marked 1 word');
        expect(notice).toContain('Skipped 2');
    });

    it('disables mark-as-learning when nothing is unknown', async () => {
        const detail = makeDetail({
            counts: { known: 1, learning: 0, unknown: 0, unresolved: 0, ambiguous: 0 },
            entries: [{ id: 1, surface: 'Haus', item_id: 10, item_type: 'word', status: 'known' }],
        });
        installIndexThen(() => jsonResponse(detail, 201));
        renderPage();

        fireEvent.change(screen.getByTestId('word-list-input'), { target: { value: 'Haus' } });
        fireEvent.click(screen.getByTestId('word-list-create'));
        await waitFor(() => expect(screen.getByTestId('word-list-detail')).toBeTruthy());

        expect((screen.getByTestId('word-list-mark-learning') as HTMLButtonElement).disabled).toBe(true);
    });

    it('triggers the export endpoint on download', async () => {
        vi.stubGlobal('URL', {
            ...URL,
            createObjectURL: vi.fn(() => 'blob:mock'),
            revokeObjectURL: vi.fn(),
        });
        installIndexThen((url, init) => {
            if (url.includes('/export')) {
                return new Response('Haus\nStraße\nBlorptzk\nBank', {
                    status: 200,
                    headers: { 'Content-Type': 'text/plain' },
                });
            }
            if (init?.method === 'POST') return jsonResponse(makeDetail(), 201);
            return jsonResponse(makeDetail());
        });
        renderPage();

        fireEvent.change(screen.getByTestId('word-list-input'), { target: { value: 'Haus' } });
        fireEvent.click(screen.getByTestId('word-list-create'));
        await waitFor(() => expect(screen.getByTestId('word-list-detail')).toBeTruthy());

        fireEvent.click(screen.getByTestId('word-list-download'));

        await waitFor(() => expect(calls.some(c => c.url.includes('/export'))).toBe(true));
    });

    it('refuses an empty list inline without calling the API', async () => {
        installIndexThen(() => jsonResponse(makeDetail(), 201));
        renderPage();

        fireEvent.change(screen.getByTestId('word-list-input'), { target: { value: '   \n  ' } });
        fireEvent.click(screen.getByTestId('word-list-create'));

        await waitFor(() => expect(screen.getByTestId('word-list-create-error')).toBeTruthy());
        expect(screen.getByTestId('word-list-create-error').textContent).toMatch(/at least one word/i);
        expect(calls.some(c => c.init?.method === 'POST')).toBe(false);
    });

    it('refuses an oversized list inline without calling the API', async () => {
        installIndexThen(() => jsonResponse(makeDetail(), 201));
        renderPage();

        const tooMany = Array.from({ length: 501 }, (_, i) => `w${i}`).join('\n');
        fireEvent.change(screen.getByTestId('word-list-input'), { target: { value: tooMany } });
        fireEvent.click(screen.getByTestId('word-list-create'));

        await waitFor(() => expect(screen.getByTestId('word-list-create-error')).toBeTruthy());
        expect(screen.getByTestId('word-list-create-error').textContent).toMatch(/limit is 500/i);
        expect(calls.some(c => c.init?.method === 'POST')).toBe(false);
    });

    it('shows a server error inline', async () => {
        installIndexThen(() => jsonResponse({ detail: 'word list is empty' }, 422));
        renderPage();

        fireEvent.change(screen.getByTestId('word-list-input'), { target: { value: 'Haus' } });
        fireEvent.click(screen.getByTestId('word-list-create'));

        await waitFor(() => expect(screen.getByTestId('word-list-create-error')).toBeTruthy());
        expect(screen.getByTestId('word-list-create-error').textContent).toContain('word list is empty');
    });

    it('shows a load error inline when the index request fails', async () => {
        installFetch(() => jsonResponse({ detail: 'boom' }, 500));
        renderPage();

        await waitFor(() => expect(screen.getByTestId('word-list-load-error')).toBeTruthy());
        expect(screen.getByTestId('word-list-load-error').textContent).toContain('boom');
    });
});

describe('WordListsPage — phrase entries', () => {
    const PHRASE = 'jdm. (Dat) etw. (Akk) sagen';

    // Own fixture rather than extending the shared one: the existing tests
    // assert exact counts, and adding an entry there would change them.
    const withPhrase = () => makeDetail({
        total: 5,
        counts: { known: 1, learning: 0, unknown: 2, unresolved: 1, ambiguous: 1 },
        entries: [
            ...makeDetail().entries,
            { id: 5, surface: PHRASE, item_id: 7, item_type: 'phrase', status: 'unknown' },
        ],
    });

    it('renders a type badge on resolved word and phrase entries', async () => {
        installIndexThen(() => jsonResponse(withPhrase(), 201));
        renderPage();

        fireEvent.change(screen.getByTestId('word-list-input'), { target: { value: 'Haus' } });
        fireEvent.click(screen.getByTestId('word-list-create'));
        await waitFor(() => expect(screen.getByTestId('word-list-detail')).toBeTruthy());

        expect(screen.getByTestId('word-list-type-Haus').textContent).toBe('word');
        expect(screen.getByTestId(`word-list-type-${PHRASE}`).textContent).toBe('phrase');
    });

    it('shows phrases alongside words in the same list', async () => {
        installIndexThen(() => jsonResponse(withPhrase(), 201));
        renderPage();

        fireEvent.change(screen.getByTestId('word-list-input'), { target: { value: 'Haus' } });
        fireEvent.click(screen.getByTestId('word-list-create'));
        await waitFor(() => expect(screen.getByTestId('word-list-detail')).toBeTruthy());

        expect(screen.getByTestId('word-list-entry-Haus')).toBeTruthy();
        expect(screen.getByTestId(`word-list-entry-${PHRASE}`)).toBeTruthy();
    });

    it('omits the type badge on unresolved and ambiguous entries', async () => {
        // They have no catalog row, so there is no type to report.
        installIndexThen(() => jsonResponse(withPhrase(), 201));
        renderPage();

        fireEvent.change(screen.getByTestId('word-list-input'), { target: { value: 'Haus' } });
        fireEvent.click(screen.getByTestId('word-list-create'));
        await waitFor(() => expect(screen.getByTestId('word-list-detail')).toBeTruthy());

        expect(screen.queryByTestId('word-list-type-Blorptzk')).toBeNull();
        expect(screen.queryByTestId('word-list-type-Bank')).toBeNull();
        // ...while the entries themselves still render.
        expect(screen.getByTestId('word-list-entry-Blorptzk')).toBeTruthy();
        expect(screen.getByTestId('word-list-entry-Bank')).toBeTruthy();
    });

    it('marks unknown as learning when the list contains phrases', async () => {
        installIndexThen((url, init) => {
            if (url.includes('mark-unknown-learning')) {
                return jsonResponse({
                    list_id: 1, marked: 2, marked_item_ids: [11, 7],
                    skipped_unresolved: 1, skipped_ambiguous: 1,
                });
            }
            if (init?.method === 'POST') return jsonResponse(withPhrase(), 201);
            return jsonResponse(withPhrase());
        });
        renderPage();

        fireEvent.change(screen.getByTestId('word-list-input'), { target: { value: 'Haus' } });
        fireEvent.click(screen.getByTestId('word-list-create'));
        await waitFor(() => expect(screen.getByTestId('word-list-detail')).toBeTruthy());

        fireEvent.click(screen.getByTestId('word-list-mark-learning'));

        await waitFor(() => expect(screen.getByTestId('word-list-notice')).toBeTruthy());
        expect(calls.some(c => c.url.includes('/mark-unknown-learning'))).toBe(true);
        expect(screen.getByTestId('word-list-notice').textContent).toContain('Marked 2 words');
    });
});

/**
 * Built-in / system lists (backend migration 037 + phase 2 seeding).
 *
 * The backend already guarantees the important half: a system list has no
 * owner, so the ownership-filtered delete can never match it and answers 404,
 * and `mark-unknown-learning` suppresses its write to the shared rows. These
 * tests cover the UI contract on top of that — separation, labelling, and not
 * offering a control that would only produce an error.
 */
function summary(overrides: Partial<import('../api/wordLists').WordListSummary> = {}) {
    return {
        list_id: 1,
        name: 'My list',
        language: 'de',
        description: null,
        created_at: '2026-07-27T10:00:00Z',
        total: 3,
        is_system: false,
        ...overrides,
    };
}

/** Index returns `lists`; everything else falls through to `handler`. */
function installIndex(lists: unknown[], handler?: (url: string, init: RequestInit | undefined) => Response) {
    installFetch((url, init) => {
        if (url.includes('/word-lists') && (!init || init.method === undefined) && !url.match(/word-lists\/\d/)) {
            return jsonResponse(lists);
        }
        return handler ? handler(url, init) : jsonResponse({}, 404);
    });
}

describe('WordListsPage — built-in lists', () => {
    it('renders system lists in their own section, separate from the user’s', async () => {
        installIndex([
            summary({ list_id: 1, name: 'My list', is_system: false }),
            summary({ list_id: 2, name: 'German Verb & Phrase Patterns', is_system: true, total: 5035 }),
        ]);
        renderPage();

        const section = await screen.findByTestId('word-list-system-section');
        expect(section).toHaveTextContent('Built-in lists');
        expect(section).toHaveTextContent('German Verb & Phrase Patterns');
        expect(section).not.toHaveTextContent('My list');
        expect(screen.getByText('Your lists')).toBeInTheDocument();
    });

    it('shows a Built-in badge on a system list', async () => {
        installIndex([summary({ list_id: 2, name: 'German Verb & Phrase Patterns', is_system: true })]);
        renderPage();

        const badge = await screen.findByTestId('word-list-system-badge-2');
        expect(badge).toHaveTextContent('Built-in');
    });

    it('hides Delete for a system list but keeps Open', async () => {
        installIndex([summary({ list_id: 2, name: 'German Verb & Phrase Patterns', is_system: true })]);
        renderPage();

        expect(await screen.findByTestId('word-list-open-2')).toBeInTheDocument();
        expect(screen.queryByTestId('word-list-delete-2')).not.toBeInTheDocument();
    });

    it('still shows Delete for a user-created list', async () => {
        installIndex([summary({ list_id: 1, is_system: false })]);
        renderPage();

        expect(await screen.findByTestId('word-list-delete-1')).toBeInTheDocument();
        expect(screen.queryByTestId('word-list-system-badge-1')).not.toBeInTheDocument();
    });

    it('treats a list with no is_system field as user-owned', async () => {
        // A response from a backend older than migration 037 must not silently
        // become read-only.
        const { is_system: _omitted, ...legacy } = summary({ list_id: 7 });
        installIndex([legacy]);
        renderPage();

        expect(await screen.findByTestId('word-list-delete-7')).toBeInTheDocument();
        expect(screen.queryByTestId('word-list-system-section')).not.toBeInTheDocument();
    });

    it('shows an empty state under Your lists when only built-ins exist', async () => {
        installIndex([summary({ list_id: 2, name: 'Top German Words', is_system: true })]);
        renderPage();

        expect(await screen.findByTestId('word-list-mine-empty')).toBeInTheDocument();
    });

    it('omits the built-in section entirely when there are none', async () => {
        installIndex([summary({ list_id: 1, is_system: false })]);
        renderPage();

        await screen.findByTestId('word-list-delete-1');
        expect(screen.queryByTestId('word-list-system-section')).not.toBeInTheDocument();
        expect(screen.queryByTestId('word-list-mine-empty')).not.toBeInTheDocument();
    });

    it('overrides the display name of Top German Words without renaming it', async () => {
        // The stored name is the seeding idempotency key, so the clearer label
        // is frontend-only.
        installIndex([summary({ list_id: 2, name: 'Top German Words', is_system: true })]);
        renderPage();

        const section = await screen.findByTestId('word-list-system-section');
        expect(section).toHaveTextContent('Top German Words & Phrases');
    });

    it('shows a built-in badge and note on the detail view', async () => {
        installIndex(
            [summary({ list_id: 2, name: 'Top German Words', is_system: true })],
            () => jsonResponse(makeDetail({ list_id: 2, name: 'Top German Words', is_system: true })),
        );
        renderPage();

        fireEvent.click(await screen.findByTestId('word-list-open-2'));

        expect(await screen.findByTestId('word-list-detail-system-badge')).toBeInTheDocument();
        expect(screen.getByTestId('word-list-detail-system-note')).toHaveTextContent(
            /read-only/i,
        );
    });

    it('keeps export and mark-learning available on a system list detail', async () => {
        installIndex(
            [summary({ list_id: 2, name: 'Top German Words', is_system: true })],
            () => jsonResponse(makeDetail({ list_id: 2, name: 'Top German Words', is_system: true })),
        );
        renderPage();

        fireEvent.click(await screen.findByTestId('word-list-open-2'));

        expect(await screen.findByTestId('word-list-download')).toBeInTheDocument();
        const mark = screen.getByTestId('word-list-mark-learning');
        expect(mark).toBeInTheDocument();
        expect(mark).not.toBeDisabled();
    });

    it('shows no built-in badge on a user list detail', async () => {
        installIndex(
            [summary({ list_id: 1, is_system: false })],
            () => jsonResponse(makeDetail({ list_id: 1, is_system: false })),
        );
        renderPage();

        fireEvent.click(await screen.findByTestId('word-list-open-1'));

        await screen.findByTestId('word-list-detail');
        expect(screen.queryByTestId('word-list-detail-system-badge')).not.toBeInTheDocument();
        expect(screen.queryByTestId('word-list-detail-system-note')).not.toBeInTheDocument();
    });
});

/**
 * Bulk-marking guard.
 *
 * Marking is irreversible in practice — auto-promotion is one-way and there is
 * no un-mark — so a built-in list with thousands of unknown words is one click
 * from burying the user's review queue. The backend caps each call at 500; the
 * UI confirms above 200 so the dialog fires before the cap ever does.
 */
describe('WordListsPage — bulk mark guard', () => {
    function detailWithUnknown(n: number): WordListDetail {
        return makeDetail({
            list_id: 2,
            name: 'Top German Words',
            is_system: true,
            total: n,
            counts: { known: 0, learning: 0, unknown: n, unresolved: 0, ambiguous: 0 },
            entries: Array.from({ length: 3 }, (_, i) => ({
                id: i + 1, surface: `w${i}`, item_id: i + 1,
                item_type: 'word' as const, status: 'unknown' as const,
            })),
        });
    }

    function installDetail(detail: WordListDetail, markResult: unknown) {
        installFetch((url, init) => {
            if (url.includes('/word-lists') && (!init || init.method === undefined) && !url.match(/word-lists\/\d/)) {
                return jsonResponse([summary({ list_id: 2, name: 'Top German Words', is_system: true })]);
            }
            if (url.includes('mark-unknown-learning')) return jsonResponse(markResult);
            return jsonResponse(detail);
        });
    }

    async function openDetail() {
        renderPage();
        fireEvent.click(await screen.findByTestId('word-list-open-2'));
        return screen.findByTestId('word-list-mark-learning');
    }

    it('asks for confirmation when the unknown count is over the threshold', async () => {
        const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
        installDetail(detailWithUnknown(3831), {
            list_id: 2, marked: 500, marked_item_ids: [], skipped_unresolved: 0,
            skipped_ambiguous: 0, remaining: 3331, capped: true,
        });

        fireEvent.click(await openDetail());

        await waitFor(() => expect(confirmSpy).toHaveBeenCalled());
        expect(confirmSpy.mock.calls[0][0]).toContain('3831');
    });

    it('does not ask for confirmation below the threshold', async () => {
        const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
        installDetail(detailWithUnknown(5), {
            list_id: 2, marked: 5, marked_item_ids: [], skipped_unresolved: 0,
            skipped_ambiguous: 0, remaining: 0, capped: false,
        });

        fireEvent.click(await openDetail());

        await screen.findByTestId('word-list-notice');
        expect(confirmSpy).not.toHaveBeenCalled();
    });

    it('makes no API call when the confirmation is cancelled', async () => {
        vi.spyOn(window, 'confirm').mockReturnValue(false);
        installDetail(detailWithUnknown(3831), {});

        fireEvent.click(await openDetail());

        await waitFor(() => expect(window.confirm).toHaveBeenCalled());
        expect(calls.some(c => c.url.includes('mark-unknown-learning'))).toBe(false);
    });

    it('calls the API when the confirmation is accepted', async () => {
        vi.spyOn(window, 'confirm').mockReturnValue(true);
        installDetail(detailWithUnknown(3831), {
            list_id: 2, marked: 500, marked_item_ids: [], skipped_unresolved: 0,
            skipped_ambiguous: 0, remaining: 3331, capped: true,
        });

        fireEvent.click(await openDetail());

        await waitFor(() =>
            expect(calls.some(c => c.url.includes('mark-unknown-learning'))).toBe(true));
    });

    it('reports the remaining count after a capped response', async () => {
        vi.spyOn(window, 'confirm').mockReturnValue(true);
        installDetail(detailWithUnknown(3831), {
            list_id: 2, marked: 500, marked_item_ids: [], skipped_unresolved: 0,
            skipped_ambiguous: 0, remaining: 3331, capped: true,
        });

        fireEvent.click(await openDetail());

        const notice = await screen.findByTestId('word-list-notice');
        expect(notice).toHaveTextContent('Marked 500 words as learning.');
        expect(notice).toHaveTextContent('3331 remaining — click again to continue.');
    });

    it('leaves the uncapped message unchanged', async () => {
        installDetail(detailWithUnknown(5), {
            list_id: 2, marked: 5, marked_item_ids: [], skipped_unresolved: 0,
            skipped_ambiguous: 0, remaining: 0, capped: false,
        });

        fireEvent.click(await openDetail());

        const notice = await screen.findByTestId('word-list-notice');
        expect(notice).toHaveTextContent('Marked 5 words as learning.');
        expect(notice).not.toHaveTextContent('remaining');
    });

    it('handles a pre-cap backend that omits remaining/capped', async () => {
        installDetail(detailWithUnknown(5), {
            list_id: 2, marked: 5, marked_item_ids: [], skipped_unresolved: 0, skipped_ambiguous: 0,
        });

        fireEvent.click(await openDetail());

        const notice = await screen.findByTestId('word-list-notice');
        expect(notice).toHaveTextContent('Marked 5 words as learning.');
        expect(notice).not.toHaveTextContent('remaining');
    });
});

/**
 * Large-list paged fetching (#43 phase 2).
 *
 * The backend windows `entries` via `limit`/`offset` while keeping `total` and
 * `counts` whole-list on every page. The page therefore holds only what it has
 * fetched, and "Show more" is a request rather than a reveal.
 *
 * The stub below pages exactly the way `word_list_service.get_list` does —
 * slice `entries`, leave `total`/`counts` alone — because the interesting
 * failure is a component that reads the wrong one of those two.
 */
describe('WordListsPage — paged detail fetching', () => {
    const CHUNK = 200;

    function bigDetail(n: number, overrides: Partial<WordListDetail> = {}): WordListDetail {
        return makeDetail({
            list_id: 2,
            name: 'Top German Words',
            is_system: true,
            total: n,
            counts: { known: 0, learning: 0, unknown: n, unresolved: 0, ambiguous: 0 },
            entries: Array.from({ length: n }, (_, i) => ({
                id: i + 1,
                surface: `w${i}`,
                item_id: i + 1,
                item_type: 'word' as const,
                status: 'unknown' as const,
            })),
            ...overrides,
        });
    }

    /** Window `entries` per the URL's limit/offset; `total`/`counts` untouched. */
    function pageOf(all: WordListDetail, url: string): WordListDetail {
        const params = new URL(url, 'http://test.local').searchParams;
        const rawLimit = params.get('limit');
        const offset = Number(params.get('offset') ?? '0');
        const limit = rawLimit === null ? all.entries.length : Number(rawLimit);
        return { ...all, entries: all.entries.slice(offset, offset + limit) };
    }

    /** Query params of every list-detail GET, in call order. */
    function detailQueries(): URLSearchParams[] {
        return calls
            .filter(c => /word-lists\/\d+(\?|$)/.test(c.url) && c.init?.method === undefined)
            .map(c => new URL(c.url, 'http://test.local').searchParams);
    }

    function installLists(listSummaries: unknown[], detailFor: (id: string) => WordListDetail,
                          markResult?: unknown) {
        installFetch((url, init) => {
            if (url.includes('/word-lists') && (!init || init.method === undefined) && !url.match(/word-lists\/\d/)) {
                return jsonResponse(listSummaries);
            }
            if (url.includes('mark-unknown-learning')) {
                return jsonResponse(markResult ?? {
                    list_id: 2, marked: 0, marked_item_ids: [],
                    skipped_unresolved: 0, skipped_ambiguous: 0, remaining: 0, capped: false,
                });
            }
            const id = url.match(/word-lists\/(\d+)/)?.[1] ?? '2';
            return jsonResponse(pageOf(detailFor(id), url));
        });
    }

    function visibleEntries() {
        return screen.queryAllByTestId(/^word-list-entry-/);
    }

    async function openBig(n = 5035, lists?: unknown[]) {
        installLists(
            lists ?? [summary({ list_id: 2, name: 'Top German Words', is_system: true })],
            id => bigDetail(n, { list_id: Number(id) }),
        );
        renderPage();
        fireEvent.click(await screen.findByTestId('word-list-open-2'));
        await screen.findByTestId('word-list-detail');
    }

    it('asks for the first page only when a list is opened', async () => {
        await openBig();

        const [first] = detailQueries();
        expect(first.get('limit')).toBe('200');
        expect(first.get('offset')).toBe('0');
    });

    it('renders only the first page of a large list', async () => {
        await openBig();

        expect(visibleEntries()).toHaveLength(CHUNK);
        expect(screen.getByTestId('word-list-entry-w0')).toBeInTheDocument();
        expect(screen.queryByTestId('word-list-entry-w200')).not.toBeInTheDocument();
    });

    it('counts the footer against the whole-list total, not the loaded page', async () => {
        await openBig();

        expect(screen.getByTestId('word-list-more')).toHaveTextContent(
            'Showing 200 of 5,035 entries',
        );
    });

    it('never reports the page size as the total', async () => {
        // The regression this phase exists to prevent: reading the denominator
        // off `entries.length` renders "Showing 200 of 200" the moment the
        // response is paged, and hides Show more with 4,835 entries unseen.
        await openBig();

        const footer = screen.getByTestId('word-list-more');
        expect(footer).not.toHaveTextContent('Showing 200 of 200 entries');
        expect(screen.getByTestId('word-list-show-more')).toBeInTheDocument();
    });

    it('requests the next page by offset on Show more', async () => {
        await openBig();

        fireEvent.click(screen.getByTestId('word-list-show-more'));

        await waitFor(() => expect(detailQueries()).toHaveLength(2));
        const second = detailQueries()[1];
        expect(second.get('offset')).toBe('200');
        expect(second.get('limit')).toBe('200');
    });

    it('appends the next page without duplicating what is already loaded', async () => {
        await openBig();

        fireEvent.click(screen.getByTestId('word-list-show-more'));

        await waitFor(() => expect(visibleEntries()).toHaveLength(CHUNK * 2));
        // Page 1 kept, page 2 added, and no surface rendered twice.
        expect(screen.getByTestId('word-list-entry-w0')).toBeInTheDocument();
        expect(screen.getByTestId('word-list-entry-w200')).toBeInTheDocument();
        expect(screen.queryAllByTestId('word-list-entry-w0')).toHaveLength(1);
        expect(screen.getByTestId('word-list-more')).toHaveTextContent(
            'Showing 400 of 5,035 entries',
        );
    });

    it('hides the control once the final partial page has loaded', async () => {
        await openBig(450);

        fireEvent.click(screen.getByTestId('word-list-show-more'));
        await waitFor(() => expect(visibleEntries()).toHaveLength(400));
        fireEvent.click(screen.getByTestId('word-list-show-more'));

        // Third page is the 50-entry remainder; nothing is left to fetch.
        await waitFor(() => expect(visibleEntries()).toHaveLength(450));
        expect(screen.queryByTestId('word-list-more')).not.toBeInTheDocument();
        expect(screen.queryByTestId('word-list-show-more')).not.toBeInTheDocument();
    });

    it('renders a list that fits in one page with no extra controls', async () => {
        installLists([summary({ list_id: 1, is_system: false })], () => makeDetail({ list_id: 1 }));
        renderPage();

        fireEvent.click(await screen.findByTestId('word-list-open-1'));
        await screen.findByTestId('word-list-detail');

        expect(visibleEntries()).toHaveLength(4);
        expect(screen.queryByTestId('word-list-more')).not.toBeInTheDocument();
        expect(screen.queryByTestId('word-list-show-more')).not.toBeInTheDocument();
    });

    it('does not filter ambiguous or unresolved entries out of a page', async () => {
        // The window is positional, never filtered by status.
        const detail = bigDetail(3);
        detail.entries[1] = { id: 2, surface: 'Bank', item_id: null, item_type: 'word', status: 'ambiguous' };
        detail.entries[2] = { id: 3, surface: 'Blorptzk', item_id: null, item_type: 'word', status: 'unresolved' };
        installLists([summary({ list_id: 2, is_system: true })], () => detail);
        renderPage();

        fireEvent.click(await screen.findByTestId('word-list-open-2'));

        expect(await screen.findByTestId('word-list-entry-Bank')).toBeInTheDocument();
        expect(screen.getByTestId('word-list-entry-Blorptzk')).toBeInTheDocument();
    });

    it('disables Show more while a page is in flight and fires one request', async () => {
        const all = bigDetail(5035);
        const gate = deferred<Response>();
        let detailCalls = 0;
        installFetch((url, init) => {
            if (url.includes('/word-lists') && (!init || init.method === undefined) && !url.match(/word-lists\/\d/)) {
                return jsonResponse([summary({ list_id: 2, is_system: true })]);
            }
            detailCalls += 1;
            // First call is the open; the Show more that follows hangs until
            // the test releases it, so the double-click window stays open.
            return detailCalls === 1 ? jsonResponse(pageOf(all, url)) : gate.promise;
        });
        renderPage();
        fireEvent.click(await screen.findByTestId('word-list-open-2'));
        await screen.findByTestId('word-list-detail');

        fireEvent.click(screen.getByTestId('word-list-show-more'));
        await waitFor(() => expect(screen.getByTestId('word-list-show-more')).toBeDisabled());
        expect(screen.getByTestId('word-list-show-more')).toHaveTextContent('Loading…');

        fireEvent.click(screen.getByTestId('word-list-show-more'));
        fireEvent.click(screen.getByTestId('word-list-show-more'));
        expect(detailCalls).toBe(2);   // the open + exactly one page

        gate.resolve(jsonResponse(pageOf(all, '/x?limit=200&offset=200')));
        await waitFor(() => expect(visibleEntries()).toHaveLength(CHUNK * 2));
        expect(screen.getByTestId('word-list-show-more')).toBeEnabled();
    });

    it('keeps loaded entries and shows an inline error when a page fails', async () => {
        const all = bigDetail(5035);
        let detailCalls = 0;
        installFetch((url, init) => {
            if (url.includes('/word-lists') && (!init || init.method === undefined) && !url.match(/word-lists\/\d/)) {
                return jsonResponse([summary({ list_id: 2, is_system: true })]);
            }
            detailCalls += 1;
            return detailCalls === 1
                ? jsonResponse(pageOf(all, url))
                : jsonResponse({ detail: 'Server exploded' }, 500);
        });
        renderPage();
        fireEvent.click(await screen.findByTestId('word-list-open-2'));
        await screen.findByTestId('word-list-detail');

        fireEvent.click(screen.getByTestId('word-list-show-more'));

        expect(await screen.findByTestId('word-list-more-error')).toHaveTextContent('Server exploded');
        // Nothing already fetched is thrown away, and the control comes back.
        expect(visibleEntries()).toHaveLength(CHUNK);
        expect(screen.getByTestId('word-list-entry-w0')).toBeInTheDocument();
        expect(screen.getByTestId('word-list-show-more')).toBeEnabled();
    });

    it('loads the next page on retry after a failure', async () => {
        const all = bigDetail(5035);
        let detailCalls = 0;
        installFetch((url, init) => {
            if (url.includes('/word-lists') && (!init || init.method === undefined) && !url.match(/word-lists\/\d/)) {
                return jsonResponse([summary({ list_id: 2, is_system: true })]);
            }
            detailCalls += 1;
            // Only the first Show more fails.
            return detailCalls === 2
                ? jsonResponse({ detail: 'Server exploded' }, 500)
                : jsonResponse(pageOf(all, url));
        });
        renderPage();
        fireEvent.click(await screen.findByTestId('word-list-open-2'));
        await screen.findByTestId('word-list-detail');

        fireEvent.click(screen.getByTestId('word-list-show-more'));
        await screen.findByTestId('word-list-more-error');

        fireEvent.click(screen.getByTestId('word-list-show-more'));

        await waitFor(() => expect(visibleEntries()).toHaveLength(CHUNK * 2));
        expect(screen.queryByTestId('word-list-more-error')).not.toBeInTheDocument();
        // Retried the SAME offset — the failed page left no gap behind it.
        expect(detailQueries()[2].get('offset')).toBe('200');
    });

    it('resets paging state when a different list is opened', async () => {
        await openBig(5035, [
            summary({ list_id: 2, name: 'Big', is_system: true }),
            summary({ list_id: 3, name: 'Also big', is_system: true }),
        ]);

        fireEvent.click(screen.getByTestId('word-list-show-more'));
        await waitFor(() => expect(visibleEntries()).toHaveLength(CHUNK * 2));

        fireEvent.click(screen.getByTestId('word-list-open-3'));

        await waitFor(() => expect(visibleEntries()).toHaveLength(CHUNK));
        // The new list starts from the top, not from where the old one stopped.
        expect(detailQueries().at(-1)!.get('offset')).toBe('0');
    });

    it('clears a page error when a different list is opened', async () => {
        const all = bigDetail(5035);
        let detailCalls = 0;
        installFetch((url, init) => {
            if (url.includes('/word-lists') && (!init || init.method === undefined) && !url.match(/word-lists\/\d/)) {
                return jsonResponse([
                    summary({ list_id: 2, name: 'Big', is_system: true }),
                    summary({ list_id: 3, name: 'Also big', is_system: true }),
                ]);
            }
            detailCalls += 1;
            return detailCalls === 2
                ? jsonResponse({ detail: 'Server exploded' }, 500)
                : jsonResponse(pageOf(all, url));
        });
        renderPage();
        fireEvent.click(await screen.findByTestId('word-list-open-2'));
        await screen.findByTestId('word-list-detail');

        fireEvent.click(screen.getByTestId('word-list-show-more'));
        await screen.findByTestId('word-list-more-error');

        fireEvent.click(screen.getByTestId('word-list-open-3'));

        await waitFor(() =>
            expect(screen.queryByTestId('word-list-more-error')).not.toBeInTheDocument(),
        );
    });

    it('resets to the first page after a mark-learning refresh', async () => {
        await openBig();
        vi.spyOn(window, 'confirm').mockReturnValue(true);

        fireEvent.click(screen.getByTestId('word-list-show-more'));
        await waitFor(() => expect(visibleEntries()).toHaveLength(CHUNK * 2));

        fireEvent.click(screen.getByTestId('word-list-mark-learning'));

        await waitFor(() => expect(visibleEntries()).toHaveLength(CHUNK));
        expect(detailQueries().at(-1)!.get('offset')).toBe('0');
    });

    it('keeps counts, export and mark-learning driven by the WHOLE list', async () => {
        await openBig();

        // 5,035 unknown, though only 200 rows have been fetched.
        expect(screen.getByTestId('word-list-count-unknown')).toHaveTextContent('5035');
        expect(screen.getByTestId('word-list-mark-learning')).toHaveTextContent('5035');
        expect(screen.getByTestId('word-list-download')).toBeInTheDocument();
    });

    it('keeps whole-list counts after a page is appended', async () => {
        await openBig();

        fireEvent.click(screen.getByTestId('word-list-show-more'));
        await waitFor(() => expect(visibleEntries()).toHaveLength(CHUNK * 2));

        // Appending entries must not let a page's own metadata overwrite these.
        expect(screen.getByTestId('word-list-count-unknown')).toHaveTextContent('5035');
        expect(screen.getByTestId('word-list-mark-learning')).toHaveTextContent('5035');
    });

    it('leaves the built-in badge and delete-hiding untouched', async () => {
        installLists([summary({ list_id: 2, name: 'Top German Words', is_system: true })],
            () => bigDetail(5035));
        renderPage();

        expect(await screen.findByTestId('word-list-system-badge-2')).toBeInTheDocument();
        expect(screen.queryByTestId('word-list-delete-2')).not.toBeInTheDocument();

        fireEvent.click(screen.getByTestId('word-list-open-2'));
        expect(await screen.findByTestId('word-list-detail-system-badge')).toBeInTheDocument();
    });

    /**
     * Two lists whose surfaces are told apart by prefix, so a page appended to
     * the wrong one is visible rather than merely miscounted.
     */
    function prefixed(listId: number, n: number, prefix: string): WordListDetail {
        return makeDetail({
            list_id: listId,
            name: `List ${listId}`,
            is_system: true,
            total: n,
            counts: { known: 0, learning: 0, unknown: n, unresolved: 0, ambiguous: 0 },
            entries: Array.from({ length: n }, (_, i) => ({
                id: i + 1,
                surface: `${prefix}${i}`,
                item_id: i + 1,
                item_type: 'word' as const,
                status: 'unknown' as const,
            })),
        });
    }

    /**
     * List 2 (`a…`) and list 3 (`b…`), with list 2's *second* page held open by
     * a gate the test resolves by hand. That is the window in which the user
     * switches lists, which is what these three tests are about.
     */
    function installTwoListsWithGatedSecondPage() {
        const a = prefixed(2, 5035, 'a');
        const b = prefixed(3, 5035, 'b');
        const gate = deferred<Response>();
        installFetch((url, init) => {
            if (url.includes('/word-lists') && (!init || init.method === undefined) && !url.match(/word-lists\/\d/)) {
                return jsonResponse([
                    summary({ list_id: 2, name: 'List 2', is_system: true }),
                    summary({ list_id: 3, name: 'List 3', is_system: true }),
                ]);
            }
            const id = url.match(/word-lists\/(\d+)/)![1];
            const offset = new URL(url, 'http://test.local').searchParams.get('offset');
            if (id === '2' && offset === '200') return gate.promise;
            return jsonResponse(pageOf(id === '2' ? a : b, url));
        });
        return { a, gate };
    }

    /** Open list 2, start its second page, leave it hanging, switch to list 3. */
    async function stallListTwoThenOpenThree() {
        const rig = installTwoListsWithGatedSecondPage();
        renderPage();

        fireEvent.click(await screen.findByTestId('word-list-open-2'));
        await screen.findByTestId('word-list-entry-a0');
        fireEvent.click(screen.getByTestId('word-list-show-more'));
        await waitFor(() => expect(screen.getByTestId('word-list-show-more')).toBeDisabled());

        fireEvent.click(screen.getByTestId('word-list-open-3'));
        await screen.findByTestId('word-list-entry-b0');
        return rig;
    }

    it('does not let one list’s pending page block another list’s paging', async () => {
        // The guard is keyed per list+offset, so list 3 asking for its own page
        // is a different request, not a duplicate of the one still hanging.
        await stallListTwoThenOpenThree();

        expect(screen.getByTestId('word-list-show-more')).toBeEnabled();
        fireEvent.click(screen.getByTestId('word-list-show-more'));

        await waitFor(() => expect(visibleEntries()).toHaveLength(CHUNK * 2));
        expect(screen.getByTestId('word-list-entry-b200')).toBeInTheDocument();
        expect(detailQueries().at(-1)!.get('offset')).toBe('200');
    });

    it('still refuses a duplicate request for the same list and page', async () => {
        // Same rig, but the click that repeats is list 2's own pending page.
        const a = prefixed(2, 5035, 'a');
        const gate = deferred<Response>();
        let pageTwoRequests = 0;
        installFetch((url, init) => {
            if (url.includes('/word-lists') && (!init || init.method === undefined) && !url.match(/word-lists\/\d/)) {
                return jsonResponse([summary({ list_id: 2, name: 'List 2', is_system: true })]);
            }
            if (new URL(url, 'http://test.local').searchParams.get('offset') === '200') {
                pageTwoRequests += 1;
                return gate.promise;
            }
            return jsonResponse(pageOf(a, url));
        });
        renderPage();

        fireEvent.click(await screen.findByTestId('word-list-open-2'));
        await screen.findByTestId('word-list-entry-a0');
        fireEvent.click(screen.getByTestId('word-list-show-more'));
        await waitFor(() => expect(screen.getByTestId('word-list-show-more')).toBeDisabled());
        fireEvent.click(screen.getByTestId('word-list-show-more'));

        expect(pageTwoRequests).toBe(1);

        gate.resolve(jsonResponse(pageOf(a, '/x?limit=200&offset=200')));
        await waitFor(() => expect(visibleEntries()).toHaveLength(CHUNK * 2));
    });

    it('still refuses a duplicate after navigating away and back', async () => {
        // The case that rules out both a single-slot guard and clearing the
        // guard in showDetail: either would forget list 2's pending page while
        // the user is on list 3, and re-issue it on the way back.
        const a = prefixed(2, 5035, 'a');
        const b = prefixed(3, 5035, 'b');
        const gate = deferred<Response>();
        let pageTwoRequests = 0;
        installFetch((url, init) => {
            if (url.includes('/word-lists') && (!init || init.method === undefined) && !url.match(/word-lists\/\d/)) {
                return jsonResponse([
                    summary({ list_id: 2, name: 'List 2', is_system: true }),
                    summary({ list_id: 3, name: 'List 3', is_system: true }),
                ]);
            }
            const id = url.match(/word-lists\/(\d+)/)![1];
            const offset = new URL(url, 'http://test.local').searchParams.get('offset');
            if (id === '2' && offset === '200') {
                pageTwoRequests += 1;
                return gate.promise;
            }
            return jsonResponse(pageOf(id === '2' ? a : b, url));
        });
        renderPage();

        fireEvent.click(await screen.findByTestId('word-list-open-2'));
        await screen.findByTestId('word-list-entry-a0');
        fireEvent.click(screen.getByTestId('word-list-show-more'));
        await waitFor(() => expect(screen.getByTestId('word-list-show-more')).toBeDisabled());

        fireEvent.click(screen.getByTestId('word-list-open-3'));
        await screen.findByTestId('word-list-entry-b0');
        fireEvent.click(screen.getByTestId('word-list-open-2'));
        await screen.findByTestId('word-list-entry-a0');

        fireEvent.click(screen.getByTestId('word-list-show-more'));

        expect(pageTwoRequests).toBe(1);
    });

    it('drops a late page from the list the user navigated away from', async () => {
        const { a, gate } = await stallListTwoThenOpenThree();

        await act(async () => {
            gate.resolve(jsonResponse(pageOf(a, '/x?limit=200&offset=200')));
        });

        // List 3 keeps its own single page; list 2's entries never appear.
        expect(visibleEntries()).toHaveLength(CHUNK);
        expect(screen.getByTestId('word-list-entry-b0')).toBeInTheDocument();
        expect(screen.queryByTestId('word-list-entry-a200')).not.toBeInTheDocument();
    });

    it('drops a late failure from the list the user navigated away from', async () => {
        const { gate } = await stallListTwoThenOpenThree();

        await act(async () => {
            gate.resolve(jsonResponse({ detail: 'Server exploded' }, 500));
        });

        expect(screen.queryByTestId('word-list-more-error')).not.toBeInTheDocument();
        expect(screen.getByTestId('word-list-show-more')).toBeEnabled();
    });

    it('shows no paging control for a freshly created list', async () => {
        // POST is the one path that fills `detail` without going through the
        // paged GET — it returns the whole list, capped at MAX_LIST_WORDS. If
        // `hasMore` ever read something other than total-vs-loaded, this is
        // where a Show more button would appear for a page that cannot exist.
        const created = bigDetail(300, { list_id: 7, is_system: false });
        installIndexThen(() => jsonResponse(created, 201));
        renderPage();

        fireEvent.change(screen.getByTestId('word-list-input'), { target: { value: 'Haus' } });
        fireEvent.click(screen.getByTestId('word-list-create'));

        await screen.findByTestId('word-list-detail');
        expect(visibleEntries()).toHaveLength(300);
        expect(screen.queryByTestId('word-list-more')).not.toBeInTheDocument();
    });

    it('files a newly created list under Your lists, not the built-ins', async () => {
        installFetch((url, init) => {
            if (url.includes('/word-lists') && (!init || init.method === undefined) && !url.match(/word-lists\/\d/)) {
                return jsonResponse([summary({ list_id: 2, name: 'Top German Words', is_system: true })]);
            }
            return jsonResponse(makeDetail({ list_id: 9, name: 'Fresh' }), 201);
        });
        renderPage();

        await screen.findByTestId('word-list-system-section');
        fireEvent.change(screen.getByTestId('word-list-input'), { target: { value: 'Haus' } });
        fireEvent.click(screen.getByTestId('word-list-create'));

        // Delete is offered, which only happens outside the built-in section.
        expect(await screen.findByTestId('word-list-delete-9')).toBeInTheDocument();
        expect(
            screen.getByTestId('word-list-system-section').contains(screen.getByTestId('word-list-open-9')),
        ).toBe(false);
    });

    it('leaves a user-created list working end to end', async () => {
        installLists([summary({ list_id: 1, name: 'My list', is_system: false })],
            () => makeDetail({ list_id: 1 }));
        renderPage();

        // Delete stays offered, the detail opens, and every entry renders.
        expect(await screen.findByTestId('word-list-delete-1')).toBeInTheDocument();
        fireEvent.click(screen.getByTestId('word-list-open-1'));
        await screen.findByTestId('word-list-detail');

        expect(screen.queryByTestId('word-list-detail-system-badge')).not.toBeInTheDocument();
        expect(screen.getByTestId('word-list-entry-Haus')).toBeInTheDocument();
        expect(screen.getByTestId('word-list-entry-Bank')).toBeInTheDocument();
        expect(screen.getByTestId('word-list-download')).toBeInTheDocument();
    });
});
