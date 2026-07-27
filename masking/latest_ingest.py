#!/usr/bin/env python3
"""
Complete PDF Processing Pipeline - Combines comprehensive_ingest.py and notebook workflow

This script provides a unified pipeline that includes all features:
- Table reconstruction from captions
- PDF masking with white rectangles
- Re-extraction from masked PDF for clean text
- Hierarchical path-based text grouping
- Paragraph stitching within sections
- Reference detection (figures/tables mentioned in text)
- References section skipping
- Database ingestion with full relationships

Usage:
    python scripts/complete_pipeline.py --pdf path/to/file.pdf --pmcid PMC1234567
    python scripts/complete_pipeline.py --pdf-dir files/organized_pdfs --db-ingest
"""

import sys
import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from collections import defaultdict, Counter
from datetime import datetime
import logging

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def setup_logging(debug=False):
    """Configure logging level based on debug flag."""
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(levelname)s - %(message)s',
        force=True
    )
    logger.setLevel(level)

# Import database (optional for text-only mode)
try:
    from database import get_db_connection, Document, TextElement, Figure, Table
    from database.models import TextElementFigureReference, TextElementTableReference
    DB_AVAILABLE = True
except ImportError:
    DB_AVAILABLE = False
    logger.warning("Database modules not available - running in text-only mode")

# Import text processing utilities
# E402: these follow the optional-dependency try/except above, which decides
# DB_AVAILABLE before anything else may import the DB-backed modules.
from parsers.text_processing import remove_citations, ContextAwareStitcher  # noqa: E402
from database import Document  # noqa: E402

# Import mask_tables for table reconstruction
try:
    sys.path.insert(0, str(Path(__file__).parent / "docling_files"))
    from masking.mask_tables import process_pdf_with_masking
    MASK_TABLES_AVAILABLE = True
except ImportError as e:
    MASK_TABLES_AVAILABLE = False
    logger.warning(f"mask_tables.py not available: {e}")

# Import visualization for table reconstruction
try:
    sys.path.insert(0, str(Path(__file__).parent))
    # F401: the import IS the availability probe — it sets VISUALIZE_AVAILABLE
    # via whether it raises. Do not replace with importlib.util.find_spec:
    # find_spec only checks the module resolves, not that this symbol exists.
    from visualize_docling_full import reconstruct_tables_from_lists  # noqa: F401
    VISUALIZE_AVAILABLE = True
except ImportError:
    VISUALIZE_AVAILABLE = False
    logger.warning("visualize_docling_full.py not available")

# Import Docling
try:
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.datamodel.base_models import InputFormat
    DOCLING_AVAILABLE = True
    logger.info("✓ Docling imported successfully")
except ImportError as e:
    DOCLING_AVAILABLE = False
    logger.warning(f"Docling not available (ImportError): {e}")
except Exception as e:
    DOCLING_AVAILABLE = False
    logger.warning(f"Docling not available ({type(e).__name__}): {e}")

# Import PyMuPDF
try:
    import fitz
    PYMUPDF_AVAILABLE = True
except ImportError:
    PYMUPDF_AVAILABLE = False
    logger.warning("PyMuPDF not available. Install with: pip install PyMuPDF")


