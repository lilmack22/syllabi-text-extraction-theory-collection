# Syllabi Text Extraction for Theory Collection

A pipeline that extracts structured metadata and reading-list references from course syllabus PDFs, producing clean Excel spreadsheets for a political theory/political economy collection.

---

## Overview

The pipeline has three scripts:

1. **`syllabi_text_review.py`** *(optional)* — extracts raw text from each PDF using pdfplumber and produces a browser-based viewer for reviewing the extracted content before running the main pipeline.
2. **`syllabi_metadata_extraction.py`** — extracts course-level metadata from each syllabus PDF (title, professor(s), university, year, term) using pdfplumber for text extraction and the Claude API for intelligent field inference. Writes `syllabi_metadata.xlsx` and `syllabi_metadata.csv`.
3. **`syllabus_literature_extraction.py`** — extracts every bibliographic reference from the reading lists in each syllabus using GROBID, deduplicates entries that appear across multiple syllabi, and writes a formatted Excel workbook with dropdown validation.

> **Run order:** Stage 1 → Stage 2 → Stage 3. Stage 3 reads `syllabi_metadata.xlsx` produced by Stage 2 to build descriptive class labels.

---

## Requirements

### Python packages

```bash
# All scripts
pip install pdfplumber pandas openpyxl

# Stage 2 (metadata extraction) — Claude API + env file loading
pip install anthropic python-dotenv

# Stage 3 (literature extraction) — GROBID client
pip install requests lxml

# Stage 1 (text review) — optional markdown rendering
pip install markdown
```

### Anthropic API key (Stage 2)

Create a `.env` file in the project root (it is already listed in `.gitignore` and will never be committed):

```
ANTHROPIC_API_KEY=your-api-key-here
```

The script loads this file automatically at startup. If the key is not set, it falls back to regex heuristics for field inference.

### GROBID (Stage 3 only)

A GROBID server must be running locally at `http://localhost:8070` before running `syllabus_literature_extraction.py`. See the [GROBID documentation](https://grobid.readthedocs.io/en/latest/Grobid-service/) for setup instructions. GROBID is **not** required for Stages 1 or 2.

---

## Project Structure

```
.
├── Syllabi to Draw From/                  # Place input syllabus PDFs here
│
├── .env                                   # API key (not committed — see .gitignore)
├── syllabi_text_review.py                 # Stage 1: text extraction viewer
├── syllabi_metadata_extraction.py         # Stage 2: course metadata extraction
├── syllabus_literature_extraction.py      # Stage 3: reading-list extraction
│
├── text_review/                           # Output of Stage 1
│   ├── <syllabus name>.md                 #   Raw extracted text per syllabus
│   └── index.html                         #   Browser viewer
│
├── syllabi_metadata.xlsx                  # Output of Stage 2
├── syllabi_metadata.csv                   # Output of Stage 2 (CSV copy)
└── literature_from_selected_syllabi.xlsx  # Output of Stage 3
```

---

## Usage

### Stage 1 — Review extracted text (optional)

```bash
python syllabi_text_review.py
```

Reads all PDFs from `Syllabi to Draw From/` and writes:
- `text_review/<name>.md` — plain-text extraction per syllabus
- `text_review/index.html` — a browser viewer with a sidebar navigation panel, rendered markdown, and a link to open the original PDF

Open `text_review/index.html` in a browser to inspect each PDF's extracted text and verify quality before running Stage 2.

---

### Stage 2 — Extract course metadata

```bash
python syllabi_metadata_extraction.py
```

Reads all PDFs from `Syllabi to Draw From/` and produces `syllabi_metadata.xlsx` and `syllabi_metadata.csv` with the following columns:

| Column | Description |
|--------|-------------|
| Original name of syllabus PDF | Source filename |
| Course Title | Extracted course title |
| Course Professors | Instructor name(s), semicolon-separated |
| Year the Course was taught | 4-digit year |
| Term (Spring, Winter, etc) the Course was Taught | Academic term |
| University where this course was taught | Institution name |
| Person in charge of digitizing this syllabus | Randomly assigned from `DIGITIZERS` list |

Extraction uses a two-layer approach:
1. **Claude API (primary)** — the first ~3,000 characters of each PDF are sent to the Claude API with a structured prompt that instructs the model to identify course title, instructors, year, term, and university while filtering out book authors, guest speakers, and publisher names.
2. **Regex heuristics (fallback)** — if the API key is absent or a call fails, the script falls back to pattern matching on a header zone of the extracted text.

Year and term are always seeded from the PDF filename first (most reliable source) and used to hint the LLM or fill gaps left by regex.

---

### Stage 3 — Extract reading-list references

```bash
python syllabus_literature_extraction.py
```

Requires GROBID to be running. Reads all PDFs from `Syllabi to Draw From/`, calls GROBID `processFulltextDocument` with citation consolidation, and produces `literature_from_selected_syllabi.xlsx`.

Readings that appear in more than one syllabus are deduplicated using token-level Jaccard similarity on normalised titles (threshold: 0.82).

The output workbook contains two sheets:
- **Literature** — one row per unique reference with columns for citation, authors, year, title, journal/publication, issue/pages, DOI link, source type, date added, and the class(es) where the reading appeared
- **_ClassList** — reference sheet used by Excel dropdown validation for the "Class/es" column

Source type is inferred heuristically: journal articles, books, and white papers are distinguished by the structure of the GROBID TEI `<biblStruct>` element. Class labels in the "Class/es" column are built from `syllabi_metadata.xlsx` in the format `"Course Title" Professor(s) – Term Year`; if that file is absent, raw PDF filenames are used.

---

## Notes

- University inference in the regex fallback rejects publisher names (Routledge, Wiley, Springer, etc.) that sometimes appear in syllabus headers.
- A summary of fully parsed / partial / failed PDFs is printed to the console after each Stage 2 run.
- The `LLM_MODEL` variable in `syllabi_metadata_extraction.py` defaults to `claude-haiku-4-5-20251001` for speed and cost. Change it to `claude-sonnet-4-6` for harder cases.
