"""
syllabi_metadata_extraction.py

Extracts structured metadata from syllabi PDFs using GROBID.

Required packages:
    pip install requests pandas openpyxl lxml

Assumptions about GROBID:
  - A GROBID server is running locally at http://localhost:8070
  - /api/processHeaderDocument may return either TEI/XML or BibTeX
    depending on the GROBID version/configuration. Both are handled.
  - /api/processFulltextDocument is used for body text (year, term,
    university, professor inference)
  - When fulltext is used, only a header zone (first ~2500 body chars +
    teiHeader text) is passed to university and professor inference so
    that bibliography entries are never mistaken for course metadata.
"""

import re
import random
import logging
import requests
import pandas as pd
from lxml import etree
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GROBID_BASE_URL   = "http://localhost:8070"
SYLLABI_FOLDER    = Path(__file__).parent / "Syllabi to Draw From"
OUTPUT_XLSX       = Path(__file__).parent / "syllabi_metadata.xlsx"
OUTPUT_CSV        = Path(__file__).parent / "syllabi_metadata.csv"

DIGITIZERS        = ["Ricardy", "Lila"]

TEI_NS            = "http://www.tei-c.org/ns/1.0"
NS                = {"tei": TEI_NS}

# Characters of body text treated as the "first page" for header-zone inference.
# Keeps bibliography entries out of university / professor inference.
HEADER_ZONE_CHARS = 2500

COLUMNS = [
    "Original name of syllabus PDF",
    "Course Title",
    "Course Professors",
    "Year the Course was taught",
    "Term (Spring, Winter, etc) the Course was Taught",
    "University where this course was taught",
    "Person in charge of digitizing this syllabus",
]

TERM_KEYWORDS = {
    "spring": "Spring",
    "summer": "Summer",
    "fall":   "Fall",
    "autumn": "Fall",
    "winter": "Winter",
}

# If any of these words appear in a university-candidate string, reject it
# as a publisher rather than a university.
PUBLISHER_VETO = {
    "press", "publisher", "publishing", "routledge", "wiley", "springer",
    "elsevier", "sage", "norton", "penguin", "blackwell", "random house",
    "taylor", "francis", "macmillan", "palgrave", "bertelsmann",
}

# Words that appear as "authors" in BibTeX but are clearly not people
# (GROBID sometimes includes day names, building names, etc.)
BIBTEX_AUTHOR_VETO = {
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "hall", "center", "centre", "room", "building", "and",
}

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GROBID API helpers
# ---------------------------------------------------------------------------

def check_grobid_alive() -> bool:
    try:
        r = requests.get(f"{GROBID_BASE_URL}/api/isalive", timeout=10)
        return r.status_code == 200
    except requests.RequestException:
        return False


def call_grobid_header(pdf_path: Path) -> str | None:
    """
    POST to processHeaderDocument.
    Returns the raw response body (may be TEI/XML or BibTeX), or None.
    """
    url = f"{GROBID_BASE_URL}/api/processHeaderDocument"
    try:
        with open(pdf_path, "rb") as fh:
            resp = requests.post(
                url,
                files={"input": (pdf_path.name, fh, "application/pdf")},
                data={"includeRawAffiliations": "1"},
                timeout=60,
            )
        if resp.status_code == 200:
            return resp.text
        if resp.status_code == 204:
            log.info("  GROBID header: no content (HTTP 204) for %s", pdf_path.name)
        else:
            log.warning("  GROBID header HTTP %s for %s", resp.status_code, pdf_path.name)
    except requests.RequestException as exc:
        log.error("  GROBID header request failed for %s: %s", pdf_path.name, exc)
    return None


def call_grobid_fulltext(pdf_path: Path) -> str | None:
    """POST to processFulltextDocument. Returns TEI/XML or None."""
    url = f"{GROBID_BASE_URL}/api/processFulltextDocument"
    try:
        with open(pdf_path, "rb") as fh:
            resp = requests.post(
                url,
                files={"input": (pdf_path.name, fh, "application/pdf")},
                data={"includeRawAffiliations": "1"},
                timeout=120,
            )
        if resp.status_code == 200:
            return resp.text
        log.warning("  GROBID fulltext HTTP %s for %s", resp.status_code, pdf_path.name)
    except requests.RequestException as exc:
        log.error("  GROBID fulltext request failed for %s: %s", pdf_path.name, exc)
    return None