class CompletePipelineProcessor:
    """
    Complete PDF processing pipeline combining all features from
    comprehensive_ingest.py and the Jupyter notebook workflow.
    """
        
    def __init__(
        self,
        output_dir: str = "out/complete_pipeline",
        figures_dir: str = "files/figures",
        tables_dir: str = "files/tables",
        text_dir: str = "out/text",
        blacklist_file: str = "out/failed_pdfs_blacklist.json"
    ):
        """Initialize the processor."""
        if DB_AVAILABLE:
            self.db = get_db_connection()
        else:
            self.db = None

        self.output_dir = Path(output_dir)
        self.figures_dir = Path(figures_dir)
        self.tables_dir = Path(tables_dir)
        self.text_dir = Path(text_dir)
        self.blacklist_file = Path(blacklist_file)

        # Create directories
        for d in [self.output_dir, self.figures_dir, self.tables_dir, self.text_dir]:
            d.mkdir(parents=True, exist_ok=True)

        # Ensure blacklist directory exists
        self.blacklist_file.parent.mkdir(parents=True, exist_ok=True)

        self.stats = {
            'processed': 0,
            'skipped': 0,
            'errors': 0,
            'blacklisted': 0,
            'total_text_elements': 0,
            'total_figures': 0,
            'total_tables': 0,
            'masked_elements': 0
        }

        # Load blacklist
        self.blacklist = self.load_blacklist()

        # Initialize converter once (only if Docling is available)
        if DOCLING_AVAILABLE:
            pipeline_options = PdfPipelineOptions()
            pipeline_options.do_table_structure = False
            pipeline_options.do_ocr = True
            self.converter = DocumentConverter(
                format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)}
            )
        else:
            self.converter = None
            logger.warning("Docling not available - PDF processing will be limited")

    def load_blacklist(self) -> Dict[str, Dict]:
        """
        Load the blacklist of failed PDFs from JSON file.

        Returns:
            Dict mapping PMCID to error information
        """
        if self.blacklist_file.exists():
            try:
                with open(self.blacklist_file, 'r') as f:
                    blacklist = json.load(f)
                logger.info(f"Loaded blacklist: {len(blacklist)} failed PDFs")
                return blacklist
            except Exception as e:
                logger.warning(f"Failed to load blacklist: {e}")
                return {}
        return {}

    def save_blacklist(self):
        """Save the blacklist to JSON file."""
        try:
            with open(self.blacklist_file, 'w') as f:
                json.dump(self.blacklist, f, indent=2)
            logger.debug(f"Saved blacklist: {len(self.blacklist)} entries")
        except Exception as e:
            logger.error(f"Failed to save blacklist: {e}")

    def is_blacklisted(self, pmcid: str) -> bool:
        """Check if a PMCID is in the blacklist."""
        return pmcid in self.blacklist

    def add_to_blacklist(self, pmcid: str, error_msg: str, pdf_path: str = None):
        """
        Add a PMCID to the blacklist with error information.

        Args:
            pmcid: The PMCID that failed
            error_msg: Description of the error
            pdf_path: Optional path to the PDF file
        """
        self.blacklist[pmcid] = {
            'error': error_msg,
            'pdf_path': pdf_path,
            'timestamp': datetime.now().isoformat(),
            'attempts': self.blacklist.get(pmcid, {}).get('attempts', 0) + 1
        }
        self.save_blacklist()
        logger.warning(f"Added {pmcid} to blacklist: {error_msg}")

    def extract_layout_with_docling(self, pdf_path: Path) -> Tuple[List[Dict], Path]:
        """
        Extract full layout using Docling and save to JSON.

        Returns:
            Tuple of (elements list, json_path)
        """
        if not DOCLING_AVAILABLE:
            logger.error("Docling is not available")
            return None, None

        try:
            # Convert document
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
            json_path = self.output_dir / f"{pdf_path.stem}_layout.json"
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump({
                    "metadata": {
                        "pdf_path": str(pdf_path),
                        "tool": "Docling",
                        "extraction_date": datetime.now().isoformat()
                    },
                    "page_dimensions": page_dimensions,
                    "elements": all_elements
                }, f, indent=2)

            logger.info(f"Extracted {len(all_elements)} elements")
            return all_elements, json_path

        except Exception as e:
            logger.error(f"Docling extraction failed: {e}", exc_info=True)
            return None, None

    def detect_references(self, text: str) -> Dict[str, List[str]]:
        """
        Detect references to figures and tables in text.

        Returns:
            Dict with 'figures' and 'tables' lists containing reference IDs
        """
        references = {'figures': [], 'tables': []}

        # Figure patterns
        fig_patterns = [
            r'\b(?:Figure|Fig\.?|FIG\.?)\s+(\d+[A-Za-z]?)',
            r'\((?:Figure|Fig\.?|FIG\.?)\s+(\d+[A-Za-z]?)\)',
        ]

        for pattern in fig_patterns:
            matches = re.finditer(pattern, text, re.IGNORECASE)
            for match in matches:
                fig_id = match.group(1)
                if fig_id not in references['figures']:
                    references['figures'].append(fig_id)

        # Table patterns
        table_patterns = [
            r'\b(?:Table|TABLE)\s+(\d+[A-Za-z]?)',
            r'\((?:Table|TABLE)\s+(\d+[A-Za-z]?)\)',
        ]

        for pattern in table_patterns:
            matches = re.finditer(pattern, text, re.IGNORECASE)
            for match in matches:
                table_id = match.group(1)
                if table_id not in references['tables']:
                    references['tables'].append(table_id)

        return references

    def build_hierarchical_structure(
        self,
        text_elements: List[Dict],
        skip_references: bool = True
    ) -> Tuple[Dict[str, List[Dict]], List[Dict]]:
        """
        Build hierarchical structure from text elements.
        Groups elements by path and detects references.
        Skips References sections if requested.

        Returns:
            Tuple of (text_by_path dict, db_elements list)
        """
        # Track hierarchy
        hierarchy_tracker = {}  # level -> section name
        text_by_path = defaultdict(list)
        db_elements = []

        in_references = False
        references_depth = None

        for elem in text_elements:
            level = elem.get('level', 0)
            text = elem.get('text', '').strip()
            elem_type = elem.get('type')

            if not text:
                continue

            # Handle section headers
            if elem_type == 'SECTION_HEADER':
                # Update hierarchy
                hierarchy_tracker[level] = text
                hierarchy_tracker = {k: v for k, v in hierarchy_tracker.items() if k <= level}

                # Check for References section
                if skip_references and 'reference' in text.lower():
                    in_references = True
                    references_depth = level
                    logger.debug(f"Entering References section at level {level}")
                    continue
                elif in_references and references_depth is not None and level <= references_depth:
                    in_references = False
                    references_depth = None
                    logger.debug("Exiting References section")

                # Skip if in references
                if in_references:
                    continue

            else:
                # Skip text in References section
                if in_references:
                    continue

                # Build path from current hierarchy
                path_parts = [
                    hierarchy_tracker.get(level, '')
                    for level in sorted(hierarchy_tracker.keys())
                    if hierarchy_tracker.get(level)
                ]
                path_string = ' > '.join(path_parts) if path_parts else 'Root'

                # Detect references in text
                refs = self.detect_references(text)

                # Add to path grouping
                text_by_path[path_string].append({
                    'text': text,
                    'page': elem.get('page'),
                    'references': refs if (refs['figures'] or refs['tables']) else {}
                })

                # Also prepare for database
                path_list = [p for p in path_parts]
                depth = len(path_list)

                db_elements.append({
                    'path_list': path_list,
                    'path_string': path_string,
                    'depth': depth,
                    'text': text,
                    'page': elem.get('page'),
                    'references': refs if (refs['figures'] or refs['tables']) else {}
                })

        logger.info(f"Built hierarchy: {len(text_by_path)} unique paths")
        if skip_references:
            logger.info("Skipped References section")

        return text_by_path, db_elements

    def stitch_paragraphs_by_path(
        self,
        text_by_path: Dict[str, List[Dict]]
    ) -> Dict[str, List[str]]:
        """
        Stitch paragraphs within each hierarchical path.

        Returns:
            Dict mapping path_string to list of stitched paragraphs
        """
        stitcher = ContextAwareStitcher()
        stitched_by_path = {}

        for path_string, elements in text_by_path.items():
            # Extract text and remove citations
            texts = [remove_citations(elem['text']) for elem in elements]

            # Stitch paragraphs
            stitched = stitcher.reconstruct_paragraphs(texts)
            stitched_by_path[path_string] = stitched

        total_original = sum(len(v) for v in text_by_path.values())
        total_stitched = sum(len(v) for v in stitched_by_path.values())
        logger.info(f"Stitched paragraphs: {total_original} → {total_stitched}")
        logger.info(f"Merged: {total_original - total_stitched} split paragraphs")

        return stitched_by_path

    def prepare_db_elements(
        self,
        stitched_by_path: Dict[str, List[str]],
        original_elements: List[Dict]
    ) -> List[Dict]:
        """
        Prepare stitched text for database ingestion.
        Attempts to preserve reference information from original elements.

        Returns:
            List of text elements ready for database
        """
        db_text_elements = []

        for path_string, stitched_paras in stitched_by_path.items():
            # Build path_list from path_string
            if path_string == 'Root':
                path_list = []
                depth = 0
            else:
                path_list = [part.strip() for part in path_string.split(' > ')]
                depth = len(path_list)

            # Get references from original elements in this path
            # (This is a best-effort approach since stitching may combine texts)
            path_refs = {'figures': [], 'tables': []}
            for elem in original_elements:
                if elem.get('path_string') == path_string:
                    elem_refs = elem.get('references', {})
                    path_refs['figures'].extend(elem_refs.get('figures', []))
                    path_refs['tables'].extend(elem_refs.get('tables', []))

            # Deduplicate references
            path_refs['figures'] = list(set(path_refs['figures']))
            path_refs['tables'] = list(set(path_refs['tables']))

            # Each stitched paragraph becomes one text element
            for para in stitched_paras:
                if para.strip():
                    db_text_elements.append({
                        'path_list': path_list,
                        'path_string': path_string,
                        'depth': depth,
                        'text': para,
                        'references': path_refs if (path_refs['figures'] or path_refs['tables']) else {}
                    })

        logger.info(f"Prepared {len(db_text_elements)} text elements for database")
        return db_text_elements

    def crop_and_save_regions(
        self,
        pdf_path: Path,
        pmcid: str,
        table_data: List[Dict],
        figure_data: List[Dict]
    ):
        """Crop and save table/figure regions from PDF as images."""
        if not PYMUPDF_AVAILABLE:
            logger.warning("PyMuPDF not available - skipping image cropping")
            return

        try:
            doc = fitz.open(str(pdf_path))

            # Crop tables
            for table in table_data:
                page_num = table['page']
                bbox = table['bbox']
                table_id = table['table_id']

                if page_num and bbox:
                    page = doc[page_num - 1]
                    page_height = page.rect.height

                    y1 = page_height - max(bbox['y1'], bbox['y2'])
                    y2 = page_height - min(bbox['y1'], bbox['y2'])
                    rect = fitz.Rect(bbox['x1'], y1, bbox['x2'], y2)

                    pix = page.get_pixmap(clip=rect, matrix=fitz.Matrix(2, 2))
                    output_path = self.tables_dir / f"{pmcid}_table_{table_id}.png"
                    pix.save(str(output_path))
                    table['image_path'] = str(output_path)

            logger.info(f"Cropped {len(table_data)} table regions")

            # Crop figures
            for figure in figure_data:
                page_num = figure['page']
                bbox = figure['bbox']
                fig_id = figure['figure_id']

                if page_num and bbox:
                    page = doc[page_num - 1]
                    page_height = page.rect.height

                    y1 = page_height - max(bbox['y1'], bbox['y2'])
                    y2 = page_height - min(bbox['y1'], bbox['y2'])
                    rect = fitz.Rect(bbox['x1'], y1, bbox['x2'], y2)

                    pix = page.get_pixmap(clip=rect, matrix=fitz.Matrix(2, 2))
                    output_path = self.figures_dir / f"{pmcid}_figure_{fig_id}.png"
                    pix.save(str(output_path))
                    figure['image_path'] = str(output_path)

            logger.info(f"Cropped {len(figure_data)} figure regions")
            doc.close()

        except Exception as e:
            logger.error(f"Failed to crop regions: {e}")

    def save_metadata(self, pmcid: str, table_data: List[Dict], figure_data: List[Dict]):
        """Save table and figure metadata as JSON files."""
        if table_data:
            tables_file = self.tables_dir / f"{pmcid}_tables.json"
            with open(tables_file, 'w') as f:
                json.dump(table_data, f, indent=2)
            logger.info(f"Saved {len(table_data)} table metadata")

        if figure_data:
            figures_file = self.figures_dir / f"{pmcid}_figures.json"
            with open(figures_file, 'w') as f:
                json.dump(figure_data, f, indent=2)
            logger.info(f"Saved {len(figure_data)} figure metadata")

    def save_text_to_file(
        self,
        pmcid: str,
        stitched_by_path: Dict[str, List[str]],
        original_elements: List[Dict]
    ) -> Path:
        """
        Save stitched text organized by hierarchical paths.
        Includes section-level references.
        """
        output_path = self.text_dir / f"{pmcid}_text.txt"

        # Build reference mapping by path
        refs_by_path = defaultdict(lambda: {'figures': [], 'tables': []})
        for elem in original_elements:
            path = elem.get('path_string', 'Root')
            elem_refs = elem.get('references', {})
            refs_by_path[path]['figures'].extend(elem_refs.get('figures', []))
            refs_by_path[path]['tables'].extend(elem_refs.get('tables', []))

        # Deduplicate
        for path in refs_by_path:
            refs_by_path[path]['figures'] = sorted(set(refs_by_path[path]['figures']))
            refs_by_path[path]['tables'] = sorted(set(refs_by_path[path]['tables']))

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(f"Document: {pmcid}\n")
            f.write(f"{'='*80}\n\n")

            for path_string in sorted(stitched_by_path.keys()):
                paragraphs = stitched_by_path[path_string]

                f.write(f"[{path_string}]\n")
                f.write(f"{'-'*80}\n\n")

                for para in paragraphs:
                    if para.strip():
                        f.write(f"{para}\n\n")

                # Add section references
                refs = refs_by_path.get(path_string, {})
                if refs['figures'] or refs['tables']:
                    f.write("\n  [Section References: ")
                    ref_parts = []
                    if refs['figures']:
                        ref_parts.append(f"Figures {', '.join(refs['figures'])}")
                    if refs['tables']:
                        ref_parts.append(f"Tables {', '.join(refs['tables'])}")
                    f.write('; '.join(ref_parts))
                    f.write("]\n")

                f.write("\n")

        logger.info(f"Saved text to: {output_path} ({output_path.stat().st_size / 1024:.1f} KB)")
        return output_path

    def ingest_to_database(
        self,
        pmcid: str,
        pdf_path: Path,
        db_text_elements: List[Dict],
        table_data: List[Dict],
        figure_data: List[Dict],
        title: Optional[str] = None,
        journal: Optional[str] = None,
        publication_year: Optional[int] = None,
        force: bool = False
    ) -> bool:
        """
        Ingest document to database with full relationships.
        """
        if not DB_AVAILABLE:
            logger.error("Database not available")
            return False

        try:
            with self.db.session_scope() as session:
                # Check if exists
                existing = session.query(Document).filter_by(pmcid=pmcid).first()
                if existing and not force:
                    logger.info(f"Document {pmcid} already exists (use --force to re-ingest)")
                    self.stats['skipped'] += 1
                    return False

                if existing and force:
                    logger.info(f"Deleting existing document {pmcid}")
                    session.delete(existing)
                    session.flush()

                # Create document
                doc = Document(
                    pmcid=pmcid,
                    filename=pdf_path.name,
                    file_path=str(pdf_path.absolute()),
                    title=title or f"Document {pmcid}",
                    journal=journal,
                    publication_year=publication_year,
                    text_source='pdf'
                )
                session.add(doc)
                session.flush()
                logger.info(f"Created document: {pmcid}")

                # Add text elements with position tracking
                path_counts = defaultdict(int)
                for elem in db_text_elements:
                    path_string = elem['path_string']
                    position = path_counts[path_string]
                    path_counts[path_string] += 1

                    unique_path = f"{pmcid}/{path_string}/{position}" if path_string else f"{pmcid}/(Root)/{position}"

                    text_elem = TextElement(
                        unique_path=unique_path,
                        document_id=doc.id,
                        path_list=elem['path_list'],
                        path_string=path_string,
                        depth=elem['depth'],
                        text_content=elem['text'],
                        position_in_section=position,
                        references=elem.get('references', {})
                    )
                    session.add(text_elem)

                session.flush()
                logger.info(f"Added {len(db_text_elements)} text elements")

                # Add figures
                for fig in figure_data:
                    image_filename = None
                    image_path = fig.get('image_path')
                    if image_path:
                        image_filename = Path(image_path).name

                    figure = Figure(
                        document_id=doc.id,
                        figure_id=fig['figure_id'],
                        figure_label=f"Figure {fig['figure_id']}",
                        figure_number=fig['figure_id'],
                        caption_text=fig.get('caption'),
                        image_filename=image_filename,
                        image_path=image_path
                    )
                    session.add(figure)

                session.flush()
                logger.info(f"Added {len(figure_data)} figures")

                # Add tables
                for tbl in table_data:
                    image_filename = None
                    image_path = tbl.get('image_path')
                    if image_path:
                        image_filename = Path(image_path).name

                    table = Table(
                        document_id=doc.id,
                        table_id=tbl['table_id'],
                        table_label=f"Table {tbl['table_id']}",
                        table_number=tbl['table_id'],
                        caption_text=tbl.get('caption'),
                        image_filename=image_filename,
                        image_path=image_path
                    )
                    session.add(table)

                session.flush()
                logger.info(f"Added {len(table_data)} tables")

                # Create reference junction tables
                text_elements_with_refs = session.query(TextElement).filter_by(document_id=doc.id).all()

                figure_refs_created = 0
                table_refs_created = 0

                for text_elem in text_elements_with_refs:
                    if not text_elem.references:
                        continue

                    # Figure references
                    for fig_id in text_elem.references.get('figures', []):
                        figure = session.query(Figure).filter_by(
                            document_id=doc.id,
                            figure_id=fig_id
                        ).first()

                        if figure:
                            ref = TextElementFigureReference(
                                text_element_id=text_elem.id,
                                figure_id=figure.id
                            )
                            session.add(ref)
                            figure_refs_created += 1

                    # Table references
                    for table_id in text_elem.references.get('tables', []):
                        table = session.query(Table).filter_by(
                            document_id=doc.id,
                            table_id=table_id
                        ).first()

                        if table:
                            ref = TextElementTableReference(
                                text_element_id=text_elem.id,
                                table_id=table.id
                            )
                            session.add(ref)
                            table_refs_created += 1

                session.flush()
                logger.info(f"Created {figure_refs_created} figure references, {table_refs_created} table references")

                self.stats['processed'] += 1
                self.stats['total_text_elements'] += len(db_text_elements)
                self.stats['total_figures'] += len(figure_data)
                self.stats['total_tables'] += len(table_data)

                return True

        except Exception as e:
            logger.error(f"Failed to ingest {pmcid}: {e}", exc_info=True)
            self.stats['errors'] += 1
            return False

    def process_document(
        self,
        pdf_path: Path,
        pmcid: str,
        title: Optional[str] = None,
        journal: Optional[str] = None,
        publication_year: Optional[int] = None,
        force: bool = False,
        db_ingest: bool = False,
        skip_references: bool = True
    ) -> bool:
        """
        Process a single document through the complete pipeline.

        Steps:
        1. Extract layout from original PDF
        2. Reconstruct tables and create masked PDF
        3. Re-extract layout from masked PDF
        4. Build hierarchical structure with reference detection
        5. Stitch paragraphs within each path
        6. Crop and save table/figure images
        7. Save text to file
        8. Optionally ingest to database
        """
        logger.info(f"{'='*80}")
        logger.info(f"Processing: {pmcid} ({pdf_path.name})")
        logger.info(f"{'='*80}")

        try:
            # Step 1: Extract layout from original PDF
            all_elements, docling_json_path = self.extract_layout_with_docling(pdf_path)
            if not all_elements:
                error_msg = "Failed to extract layout from PDF"
                logger.error(error_msg)
                self.add_to_blacklist(pmcid, error_msg, str(pdf_path))
                self.stats['errors'] += 1
                return False

            types = Counter([el['type'] for el in all_elements])
            logger.info(f"Original PDF: {len(all_elements)} elements")
            logger.info(f"  Tables: {types.get('TABLE', 0)}, Figures: {types.get('PICTURE', 0)}, Captions: {types.get('CAPTION', 0)}")

            # Step 2: Process with masking and table reconstruction
            if MASK_TABLES_AVAILABLE:
                logger.info("Creating masked PDF with table reconstruction...")
                masked_pdf_path, _, masked_elements = process_pdf_with_masking(
                    pdf_path=pdf_path,
                    json_path=docling_json_path,
                    output_dir=Path("out/masked_pdfs")
                )
                logger.info(f"Masked {len(masked_elements)} elements")
            else:
                logger.warning("mask_tables not available - skipping masking")
                masked_elements = []
                masked_pdf_path = pdf_path

            # Step 3: Re-extract from masked PDF
            if masked_pdf_path != pdf_path:
                logger.info("Re-extracting layout from masked PDF...")
                masked_elements_extracted, masked_json_path = self.extract_layout_with_docling(masked_pdf_path)

                # Filter to text elements only
                text_element_types = {'TEXT', 'PARAGRAPH', 'SECTION_HEADER', 'TITLE', 'LIST', 'LIST_ITEM'}
                text_elements = [el for el in masked_elements_extracted if el.get('type') in text_element_types]
                logger.info(f"Extracted {len(text_elements)} clean text elements from masked PDF")
            else:
                # Use original elements if no masking
                text_element_types = {'TEXT', 'PARAGRAPH', 'SECTION_HEADER', 'TITLE', 'LIST', 'LIST_ITEM'}
                text_elements = [el for el in all_elements if el.get('type') in text_element_types]
                logger.info(f"Using {len(text_elements)} text elements from original PDF")

            # Step 4: Build hierarchical structure with reference detection
            logger.info("Building hierarchical structure and detecting references...")
            text_by_path, db_elements_raw = self.build_hierarchical_structure(
                text_elements,
                skip_references=skip_references
            )

            # Step 5: Stitch paragraphs within each path
            logger.info("Stitching paragraphs within hierarchical paths...")
            stitched_by_path = self.stitch_paragraphs_by_path(text_by_path)

            # Step 6: Prepare database elements from stitched text
            db_text_elements = self.prepare_db_elements(stitched_by_path, db_elements_raw)

            # Step 7: Extract table/figure captions from ORIGINAL PDF
            # ONLY extract from TEXT/CAPTION elements (ignore TABLE/PICTURE elements)
            logger.info("Extracting table/figure captions from original PDF...")
            table_data = []
            figure_data = []

            def extract_caption_info(text, element_type):
                """
                Extract ID and full caption with strict priority:
                1. "Table/Figure x." (with period)
                2. "Table/Figure x" (standard)
                3. Alternatives (e.g., "Fig. x")
                """
                if not text:
                    return None, None

                if element_type == 'table':
                    patterns = [
                        r'Table\s+(\d+[A-Za-z]?)\.',  # Priority 1: "Table 1."
                        r'Table\s+(\d+[A-Za-z]?)',     # Priority 2: "Table 1"
                    ]
                else:  # figure
                    patterns = [
                        r'Figure\s+(\d+[A-Za-z]?)\.', # Priority 1: "Figure 1."
                        r'Figure\s+(\d+[A-Za-z]?)',    # Priority 2: "Figure 1"
                        r'Fig\.\s+(\d+[A-Za-z]?)',     # Priority 3: "Fig. 1"
                    ]

                for pattern in patterns:
                    match = re.search(pattern, text, re.IGNORECASE)
                    if match:
                        element_id = match.group(1)
                        return element_id, text.strip()

                return None, None

            seen_table_ids = {}
            seen_figure_ids = {}

            # PASS 1: HIGHEST PRIORITY - Process formal CAPTION elements first
            for elem in all_elements:
                if elem.get('type') == 'CAPTION':
                    text = elem.get('text', '')
                    
                    # Table Check
                    t_id, t_cap = extract_caption_info(text, 'table')
                    if t_id and t_id not in seen_table_ids:
                        seen_table_ids[t_id] = True
                        table_data.append({
                            'table_id': t_id, 'caption': t_cap, 'page': elem.get('page'),
                            'bbox': elem.get('bbox'), 'type': 'formal_caption'
                        })
                        continue

                    # Figure Check
                    f_id, f_cap = extract_caption_info(text, 'figure')
                    if f_id and f_id not in seen_figure_ids:
                        seen_figure_ids[f_id] = True
                        figure_data.append({
                            'figure_id': f_id, 'caption': f_cap, 'page': elem.get('page'),
                            'bbox': elem.get('bbox'), 'type': 'formal_caption'
                        })

            # PASS 2: LOWER PRIORITY - Process TEXT elements for missing IDs
            for elem in all_elements:
                if elem.get('type') == 'TEXT':
                    text = elem.get('text', '')

                    # Only capture if the ID wasn't already found in a formal CAPTION
                    t_id, t_cap = extract_caption_info(text, 'table')
                    if t_id and t_id not in seen_table_ids:
                        seen_table_ids[t_id] = True
                        table_data.append({
                            'table_id': t_id, 'caption': t_cap, 'page': elem.get('page'),
                            'bbox': elem.get('bbox'), 'type': 'text_fallback'
                        })
                        continue

                    f_id, f_cap = extract_caption_info(text, 'figure')
                    if f_id and f_id not in seen_figure_ids:
                        seen_figure_ids[f_id] = True
                        figure_data.append({
                            'figure_id': f_id, 'caption': f_cap, 'page': elem.get('page'),
                            'bbox': elem.get('bbox'), 'type': 'text_fallback'
                        })
            logger.info(f"Extracted {len(table_data)} unique table captions, {len(figure_data)} unique figure captions")

            # Step 8: Crop and save regions
            logger.info("Cropping table/figure regions...")
            self.crop_and_save_regions(pdf_path, pmcid, table_data, figure_data)

            # Step 9: Save metadata
            self.save_metadata(pmcid, table_data, figure_data)

            # Step 10: Save text to file
            logger.info("Saving text to file...")
            self.save_text_to_file(pmcid, stitched_by_path, db_elements_raw)

            # Step 11: Database ingestion (optional)
            if db_ingest:
                logger.info("Ingesting to database...")
                success = self.ingest_to_database(
                    pmcid=pmcid,
                    pdf_path=pdf_path,
                    db_text_elements=db_text_elements,
                    table_data=table_data,
                    figure_data=figure_data,
                    title=title,
                    journal=journal,
                    publication_year=publication_year,
                    force=force
                )

                if success:
                    logger.info(f"✅ Successfully processed and ingested {pmcid}")
                else:
                    logger.warning(f"⚠️  Processed but skipped database ingestion for {pmcid}")
            else:
                logger.info(f"✅ Successfully processed {pmcid} (text-only mode)")
                self.stats['processed'] += 1

            return True

        except Exception as e:
            error_msg = f"Exception during processing: {str(e)}"
            logger.error(f"❌ Failed to process {pmcid}: {e}", exc_info=True)
            self.add_to_blacklist(pmcid, error_msg, str(pdf_path))
            self.stats['errors'] += 1
            return False

    def check_existing_files(self, pmcid: str) -> Dict[str, bool]:
        """
        Check which output files already exist for a given PMCID.

        Returns:
            Dict with file existence status
        """
        status = {
            'tables_json': (self.tables_dir / f"{pmcid}_tables.json").exists(),
            'figures_json': (self.figures_dir / f"{pmcid}_figures.json").exists(),
            'masked_layout_json': (self.output_dir / f"{pmcid}_masked_layout.json").exists(),
        }
        status['all_exist'] = all(status.values())
        return status

    def load_existing_data(self, pmcid: str) -> Tuple[List[Dict], List[Dict], List[Dict]]:
        """
        Load table/figure metadata and text elements from existing files.

        Returns:
            Tuple of (table_data, figure_data, text_elements)
        """
        table_data = []
        figure_data = []
        text_elements = []

        # Load table metadata
        tables_file = self.tables_dir / f"{pmcid}_tables.json"
        if tables_file.exists():
            with open(tables_file, 'r') as f:
                table_data = json.load(f)
            logger.info(f"Loaded {len(table_data)} tables from existing file")

        # Load figure metadata
        figures_file = self.figures_dir / f"{pmcid}_figures.json"
        if figures_file.exists():
            with open(figures_file, 'r') as f:
                figure_data = json.load(f)
            logger.info(f"Loaded {len(figure_data)} figures from existing file")

        # Load text from masked layout JSON
        masked_layout_file = self.output_dir / f"{pmcid}_masked_layout.json"
        if masked_layout_file.exists():
            with open(masked_layout_file, 'r', encoding='utf-8') as f:
                layout_data = json.load(f)

            # Extract text elements from layout
            all_elements = layout_data.get('elements', [])
            text_element_types = {'TEXT', 'PARAGRAPH', 'SECTION_HEADER', 'TITLE', 'LIST', 'LIST_ITEM'}
            text_elements = [el for el in all_elements if el.get('type') in text_element_types]

            logger.info(f"Loaded {len(text_elements)} text elements from masked layout JSON")

        return table_data, figure_data, text_elements

    def print_stats(self):
        """Print processing statistics."""
        logger.info(f"\n{'='*80}")
        logger.info("Processing Statistics")
        logger.info(f"{'='*80}")
        logger.info(f"Processed:        {self.stats['processed']}")
        logger.info(f"Skipped:          {self.stats['skipped']}")
        logger.info(f"Blacklisted:      {self.stats['blacklisted']}")
        logger.info(f"Errors:           {self.stats['errors']}")
        logger.info(f"Text elements:    {self.stats['total_text_elements']}")
        logger.info(f"Figures:          {self.stats['total_figures']}")
        logger.info(f"Tables:           {self.stats['total_tables']}")
        logger.info(f"{'='*80}\n")


