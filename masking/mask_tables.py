#!/usr/bin/env python3
"""
Process Docling JSON results to create masked PDFs.

Run this AFTER you've run Docling to mask tables/figures in PDFs.
"""

import fitz  # PyMuPDF
from pathlib import Path
import sys
import logging
import shutil

sys.path.insert(0, str(Path(__file__).parent.parent))
from visualize_docling_full import reconstruct_tables_from_lists

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

# load the correct file
# for element in elements
# if element is caption or table or reconstructed table, redact the coordinates x1, x2, y1, y2

def mask_pdf_from_json(pdf_path: Path, json_path: Path, output_dir: Path, return_elements: bool = False):
    """
    Mask tables/figures in PDF using Docling JSON output.

    Args:
        pdf_path: Original PDF
        json_path: Docling JSON with bounding boxes
        output_dir: Where to save masked PDF
        return_elements: If True, return (masked_pdf_path, text_elements, masked_elements)

    Returns:
        Path to masked PDF, or tuple if return_elements=True
    """
    logger.info(f"Processing: {pdf_path.name}")

    # Load JSON
    if not json_path.exists():
        logger.warning(f"  No JSON found: {json_path}")
        return None

    # Reconstruct tables from raw Docling elements
    logger.info("  Reconstructing tables from elements...")
    elements = reconstruct_tables_from_lists(json_path)

    if not elements:
        logger.info("  No figures/tables detected")
        return None

    # Count maskable elements
    maskable_count = sum(1 for el in elements
                        if el.get('type') in ["PICTURE", "TABLE", "CAPTION", "RECONSTRUCTED_TABLE"])
    logger.info(f"  Found {maskable_count} maskable elements (including {sum(1 for el in elements if el.get('type') == 'RECONSTRUCTED_TABLE')} reconstructed tables)")

    # Separate elements into text and maskable (tables/figures)
    text_elements = []
    masked_elements = []
    maskable_types = {"PICTURE", "TABLE", "CAPTION", "RECONSTRUCTED_TABLE"}

    # Open PDF
    doc = fitz.open(str(pdf_path))
    masked_count = 0

    # Mask each figure/table and categorize all elements
    for item in elements:
        type_ = item.get('type', '')
        region = item.get('bbox', {})

        # Categorize element
        if type_ in maskable_types:
            masked_elements.append(item)
        else:
            # Text-like elements
            if type_ in {'TEXT', 'PARAGRAPH', 'SECTION_HEADER', 'TITLE', 'LIST', 'LIST_ITEM'}:
                text_elements.append(item)

        # Mask visual elements
        try:
            if type_ not in maskable_types or not region:
                continue

            page_num = item.get('page', 0) - 1
            if page_num >= len(doc):
                continue

            page = doc[page_num]
            page_height = page.rect.height

            # Convert from Docling coordinates (origin bottom-left, y-up)
            # to PyMuPDF coordinates (origin top-left, y-down)
            x1 = region.get('x1', 0)
            y1_docling = region.get('y1', 0)
            x2 = region.get('x2', 0)
            y2_docling = region.get('y2', 0)

            # Transform Y coordinates: flip vertically
            y1 = page_height - y1_docling
            y2 = page_height - y2_docling

            # Create rect with proper coordinates
            rect = fitz.Rect(x1, y1, x2, y2)

            # Use redaction to actually REMOVE content (not just cover it)
            page.add_redact_annot(rect, fill=(1, 1, 1))
            masked_count += 1

            fig_text = item.get('text', 'NO_TEXT')
            logger.info(f"    ✓ Masked element {type_} with text = {fig_text} on page {page_num + 1}")

        except Exception as e:
            logger.warning(f"    Failed to mask region: {e}")

    # Apply all redactions (actually removes content from PDF)
    for page_num in range(len(doc)):
        page = doc[page_num]
        page.apply_redactions()

    # Save masked PDF
    masked_pdf = output_dir / f"{pdf_path.stem}_masked.pdf"
    doc.save(str(masked_pdf))
    doc.close()

    logger.info(f"  ✓ Saved masked PDF: {masked_pdf}")
    logger.info(f"  ✓ Masked {masked_count} regions")
    logger.info(f"  ✓ Extracted {len(text_elements)} text elements\n")

    if return_elements:
        return masked_pdf, text_elements, masked_elements
    return masked_pdf


