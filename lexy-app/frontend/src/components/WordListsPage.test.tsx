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
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

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
