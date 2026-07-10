"""
FALKE Matrix Pipeline — Extraction Layer Data Contract
=======================================================
This module defines the Pydantic v2 schema that represents the **output of the
extraction layer** and the **input contract for the normalization layer**.

Pipeline position:
    Raw bid PDF  →  [Ingestion Router]  →  [Extractor (pdfplumber / OCR)]
    →  BidDocument (this schema)  →  [Normalization Layer]  →  Leveled Matrix

Every field here is an extraction-time signal.  The normalization layer reads
these models and applies bid-leveling logic (scope-gap detection, contractor
total reconciliation, CSI taxonomy normalization, board-memo generation).
Any field whose semantics could affect leveling math or scope classification
must be represented here — it cannot be inferred downstream.

Canonical reference: FALKE/03_Matrix/README.md
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class CostStructure(str, Enum):
    """
    How the contractor structured their pricing within a division.

    Used by the normalization layer to decide whether a division total was
    explicitly stated as a single number (LUMP_SUM) or can be derived from
    sub-line arithmetic (ITEMIZED / PARTIAL_ITEMIZED).  Critical for
    near-universal-lump-sum bids vs. fully-itemized bids.
    """
    LUMP_SUM = "LUMP_SUM"
    """Contractor provided one total for the entire division with no sub-line breakdown."""
    ITEMIZED = "ITEMIZED"
    """Contractor broke the division into named sub-lines, each with an amount."""
    PARTIAL_ITEMIZED = "PARTIAL_ITEMIZED"
    """Mix: some sub-lines have amounts; others are rolled up into a group total."""


class ClassificationSource(str, Enum):
    """
    Whether the CSI division code on a DivisionBid originated from the
    contractor's own document or was remapped by the pipeline.

    When PIPELINE_REMAPPED, `contractor_native_code` preserves the original
    code for audit trail and QA.  A legacy-format bidder, for example, uses
    CSI-1995 2-digit codes that the pipeline remaps to the DIV XX 00 00 format.
    """
    CONTRACTOR_NATIVE = "CONTRACTOR_NATIVE"
    """The contractor used this exact division code in their submission."""
    PIPELINE_REMAPPED = "PIPELINE_REMAPPED"
    """Pipeline translated a non-standard contractor code to the canonical Falke format."""


class GrandTotalConfidence(str, Enum):
    """
    Confidence that the extracted grand total in BidFooter is complete and
    internally consistent.

    HIGH   — all footer rows (construction subtotal, GC fee, insurance, etc.)
              are explicitly stated and reconcile to the grand total within $1.
    MEDIUM — GC fee is stated but insurance appears baked into the subtotal
              rather than called out as a separate line.
    LOW    — GC fee and/or insurance are absent; the relationship between the
              construction subtotal and the grand total is unexplained.
    """
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class InputType(str, Enum):
    """
    Physical nature of the source PDF.  Drives the ingestion router's choice
    of extraction engine.

    DIGITAL_NATIVE → pdfplumber (text layer present)
    IMAGE_SCAN     → OCR (AWS Textract or Google Document AI)
    HYBRID         → per-page routing (e.g. digital-native Falke form with
                     scanned lead-in pages)

    IMAGE_SCAN is the failure mode for a fully-rasterized submission: a bid PDF
    where 0 characters are extracted by both pdfplumber and PyMuPDF across all
    pages (every page a full-page raster scan) must be routed to OCR, never
    treated as digital-native.
    """
    DIGITAL_NATIVE = "DIGITAL_NATIVE"
    IMAGE_SCAN = "IMAGE_SCAN"
    HYBRID = "HYBRID"


class FormType(str, Enum):
    """
    Whether the contractor used Falke's standardised bid form, their own
    format, or a hybrid.  Drives extraction heuristics and normalization
    mapping — the extractor must always set this explicitly.

    FALKE_STANDARD — Contractor filled and returned Falke's issued bid form.
    CONTRACTOR_OWN — Contractor submitted in their own proprietary format
                     (e.g. a legacy CSI-1995 2-digit cost-code schedule).
    HYBRID         — Lead-in pages in contractor's own format (e.g. an
                     exclusion letter) followed by the Falke standard form.
    """
    FALKE_STANDARD = "FALKE_STANDARD"
    CONTRACTOR_OWN = "CONTRACTOR_OWN"
    HYBRID = "HYBRID"


class ExtractionConfidence(str, Enum):
    """
    Overall confidence that the extraction layer correctly captured the
    contractor's intent for this document.  Set by the extractor; consumed
    by the normalization layer and QA review step.

    HIGH   — All fields extracted cleanly; no ambiguities or inferences.
    MEDIUM — Minor ambiguities resolved by heuristic; worth human spot-check.
    LOW    — Significant inference or OCR uncertainty; human review required
             before the extracted values enter the leveled matrix.
    """
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


# ---------------------------------------------------------------------------
# Core models
# ---------------------------------------------------------------------------

class LineItem(BaseModel):
    """A single priced (or explicitly zero-priced) scope item within a division."""

    description: str

    amount: Optional[Decimal] = None
    """Extracted dollar amount.  None means the cell was blank in the source document."""

    is_allowance: bool = False
    """True when the contractor flagged this item as an allowance (estimate, not firm price)."""

    allowance_basis: Optional[str] = None
    """
    Why this item is an allowance — e.g. 'fixture schedule incomplete', 'specs
    not provided', 'scope TBD', 'building required vendor'.  Only meaningful
    when is_allowance=True.  Some bidders include this context in a comments
    column; required for the board-memo layer to explain the allowance.
    """

    is_excluded: bool = False
    """
    True when the contractor explicitly excludes this item from their scope —
    they will not perform or coordinate it.  Distinct from is_by_owner_others:
    exclusion means the contractor declines responsibility; by-others means
    the contractor acknowledges the item exists but flags another party as
    responsible (and may include a pass-through cost).
    """

    is_by_owner_others: bool = False
    """
    True when the contractor marks the item as 'By Others', 'Budget by Others',
    'NIC — By Others', 'By Owner', or equivalent language.  Items flagged True
    must be excluded from the contractor's leveled construction total during
    normalization — they are not the contractor's direct cost.
    """

    by_others_verbatim: Optional[str] = None
    """
    Exact language used by the contractor to indicate 'by others' / 'by owner'.
    Preserved for audit trail and to support board-memo explanations.
    Only populated when is_by_owner_others=True.
    """

    is_explicit_zero: bool = False
    """
    True when the PDF contains a literal '0' or '$0.00' typed by the contractor.
    False when the source cell was blank (form default that renders as $0).

    This distinction is foundational:
      - Explicit zero  → contractor says 'this item is in scope at no additional cost'.
      - Blank / False  → potential scope gap; normalization layer must flag for review.
    """

    is_not_comparable: bool = False
    """
    True when the item is classified 'Not Comparable' (Falke §2 R3 vocabulary,
    typically via structured intake). The amount is KEPT as submitted (R33 —
    never silently alter a bid; it stays in the bidder's own subtotal math)
    but is EXCLUDED from every cross-bid benchmark median (R7/A5, ENC-2).
    """

    notes: Optional[str] = None
    """Any additional contractor notes or comments captured alongside this line item."""

    @model_validator(mode='after')
    def explicit_zero_requires_amount(self) -> 'LineItem':
        if self.is_explicit_zero and self.amount is None:
            raise ValueError(
                'is_explicit_zero=True requires amount to be set (Decimal(0) for $0.00); '
                'amount=None means the cell was blank, which is contradictory.'
            )
        return self


class DivisionBid(BaseModel):
    """
    A single CSI division's bid data as extracted from one contractor's submission.
    """

    csi_code: str
    """
    Canonical Falke CSI division code in DIV XX 00 00 format.
    When classification_source=PIPELINE_REMAPPED, this is the normalized code;
    contractor_native_code holds the original.
    """

    division_name: str

    cost_structure: CostStructure = CostStructure.ITEMIZED
    """
    Pricing structure the contractor used for this division.
    Critical for normalization: determines whether division_subtotal was
    explicitly stated or must be derived from sub-line arithmetic.
    """

    classification_source: ClassificationSource = ClassificationSource.CONTRACTOR_NATIVE
    """Whether this division code originated from the contractor or was remapped by the pipeline."""

    contractor_native_code: Optional[str] = None
    """
    The original division code as it appeared in the contractor's document.
    Only populated when classification_source=PIPELINE_REMAPPED.
    Preserved for audit trail and QA.
    """

    line_items: list[LineItem] = []

    division_subtotal: Optional[Decimal] = None
    """
    Extracted division total.  When cost_structure=LUMP_SUM, this is the single
    number the contractor provided; when ITEMIZED, it should reconcile with the
    sum of line_item amounts (normalization layer validates this).
    """

    @model_validator(mode='after')
    def remapped_requires_native_code(self) -> 'DivisionBid':
        if (self.classification_source == ClassificationSource.PIPELINE_REMAPPED
                and self.contractor_native_code is None):
            raise ValueError(
                'classification_source=PIPELINE_REMAPPED requires contractor_native_code '
                'to be set for audit trail. Set it to the original code from the contractor document.'
            )
        return self


class BidFooter(BaseModel):
    """
    The fee, insurance, and grand-total section at the bottom of a bid form.
    All fields are optional because contractor submissions vary widely in
    which footer rows they populate.
    """

    construction_cost_subtotal: Optional[Decimal] = None
    """Sum of all division subtotals before fees and insurance."""

    general_liability_insurance: Optional[Decimal] = None
    builders_risk_insurance: Optional[Decimal] = None
    gc_fee: Optional[Decimal] = None
    overhead_and_profit: Optional[Decimal] = None
    other_fees_subtotal: Optional[Decimal] = None

    grand_total: Optional[Decimal] = None
    """Final bid total as stated by the contractor."""

    bond: Optional[Decimal] = None

    alternates: list[LineItem] = []
    """Bid alternates (add/deduct options) extracted from the footer or addendum section."""

    grand_total_confidence: GrandTotalConfidence = GrandTotalConfidence.LOW
    """
    Confidence assessment for the extracted grand total.
    Set by the extractor based on which footer rows were found and whether
    the arithmetic reconciles.  Drives normalization-layer warnings and
    board-memo caveats.
    """

    confidence_flags: list[str] = Field(default_factory=list)
    """
    Machine-readable reasons for a non-HIGH grand_total_confidence rating.
    Examples: ['GC_FEE_MISSING', 'INSURANCE_NOT_STATED', 'ARITHMETIC_DISCREPANCY'].
    Consumed by the normalization layer to generate structured warnings.
    """

    @model_validator(mode='after')
    def high_confidence_forbids_flags(self) -> 'BidFooter':
        if (self.grand_total_confidence == GrandTotalConfidence.HIGH
                and self.confidence_flags):
            raise ValueError(
                f'grand_total_confidence=HIGH is incompatible with non-empty confidence_flags '
                f'{self.confidence_flags}. HIGH means the footer fully reconciles; there is nothing to flag.'
            )
        return self


class BidQualifications(BaseModel):
    """
    Contractor qualifications, exclusions, assumptions, and payment terms —
    typically found in a 'Notes & Qualifications' section appended to the bid.
    All fields are free-text captures from the source document.
    """

    notes: Optional[str] = None
    qualifications: Optional[str] = None
    exclusions: Optional[str] = None
    assumptions: Optional[str] = None
    terms: Optional[str] = None


class BidDocument(BaseModel):
    """
    Top-level extraction artifact: one contractor's complete bid submission as
    extracted from a single PDF.  This is the unit of work handed from the
    extraction layer to the normalization layer.
    """

    contractor_name: str
    project_name: Optional[str] = None
    project_address: Optional[str] = None
    bid_date: Optional[str] = None
    """Bid date as a raw string; normalization layer parses to date if needed."""

    total_gsf: Optional[int] = None
    """Gross square footage of the project, if stated in the bid document."""

    form_type: FormType
    """
    Bid form type — whether the contractor used Falke's standardised form,
    their own format, or a hybrid.  The extractor must set this explicitly;
    it drives extraction heuristics and normalization mapping.
    """

    bid_document_input_type: InputType = InputType.DIGITAL_NATIVE
    """
    Physical nature of the source PDF.  Drives the ingestion router's engine
    selection before extraction begins.

    DIGITAL_NATIVE → pdfplumber
    IMAGE_SCAN     → OCR (AWS Textract / Google Document AI)
    HYBRID         → per-page routing

    A fully-rasterized submission (0 chars extracted by pdfplumber and PyMuPDF
    across all pages; full-page rasters) is IMAGE_SCAN and must be OCR'd.
    """

    divisions: list[DivisionBid]
    footer: BidFooter
    qualifications: BidQualifications

    extraction_confidence: ExtractionConfidence
    """
    Overall extraction confidence.  The extractor must set this explicitly.
    LOW confidence requires human review before values enter the leveled matrix.
    """

    extraction_warnings: list[str] = []
    """
    Human-readable extraction warnings generated during PDF parsing.
    Examples: field OCR uncertainty, arithmetic mismatches, unexpected form layout.
    """