# ---------------------------------------------------------------------------
# BibTeX parsing
# (GROBID's processHeaderDocument sometimes returns @misc{...} instead of TEI)
# ---------------------------------------------------------------------------

def parse_bibtex_response(text: str) -> dict[str, str]:
    """
    Parse a BibTeX @misc entry as returned by GROBID, e.g.:
        @misc{-1,
          author = {Last, First and Last, First},
          title  = {Course Title Here},
          date   = {2025-09-01},
          abstract = {…}
        }
    Returns a dict of lowercase field names → string values.
    """
    fields: dict[str, str] = {}
    # Match:  fieldname = { ... }  — handles one level of nested braces
    pattern = re.compile(
        r'(\w+)\s*=\s*\{((?:[^{}]|\{[^{}]*\})*)\}',
        re.DOTALL,
    )
    for m in pattern.finditer(text):
        key   = m.group(1).lower().strip()
        value = m.group(2).strip()
        if value:
            fields[key] = value
    return fields


def bibtex_authors_to_names(author_str: str) -> list[str]:
    """
    Convert BibTeX "Last, First and Last, First" to ["First Last", …].
    Filters out tokens that are clearly not people (day names, building names, etc.)
    """
    names: list[str] = []
    for raw in author_str.split(" and "):
        raw = raw.strip()
        if not raw:
            continue
        # Veto if any token is a known non-name word
        tokens = [t.lower().rstrip(",") for t in raw.split()]
        if any(t in BIBTEX_AUTHOR_VETO for t in tokens):
            continue
        if "," in raw:
            last, _, first = raw.partition(",")
            full = f"{first.strip()} {last.strip()}".strip()
        else:
            full = raw
        if full:
            names.append(full)
    return names


# ---------------------------------------------------------------------------
# TEI/XML parsing helpers
# ---------------------------------------------------------------------------

def parse_tei(xml_text: str, label: str = "") -> etree._Element | None:
    """
    Parse a TEI/XML string. Tries strict mode first, then lxml recovery mode.
    Returns the root element or None.
    """
    if not xml_text or not xml_text.strip():
        log.warning("  TEI response was empty%s", f" ({label})" if label else "")
        return None
    try:
        return etree.fromstring(xml_text.encode("utf-8"))
    except etree.XMLSyntaxError as exc:
        log.warning(
            "  TEI strict parse failed%s: %s — retrying with recovery parser",
            f" ({label})" if label else "", exc,
        )
    try:
        parser = etree.XMLParser(recover=True)
        root   = etree.fromstring(xml_text.encode("utf-8"), parser)
        if root is not None:
            log.info("  TEI recovery parse succeeded%s", f" ({label})" if label else "")
            return root
    except Exception as exc2:
        log.warning("  TEI recovery parse failed%s: %s", f" ({label})" if label else "", exc2)
    log.warning(
        "  Could not parse TEI%s. First 200 chars: %s",
        f" ({label})" if label else "",
        xml_text[:200].replace("\n", " "),
    )
    return None


def get_tei_header_text(root: etree._Element) -> str:
    """Text from <teiHeader> only — structured metadata, no body content."""
    header = root.find(f".//{{{TEI_NS}}}teiHeader")
    if header is not None:
        return " ".join(header.itertext())
    return ""


def get_header_zone(root: etree._Element) -> str:
    """
    Safe inference zone = <teiHeader> text + first HEADER_ZONE_CHARS body chars.
    Never reaches the bibliography / reading-list section.
    """
    header_text = get_tei_header_text(root)
    body_text   = ""
    body = root.find(f".//{{{TEI_NS}}}body")
    if body is not None:
        body_text = " ".join(body.itertext())[:HEADER_ZONE_CHARS]
    elif not header_text:
        body_text = " ".join(root.itertext())[:HEADER_ZONE_CHARS]
    return f"{header_text} {body_text}".strip()


def get_all_text(root: etree._Element) -> str:
    return " ".join(root.itertext())


# TEI structured field extractors -------------------------------------------

def tei_extract_title(root: etree._Element) -> str:
    for xpath in [
        ".//tei:fileDesc//tei:titleStmt/tei:title[@level='a']",
        ".//tei:fileDesc//tei:titleStmt/tei:title",
        ".//tei:analytic/tei:title[@level='a']",
        ".//tei:analytic/tei:title",
    ]:
        for node in root.xpath(xpath, namespaces=NS):
            text = (node.text or "").strip()
            if text:
                return text
    return ""


