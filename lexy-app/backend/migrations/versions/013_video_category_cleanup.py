"""Fix video_category table: drop language column, seed YouTube categories, add FKs

Revision ID: 013
Revises: 012
Create Date: 2026-03-28
"""
from alembic import op

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None

# Standard YouTube categories returned by yt-dlp, plus a catch-all.
CATEGORIES = [
    "other",
    "Film & Animation",
    "Autos & Vehicles",
    "Music",
    "Pets & Animals",
    "Sports",
    "Short Movies",
    "Travel & Events",
    "Gaming",
    "Videoblogging",
    "People & Blogs",
    "Comedy",
    "Entertainment",
    "News & Politics",
    "Howto & Style",
    "Education",
    "Science & Technology",
    "Nonprofits & Activism",
]


def upgrade() -> None:
    # 1. Drop the FK and language column — categories are global, not per-language.
    op.execute("""
        ALTER TABLE video_category
            DROP CONSTRAINT IF EXISTS fk_category_language,
            DROP COLUMN IF EXISTS language
    """)

    # 2. Seed all standard YouTube categories.
    for cat in CATEGORIES:
        op.execute(
            f"INSERT INTO video_category (category) VALUES ('{cat}') ON CONFLICT DO NOTHING"
        )

    # 3. Normalise any existing video rows that have a category not in the table.
    op.execute("""
        UPDATE video
           SET category = 'other'
         WHERE category NOT IN (SELECT category FROM video_category)
    """)

    # 4. Add FK from video.category → video_category.category.
    op.execute("""
        ALTER TABLE video
            ADD CONSTRAINT fk_video_category
            FOREIGN KEY (category) REFERENCES video_category (category)
    """)

    # 5. Normalise any existing user_video_category rows pointing at unknown categories.
    op.execute("""
        DELETE FROM user_video_category
         WHERE video_category NOT IN (SELECT category FROM video_category)
    """)

    # 6. Add FK from user_video_category.video_category → video_category.category.
    op.execute("""
        ALTER TABLE user_video_category
            ADD CONSTRAINT fk_uvc_category
            FOREIGN KEY (video_category) REFERENCES video_category (category)
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE user_video_category DROP CONSTRAINT IF EXISTS fk_uvc_category")
    op.execute("ALTER TABLE video DROP CONSTRAINT IF EXISTS fk_video_category")
    # upgrade() dropped the language values without saving them, so this is not a
    # restore: every row gets the placeholder default 'de', not its original
    # per-row language. Restore from backup if the real values are needed.
    op.execute("ALTER TABLE video_category ADD COLUMN IF NOT EXISTS language TEXT NOT NULL DEFAULT 'de'")
    op.execute("""
        ALTER TABLE video_category
            ADD CONSTRAINT fk_category_language
            FOREIGN KEY (language) REFERENCES language_table (language) ON DELETE CASCADE
    """)
