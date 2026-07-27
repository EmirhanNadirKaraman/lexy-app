# CLI report: emits a formatted vocabulary-coverage-by-file table to stdout,
# which IS the product. Intentionally left as print(), not logging.

import sys
from pathlib import Path
import psycopg2
from psycopg2.extras import RealDictCursor
import os

# Add parent directory to Python path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Now we can import from postprocessing
from postprocessing.db_config import DB_CONFIG


def get_words_in_file(file_name):
    """Get all words from a specific file."""
    conn = psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    dictionary_entry,
                    frequency,
                    match_type,
                    word_type
                FROM word_occurrences
                WHERE file_name = %s
                ORDER BY frequency DESC
                """,
                (file_name,)
            )

            results = cur.fetchall()

            # print(f"\n{'='*80}")
            # print(f"Words in: {file_name}")
            # print(f"{'='*80}\n")
            # print(f"{'Word/Phrase':<50} {'Frequency':>10} {'Match Type':>15}")
            # print("-"*80)

            # for row in results:
            #     print(f"{row['dictionary_entry']:<50} {row['frequency']:>10} {row['match_type']:>15}")

            # print(f"\nTotal: {len(results)} unique words")

            return results
    finally:
        conn.close()


def get_vocabulary_coverage():
    """Calculate what percentage of total vocabulary each file covers."""
    conn = psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)

    try:
        with conn.cursor() as cur:
            # Query to get coverage percentage per file
            query = """
            WITH total_vocab AS (
                -- Total unique words across all files
                SELECT COUNT(DISTINCT dictionary_entry) as total_unique_words
                FROM word_occurrences
            ),
            file_vocab AS (
                -- Unique words per file
                SELECT
                    file_name,
                    COUNT(DISTINCT dictionary_entry) as unique_words_in_file,
                    SUM(frequency) as total_word_occurrences
                FROM word_occurrences
                GROUP BY file_name
            )
            SELECT
                fv.file_name,
                fv.unique_words_in_file,
                tv.total_unique_words,
                ROUND((fv.unique_words_in_file::numeric / tv.total_unique_words * 100), 2) as coverage_percentage,
                fv.total_word_occurrences
            FROM file_vocab fv, total_vocab tv
            ORDER BY coverage_percentage DESC;
            """

            cur.execute(query)
            results = cur.fetchall()

            if not results:
                print("No data found in database. Run the processing script first.")
                return

            # Print header
            print("\n" + "="*100)
            print("VOCABULARY COVERAGE BY FILE")
            print("="*100)
            print(f"Total unique words across all files: {results[0]['total_unique_words']}")
            print("="*100)
            print()

            # Print table header
            print(f"{'File Name':<50} {'Unique Words':>15} {'Coverage %':>12} {'Total Words':>15}")
            print("-"*100)

            # Print each file
            for row in results:
                print(f"{row['file_name']:<50} {row['unique_words_in_file']:>15} {row['coverage_percentage']:>11}% {row['total_word_occurrences']:>15}")

            print()
            print("="*100)

            # Calculate cumulative coverage
            print("\nCUMULATIVE COVERAGE (Reading files in order of most unique words)")
            print("="*100)

            cumulative_query = """
            WITH file_vocab AS (
                SELECT
                    file_name,
                    COUNT(DISTINCT dictionary_entry) as unique_words
                FROM word_occurrences
                GROUP BY file_name
                ORDER BY unique_words DESC
            ),
            ranked_files AS (
                SELECT
                    file_name,
                    unique_words,
                    SUM(unique_words) OVER (ORDER BY unique_words DESC ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) as cumulative_words,
                    (SELECT COUNT(DISTINCT dictionary_entry) FROM word_occurrences) as total_words
                FROM file_vocab
            )
            SELECT
                file_name,
                unique_words,
                cumulative_words,
                total_words,
                ROUND((cumulative_words::numeric / total_words * 100), 2) as cumulative_coverage_pct
            FROM ranked_files
            ORDER BY unique_words DESC;
            """

            cur.execute(cumulative_query)
            cumulative_results = cur.fetchall()

            print(f"{'File Name':<50} {'Unique Words':>15} {'Cumulative':>15} {'Coverage %':>12}")
            print("-"*100)

            for row in cumulative_results:
                print(f"{row['file_name']:<50} {row['unique_words']:>15} {row['cumulative_words']:>15} {row['cumulative_coverage_pct']:>11}%")

            print()

            # Find how many files needed for 80%, 90%, 95% coverage
            print("\nFILES NEEDED FOR TARGET COVERAGE:")
            print("-"*100)

            targets = [50, 75, 80, 90, 95, 100]
            for target in targets:
                for idx, row in enumerate(cumulative_results, 1):
                    if row['cumulative_coverage_pct'] >= target:
                        print(f"{target}% coverage: {idx} file(s)")
                        break

            print()

    finally:
        conn.close()


def get_exclusive_words():
    """Show words that appear in only one file."""
    conn = psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)

    try:
        with conn.cursor() as cur:
            query = """
            SELECT
                file_name,
                COUNT(DISTINCT dictionary_entry) as exclusive_words
            FROM word_occurrences
            WHERE dictionary_entry IN (
                -- Words that appear in only one file
                SELECT dictionary_entry
                FROM word_occurrences
                GROUP BY dictionary_entry
                HAVING COUNT(DISTINCT file_name) = 1
            )
            GROUP BY file_name
            ORDER BY exclusive_words DESC;
            """

            cur.execute(query)
            results = cur.fetchall()

            print("\n" + "="*100)
            print("EXCLUSIVE WORDS (Words that appear ONLY in that file)")
            print("="*100)
            print(f"{'File Name':<70} {'Exclusive Words':>20}")
            print("-"*100)

            for row in results:
                print(f"{row['file_name']:<70} {row['exclusive_words']:>20}")

            print()

    finally:
        conn.close()

def get_all_words(file_path='data/real_final_result.txt'): 
    with open(file_path, 'r') as f:
        result = set([line.strip() for line in f.readlines()])
    return result


def find_optimal_reading_order(all_words, get_words_in_file):
    """
    Greedy algorithm to find the optimal reading order.
    At each step, it picks the file that provides the most UNSEEN target words.
    """
    target_vocab = set(all_words)
    seen_words = set()
    reading_order = []
    
    # Get a list of all available files
    available_files = sorted([f for f in os.listdir('files/text') if f.endswith('.txt')])
    
    # Pre-load vocabularies to avoid repeated database/file hits
    file_vocabs = {}
    for f in available_files:
        # Extract dictionary entries from each file
        file_vocabs[f] = set([row['dictionary_entry'] for row in get_words_in_file(f)])
    
    print(f"Starting optimization for {len(target_vocab)} target words across {len(available_files)} files...")

    while available_files:
        best_file = None
        best_new_words = set()

        for file_name in available_files:
            # Calculate intersection of (words in file) AND (target list) MINUS (words we already know)
            new_words_in_this_file = (file_vocabs[file_name] & target_vocab) - seen_words
            
            if len(new_words_in_this_file) > len(best_new_words):
                best_new_words = new_words_in_this_file
                best_file = file_name
        
        # If no book adds new words, we are finished (or remaining books are redundant)
        if not best_file or len(best_new_words) == 0:
            break

        # Update tracking
        seen_words.update(best_new_words)
        reading_order.append({
            'file': best_file,
            'new_words_added': len(best_new_words),
            'total_coverage_pct': (len(seen_words) / len(target_vocab)) * 100
        })
        
        # Remove the chosen file from availability
        available_files.remove(best_file)
        
        # print(f"Next book: {best_file} (+{len(best_new_words)} words). Total Coverage: {len(seen_words)/len(target_vocab):.2%}")

    return reading_order


def find_optimal_reading_order_with_history(all_words, get_words_in_file, read_files):
    """
    Initializes known words from already read books, then finds the next best books.
    """
    target_vocab = set(all_words)
    seen_words = set()
    
    # 1. INITIALIZE: Add words from books you have already read
    print(f"Initializing with {len(read_files)} read books...")
    for file_name in read_files:
        # Get dictionary entries from read books and add to known words
        known_from_book = set([row['dictionary_entry'] for row in get_words_in_file(file_name)])
        # We only care about words that were actually in our target list
        seen_words.update(known_from_book & target_vocab)
    
    initial_coverage = (len(seen_words) / len(target_vocab)) * 100
    print(f"Initial known vocabulary coverage: {initial_coverage:.2f}%")

    # 2. GREEDY LOOP: Find the next best books
    available_files = sorted([
        f for f in os.listdir('files/text') 
        if f.endswith('.txt') and f not in read_files
    ])
    
    reading_order = []
    file_vocabs = {f: set([row['dictionary_entry'] for row in get_words_in_file(f)]) for f in available_files}

    while available_files:
        best_file = None
        best_new_words = set()

        for file_name in available_files:
            # New words = (Words in this book ∩ Target list) - Already known words
            new_words_in_this_file = (file_vocabs[file_name] & target_vocab) - seen_words
            
            if len(new_words_in_this_file) > len(best_new_words):
                best_new_words = new_words_in_this_file
                best_file = file_name
        
        if not best_file or len(best_new_words) == 0:
            break

        seen_words.update(best_new_words)
        reading_order.append({
            'file': best_file,
            'new_words_added': len(best_new_words),
            'total_coverage_pct': (len(seen_words) / len(target_vocab)) * 100
        })
        
        available_files.remove(best_file)
        print(f"Next recommendation: {best_file} (+{len(best_new_words)} words). New Total: {len(seen_words)/len(target_vocab):.2%}")

    return reading_order


def find_optimal_reading_order_enhanced(all_words, get_words_in_file, read_files=None, external_known_words=None):
    """
    Finds the best reading order by accounting for:
    1. Books already read.
    2. A custom list of words you already know (external_known_words).
    """
    target_vocab = set(all_words)
    seen_words = set()
    
    # 1. ADD EXTERNAL KNOWN WORDS (Flashcards, Anki, etc.)
    if external_known_words:
        # Only add words that are actually in our target corpus
        known_set = set(external_known_words)
        seen_words.update(known_set & target_vocab)
        print(f"Added {len(seen_words)} known words from your external list.")

    # 2. ADD WORDS FROM PREVIOUSLY READ BOOKS
    if read_files:
        for file_name in read_files:
            known_from_book = set([row['dictionary_entry'] for row in get_words_in_file(file_name)])
            seen_words.update(known_from_book & target_vocab)
        print(f"Total known words after processing {len(read_files)} read books: {len(seen_words)}")

    initial_coverage = (len(seen_words) / len(target_vocab)) * 100
    print(f"Starting Coverage: {initial_coverage:.2f}%")

    # 3. GREEDY OPTIMIZATION
    available_files = [f for f in os.listdir('files/text') if f.endswith('.txt') and f not in (read_files or [])]
    file_vocabs = {f: set([row['dictionary_entry'] for row in get_words_in_file(f)]) for f in available_files}
    
    reading_order = []
    while available_files:
        best_file = None
        best_new_words = set()

        for file_name in available_files:
            # New words = (Words in this book ∩ Target list) - Everything currently known
            potential_new = (file_vocabs[file_name] & target_vocab) - seen_words
            
            if len(potential_new) > len(best_new_words):
                best_new_words = potential_new
                best_file = file_name
        
        if not best_file or not best_new_words:
            break

        seen_words.update(best_new_words)
        reading_order.append({
            'file': best_file,
            'new_words': len(best_new_words),
            'coverage': (len(seen_words) / len(target_vocab)) * 100
        })
        available_files.remove(best_file)
        print(f"Recommended: {best_file} (+{len(best_new_words)} words). Coverage: {len(seen_words)/len(target_vocab):.2%}")

    return reading_order

def get_known_words(): 
    with open('data/known_words.txt', 'r') as f: 
        return set([word.strip() for word in f.readlines()])

def manage_known_words(word, file_path='data/known_words.txt'):
    """
    Checks if a word is known. If not, prompts the user to add it to the file.
    """
    file_path = Path(file_path)
    
    # Create file if it doesn't exist
    if not file_path.exists():
        file_path.touch()

    # Read current known words
    with open(file_path, 'r', encoding='utf-8') as f:
        # Using a set for O(1) lookups
        known_words = {line.strip() for line in f if line.strip()}

    if word in known_words:
        print(f"✓ '{word}' is already in your known words.")
        return True

    # Prompt user
    choice = input(f"Do you know the word '{word}'? (y/n): ").strip().lower()
    
    if choice == 'y':
        known_words.add(word)
        # Save back to file, sorted alphabetically
        with open(file_path, 'w', encoding='utf-8') as f:
            for w in sorted(known_words):
                f.write(f"{w}\n")
        print(f"Added '{word}' to {file_path}.")
        return True
    else:
        print(f"'{word}' remains on your learning list.")
        return False

# Example usage:
# manage_known_words("laufen")

def main():
    """Main function - shows all analyses."""
    print("\nConnecting to database...")

    try:
        # Show vocabulary coverage analysis
        # get_vocabulary_coverage()
        # get_exclusive_words()

        # Example: Get words from a specific file
        # Uncomment to see words from a specific file:
        
        # all_words = get_all_words('data/b1_parsed.txt')
        all_words = get_all_words()
        words = set()

        # for file_name in sorted(os.listdir('files/text')):
        #     words_in_file = set([row['dictionary_entry'] for row in get_words_in_file(file_name)])
        #     top_freq_words = [word for word in words_in_file if word in all_words]
        #     words.update(top_freq_words)
        #     print(len(words) / len(all_words))

        read_files = [
            '01.Das Herz von Dresden (B1).txt', 
            '04.Heisse Spur in Muenchen (B1).txt', 
            # '07.Wiener Blut (B1).txt'
        ]

        known_words = get_known_words()

        # optimal_order = find_optimal_reading_order(all_words=all_words, get_words_in_file=get_words_in_file)
        optimal_order = find_optimal_reading_order_enhanced(
            all_words=all_words, 
            get_words_in_file=get_words_in_file, 
            read_files=read_files,
            external_known_words=known_words
        )
        
        for row in optimal_order: 
            print(row)

        remaining_words = all_words - words - known_words

        print(remaining_words)
        # for word in remaining_words: 
        #     manage_known_words(word=word)

    except psycopg2.Error as e:
        print(f"\nDatabase error: {e}")
        print("Make sure the database is running and you've processed some files.")
    except Exception as e:
        print(f"\nError: {e}")


if __name__ == '__main__':
    main()