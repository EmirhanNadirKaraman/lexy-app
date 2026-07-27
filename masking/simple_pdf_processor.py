#!/usr/bin/env python3
"""
Simplified PDF Processor
Masks PDFs and extracts text to files

Usage:
    python simple_pdf_processor.py
"""

import sys
import logging
from pathlib import Path
from typing import Optional

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Import Docling
try:
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.datamodel.base_models import InputFormat
    DOCLING_AVAILABLE = True
except ImportError as e:
    DOCLING_AVAILABLE = False
    logger.error(f"Docling not available: {e}")
    sys.exit(1)

# Import masking function
try:
    from masking.mask_tables import mask_pdf_from_json
    MASKING_AVAILABLE = True
except ImportError as e:
    MASKING_AVAILABLE = False
    logger.error(f"mask_tables not available: {e}")
    sys.exit(1)

# Import text processing utilities
try:
    from parsers.text_processing import remove_citations, ContextAwareStitcher
    TEXT_PROCESSING_AVAILABLE = True
except ImportError as e:
    TEXT_PROCESSING_AVAILABLE = False
    logger.warning(f"text_processing not available - text won't be stitched: {e}")


class SimplePDFProcessor:
    """Simple PDF processor that masks PDFs and extracts text."""

    def __init__(
        self,
        pdf_dir: str = "files/book_pdfs",
        masked_dir: str = "files/masked",
        text_dir: str = "files/text",
        json_dir: str = "files/json"
    ):
        """Initialize the processor."""
        self.pdf_dir = Path(pdf_dir)
        self.masked_dir = Path(masked_dir)
        self.text_dir = Path(text_dir)
        self.json_dir = Path(json_dir)

        # Create directories
        for d in [self.masked_dir, self.text_dir, self.json_dir]:
            d.mkdir(parents=True, exist_ok=True)

        # Initialize Docling converter
        pipeline_options = PdfPipelineOptions()
        pipeline_options.do_table_structure = False
        pipeline_options.do_ocr = True
        self.converter = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)}
        )

        self.stats = {
            'processed': 0,
            'skipped': 0,
            'errors': 0
        }

    def extract_layout_json(self, pdf_path: Path, output_json_path: Path) -> bool:
        """
        Extract PDF layout to JSON using Docling.

        Args:
            pdf_path: Path to the PDF file
            output_json_path: Where to save the JSON

        Returns:
            True if successful, False otherwise
        """
        try:
            logger.info(f"Extracting layout from {pdf_path.name}...")
            result = self.converter.convert(str(pdf_path))
            doc = result.document

            # Collect all elements
            all_elements = []
            for element, level in doc.iterate_items():
                label = str(getattr(element, "label", "UNKNOWN")).split('.')[-1].upper()

                if not (hasattr(element, 'prov') and element.prov):
                    continue

                prov = element.prov[0]
                bbox = prov.bbox

                # Get text content
                text = ""
                if hasattr(element, 'text'):
                    text = element.text
                elif hasattr(element, 'caption') and element.caption:
                    text = element.caption.text

                all_elements.append({
                    "type": label,
                    "page": prov.page_no,
                    "level": level,
                    "bbox": {"x1": bbox.l, "y1": bbox.t, "x2": bbox.r, "y2": bbox.b},
                    "text": text.strip() if text else None
                })

            # Get page dimensions
            page_dimensions = {
                no: {"width": p.size.width, "height": p.size.height}
                for no, p in doc.pages.items()
            }

            # Save to JSON
            import json
            with open(output_json_path, 'w', encoding='utf-8') as f:
                json.dump({
                    "metadata": {
                        "pdf_path": str(pdf_path),
                        "tool": "Docling"
                    },
                    "page_dimensions": page_dimensions,
                    "elements": all_elements
                }, f, indent=2)

            logger.info(f"  Extracted {len(all_elements)} elements to JSON")
            return True

        except Exception as e:
            logger.error(f"  Failed to extract layout: {e}")
            return False

    def detect_page_and_line_numbers(self, elements: list) -> list:
        """
        Detect page numbers and line numbers from layout elements.

        Args:
            elements: List of layout elements from JSON

        Returns:
            List of bounding boxes to mask
        """
        import re
        from collections import defaultdict

        mask_regions = []

        # Group elements by page
        by_page = defaultdict(list)
        for elem in elements:
            if elem.get('type') == 'TEXT' and elem.get('text'):
                page = elem.get('page', 1)
                by_page[page].append(elem)

        for page_num, page_elements in by_page.items():
            # Detect page numbers (usually at top or bottom, small text with just numbers)
            for elem in page_elements:
                text = elem.get('text', '').strip()
                bbox = elem.get('bbox', {})

                # Page number pattern: just digits, possibly with "Page" prefix
                if re.match(r'^(?:Page\s+)?\d+$', text, re.IGNORECASE):
                    # Check if it's in typical page number location (top 10% or bottom 10% of page)
                    # This is a heuristic - you may need to adjust
                    y1, _y2 = bbox.get('y1', 0), bbox.get('y2', 0)
                    # Assume page height ~792 points (US Letter)
                    if y1 < 80 or y1 > 700:  # Top or bottom margin
                        mask_regions.append({
                            'page': page_num,
                            'bbox': bbox,
                            'type': 'PAGE_NUMBER'
                        })
                        continue

                # Line number pattern: single or double digit on left/right margin
                if re.match(r'^\d{1,3}$', text):
                    x1 = bbox.get('x1', 0)
                    # Check if in left margin (x < 60) or right margin (x > 550)
                    if x1 < 60 or x1 > 550:
                        mask_regions.append({
                            'page': page_num,
                            'bbox': bbox,
                            'type': 'LINE_NUMBER'
                        })

        return mask_regions

    def mask_pdf_with_extras(self, pdf_path: Path, json_path: Path, output_path: Path) -> Optional[Path]:
        """
        Create masked PDF with tables/figures/page numbers/line numbers removed.

        Args:
            pdf_path: Original PDF
            json_path: Layout JSON from Docling
            output_path: Where to save masked PDF

        Returns:
            Path to masked PDF if successful, None otherwise
        """
        try:
            import json
            import fitz

            logger.info(f"Masking PDF: {pdf_path.name}...")

            # Load JSON
            with open(json_path, 'r', encoding='utf-8') as f:
                layout_data = json.load(f)

            elements = layout_data.get('elements', [])

            # Detect page/line numbers
            number_regions = self.detect_page_and_line_numbers(elements)
            logger.info(f"  Detected {len(number_regions)} page/line number regions")

            # First, use the existing masking for tables/figures
            temp_masked = mask_pdf_from_json(
                pdf_path=pdf_path,
                json_path=json_path,
                output_dir=output_path.parent,
                return_elements=False
            )

            # Use the result as base, or original if masking wasn't needed
            base_pdf = temp_masked if temp_masked and temp_masked.exists() else pdf_path

            # Now add additional masking for page/line numbers
            if number_regions:
                doc = fitz.open(str(base_pdf))

                for region in number_regions:
                    page_num = region['page'] - 1  # Convert to 0-indexed
                    if page_num >= len(doc):
                        continue

                    page = doc[page_num]
                    page_height = page.rect.height
                    bbox = region['bbox']

                    # Convert coordinates (Docling uses bottom-left origin, PyMuPDF uses top-left)
                    x1 = bbox.get('x1', 0)
                    y1_docling = bbox.get('y1', 0)
                    x2 = bbox.get('x2', 0)
                    y2_docling = bbox.get('y2', 0)

                    # Transform Y coordinates
                    y1 = page_height - y2_docling
                    y2 = page_height - y1_docling

                    # Create rectangle and mask it
                    rect = fitz.Rect(x1, y1, x2, y2)
                    page.draw_rect(rect, color=None, fill=(1, 1, 1), overlay=True)

                # Save the final masked PDF
                doc.save(str(output_path))
                doc.close()

                # Clean up temp file if it exists and is different from output
                if temp_masked and temp_masked.exists() and temp_masked != output_path:
                    temp_masked.unlink()

                logger.info(f"  Created masked PDF with {len(number_regions)} number regions masked: {output_path.name}")
                return output_path
            else:
                # No number regions to mask, just use the result from table/figure masking
                if temp_masked and temp_masked.exists():
                    if temp_masked != output_path:
                        import shutil
                        shutil.move(str(temp_masked), str(output_path))
                    logger.info(f"  Created masked PDF: {output_path.name}")
                    return output_path
                else:
                    logger.warning("  No masking needed")
                    return None

        except Exception as e:
            logger.error(f"  Failed to mask PDF: {e}", exc_info=True)
            return None

    def mask_pdf(self, pdf_path: Path, json_path: Path, output_path: Path) -> Optional[Path]:
        """
        Create masked PDF with tables/figures removed.
        This is kept for backward compatibility but now calls mask_pdf_with_extras.
        """
        return self.mask_pdf_with_extras(pdf_path, json_path, output_path)

    def extract_text_from_pdf(self, pdf_path: Path, output_txt_path: Path) -> bool:
        """
        Extract text from PDF using Docling, with paragraph stitching.

        Args:
            pdf_path: Path to the PDF (masked or original)
            output_txt_path: Where to save the text file

        Returns:
            True if successful, False otherwise
        """
        try:
            logger.info(f"Extracting text from {pdf_path.name}...")
            result = self.converter.convert(str(pdf_path))
            doc = result.document

            # Extract all text elements
            text_segments = []
            section_headers = []
            figure_texts = []

            for element, level in doc.iterate_items():
                label = str(getattr(element, "label", "UNKNOWN")).split('.')[-1].upper()

                # Get text content
                text = ""
                if hasattr(element, 'text'):
                    text = element.text
                elif hasattr(element, 'caption') and element.caption:
                    text = element.caption.text

                if text and text.strip():
                    if label == "SECTION_HEADER":
                        section_headers.append(text.strip())
                    elif label in ["PICTURE", "CAPTION"]:
                        # Extract text from figures/captions
                        figure_texts.append({
                            'type': label,
                            'text': text.strip()
                        })
                    else:
                        text_segments.append(text.strip())

            # Apply paragraph stitching if available
            if TEXT_PROCESSING_AVAILABLE and text_segments:
                logger.info(f"  Stitching {len(text_segments)} text segments...")
                stitcher = ContextAwareStitcher()

                # Remove citations
                cleaned_segments = [remove_citations(seg) for seg in text_segments]

                # Stitch paragraphs
                stitched_paragraphs = stitcher.reconstruct_paragraphs(cleaned_segments)

                logger.info(f"  Merged into {len(stitched_paragraphs)} paragraphs")
                final_text = '\n\n'.join(stitched_paragraphs)
            else:
                # Fallback: simple joining
                if not TEXT_PROCESSING_AVAILABLE:
                    logger.warning("  Text processing not available - using simple joining")
                final_text = '\n\n'.join(text_segments)

            # Log figure texts found
            if figure_texts:
                logger.info(f"  Found {len(figure_texts)} figure/caption texts")

            # Save to text file
            with open(output_txt_path, 'w', encoding='utf-8') as f:
                # Write section headers if any
                if section_headers:
                    f.write("DOCUMENT SECTIONS:\n")
                    for header in section_headers:
                        f.write(f"  • {header}\n")
                    f.write("\n" + "="*80 + "\n\n")

                # Write main text
                f.write(final_text)

                # Write figure texts in boxes if any
                if figure_texts:
                    f.write("\n\n" + "="*80 + "\n")
                    f.write("FIGURE/CAPTION TEXT\n")
                    f.write("="*80 + "\n\n")

                    for idx, fig in enumerate(figure_texts, 1):
                        f.write("="*80 + "\n")
                        f.write(f"║ {fig['type']} {idx}\n")
                        f.write("="*80 + "\n")
                        f.write(fig['text'])
                        f.write("\n" + "="*80 + "\n\n")

            logger.info(f"  Saved text to: {output_txt_path.name} ({output_txt_path.stat().st_size / 1024:.1f} KB)")
            return True

        except Exception as e:
            logger.error(f"  Failed to extract text: {e}", exc_info=True)
            return False

    def process_pdf(self, pdf_path: Path) -> bool:
        """
        Process a single PDF:
        1. Extract layout JSON
        2. Create masked PDF
        3. Extract text from masked PDF

        Args:
            pdf_path: Path to original PDF

        Returns:
            True if successful, False otherwise
        """
        # Use PDF filename (without extension) as identifier
        pdf_name = pdf_path.stem

        logger.info(f"\n{'='*80}")
        logger.info(f"Processing: {pdf_path.name}")
        logger.info(f"{'='*80}")

        try:
            # Step 1: Extract layout JSON
            json_path = self.json_dir / f"{pdf_name}_layout.json"
            if not self.extract_layout_json(pdf_path, json_path):
                logger.error("Failed to extract layout JSON")
                self.stats['errors'] += 1
                return False

            # Step 2: Create masked PDF
            masked_pdf_path = self.masked_dir / f"{pdf_name}_masked.pdf"
            masked_result = self.mask_pdf(pdf_path, json_path, masked_pdf_path)

            # Use masked PDF if available, otherwise use original
            pdf_for_text = masked_result if masked_result else pdf_path

            # Step 3: Extract text
            txt_path = self.text_dir / f"{pdf_name}.txt"
            if not self.extract_text_from_pdf(pdf_for_text, txt_path):
                logger.error("Failed to extract text")
                self.stats['errors'] += 1
                return False

            logger.info(f"✅ Successfully processed {pdf_name}")
            self.stats['processed'] += 1
            return True

        except Exception as e:
            logger.error(f"❌ Failed to process {pdf_name}: {e}", exc_info=True)
            self.stats['errors'] += 1
            return False

    def process_all_pdfs(self):
        """Process all PDFs in the PDF directory."""
        pdf_files = list(self.pdf_dir.glob("*.pdf"))
        logger.info(f"Found {len(pdf_files)} PDF files in {self.pdf_dir}")

        for index, pdf_file in enumerate(pdf_files, 1):
            pdf_name = pdf_file.stem

            # Check if already processed
            txt_path = self.text_dir / f"{pdf_name}.txt"
            if txt_path.exists():
                logger.info(f"[{index}/{len(pdf_files)}] ✓ Skipping {pdf_name} - already processed")
                self.stats['skipped'] += 1
                continue

            # Process PDF
            logger.info(f"[{index}/{len(pdf_files)}] Processing {pdf_name}...")
            self.process_pdf(pdf_file)

        # Print stats
        logger.info(f"\n{'='*80}")
        logger.info("Processing Statistics")
        logger.info(f"{'='*80}")
        logger.info(f"Processed:  {self.stats['processed']}")
        logger.info(f"Skipped:    {self.stats['skipped']}")
        logger.info(f"Errors:     {self.stats['errors']}")
        logger.info(f"{'='*80}\n")


def main():
    """Main entry point."""
    processor = SimplePDFProcessor()
    processor.process_all_pdfs()


if __name__ == "__main__":
    main()
