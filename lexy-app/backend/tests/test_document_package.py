"""Document-package import (roadmap A2).

Hermetic — no database, no network, no LLM. Every fixture is a small synthetic
package built in ``tmp_path`` rather than a committed blob, so a test names the
exact defect it introduces.
"""
import hashlib
import json
from pathlib import Path

import pytest

from services import book_import_service
from services.document_package import contract
from services.document_package.coordinates import (
    CoordinateError,
    FitzBox,
    from_fitz,
    page_height_for,
    to_fitz,
)
from services.document_package.issues import FATAL, WARNING, IssueCollector
from services.document_package.loader import (
    PackageError,
    load_package,
    parse_checksums,
    resolve_within,
)
from services.document_package.persistence import (
    DryRunPersistence,
    PendingSchemaPersistence,
    PersistenceNotAvailable,
    build_plan,
)
from services.document_package.validators import (
    validate_bbox_page_consistency,
    validate_bboxes,
    validate_confidence,
    validate_coordinate_space,
    validate_element_ids,
    validate_page_dims,
    validate_page_indices,
    validate_parent_ids,
    validate_reading_order,
)

# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------

PAGE_W, PAGE_H = 383.0, 581.4


def element(
    element_id="p0001-e0001",
    page=1,
    etype="paragraph",
    text="Der Hof war still.",
    order=0,
    bbox=True,
    **extra,
):
    payload = {
        "id": element_id,
        "page_index": page,
        "type": etype,
        "text": text,
        "reading_order": order,
        "provenance": {"extractor_stage": "layout"},
        "confidence": {"classification": 0.98, "text": None},
    }
    if bbox:
        # Docling space: y-up, so y1 (top) > y2 (bottom).
        payload["bbox"] = {
            "x1": 72.0,
            "y1": 200.0,
            "x2": 300.0,
            "y2": 150.0,
            "page": page,
            "coordinate_space": contract.COORDINATE_SPACE,
        }
    payload.update(extra)
    return payload


def write_package(
    root: Path,
    name="pkg",
    *,
    manifest_overrides=None,
    elements=None,
    page_dims=None,
    checksums=True,
    manifest_raw=None,
    document_raw=None,
):
    """Create a minimal valid package, then apply the requested damage."""
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)

    dims = page_dims if page_dims is not None else {"1": {"width": PAGE_W, "height": PAGE_H}}
    manifest = {
        "package_schema_version": contract.PACKAGE_SCHEMA_VERSION,
        "producer": "nlp-histo@0.1.0",
        "document_id": name,
        "source": {"filename": "x.pdf", "sha256": "a" * 64, "bytes": 10, "page_count": 1},
        "extractor": {"profile": "german_fiction", "config_digest": "d" * 12},
        "created_at": "2026-07-29T12:00:00Z",
        "language": "de",
        "coordinate_space": contract.COORDINATE_SPACE,
        "page_dims": dims,
        "counts": {"pages": len(dims), "elements": 1, "warnings": 0},
        "warnings": [],
    }
    if manifest_overrides:
        manifest.update(manifest_overrides)

    document = {"elements": elements if elements is not None else [element()]}

    manifest_text = manifest_raw if manifest_raw is not None else json.dumps(manifest)
    document_text = document_raw if document_raw is not None else json.dumps(document)
    (directory / contract.MANIFEST_FILENAME).write_text(manifest_text, encoding="utf-8")
    (directory / contract.DOCUMENT_FILENAME).write_text(document_text, encoding="utf-8")

    if checksums:
        lines = []
        for filename in (contract.MANIFEST_FILENAME, contract.DOCUMENT_FILENAME):
            digest = hashlib.sha256((directory / filename).read_bytes()).hexdigest()
            lines.append(f"{digest}  {filename}")
        (directory / contract.CHECKSUMS_FILENAME).write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
    return directory


def issues_for(root, name="pkg"):
    return book_import_service.validate_package(load_package(name, root))


# ---------------------------------------------------------------------------
# the architectural boundary
# ---------------------------------------------------------------------------


