import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from .database import create_pool, close_pool, get_pool
from .routers.analytics import router as analytics_router
from .routers.books import router as books_router
from .routers.reading import router as reading_router
from .routers.reminders import router as reminders_router
from .routers.insights import router as insights_router
from .routers.phrases import router as phrases_router
from .routers.playlists import router as playlists_router
from .routers.recommendations import router as recommendations_router
from .routers.settings import router as settings_router
from .routers.auth import router as auth_router
from .routers.chat import router as chat_router
from .routers.matcher import router as matcher_router
from .routers.search import router as search_router
from .routers.srs import router as srs_router
from .routers.videos import router as videos_router
from .routers.words import router as words_router
from .routers.content_requests import router as content_requests_router
from .routers.notifications import router as notifications_router
from .routers.errors import router as errors_router
from .routers.account import router as account_router
from .routers.lemma_corrections import router as lemma_corrections_router
from .routers.word_lists import router as word_lists_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_pool()

    # Seed phrase_table from the verb dict already loaded by matcher_service.
    # ON CONFLICT DO NOTHING makes this safe on every restart.
    #
    # Known failure modes: DB errors (asyncpg.PostgresError), missing data file
    # (FileNotFoundError when matcher_service can't find data/final_result.txt),
    # or import failure if phrase_finder dependencies aren't installed.
    # The outer broad catch is intentional — startup must NEVER crash on a seed
    # failure; the manual POST /api/v1/phrases/seed endpoint can recover later.
    try:
        from .services import matcher_service, phrase_service
        pool = get_pool()
        await phrase_service.seed_from_blueprint_map(
            pool, matcher_service.get_blueprint_map(), language="de"
        )
    except (asyncpg.PostgresError, FileNotFoundError, ImportError) as exc:
        logger.warning("Phrase table seed failed at startup (known mode): %s", exc, exc_info=True)
    except Exception:
        # Defensive: unknown failure mode. Log full trace; app still starts.
        logger.exception("Phrase table seed failed at startup (unexpected error)")

    # Seed grammar_rule_table with the curated German rule set.
    # ON CONFLICT (slug, language) DO NOTHING makes this idempotent.
    try:
        from .services import grammar_service
        pool = get_pool()
        await grammar_service.seed_rules(pool, language="de")
    except asyncpg.PostgresError as exc:
        logger.warning("Grammar rule seed failed at startup (DB error): %s", exc, exc_info=True)
    except Exception:
        logger.exception("Grammar rule seed failed at startup (unexpected error)")

    # Resume any pending content requests left over from a previous run.
    # Failure modes: DB unavailable (asyncpg.PostgresError) or subprocess can't
    # be spawned (OSError — bad scraper path, missing python). Broad catch
    # remains as a final safety net so a pending-request scan can't crash startup.
    try:
        from .routers.content_requests import _spawn_pipeline
        from .services import content_request_service
        pool = get_pool()
        count = await content_request_service.count_pending(pool)
        if count:
            await _spawn_pipeline()
    except (asyncpg.PostgresError, OSError) as exc:
        logger.warning("Failed to resume pending content requests (known mode): %s", exc, exc_info=True)
    except Exception:
        logger.exception("Failed to resume pending content requests (unexpected error)")

    yield
    await close_pool()


def _docs_enabled() -> bool:
    """Whether to expose the interactive API docs + schema (S11).

    Off by default so a production deploy doesn't publish `/docs`, `/redoc`, or
    `/openapi.json` (which enumerates every route and model). Opt in for local
    development with `ENABLE_DOCS=true`. Matches the `ENABLE_HSTS` env idiom.
    """
    return os.getenv("ENABLE_DOCS", "").lower() in ("1", "true", "yes")


_DOCS_ENABLED = _docs_enabled()

app = FastAPI(
    title="Lexy Clone",
    lifespan=lifespan,
    # S11: no interactive docs / schema unless explicitly enabled (e.g. local dev).
    docs_url="/docs" if _DOCS_ENABLED else None,
    redoc_url="/redoc" if _DOCS_ENABLED else None,
    openapi_url="/openapi.json" if _DOCS_ENABLED else None,
)


def _parse_cors_origins(raw: str | None) -> list[str]:
    """Parse a comma-separated CORS_ORIGINS env var.

    - whitespace around each origin is stripped
    - empty entries are dropped
    - falsy input (None / empty / whitespace-only) falls back to localhost dev origin
    """
    default = ["http://localhost:5173"]
    if not raw:
        return default
    origins = [piece.strip() for piece in raw.split(",")]
    origins = [o for o in origins if o]
    return origins or default


CORS_ORIGINS = _parse_cors_origins(os.getenv("CORS_ORIGINS"))

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# Security headers (S5). Added after CORS so it is the OUTERMOST middleware and
# stamps headers on every response, including CORS-handled preflights and error
# responses. HSTS within is gated on ENABLE_HSTS.
from .core.security_headers import SecurityHeadersMiddleware  # noqa: E402
app.add_middleware(SecurityHeadersMiddleware)

app.include_router(search_router,          prefix="/api")       # /api/search, /api/suggest, etc.
app.include_router(auth_router,            prefix="/api/v1")    # /api/v1/auth/register, /api/v1/auth/login
app.include_router(words_router,           prefix="/api/v1")    # /api/v1/words/knowledge, /api/v1/words/{type}/{id}/status
app.include_router(videos_router,          prefix="/api/v1")    # /api/v1/videos/{video_id}/reading-stats
app.include_router(matcher_router,         prefix="/api/v1")    # /api/v1/sentences/match
app.include_router(phrases_router,         prefix="/api/v1")    # /api/v1/phrases, /api/v1/phrases/match, /api/v1/phrases/seed
app.include_router(srs_router,             prefix="/api/v1")    # /api/v1/srs/due, /api/v1/srs/review/{card_id}
app.include_router(chat_router,            prefix="/api/v1")    # /api/v1/chat/sessions, /api/v1/chat/sessions/{id}/messages
app.include_router(analytics_router,       prefix="/api/v1")    # /api/v1/analytics/...
app.include_router(insights_router,        prefix="/api/v1")    # /api/v1/insights/cards, /prep, /prep/generate-examples
app.include_router(playlists_router,       prefix="/api/v1")    # /api/v1/playlists/generate
app.include_router(recommendations_router, prefix="/api/v1")    # /api/v1/recommendations/sentences, /videos, /items
app.include_router(settings_router,        prefix="/api/v1")    # /api/v1/settings/preferences
app.include_router(books_router,           prefix="/api/v1")    # /api/v1/books/upload, /books, /books/{id}/...
app.include_router(reading_router,         prefix="/api/v1")    # /api/v1/books/{id}/selections, /reading/translate, /reading/explain
app.include_router(reminders_router,       prefix="/api/v1")    # /api/v1/reminders/summary
app.include_router(content_requests_router, prefix="/api/v1")  # /api/v1/content-requests
app.include_router(notifications_router,    prefix="/api/v1")  # /api/v1/notifications/stream
app.include_router(errors_router,           prefix="/api/v1")  # /api/v1/errors/client
app.include_router(account_router,          prefix="/api/v1")  # /api/v1/account (DELETE)
app.include_router(lemma_corrections_router, prefix="/api/v1")  # /api/v1/lemma-corrections, /api/v1/admin/lemma-corrections
app.include_router(word_lists_router,       prefix="/api/v1")  # /api/v1/word-lists, /word-lists/{id}/export

frontend_dist = Path(__file__).parent.parent / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="static")