def main():
    # Setup logging
    setup_logging(debug=False)

    # Create processor
    processor = CompletePipelineProcessor()
    PDF_DIR = "files/organized_pdfs"

    # Batch mode
    pdf_dir = Path(PDF_DIR)
    pdf_files = list(pdf_dir.glob("*.pdf"))

    logger.info(f"Found {len(pdf_files)} PDF files in {pdf_dir}")

    for index, pdf_file in enumerate(pdf_files):
        # Extract PMCID from filename
        print(f"index = {index + 1} / {len(pdf_files)}")
        stem = pdf_file.stem
        if stem.startswith("PMC"):
            pmcid = stem.split('_')[0]
        else:
            logger.warning(f"Skipping {pdf_file.name} - cannot extract PMCID")
            continue

        # 1. CHECK IF BLACKLISTED
        if processor.is_blacklisted(pmcid):
            blacklist_info = processor.blacklist[pmcid]
            error_reason = blacklist_info.get('error', 'Unknown error')
            logger.info(f"[{index + 1}/{len(pdf_files)}] ⚠️  Skipping {pmcid} - Blacklisted (failed {blacklist_info.get('attempts', 1)} time(s))")
            logger.info(f"  Reason: {error_reason}")
            processor.stats['blacklisted'] += 1
            continue

        # 2. CHECK IF ALREADY IN DATABASE
        if processor.db:
            with processor.db.session_scope() as session:
                existing = session.query(Document).filter_by(pmcid=pmcid).first()
                if existing:
                    logger.info(f"[{index + 1}/{len(pdf_files)}] ✓ Skipping {pmcid} - Already in database")
                    processor.stats['skipped'] += 1
                    continue

        # 3. CHECK IF OUTPUT FILES EXIST
        file_status = processor.check_existing_files(pmcid)

        if file_status['all_exist']:
            # Files exist - load and ingest directly
            logger.info(f"[{index + 1}/{len(pdf_files)}] ⚡ Loading existing files for {pmcid}")
            try:
                table_data, figure_data, text_elements = processor.load_existing_data(pmcid)

                # Process text elements through the pipeline
                logger.info("Building hierarchical structure from loaded elements...")
                text_by_path, db_elements_raw = processor.build_hierarchical_structure(
                    text_elements,
                    skip_references=True
                )

                logger.info("Stitching paragraphs within hierarchical paths...")
                stitched_by_path = processor.stitch_paragraphs_by_path(text_by_path)

                logger.info("Preparing database elements...")
                db_text_elements = processor.prepare_db_elements(stitched_by_path, db_elements_raw)

                # Ingest to database
                success = processor.ingest_to_database(
                    pmcid=pmcid,
                    pdf_path=pdf_file,
                    db_text_elements=db_text_elements,
                    table_data=table_data,
                    figure_data=figure_data,
                    force=False
                )

                if success:
                    logger.info(f"✅ Successfully ingested {pmcid} from existing files")
                else:
                    logger.warning(f"⚠️  Failed to ingest {pmcid} from existing files")

            except Exception as e:
                error_msg = f"Error loading existing files: {str(e)}"
                logger.error(f"❌ Error loading existing files for {pmcid}: {e}", exc_info=True)
                processor.add_to_blacklist(pmcid, error_msg, str(pdf_file))
                processor.stats['errors'] += 1
        else:
            # Files don't exist - process from scratch
            logger.info(f"[{index + 1}/{len(pdf_files)}] 🔄 Processing {pmcid} from scratch")
            processor.process_document(
                pdf_path=pdf_file,
                pmcid=pmcid,
                force=False,
                db_ingest=True,
                skip_references=True
            )

    processor.print_stats()

if __name__ == "__main__":
    main()