def tei_extract_authors(root: etree._Element) -> str:
    """
    Pull authors only from <fileDesc> and <analytic> — NOT from <listBibl>
    (the references section), to avoid picking up cited authors.
    """
    authors: list[str] = []
    for author in root.xpath(
        ".//tei:fileDesc//tei:author | .//tei:analytic//tei:author",
        namespaces=NS,
    ):
        forename = " ".join(
            n.text.strip()
            for n in author.xpath(".//tei:forename", namespaces=NS) if n.text
        )
        surname = " ".join(
            n.text.strip()
            for n in author.xpath(".//tei:surname", namespaces=NS) if n.text
        )
        full = f"{forename} {surname}".strip()
        if not full:
            p = author.find(f"{{{TEI_NS}}}persName")
            if p is not None:
                full = "".join(p.itertext()).strip()
        if full:
            authors.append(full)
    return "; ".join(authors)


def tei_extract_date(root: etree._Element) -> str:
    for node in root.xpath(
        ".//tei:publicationStmt//tei:date | .//tei:date", namespaces=NS
    ):
        for candidate in [node.get("when", ""), (node.text or "")]:
            m = re.search(r"\b(19[9]\d|20[0-3]\d)\b", candidate)
            if m:
                return m.group()
    return ""


def tei_extract_affiliation(root: etree._Element) -> str:
    """Pull institutional affiliation; reject publisher-sounding matches."""
    for xpath in [
        ".//tei:affiliation/tei:orgName[@type='institution']",
        ".//tei:affiliation/tei:orgName",
    ]:
        for node in root.xpath(xpath, namespaces=NS):
            text = "".join(node.itertext()).strip()
            if text and not _is_publisher(text):
                return text
    return ""


# ---------------------------------------------------------------------------
# Plain-text inference helpers
# (all receive only header-zone text, never full body)
# ---------------------------------------------------------------------------

def _is_publisher(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in PUBLISHER_VETO)


def infer_year_from_filename(filename: str) -> str:
    """Extract a 4-digit year from the filename itself."""
    m = re.search(r"\b(19[9]\d|20[0-3]\d)\b", filename)
    return m.group() if m else ""


def infer_term_from_filename(filename: str) -> str:
    lower = filename.lower()
    for kw, norm in TERM_KEYWORDS.items():
        if kw in lower:
            return norm
    return ""


def infer_year(text: str) -> str:
    m = re.search(r"\b(19[9]\d|20[0-3]\d)\b", text)
    return m.group() if m else ""


def infer_term(text: str) -> str:
    lower = text.lower()
    for kw, norm in TERM_KEYWORDS.items():
        if kw in lower:
            return norm
    return ""


def infer_university(header_zone: str) -> str:
    """
    Heuristically extract a university name from the header zone.
    Rejects publisher-sounding matches.
    """
    patterns = [
        r"University of [A-Z][A-Za-z\s\-]+",
        r"[A-Z][A-Za-z\s\-]+ University",
        r"Universit[àáâãäå][A-Za-z\s\-de]*",   # handles accented forms, e.g. Universitat, Università
        r"[A-Z][A-Za-z\s\-]+ College",
        r"[A-Z][A-Za-z\s\-]+ School of [A-Za-z\s\-]+",
        r"[A-Z][A-Za-z\s\-]+ Institute of Technology",
    ]
    for pattern in patterns:
        for m in re.finditer(pattern, header_zone):
            candidate = m.group().strip()
            wc = len(candidate.split())
            if 2 <= wc <= 8 and not _is_publisher(candidate):
                return candidate
    return ""


