# German Text Processing and Vocabulary Extraction

This script processes German text files sentence-by-sentence, extracts vocabulary using phrase_finder, filters to dictionary entries only, and stores results in both text files and PostgreSQL database.

## Features

- ✅ Merges and cleans German text (handles compound words, quotation marks)
- ✅ Filters out glossary entries and page numbers
- ✅ **Processes text sentence-by-sentence** for accurate word extraction
- ✅ Extracts vocabulary using phrase_finder's `get_words_array()`
- ✅ **Includes all words found by phrase_finder** (exact, fuzzy, and lemma matches)
- ✅ Counts word frequencies across all sentences
- ✅ **Orders results by frequency (most common first)**
- ✅ Saves results per file and combined
- ✅ Stores in PostgreSQL database with indexes
- ✅ Progress tracking with timestamps

## How It Works

1. **Text Cleaning**: Removes line numbers, page numbers, and glossary entries
2. **Sentence Segmentation**: Uses spaCy to split text into sentences
3. **Word Extraction**: For each sentence, calls `get_words_array()` from phrase_finder
   - Example: `"Ich lade morgen meine Freunde zum Essen ein"` → `['ich', 'jdn. (Akk) zu etw. einladen', 'morgen', 'mein', 'der Freund', 'zu', 'etw. (Akk) essen', 'ein']`
4. **Frequency Counting**: Counts occurrences of each word (includes exact, fuzzy, and lemma matches)
5. **Output**: Writes frequency-sorted lists to files and PostgreSQL

## Setup

### 1. Install Dependencies

```bash
pip install spacy psycopg2-binary python-dotenv
python -m spacy download de_core_news_sm
```

### 2. Configure Environment Variables

**There is no `.env` or `.env.example` in this folder, and this script does not
want one.** `db_config.py` loads the **repo-root** `.env`
(`Path(__file__).parent.parent / '.env'`), the same file the backend reads. Use
the root `.env.example` for the variable names:

```env
DB_NAME=german_vocabulary
DB_USER=postgres
DB_PASSWORD=your_password_here
DB_HOST=localhost
DB_PORT=5432
```

Those are also `db_config.py`'s built-in defaults, so an unset variable falls
back rather than failing. The root `.env` is gitignored.

*(Corrected 2026-08-25: this section used to say `cp .env.example .env` from
inside `postprocessing/`. No such file has ever existed here.)*

### 3. Create Database

```bash
createdb german_vocabulary
```

Or using psql:
```sql
CREATE DATABASE german_vocabulary;
```

## Usage

### Basic Usage

```bash
python postprocessing/script.py
```

The script will automatically **skip files that have already been processed** (by checking if output files exist in `postprocessing/results/`).

### Command-Line Options

```bash
# Force reprocessing of all files (ignore existing outputs)
python postprocessing/script.py --force

# Clear the database before processing
python postprocessing/script.py --clear-db

# Use parallel processing with 4 workers (faster!)
python postprocessing/script.py --workers 4

# Disable parallel processing (slower, but uses less memory)
python postprocessing/script.py --no-parallel

# Combine options
python postprocessing/script.py --force --clear-db --workers 8
```

**Options:**
- `--force`: Reprocess all files, even if output files already exist
- `--clear-db`: Delete all entries from the database before processing
- `--workers N`: Use N parallel workers (default: CPU count)
- `--no-parallel`: Disable parallel processing (sequential mode)

## Output

### Text Files (in `postprocessing/results/`)

Each file contains:
1. **WORDS BY FREQUENCY** - Most common words first (×count)
2. **ALPHABETICAL LIST** - All words sorted A-Z with counts
3. **DETAILED VIEW** - Grouped by word type (VERB, NOUN, ADJ, etc.), sorted by frequency

Files generated:
- `{filename}_analysis.txt` - Per file results
- `ALL_FILES_combined_analysis.txt` - Combined results

### PostgreSQL Database

Table: `word_occurrences`

Columns:
- `id` - Primary key
- `file_name` - Source file name
- `phrase` - Phrase as it appears in text
- `dictionary_entry` - Dictionary form (lemma/blueprint)
- `word_type` - POS tag (VERB, NOUN, etc.)
- `match_type` - Match type (exact, exact with article)
- `frequency` - Occurrence count
- `created_at` - Timestamp

Indexes on `dictionary_entry` and `file_name` for fast queries.

## Example Queries

```sql
-- Top 20 most common words across all files
SELECT dictionary_entry, SUM(frequency) as total
FROM word_occurrences
GROUP BY dictionary_entry
ORDER BY total DESC
LIMIT 20;

-- All words from a specific file
SELECT * FROM word_occurrences
WHERE file_name = '01_chapter1.txt'
ORDER BY frequency DESC;

-- All verbs ordered by frequency
SELECT dictionary_entry, frequency
FROM word_occurrences
WHERE word_type LIKE '%VERB%'
ORDER BY frequency DESC;
```

## Notes

- Reads **every** `.txt` file in `files/text/` — `FOLDER_PATH = Path('files/text')`
  (`script.py:268`) and `FOLDER_PATH.glob('*.txt')` (`script.py:292`). *(Corrected
  2026-08-25: this line used to claim it only processed files starting with `01`,
  configurable at line 243. Neither is true — there is no `01` filter in the
  script, and line 243 is inside `main()`'s argument parsing.)*
- **Run it from the repo root.** `FOLDER_PATH` and `OUTPUT_FOLDER` are relative
  paths, so `python postprocessing/script.py` works and running it from inside
  `postprocessing/` does not.
- Filters to **exact dictionary matches only** - words not in your dictionary are excluded
- Database connection is optional - script continues if connection fails
- All results are sorted by frequency in descending order