class TestBoundary:
    def test_import_service_does_not_pull_in_worker_dependencies(self):
        """The package on disk is the entire interface (§3).

        Asserted rather than trusted: an accidental ``import nlp_histo`` would
        drag Docling/Torch into the API process and silently undo the
        deployment boundary the whole design rests on.

        Run in a **subprocess** rather than inspecting this process's
        ``sys.modules``. That distinction is load-bearing: ``sys.modules`` is
        process-global, `spacy` transitively imports `torch`, and other tests in
        the suite import `spacy` — so an in-process check passes alone and fails
        under ``pytest -n auto`` purely on test ordering. What we care about is
        this package's *own* import graph, which is exactly what a clean
        interpreter measures.
        """
        import subprocess
        import sys
        from pathlib import Path

        backend_root = Path(__file__).resolve().parents[1]
        probe = (
            "import sys; import services.book_import_service; "
            "leaked=[m for m in ('nlp_histo','docling','torch','transformers') "
            "if m in sys.modules]; print(','.join(leaked))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=str(backend_root),
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert completed.returncode == 0, completed.stderr
        leaked = completed.stdout.strip()
        assert not leaked, (
            f"the import service pulled in {leaked}; the package-on-disk "
            "boundary exists precisely to keep GPU dependencies out of the API "
            "process"
        )


# ---------------------------------------------------------------------------
# gate 1 — schema
# ---------------------------------------------------------------------------


class TestSchemaGate:
    def test_valid_package_has_no_fatal_issues(self, tmp_path):
        write_package(tmp_path)
        assert issues_for(tmp_path).ok

    def test_incompatible_major_version_is_fatal(self, tmp_path):
        write_package(tmp_path, manifest_overrides={"package_schema_version": "2.0.0"})
        issues = issues_for(tmp_path)
        assert not issues.ok
        assert "incompatible_schema_version" in issues.codes()

    def test_compatible_minor_version_is_accepted(self, tmp_path):
        write_package(tmp_path, manifest_overrides={"package_schema_version": "1.7.3"})
        assert issues_for(tmp_path).ok

    def test_incompatible_version_short_circuits_structural_noise(self, tmp_path):
        """A v2 package's fields may mean something else entirely, so reporting
        structural errors against them would be misleading."""
        write_package(
            tmp_path,
            manifest_overrides={"package_schema_version": "2.0.0"},
            elements=[element(element_id="dup"), element(element_id="dup")],
        )
        assert "duplicate_element_id" not in issues_for(tmp_path).codes()

    def test_unparseable_version_is_fatal(self, tmp_path):
        write_package(tmp_path, manifest_overrides={"package_schema_version": "banana"})
        assert "incompatible_schema_version" in issues_for(tmp_path).codes()

    def test_missing_manifest_key_reported(self, tmp_path):
        directory = write_package(tmp_path)
        payload = json.loads((directory / contract.MANIFEST_FILENAME).read_text())
        del payload["extractor"]
        (directory / contract.MANIFEST_FILENAME).write_text(json.dumps(payload))
        assert "missing_manifest_key" in issues_for(tmp_path).codes()

    def test_malformed_manifest_json_raises_package_error(self, tmp_path):
        write_package(tmp_path, manifest_raw="{not json")
        with pytest.raises(PackageError, match="malformed manifest"):
            load_package("pkg", tmp_path)

    def test_malformed_document_json_raises_package_error(self, tmp_path):
        write_package(tmp_path, document_raw="[]")
        with pytest.raises(PackageError, match="must be a JSON object"):
            load_package("pkg", tmp_path)

    def test_missing_package_raises(self, tmp_path):
        with pytest.raises(PackageError, match="no such package"):
            load_package("absent", tmp_path)


# ---------------------------------------------------------------------------
# gate 2 — checksums
# ---------------------------------------------------------------------------


class TestChecksumGate:
    def test_valid_checksums_pass(self, tmp_path):
        write_package(tmp_path)
        assert issues_for(tmp_path).stage_status("checksums") == "pass"

    def test_mismatched_checksum_is_fatal(self, tmp_path):
        directory = write_package(tmp_path)
        (directory / contract.DOCUMENT_FILENAME).write_text(
            json.dumps({"elements": [element()]}) + " ", encoding="utf-8"
        )
        issues = issues_for(tmp_path)
        assert not issues.ok
        assert "checksum_mismatch" in issues.codes()

    def test_missing_checksums_file_is_fatal(self, tmp_path):
        """CHECKSUMS.txt is a required member of the package layout (§4).

        Tightened 2026-07-29 — this previously warned and imported. Treating the
        one gate that proves the bytes are what the worker wrote as optional
        made "unverified" the common case. Producer compatibility belongs in the
        schema version, where it is explicit, not in a validator that quietly
        accepts less.
        """
        write_package(tmp_path, checksums=False)
        issues = issues_for(tmp_path)
        assert not issues.ok
        assert "missing_checksums" in issues.codes()
        assert issues.stage_status("checksums") == "fail"

    def test_missing_checksums_still_reports_structural_defects(self, tmp_path):
        """Multi-error collection is preserved where it is safe to do so.

        An absent inventory is a completeness failure, but the content still
        parsed and is internally consistent — so its structural errors are real
        and worth reporting in the same pass.
        """
        write_package(
            tmp_path,
            checksums=False,
            elements=[element(element_id="dup"), element(element_id="dup", order=1)],
        )
        codes = issues_for(tmp_path).codes()
        assert {"missing_checksums", "duplicate_element_id"} <= codes

    def test_integrity_failure_suppresses_structural_noise(self, tmp_path):
        """A mismatching digest means the bytes are not what was hashed, so
        structural findings would describe corruption and send someone chasing
        a bug that does not exist."""
        directory = write_package(
            tmp_path,
            elements=[element(element_id="dup"), element(element_id="dup", order=1)],
        )
        (directory / contract.DOCUMENT_FILENAME).write_text(
            json.dumps({"elements": [element()]}), encoding="utf-8"
        )
        codes = issues_for(tmp_path).codes()
        assert "checksum_mismatch" in codes
        assert "duplicate_element_id" not in codes

    def test_manifest_must_be_covered_by_the_inventory(self, tmp_path):
        directory = write_package(tmp_path)
        digest = hashlib.sha256(
            (directory / contract.DOCUMENT_FILENAME).read_bytes()
        ).hexdigest()
        (directory / contract.CHECKSUMS_FILENAME).write_text(
            f"{digest}  {contract.DOCUMENT_FILENAME}\n", encoding="utf-8"
        )
        issues = issues_for(tmp_path)
        assert not issues.ok
        assert "unchecksummed_required_file" in issues.codes()

    def test_document_must_be_covered_by_the_inventory(self, tmp_path):
        directory = write_package(tmp_path)
        digest = hashlib.sha256(
            (directory / contract.MANIFEST_FILENAME).read_bytes()
        ).hexdigest()
        (directory / contract.CHECKSUMS_FILENAME).write_text(
            f"{digest}  {contract.MANIFEST_FILENAME}\n", encoding="utf-8"
        )
        assert "unchecksummed_required_file" in issues_for(tmp_path).codes()

    def test_extra_entries_for_real_files_are_verified_not_ignored(self, tmp_path):
        """An inventory may legitimately cover more than the required two —
        page images, assets. Those entries must still be checked."""
        directory = write_package(tmp_path)
        extra = directory / "pages"
        extra.mkdir()
        (extra / "0001.png").write_bytes(b"not-a-real-png")
        with (directory / contract.CHECKSUMS_FILENAME).open("a", encoding="utf-8") as fh:
            fh.write(f"{'0' * 64}  pages/0001.png\n")
        issues = issues_for(tmp_path)
        assert "checksum_mismatch" in issues.codes()

    def test_extra_entry_matching_its_file_passes(self, tmp_path):
        directory = write_package(tmp_path)
        extra = directory / "pages"
        extra.mkdir()
        payload = b"page-bytes"
        (extra / "0001.png").write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        with (directory / contract.CHECKSUMS_FILENAME).open("a", encoding="utf-8") as fh:
            fh.write(f"{digest}  pages/0001.png\n")
        assert issues_for(tmp_path).ok

    def test_listed_but_absent_file_is_fatal(self, tmp_path):
        directory = write_package(tmp_path)
        with (directory / contract.CHECKSUMS_FILENAME).open("a", encoding="utf-8") as fh:
            fh.write(f"{'b' * 64}  pages/0001.png\n")
        assert "checksum_missing_file" in issues_for(tmp_path).codes()

    def test_checksums_referencing_outside_path_is_fatal(self, tmp_path):
        directory = write_package(tmp_path)
        with (directory / contract.CHECKSUMS_FILENAME).open("a", encoding="utf-8") as fh:
            fh.write(f"{'b' * 64}  ../../etc/passwd\n")
        assert "checksum_path_escape" in issues_for(tmp_path).codes()

    def test_malformed_line_is_fatal(self, tmp_path):
        directory = write_package(tmp_path)
        with (directory / contract.CHECKSUMS_FILENAME).open("a", encoding="utf-8") as fh:
            fh.write("this-line-has-no-path\n")
        issues = issues_for(tmp_path)
        assert not issues.ok
        assert "malformed_checksum_line" in issues.codes()

    def test_duplicate_entry_is_fatal(self, tmp_path):
        """Previously the second entry silently replaced the first, so a package
        could carry one digest that verifies and one that does not and still
        pass."""
        directory = write_package(tmp_path)
        digest = hashlib.sha256(
            (directory / contract.MANIFEST_FILENAME).read_bytes()
        ).hexdigest()
        with (directory / contract.CHECKSUMS_FILENAME).open("a", encoding="utf-8") as fh:
            fh.write(f"{digest}  {contract.MANIFEST_FILENAME}\n")
        issues = issues_for(tmp_path)
        assert not issues.ok
        assert "duplicate_checksum_entry" in issues.codes()

    @pytest.mark.parametrize(
        "digest",
        [
            "abc123",  # too short
            "z" * 64,  # right length, not hex
            "a" * 40,  # sha1 length — a different algorithm
            "a" * 128,  # sha512 length
        ],
    )
    def test_invalid_digest_is_fatal(self, tmp_path, digest):
        """Only sha256 is documented; an unfamiliar digest is rejected rather
        than guessed at."""
        directory = write_package(tmp_path)
        with (directory / contract.CHECKSUMS_FILENAME).open("a", encoding="utf-8") as fh:
            fh.write(f"{digest}  pages/0001.png\n")
        assert "invalid_checksum_digest" in issues_for(tmp_path).codes()

    def test_empty_checksums_file_is_fatal(self, tmp_path):
        directory = write_package(tmp_path)
        (directory / contract.CHECKSUMS_FILENAME).write_text(
            "# nothing here\n", encoding="utf-8"
        )
        issues = issues_for(tmp_path)
        assert not issues.ok
        assert "empty_checksums" in issues.codes()

    # -- what the gate CLAIMS, not just what it rejects ---------------------

    def test_a_clean_gate_reports_what_it_verified(self, tmp_path):
        write_package(tmp_path)
        issues = issues_for(tmp_path)
        claims = [i.message for i in issues.infos if i.code == "checksums_verified"]
        # The count is the substantive part; the prose around it is free to
        # change (issues.py — codes are the contract, messages are not).
        assert len(claims) == 1 and claims[0].startswith("verified 2 file(s)")
        assert "checksums_checked" not in issues.codes()

    @pytest.mark.parametrize(
        "damage",
        [
            "mismatch",  # a digest in the loop does not match
            "uncovered_required_file",  # every entry verifies, coverage does not
            "malformed_line",  # parse defect, raised before the loop runs
        ],
    )
    def test_a_failed_gate_never_claims_verification(self, tmp_path, damage):
        """The report said "verified 2 file(s)" next to the mismatch that
        refuted it (audit ``ingestion_pipeline:ing-06``).

        Two of these three cases fail *outside* the digest loop — a package can
        have every listed digest match and still be unverifiable, because the
        inventory is malformed or does not cover the files that define the
        package. So the claim is gated on the whole gate being clean, not on the
        loop alone.
        """
        directory = write_package(tmp_path)
        if damage == "mismatch":
            (directory / contract.DOCUMENT_FILENAME).write_text(
                json.dumps({"elements": [element()]}) + " ", encoding="utf-8"
            )
        elif damage == "uncovered_required_file":
            digest = hashlib.sha256(
                (directory / contract.DOCUMENT_FILENAME).read_bytes()
            ).hexdigest()
            (directory / contract.CHECKSUMS_FILENAME).write_text(
                f"{digest}  {contract.DOCUMENT_FILENAME}\n", encoding="utf-8"
            )
        else:
            with (directory / contract.CHECKSUMS_FILENAME).open("a", encoding="utf-8") as fh:
                fh.write("this-line-has-no-path\n")

        issues = issues_for(tmp_path)
        assert not issues.ok
        assert "checksums_verified" not in issues.codes()
        # Still says how much was walked — honest, and useful when the failure
        # is one bad entry in a large inventory.
        assert "checksums_checked" in issues.codes()

    def test_an_absent_inventory_claims_nothing_either_way(self, tmp_path):
        """Nothing was hashed, so neither line belongs in the report."""
        write_package(tmp_path, checksums=False)
        assert not issues_for(tmp_path).codes() & {
            "checksums_verified",
            "checksums_checked",
        }

    def test_parse_ignores_comments_and_blanks_but_reports_garbage(self):
        parsed = parse_checksums(f"# comment\n\n{'a' * 64}  manifest.json\ngarbage\n")
        assert parsed.entries == {"manifest.json": "a" * 64}
        assert [e["code"] for e in parsed.errors] == ["malformed_checksum_line"]

    def test_parse_reports_duplicates_without_overwriting(self):
        parsed = parse_checksums(
            f"{'a' * 64}  manifest.json\n{'b' * 64}  manifest.json\n"
        )
        assert parsed.entries == {"manifest.json": "a" * 64}
        assert parsed.errors[0]["code"] == "duplicate_checksum_entry"


# ---------------------------------------------------------------------------
# gate 3 — containment (security)
# ---------------------------------------------------------------------------


class TestContainment:
    @pytest.mark.parametrize(
        "hostile",
        [
            "../escape.png",
            "../../etc/passwd",
            "pages/../../../etc/passwd",
            "/etc/passwd",
            "/absolute/path.png",
        ],
    )
    def test_traversal_is_refused(self, tmp_path, hostile):
        (tmp_path / "pkg").mkdir()
        with pytest.raises(PackageError):
            resolve_within(tmp_path / "pkg", hostile)

    def test_legitimate_relative_path_resolves(self, tmp_path):
        root = tmp_path / "pkg"
        root.mkdir()
        assert resolve_within(root, "pages/0001.png") == (root / "pages/0001.png").resolve()

    def test_empty_path_is_refused(self, tmp_path):
        (tmp_path / "pkg").mkdir()
        with pytest.raises(PackageError, match="empty path"):
            resolve_within(tmp_path / "pkg", "   ")

    def test_symlink_escape_is_refused(self, tmp_path):
        root = tmp_path / "pkg"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (root / "link").symlink_to(outside)
        with pytest.raises(PackageError, match="escapes package root"):
            resolve_within(root, "link/secret.png")

    def test_element_asset_path_traversal_is_fatal(self, tmp_path):
        write_package(
            tmp_path,
            elements=[element(image_path="../../../etc/passwd")],
        )
        issues = issues_for(tmp_path)
        assert not issues.ok
        assert "path_escape" in issues.codes()

    def test_page_image_template_traversal_is_fatal(self, tmp_path):
        write_package(
            tmp_path,
            manifest_overrides={
                "page_images": {"dpi": 200, "path_template": "../{page_index}.png"}
            },
        )
        assert "path_escape" in issues_for(tmp_path).codes()

    def test_package_name_itself_cannot_escape(self, tmp_path):
        with pytest.raises(PackageError):
            load_package("../../etc", tmp_path)


# ---------------------------------------------------------------------------
# gate 4 — structural
# ---------------------------------------------------------------------------


class TestStructuralValidators:
    def test_duplicate_element_ids_are_fatal(self, tmp_path):
        write_package(
            tmp_path, elements=[element(element_id="dup"), element(element_id="dup", order=1)]
        )
        issues = issues_for(tmp_path)
        assert not issues.ok
        assert "duplicate_element_id" in issues.codes()

    def test_unresolvable_parent_is_fatal(self):
        issues = IssueCollector()
        pkg = _fake_pkg([element(parent_id="p9999-e0001")])
        validate_parent_ids(pkg, issues)
        assert "unresolvable_parent_id" in issues.codes()

    def test_page_container_parent_resolves(self):
        issues = IssueCollector()
        pkg = _fake_pkg([element(parent_id="p0001")])
        validate_parent_ids(pkg, issues)
        assert issues.ok

    def test_self_parent_is_fatal(self):
        issues = IssueCollector()
        pkg = _fake_pkg([element(element_id="a", parent_id="a")])
        validate_parent_ids(pkg, issues)
        assert "self_parent" in issues.codes()

    def test_duplicate_reading_order_is_fatal(self):
        """Two elements at position 42 have no defined order; picking one
        silently would reorder a learner's text."""
        issues = IssueCollector()
        pkg = _fake_pkg([element(element_id="a", order=1), element(element_id="b", order=1)])
        validate_reading_order(pkg, issues)
        assert "duplicate_reading_order" in issues.codes()
        assert any(i.severity == FATAL for i in issues.fatal)

    def test_reading_order_gap_is_only_a_warning(self):
        """A gap means the worker dropped something — surfaced elsewhere — but
        the sequence is still totally ordered, so the reader still works."""
        issues = IssueCollector()
        pkg = _fake_pkg([element(element_id="a", order=0), element(element_id="b", order=5)])
        validate_reading_order(pkg, issues)
        assert issues.ok
        assert "reading_order_gaps" in issues.codes()

    def test_negative_reading_order_is_fatal(self):
        issues = IssueCollector()
        validate_reading_order(_fake_pkg([element(order=-1)]), issues)
        assert "invalid_reading_order" in issues.codes()

    def test_missing_reading_order_warns(self):
        issues = IssueCollector()
        el = element()
        del el["reading_order"]
        validate_reading_order(_fake_pkg([el]), issues)
        assert issues.ok
        assert "missing_reading_order" in issues.codes()

    def test_invalid_page_index_is_fatal(self, tmp_path):
        write_package(tmp_path, elements=[element(page=0)])
        assert "invalid_page_index" in issues_for(tmp_path).codes()

    def test_undeclared_page_is_fatal(self, tmp_path):
        write_package(tmp_path, elements=[element(page=7)])
        assert "undeclared_page" in issues_for(tmp_path).codes()

    def test_missing_page_dims_is_fatal(self):
        """A bbox is meaningless without page height (§6)."""
        issues = IssueCollector()
        validate_page_dims(_fake_pkg([element()], page_dims={}), issues)
        assert "missing_page_dims" in issues.codes()

    @pytest.mark.parametrize("bad", [0, -5, "tall", None, True])
    def test_invalid_page_dimension_is_fatal(self, bad):
        issues = IssueCollector()
        pkg = _fake_pkg([element()], page_dims={"1": {"width": PAGE_W, "height": bad}})
        validate_page_dims(pkg, issues)
        assert "invalid_page_dimension" in issues.codes()

    def test_coordinate_space_mismatch_in_manifest_is_fatal(self, tmp_path):
        write_package(tmp_path, manifest_overrides={"coordinate_space": "pixels_top_left"})
        issues = issues_for(tmp_path)
        assert not issues.ok
        assert "coordinate_space_mismatch" in issues.codes()

    def test_missing_coordinate_space_is_fatal(self):
        issues = IssueCollector()
        pkg = _fake_pkg([element()], manifest_extra={"coordinate_space": None})
        pkg.manifest.pop("coordinate_space", None)
        validate_coordinate_space(pkg, issues)
        assert "missing_coordinate_space" in issues.codes()

    def test_element_level_coordinate_mismatch_is_fatal(self):
        issues = IssueCollector()
        el = element()
        el["bbox"]["coordinate_space"] = "pixels_top_left"
        validate_coordinate_space(_fake_pkg([el]), issues)
        assert "coordinate_space_mismatch" in issues.codes()

    def test_bbox_outside_page_is_fatal(self):
        issues = IssueCollector()
        el = element()
        el["bbox"]["x2"] = PAGE_W + 50
        validate_bboxes(_fake_pkg([el]), issues)
        assert "bbox_outside_page" in issues.codes()

    def test_bbox_rounding_slack_is_tolerated(self):
        issues = IssueCollector()
        el = element()
        el["bbox"]["x2"] = PAGE_W + 0.4
        validate_bboxes(_fake_pkg([el]), issues)
        assert "bbox_outside_page" not in issues.codes()

    def test_inverted_y_warns_not_fails(self):
        """y1 < y2 contradicts the y-up convention, but the geometry is still
        recoverable — the converter's max/min handles it."""
        issues = IssueCollector()
        el = element()
        el["bbox"]["y1"], el["bbox"]["y2"] = 150.0, 200.0
        validate_bboxes(_fake_pkg([el]), issues)
        assert issues.ok
        assert "inverted_bbox_y" in issues.codes()

    def test_malformed_bbox_is_fatal(self):
        issues = IssueCollector()
        el = element()
        el["bbox"]["x1"] = "left"
        validate_bboxes(_fake_pkg([el]), issues)
        assert "malformed_bbox" in issues.codes()

    def test_bbox_page_matching_page_index_is_clean(self):
        """The shared builder writes ``bbox.page == page_index``; that is the
        contract, not an accident of the fixture."""
        issues = IssueCollector()
        validate_bbox_page_consistency(_fake_pkg([element(page=1)]), issues)
        assert not issues.codes()

    def test_bbox_page_mismatch_is_fatal(self):
        """One page number written twice. A disagreement means one of the two
        is wrong and we cannot tell which — and ``build_plan`` picks the page
        height by ``page_index``, so the y-flip would be off by the height
        difference (§2b: misplaced content is fatal)."""
        issues = IssueCollector()
        el = element(page=1)
        el["bbox"]["page"] = 2
        validate_bbox_page_consistency(_fake_pkg([el]), issues)
        assert not issues.ok
        assert "bbox_page_mismatch" in issues.codes()

    def test_bbox_page_mismatch_is_caught_by_full_validation(self, tmp_path):
        """Registration pin: the unit tests above call the validator directly,
        so an unregistered function would pass them and do nothing in the
        pipeline."""
        el = element(page=1)
        el["bbox"]["page"] = 12
        write_package(tmp_path, elements=[el])
        issues = issues_for(tmp_path)
        assert not issues.ok
        assert "bbox_page_mismatch" in issues.codes()

    def test_absent_bbox_page_is_accepted(self):
        """``BoundingBox.to_dict()`` drops it in the worker today (§4), and
        ``page_index`` alone is unambiguous — warning here would fire on every
        real package while proving nothing."""
        issues = IssueCollector()
        el = element()
        del el["bbox"]["page"]
        validate_bbox_page_consistency(_fake_pkg([el]), issues)
        assert not issues.codes()

    @pytest.mark.parametrize("bad", ["1", 1.0, None, True])
    def test_non_integer_bbox_page_is_fatal(self, bad):
        """Including ``True``, which would otherwise compare equal to page 1."""
        issues = IssueCollector()
        el = element(page=1)
        el["bbox"]["page"] = bad
        validate_bbox_page_consistency(_fake_pkg([el]), issues)
        assert "bbox_page_mismatch" in issues.codes()

    def test_bbox_page_not_reported_when_page_index_is_unusable(self):
        """``validate_page_indices`` already owns that defect; reporting it
        twice would send someone chasing a bbox that is fine."""
        issues = IssueCollector()
        el = element(page="eleven")
        el["bbox"]["page"] = 11
        validate_bbox_page_consistency(_fake_pkg([el]), issues)
        assert not issues.codes()

    def test_element_without_bbox_has_no_page_consistency_issue(self):
        issues = IssueCollector()
        validate_bbox_page_consistency(_fake_pkg([element(bbox=False)]), issues)
        assert not issues.codes()

    def test_page_numbering_is_one_based_throughout(self):
        """The convention itself, pinned once: ``page_index``, ``bbox.page``
        and the ``page_dims`` keys all count from the same base, so page 0 is
        invalid rather than "the first page under a zero-based scheme"."""
        assert contract.PAGE_INDEX_BASE == 1
        issues = IssueCollector()
        el = element(page=0)
        el["bbox"]["page"] = 0
        validate_page_indices(_fake_pkg([el]), issues)
        validate_bbox_page_consistency(_fake_pkg([el]), issues)
        assert "invalid_page_index" in issues.codes()
        assert "bbox_page_mismatch" not in issues.codes()

    def test_unknown_element_type_warns_and_is_preserved(self, tmp_path):
        """Deleting an element for an unrecognised label is the irreversible
        move the architecture forbids (§2b)."""
        write_package(tmp_path, elements=[element(etype="wingding")])
        issues = issues_for(tmp_path)
        assert issues.ok
        assert "unknown_element_type" in issues.codes()

    def test_confidence_out_of_range_warns(self):
        issues = IssueCollector()
        validate_confidence(_fake_pkg([element(confidence={"classification": 4.2})]), issues)
        assert issues.ok
        assert "confidence_out_of_range" in issues.codes()

    def test_malformed_confidence_warns(self):
        issues = IssueCollector()
        validate_confidence(_fake_pkg([element(confidence="high")]), issues)
        assert "malformed_confidence" in issues.codes()

    def test_missing_provenance_warns(self):
        issues = IssueCollector()
        el = element()
        del el["provenance"]
        pkg = _fake_pkg([el])
        from services.document_package.validators import validate_provenance

        validate_provenance(pkg, issues)
        assert issues.ok
        assert "missing_provenance" in issues.codes()

    def test_non_object_element_is_reported_not_crashed(self):
        issues = IssueCollector()
        validate_element_ids(_fake_pkg(["not-an-object"]), issues)
        assert "malformed_element" in issues.codes()

    def test_multiple_independent_defects_are_all_collected(self, tmp_path):
        """One malformed element must not hide the ones after it."""
        write_package(
            tmp_path,
            manifest_overrides={"coordinate_space": "pixels_top_left"},
            elements=[element(element_id="dup"), element(element_id="dup", page=99, order=1)],
        )
        codes = issues_for(tmp_path).codes()
        assert {"duplicate_element_id", "undeclared_page", "coordinate_space_mismatch"} <= codes


# ---------------------------------------------------------------------------
# gate 5 — profile
# ---------------------------------------------------------------------------


class TestProfileGate:
    def test_biomedical_profile_is_refused(self, tmp_path):
        """is_relevant_para deletes 30.5% of German prose; by the time the
        package exists that content is already gone (§2)."""
        write_package(tmp_path, manifest_overrides={"extractor": {"profile": "biomedical"}})
        issues = issues_for(tmp_path)
        assert not issues.ok
        assert "unsupported_profile" in issues.codes()

    def test_missing_profile_is_fatal(self, tmp_path):
        write_package(tmp_path, manifest_overrides={"extractor": {"config_digest": "x"}})
        assert "missing_profile" in issues_for(tmp_path).codes()

    def test_worker_warnings_are_promoted(self, tmp_path):
        write_package(
            tmp_path,
            manifest_overrides={
                "warnings": [
                    {"code": "unresolved_reading_order", "page_index": 3, "detail": "columns"}
                ]
            },
        )
        issues = issues_for(tmp_path)
        assert issues.ok
        assert "unresolved_reading_order" in issues.codes()


# ---------------------------------------------------------------------------
# coordinates (§6)
# ---------------------------------------------------------------------------


class TestCoordinates:
    def test_round_trip_is_identity(self):
        original = {"x1": 72.0, "y1": 200.0, "x2": 300.0, "y2": 150.0}
        box = to_fitz(original, PAGE_H)
        assert from_fitz(box, PAGE_H) == pytest.approx(original)

    def test_y_axis_is_flipped(self):
        box = to_fitz({"x1": 0.0, "y1": PAGE_H, "x2": 10.0, "y2": PAGE_H - 10}, PAGE_H)
        # Top of the page in Docling space (y = page height) is y0 = 0 in fitz.
        assert box.y0 == pytest.approx(0.0)
        assert box.y1 == pytest.approx(10.0)

    def test_inverted_input_still_produces_a_sane_rect(self):
        box = to_fitz({"x1": 0.0, "y1": 150.0, "x2": 10.0, "y2": 200.0}, PAGE_H)
        assert box.y1 > box.y0

    def test_zero_page_height_is_refused(self):
        with pytest.raises(CoordinateError):
            to_fitz({"x1": 0, "y1": 1, "x2": 1, "y2": 0}, 0)

    def test_page_height_lookup_accepts_int_or_str_keys(self):
        assert page_height_for({"1": {"height": 10}}, 1) == 10
        assert page_height_for({1: {"height": 10}}, 1) == 10

    def test_missing_page_height_raises(self):
        with pytest.raises(CoordinateError):
            page_height_for({}, 1)

    def test_as_columns_matches_book_blocks(self):
        assert set(FitzBox(1, 2, 3, 4).as_columns()) == {
            "bbox_x0",
            "bbox_y0",
            "bbox_x1",
            "bbox_y1",
        }


# ---------------------------------------------------------------------------
# planning + persistence seam
# ---------------------------------------------------------------------------


class TestPlanning:
    def test_plan_orders_by_reading_order(self):
        pkg = _fake_pkg(
            [element(element_id="b", order=1, text="Zweitens."),
             element(element_id="a", order=0, text="Erstens.")]
        )
        plan, _ = build_plan(pkg)
        assert [b.element_id for b in plan] == ["a", "b"]

    def test_block_index_is_dense_per_page(self):
        """The native path leaves gaps (book_service.py:142); the importer
        must not inherit that."""
        pkg = _fake_pkg(
            [element(element_id=f"e{i}", order=i, text=f"Satz {i}.") for i in range(4)]
        )
        plan, _ = build_plan(pkg)
        assert [b.block_index for b in plan] == [0, 1, 2, 3]

    def test_bbox_is_converted_to_fitz(self):
        plan, _ = build_plan(_fake_pkg([element()]))
        assert plan[0].bbox is not None
        assert plan[0].bbox["bbox_y0"] == pytest.approx(PAGE_H - 200.0)

    def test_element_without_bbox_still_imports(self):
        """Losing geometry is recoverable; losing text is not."""
        plan, _ = build_plan(_fake_pkg([element(bbox=False)]))
        assert len(plan) == 1
        assert plan[0].bbox is None

    def test_empty_text_is_skipped_and_itemised(self):
        _, skipped = build_plan(_fake_pkg([element(text="   ")]))
        assert skipped and skipped[0]["reason"] == "empty_text"
        assert skipped[0]["element_id"] == "p0001-e0001"

    def test_unknown_type_degrades_rather_than_dropping(self):
        plan, _ = build_plan(_fake_pkg([element(etype="wingding")]))
        assert plan[0].block_type == "unknown"


class TestPersistenceSeam:
    async def test_dry_run_reports_counts_without_writing(self):
        pkg = _fake_pkg([element(element_id="a", order=0), element(element_id="b", order=1)])
        plan, _ = build_plan(pkg)
        outcome = await DryRunPersistence().persist(pkg, plan, user_id="u")
        assert outcome.dry_run is True
        assert outcome.blocks_written == 2
        assert outcome.pages_written == 1
        assert outcome.doc_id is None

    async def test_real_persistence_refuses_until_a3(self):
        pkg = _fake_pkg([element()])
        plan, _ = build_plan(pkg)
        with pytest.raises(PersistenceNotAvailable, match="A3"):
            await PendingSchemaPersistence().persist(pkg, plan, user_id="u")


# ---------------------------------------------------------------------------
# orchestration + import result
# ---------------------------------------------------------------------------


class TestImportFlow:
    async def test_dry_run_success(self, tmp_path):
        write_package(tmp_path)
        result = await book_import_service.dry_run_package("pkg", user_id="u", root=tmp_path)
        assert result.status == contract.STATUS_IMPORTED
        assert result.dry_run is True
        assert result.counts["blocks_planned"] == 1
        assert result.validation["schema"] == "pass"
        assert result.errors == []

    async def test_dry_run_failure_reports_every_gate(self, tmp_path):
        write_package(
            tmp_path,
            manifest_overrides={"extractor": {"profile": "biomedical"}},
            elements=[element(element_id="dup"), element(element_id="dup", order=1)],
        )
        result = await book_import_service.dry_run_package("pkg", user_id="u", root=tmp_path)
        assert result.status == contract.STATUS_REJECTED
        assert result.validation["profile"] == "fail"
        assert result.validation["structural"] == "fail"
        assert len(result.errors) >= 2

    async def test_dry_run_writes_nothing_to_the_package(self, tmp_path):
        directory = write_package(tmp_path)
        before = sorted(p.name for p in directory.iterdir())
        await book_import_service.dry_run_package("pkg", user_id="u", root=tmp_path)
        assert sorted(p.name for p in directory.iterdir()) == before

    async def test_real_import_refuses_and_reports_rather_than_raising(self, tmp_path):
        write_package(tmp_path)
        result = await book_import_service.import_package(
            "pkg", user_id="u", dry_run=False, root=tmp_path, write_result=False
        )
        assert result.status == contract.STATUS_REJECTED
        assert "persistence_unavailable" in {e["code"] for e in result.errors}

    async def test_unreadable_package_returns_a_result_not_an_exception(self, tmp_path):
        result = await book_import_service.import_package(
            "missing", user_id="u", root=tmp_path, write_result=False
        )
        assert result.status == contract.STATUS_REJECTED
        assert result.errors[0]["code"] == "package_unreadable"

    async def test_import_result_is_written_beside_the_package(self, tmp_path):
        directory = write_package(tmp_path)
        await book_import_service.import_package(
            "pkg", user_id="u", dry_run=True, root=tmp_path, write_result=True
        )
        payload = json.loads(
            (directory / contract.IMPORT_RESULT_FILENAME).read_text(encoding="utf-8")
        )
        assert payload["import_schema_version"] == contract.IMPORT_SCHEMA_VERSION
        assert payload["status"] == contract.STATUS_IMPORTED

    async def test_result_carries_all_three_statuses_in_the_vocabulary(self):
        assert {
            contract.STATUS_IMPORTED,
            contract.STATUS_REJECTED,
            contract.STATUS_PARTIAL,
        } == {"imported", "rejected", "partial_rejected"}

    async def test_skipped_elements_are_itemised_not_counted(self, tmp_path):
        write_package(
            tmp_path,
            elements=[element(element_id="a", order=0), element(element_id="b", order=1, text="")],
        )
        result = await book_import_service.dry_run_package("pkg", user_id="u", root=tmp_path)
        assert result.counts["elements_skipped"] == 1
        assert result.skipped[0]["element_id"] == "b"

    async def test_warnings_are_promoted_to_review_work(self, tmp_path):
        write_package(tmp_path, elements=[element(etype="wingding")])
        result = await book_import_service.dry_run_package("pkg", user_id="u", root=tmp_path)
        assert result.status == contract.STATUS_IMPORTED
        assert any(w["code"] == "unknown_element_type" for w in result.warnings_promoted)
        assert all(w["action"] == "queued_for_review" for w in result.warnings_promoted)

    async def test_missing_checksums_rejects_the_dry_run(self, tmp_path):
        write_package(tmp_path, checksums=False)
        result = await book_import_service.dry_run_package("pkg", user_id="u", root=tmp_path)
        assert result.status == contract.STATUS_REJECTED
        assert result.validation["checksums"] == "fail"
        assert "missing_checksums" in {e["code"] for e in result.errors}

    async def test_missing_checksums_rejects_a_real_import(self, tmp_path):
        write_package(tmp_path, checksums=False)
        result = await book_import_service.import_package(
            "pkg", user_id="u", dry_run=False, root=tmp_path, write_result=False
        )
        assert result.status == contract.STATUS_REJECTED
        assert "missing_checksums" in {e["code"] for e in result.errors}

    async def test_a_rejected_result_carries_no_verification_claim(self, tmp_path):
        """The info list is part of the import report (§4), so a success claim
        there is read by whoever triages the rejection."""
        directory = write_package(tmp_path)
        (directory / contract.DOCUMENT_FILENAME).write_text(
            json.dumps({"elements": [element()]}) + " ", encoding="utf-8"
        )
        result = await book_import_service.dry_run_package("pkg", user_id="u", root=tmp_path)
        assert result.status == contract.STATUS_REJECTED
        assert "checksum_mismatch" in {e["code"] for e in result.errors}
        assert "checksums_verified" not in {i["code"] for i in result.info}

    async def test_persistence_is_not_called_after_a_checksum_failure(self, tmp_path):
        """The gate must stop the pipeline, not merely annotate it."""
        write_package(tmp_path, checksums=False)

        class ExplodingPersistence:
            name = "exploding"
            called = False

            async def persist(self, pkg, plan, *, user_id):
                type(self).called = True
                raise AssertionError("persistence ran despite a fatal checksum issue")

        backend = ExplodingPersistence()
        result = await book_import_service.import_package(
            "pkg",
            user_id="u",
            dry_run=False,
            root=tmp_path,
            backend=backend,
            write_result=False,
        )
        assert result.status == contract.STATUS_REJECTED
        assert ExplodingPersistence.called is False

    async def test_api_reports_checksum_failure_without_raising(self, client, db_pool, tmp_path, monkeypatch):
        """API, dry-run and direct-service callers must agree."""
        from tests._auth_helper import register_and_login
        from tests._email_helper import make_test_email

        write_package(tmp_path, name="nochecks", checksums=False)
        monkeypatch.setenv("PACKAGE_ROOT", str(tmp_path))

        headers, _ = await register_and_login(client, db_pool, make_test_email())
        r = await client.post(
            "/api/v1/books/import",
            json={"package_name": "nochecks"},
            headers=headers,
        )
        assert r.status_code == 200
        payload = r.json()
        assert payload["status"] == contract.STATUS_REJECTED
        assert payload["validation"]["checksums"] == "fail"
        assert "missing_checksums" in {e["code"] for e in payload["errors"]}

    def test_list_packages_discovers_only_valid_directories(self, tmp_path):
        write_package(tmp_path, name="good")
        (tmp_path / "empty").mkdir()
        assert book_import_service.list_packages(tmp_path) == ["good"]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _fake_pkg(elements, page_dims=None, manifest_extra=None):
    """An in-memory LoadedPackage for validator-level unit tests."""
    from services.document_package.loader import LoadedPackage

    manifest = {
        "package_schema_version": contract.PACKAGE_SCHEMA_VERSION,
        "coordinate_space": contract.COORDINATE_SPACE,
        "page_dims": page_dims if page_dims is not None else {"1": {"width": PAGE_W, "height": PAGE_H}},
        "extractor": {"profile": "german_fiction"},
        "source": {"sha256": "a" * 64},
        "document_id": "fake",
    }
    if manifest_extra:
        manifest.update(manifest_extra)
    return LoadedPackage(
        package_root=Path("/nonexistent"),
        document_id="fake",
        manifest=manifest,
        document={"elements": elements},
    )


assert WARNING and FATAL  # imported for readability in assertions above


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


class TestImportEndpoint:
    """The API takes a package *name*, never a path."""

    async def test_requires_auth(self, client):
        r = await client.post("/api/v1/books/import", json={"package_name": "pkg"})
        assert r.status_code in (401, 403)

    async def test_blank_name_is_422(self, client, db_pool):
        from tests._auth_helper import register_and_login
        from tests._email_helper import make_test_email

        headers, _ = await register_and_login(client, db_pool, make_test_email())
        r = await client.post(
            "/api/v1/books/import", json={"package_name": "   "}, headers=headers
        )
        assert r.status_code == 422

    async def test_unknown_package_returns_200_with_a_rejected_result(
        self, client, db_pool
    ):
        """The body is the diagnostic. A caller fixing a worker bug needs the
        findings, not just a status code."""
        from tests._auth_helper import register_and_login
        from tests._email_helper import make_test_email

        headers, _ = await register_and_login(client, db_pool, make_test_email())
        r = await client.post(
            "/api/v1/books/import",
            json={"package_name": "does-not-exist"},
            headers=headers,
        )
        assert r.status_code == 200
        payload = r.json()
        assert payload["status"] == contract.STATUS_REJECTED
        assert payload["errors"][0]["code"] == "package_unreadable"

    async def test_traversal_in_package_name_is_refused(self, client, db_pool):
        from tests._auth_helper import register_and_login
        from tests._email_helper import make_test_email

        headers, _ = await register_and_login(client, db_pool, make_test_email())
        r = await client.post(
            "/api/v1/books/import",
            json={"package_name": "../../etc"},
            headers=headers,
        )
        assert r.status_code == 200
        assert r.json()["status"] == contract.STATUS_REJECTED

    async def test_package_listing_requires_auth(self, client):
        assert (await client.get("/api/v1/books/packages")).status_code in (401, 403)