def infer_title_from_text(header_zone: str) -> str:
    """
    Infer course title from header zone.
    Prefers an explicit 'Course Title:' label; falls back to the first
    substantial capitalised line that doesn't look like metadata noise.
    """
    m = re.search(
        r"(?:course\s+title|course\s+name)\s*[:\-]\s*(.+)",
        header_zone, re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()

    skip = re.compile(
        r"^(syllabus|course|professor|instructor|spring|fall|winter|summer|"
        r"office|email|phone|prereq|credit|description|required|textbook|"
        r"readings?|schedule|week\s*\d|january|february|march|april|may|june|"
        r"july|august|september|october|november|december|\d)",
        re.IGNORECASE,
    )
    for line in header_zone.splitlines():
        line = line.strip()
        if len(line) > 12 and line[0].isupper() and not skip.match(line):
            if not re.search(r"\(\d{4}\)", line):  # skip citation-style lines
                return line
    return ""


def infer_professors_from_text(header_zone: str) -> str:
    """
    Look for explicit instructor labels followed by a name in the header zone.
    Returns a semicolon-separated string.
    Only matches when a label is present — avoids reading-list author false-positives.
    """
    pattern = re.compile(
        r"(?:professor|instructor|lecturer|taught by|faculty|prof\.?|"
        r"course\s+(?:instructor|director)|instructor\s+of\s+record)"
        r"\s*[:\-]?\s*"
        r"([A-Z][A-Za-z\-]+(?:\s+[A-Z][A-Za-z\-]+){0,3})",
        re.IGNORECASE,
    )
    seen:   set[str]  = set()
    unique: list[str] = []
    noise = {"office", "hours", "email", "phone", "notes", "syllabus", "course"}
    for m in pattern.finditer(header_zone):
        name = m.group(1).strip()
        if name.lower() in noise:
            continue
        key = name.lower()
        if key not in seen:
            seen.add(key)
            unique.append(name)
    return "; ".join(unique)


def clean_bibtex_title(title: str) -> str:
    """
    Strip trailing noise that GROBID sometimes appends to the title field,
    e.g. year/term tokens, location strings, course codes after the main title.
    Keep it short and descriptive.
    """
    # Remove common trailing fragments that bleed into the title
    noise_suffixes = re.compile(
        r"\s*(?:spring|fall|summer|winter|autumn)?\s*\d{4}.*$"
        r"|\s+(?:mon|tue|wed|thu|fri|sat|sun)[^A-Z]*$"
        r"|\s+\d{1,2}:\d{2}.*$",
        re.IGNORECASE,
    )
    cleaned = noise_suffixes.sub("", title).strip()
    # Remove trailing pipe | University ... artefacts
    cleaned = re.split(r"\s*\|\s*[A-Z]", cleaned)[0].strip()
    return cleaned if cleaned else title


# ---------------------------------------------------------------------------
# Per-file processing
# ---------------------------------------------------------------------------

def process_pdf(pdf_path: Path) -> dict:
    filename = pdf_path.name
    log.info("Processing: %s", filename)

    row = {col: "" for col in COLUMNS}
    row["Original name of syllabus PDF"]              = filename
    row["Person in charge of digitizing this syllabus"] = random.choice(DIGITIZERS)

    # Seed year/term from filename — often the most reliable source
    year = infer_year_from_filename(filename)
    term = infer_term_from_filename(filename)

    # ------------------------------------------------------------------
    # Step 1: Call processHeaderDocument
    # ------------------------------------------------------------------
    header_raw   = call_grobid_header(pdf_path)
    bibtex_fields: dict[str, str] | None = None
    header_root:  etree._Element  | None = None

    if header_raw:
        if header_raw.strip().startswith("@"):
            # GROBID returned BibTeX — parse it directly
            log.info("  Header response is BibTeX; parsing directly")
            bibtex_fields = parse_bibtex_response(header_raw)
        else:
            header_root = parse_tei(header_raw, label="header")

    # ------------------------------------------------------------------
    # Step 2: Extract structured fields from BibTeX (if present)
    # ------------------------------------------------------------------
    title   = ""
    authors = ""

    if bibtex_fields:
        raw_title = bibtex_fields.get("title", "")
        if raw_title:
            title = clean_bibtex_title(raw_title)

        # Year from BibTeX date field
        if not year and "date" in bibtex_fields:
            m = re.search(r"\b(19[9]\d|20[0-3]\d)\b", bibtex_fields["date"])
            if m:
                year = m.group()

        # BibTeX authors: useful but noisy — keep as a fallback pool.
        # We will prefer label-based inference from fulltext; only use these
        # if that fails.
        bibtex_author_list = bibtex_authors_to_names(bibtex_fields.get("author", ""))

    # ------------------------------------------------------------------
    # Step 3: Extract structured fields from TEI (if header returned XML)
    # ------------------------------------------------------------------
    if header_root is not None:
        if not title:
            title = tei_extract_title(header_root)
        if not authors:
            authors = tei_extract_authors(header_root)
        if not year:
            year = tei_extract_date(header_root)

    # ------------------------------------------------------------------
    # Step 4: Fetch fulltext for body-text inference
    # ------------------------------------------------------------------
    ft_text = call_grobid_fulltext(pdf_path)
    ft_root = parse_tei(ft_text, label="fulltext") if ft_text else None

    if ft_root is not None:
        header_zone = get_header_zone(ft_root)
        all_text    = get_all_text(ft_root)
        log.info("  Using header zone (%d chars) for sensitive field inference", len(header_zone))
    else:
        # Last resort: use abstract from BibTeX as the header zone
        header_zone = bibtex_fields.get("abstract", "") if bibtex_fields else ""
        all_text    = header_zone
        log.warning("  No fulltext parse available for %s; inference may be limited", filename)

    # ------------------------------------------------------------------
    # Step 5: Fill remaining fields via text inference
    # ------------------------------------------------------------------
    if not title:
        title = infer_title_from_text(header_zone)

    # Try label-based professor inference from the fulltext header zone first
    if not authors:
        authors = infer_professors_from_text(header_zone)

    # If that found nothing, fall back to filtered BibTeX authors
    if not authors and bibtex_fields:
        filtered = [
            n for n in bibtex_author_list
            if not _is_publisher(n)
        ]
        authors = "; ".join(filtered)

    if not year:
        year = infer_year(all_text)

    if not term:
        term = infer_term(all_text)

    # University: try TEI structured field, then header-zone text inference
    uni = ""
    if header_root is not None:
        uni = tei_extract_affiliation(header_root)
    if not uni:
        uni = infer_university(header_zone)

    # ------------------------------------------------------------------
    # Step 6: Populate row
    # ------------------------------------------------------------------
    row["Course Title"]                                     = title
    row["Course Professors"]                                = authors
    row["Year the Course was taught"]                       = year
    row["Term (Spring, Winter, etc) the Course was Taught"] = term
    row["University where this course was taught"]          = uni

    return row


# ---------------------------------------------------------------------------
# Summary statistics
# ---------------------------------------------------------------------------

def score_row(row: dict) -> str:
    key_fields = [
        "Course Title",
        "Course Professors",
        "Year the Course was taught",
        "Term (Spring, Winter, etc) the Course was Taught",
        "University where this course was taught",
    ]
    filled = sum(1 for f in key_fields if row.get(f, "").strip())
    if filled == len(key_fields):
        return "success"
    if filled > 0:
        return "partial"
    return "failed"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not check_grobid_alive():
        log.error(
            "GROBID server is not responding at %s. "
            "Please start GROBID before running this script.",
            GROBID_BASE_URL,
        )
        return

    pdf_files = sorted(SYLLABI_FOLDER.glob("*.pdf"))
    if not pdf_files:
        log.error("No PDF files found in: %s", SYLLABI_FOLDER)
        return

    log.info("Found %d PDF(s) in '%s'", len(pdf_files), SYLLABI_FOLDER)

    rows     = []
    counters = {"success": 0, "partial": 0, "failed": 0}

    for pdf_path in pdf_files:
        try:
            row = process_pdf(pdf_path)
        except Exception as exc:
            log.error("Unexpected error processing %s: %s", pdf_path.name, exc)
            row = {col: "" for col in COLUMNS}
            row["Original name of syllabus PDF"]               = pdf_path.name
            row["Person in charge of digitizing this syllabus"] = random.choice(DIGITIZERS)
        rows.append(row)
        counters[score_row(row)] += 1

    df = pd.DataFrame(rows, columns=COLUMNS)
    df.to_excel(OUTPUT_XLSX, index=False)
    df.to_csv(OUTPUT_CSV, index=False)
    log.info("Saved Excel → %s", OUTPUT_XLSX)
    log.info("Saved CSV   → %s", OUTPUT_CSV)

    total = len(pdf_files)
    print("\n" + "=" * 50)
    print("  EXTRACTION SUMMARY")
    print("=" * 50)
    print(f"  PDFs processed   : {total}")
    print(f"  Fully parsed     : {counters['success']}")
    print(f"  Partial metadata : {counters['partial']}")
    print(f"  Failed / empty   : {counters['failed']}")
    print("=" * 50 + "\n")


if __name__ == "__main__":
    main()
