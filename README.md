# Syllabi Text Extraction for Theory Collection

A pipeline that extracts structured metadata and reading-list references from course syllabus PDFs, producing clean Excel spreadsheets for a political theory/political economy collection.

---

## Overview

The pipeline has three extraction stages plus two optional visualization scripts:

1. **`syllabi_text_review.py`** *(optional)* — extracts raw text from each PDF using pdfplumber and produces a browser-based viewer for reviewing the extracted content before running the main pipeline.
2. **`syllabi_metadata_extraction.py`** — extracts course-level metadata from each syllabus PDF (title, professor(s), university, year, term) using pdfplumber for text extraction and the Claude API for intelligent field inference. Writes `syllabi_metadata.xlsx` and `syllabi_metadata.csv`.
3. **`syllabus_literature_extraction.py`** — extracts every bibliographic reference from the reading lists in each syllabus using the Claude API, deduplicates entries that appear across multiple syllabi, enriches missing DOIs via Crossref, and writes a formatted Excel workbook with dropdown validation. Also saves per-syllabus JSON files to `extracted_references/`.
4. **`reading_leaderboard.py`** *(optional)* — generates an interactive HTML bar chart ranking readings by how many syllabi they appear in. Reads `literature_from_selected_syllabi.xlsx` and writes `reading_leaderboard.html`.
5. **`newest_readings_leaderboard.py`** *(optional)* — generates an HTML leaderboard of the most recently published readings, sorted by year (newest first). Reads `literature_from_selected_syllabi.xlsx` and writes `newest_readings_leaderboard.html`.

> **Run order:** Stage 1 → Stage 2 → Stage 3. Stages 4 and 5 can be run any time after Stage 3. Stage 3 reads `syllabi_metadata.xlsx` produced by Stage 2 to build descriptive class labels.

---

## Requirements

### Python packages

```bash
# All scripts
pip install pdfplumber pandas openpyxl

# Stages 2 & 3 (metadata + literature extraction) — Claude API + env file loading
pip install anthropic python-dotenv

# Stage 3 (literature extraction) — Crossref DOI enrichment
pip install requests

# Stage 1 (text review) — optional markdown rendering
pip install markdown
```

### Anthropic API key (Stages 2 & 3)

Create a `.env` file in the project root (it is already listed in `.gitignore` and will never be committed):

```
ANTHROPIC_API_KEY=your-api-key-here
```

The scripts load this file automatically at startup. For Stage 2, if the key is not set, it falls back to regex heuristics for field inference. Stage 3 requires a valid key.

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
├── reading_leaderboard.py                 # Stage 4: cross-syllabus frequency leaderboard
├── newest_readings_leaderboard.py         # Stage 5: most-recent readings leaderboard
│
├── text_review/                           # Output of Stage 1
│   ├── <syllabus name>.md                 #   Raw extracted text per syllabus
│   └── index.html                         #   Browser viewer
│
├── syllabi_metadata.xlsx                  # Output of Stage 2
├── syllabi_metadata.csv                   # Output of Stage 2 (CSV copy)
│
├── extracted_references/                  # Output of Stage 3 (per-syllabus JSON)
│   └── <class label>.json                 #   Extracted references per syllabus
├── literature_from_selected_syllabi.xlsx  # Output of Stage 3 (combined workbook)
│
├── reading_leaderboard.html               # Output of Stage 4
└── newest_readings_leaderboard.html       # Output of Stage 5
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

Reads all PDFs from `Syllabi to Draw From/`, uses pdfplumber to extract text, sends it to the Claude API in chunks to identify and parse every assigned reading, then queries Crossref to fill in missing DOIs. Produces `literature_from_selected_syllabi.xlsx` and saves intermediate per-syllabus results to `extracted_references/`.

Readings that appear in more than one syllabus are deduplicated using token-level Jaccard similarity on normalised titles (threshold: 0.82).

The output workbook contains two sheets:
- **Literature** — one row per unique reference with columns for citation, authors, year, title, journal/publication, issue/pages, DOI link, source type, date added, and the class(es) where the reading appeared
- **_ClassList** — reference sheet used by Excel dropdown validation for the "Class/es" column

Source type is classified by Claude during extraction. Class labels in the "Class/es" column are built from `syllabi_metadata.xlsx` in the format `"Course Title" Professor(s) – Term Year`; if that file is absent, raw PDF filenames are used.

---

### Stage 4 — Reading frequency leaderboard

```bash
python reading_leaderboard.py
```

Reads `literature_from_selected_syllabi.xlsx` and produces `reading_leaderboard.html`: an interactive bar chart ranking readings by how many syllabi they appear in. Only readings that appear in at least `MIN_SYLLABI` syllabi (default: 2) are included. The page also lists all analyzed syllabi with course title, instructors, term, year, and university pulled from `syllabi_metadata.xlsx`.

Open `reading_leaderboard.html` in a browser — no server required.

---

### Stage 5 — Newest readings leaderboard

```bash
python newest_readings_leaderboard.py
```

Reads `literature_from_selected_syllabi.xlsx` and produces `newest_readings_leaderboard.html`: a sortable HTML table of the top `TOP_N` most recently published readings (default: 50), sorted by year (newest first). Each row shows title, authors, year, publication venue, DOI link, and the syllabus/syllabi where the reading was assigned.

Open `newest_readings_leaderboard.html` in a browser — no server required.

---

## Notes

- University inference in the regex fallback rejects publisher names (Routledge, Wiley, Springer, etc.) that sometimes appear in syllabus headers.
- A summary of fully parsed / partial / failed PDFs is printed to the console after each Stage 2 run.
- The `LLM_MODEL` variable in `syllabi_metadata_extraction.py` defaults to `claude-haiku-4-5-20251001` for speed and cost. Change it to `claude-sonnet-4-6` for harder cases.