def process_pdf_with_masking(pdf_path: Path, json_path: Path, output_dir: Path = None):
    """
    Process PDF with table reconstruction and masking.
    Returns masked PDF path and separated text/masked elements.

    Args:
        pdf_path: Path to PDF file
        json_path: Path to Docling JSON file
        output_dir: Output directory for masked PDF (default: files/small_masked)

    Returns:
        Tuple of (masked_pdf_path, text_elements, masked_elements)
    """
    if output_dir is None:
        output_dir = Path('files/small_masked')
    output_dir.mkdir(exist_ok=True)

    return mask_pdf_from_json(pdf_path, json_path, output_dir, return_elements=True)


def main():
    """Process all Docling results."""
    import time

    pdf_dir = Path('files/book_pdfs')
    json_dir = Path('out/docling_full')
    output_base = Path('files/small_masked')
    output_base.mkdir(exist_ok=True)

    # Find all JSON files
    json_files = sorted(json_dir.glob('*.json'))

    if not json_files:
        logger.error("No JSON files found in out/docling_full")
        logger.error("Did you run Docling first?")
        return

    logger.info(f"{'='*70}")
    logger.info(f"Processing {len(json_files)} Docling results")
    logger.info(f"{'='*70}\n")

    # Track statistics
    stats = {
        'processed': 0,
        'skipped_no_pdf': 0,
        'skipped_already_done': 0,
        'copied_no_figures': 0,
        'total_masked_regions': 0,
        'errors': 0
    }

    start_time = time.time()

    for idx, json_file in enumerate(json_files, 1):
        logger.info(f"\n[{idx}/{len(json_files)}] Processing: {json_file.stem}")

        # Find corresponding PDF
        pdf_name = json_file.stem.split('_full_layout')[0] + '.pdf'
        pdf_path = pdf_dir / pdf_name

        if not pdf_path.exists():
            logger.warning(f"  ✗ PDF not found: {pdf_name}")
            stats['skipped_no_pdf'] += 1
            continue

        # Check if already processed
        masked_pdf_path = output_base / f"{pdf_path.stem}_masked.pdf"
        if masked_pdf_path.exists():
            logger.info("  ✓ Already processed, skipping")
            stats['skipped_already_done'] += 1
            continue

        try:
            # Mask PDF (save directly to output_base)
            masked_pdf = mask_pdf_from_json(pdf_path, json_file, output_base)

            if masked_pdf:
                stats['processed'] += 1
            else:
                # No figures detected - copy original PDF
                output_pdf = output_base / f"{pdf_path.stem}_masked.pdf"
                shutil.copy2(pdf_path, output_pdf)
                logger.info("  ℹ No figures detected - copied original PDF")
                logger.info(f"  ✓ Saved: {output_pdf}\n")
                stats['copied_no_figures'] += 1

        except Exception as e:
            logger.error(f"  ✗ Error processing {json_file.stem}: {e}")
            stats['errors'] += 1

    elapsed = time.time() - start_time

    logger.info(f"\n{'='*70}")
    logger.info("✓ Processing complete!")
    logger.info(f"{'='*70}")
    logger.info(f"Total JSON files: {len(json_files)}")
    logger.info(f"Masked (with tables): {stats['processed']}")
    logger.info(f"Copied (no tables): {stats['copied_no_figures']}")
    logger.info(f"Skipped (already done): {stats['skipped_already_done']}")
    logger.info(f"Skipped (no PDF): {stats['skipped_no_pdf']}")
    logger.info(f"Errors: {stats['errors']}")
    logger.info(f"Time elapsed: {elapsed:.1f}s ({elapsed/60:.1f} minutes)")
    logger.info(f"Results saved to: {output_base.resolve()}")
    logger.info(f"{'='*70}")


if __name__ == '__main__':
    main()
