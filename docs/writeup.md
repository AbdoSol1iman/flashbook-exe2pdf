# How I Converted a Flash eBook (.exe) to PDF — Complete Method

> Case study: `Natural Language Processing .exe` (24 MB) → `Natural Language Processing.pdf` (207 pages, 12.5 MB)
> Written **the day it was done**, with the actual numbers from this book, so you can follow along on a similar file.

---

## 1. The big picture (read this first)

Old "eBook .exe" files (especially those made with **Flip PDF Professional**) are not documents —
they are **programs that hide a small filesystem inside themselves**:

```
┌─────────────────────────────────────────────────┐
│  Windows executable (Flash Player + app code)   │  ← the visible program
├─────────────────────────────────────────────────┤
│  APPENDED OVERLAY:                              │
│    1. manifest  (name=index, one per line)      │  ← table of contents
│    2. file blobs (SWFs, XMLs, JPEGs in order)   │  ← the actual book
└─────────────────────────────────────────────────┘
```

So the whole job is:

```
 FIND overlay → walk blobs → identify pages → fix page ORDER → render → merge
```

That's it. Every code block below is a real snippet from this conversion.

**Tools used (all free):**

| Tool | Why |
|---|---|
| Python 3 | parsing / carving (no special libs needed — just `struct`, `zlib`, `re`) |
| `7z` | quick check "is it a self-extractor?" (here: it wasn't) |
| **JPEXS Free Flash Decompiler** (ffdec) | the star: opens .exe/.swf, exports **frame → PDF** directly |
| `pypdf` | merging 207 single-page PDFs + bookmarks |
| `pdftotext`, `pdftoppm`, `pdffonts` | verifying the result |

---

## 2. Step 0 — Identify what kind of .exe you have

```bash
file "Book .exe"                          # → PE32 executable ... GUI
7z l "Book .exe"                          # is it a standard SFX archive? (here: NO)
strings -n 8 "Book .exe" | head           # look for telltale words
```

The `strings` step is the key trick — it instantly showed:

```text
Adobe Flash Player          ← embedded Flash (dead tech → must convert)
book-data/book.swf          ← a Flash book loader
files/config.xml            ← configuration
files/search/bookText.xml   ← search index (text of every page!)
```

Also handy: an .exe full of `<?xml` fragments and `<Search><page Index="N">` text means
the pages exist **somewhere as structured data** — great news.

> **Lesson:** before ANY heavy tool, read strings. 30 seconds of `strings` saved hours here.

---

## 3. Step 1 — Find the appended overlay with the PE header

Windows .exe = PE format. A "packer/launcher" type .exe stores hidden files **after the PE sections**.
Parse the section table, find where the real sections end, and everything past that is overlay:

```python
import struct
data = open('book.exe','rb').read()

pe    = struct.unpack('<I', data[0x3c:0x40])[0]     # e_lfanew pointer
nsec  = struct.unpack('<H', data[pe+6:pe+8])[0]     # number of sections
optsz = struct.unpack('<H', data[pe+20:pe+22])[0]   # size of optional header

sec = pe + 24 + optsz
end = 0
for i in range(nsec):
    s = sec + i*40
    rsz, ro = struct.unpack('<II', data[s+16:s+24])  # SizeOfRawData, PointerToRawData
    end = max(end, ro + rsz)

print(hex(end), "file size:", len(data))   # → PE ends at 0xb4a00, overlay = 23 MB
```

In this book the overlay started EXACTLY with the manifest text:
`book.swf=1\r\nbookContent.swf=2\r\n...` — confirmed the container structure.

---

## 4. Step 2 — Parse the manifest

The manifest is plain text: `filerelativepath=index\r\n`, **sorted lexicographically**
(oops — that will matter later!):

```python
start = data.find(b'book.swf=1\r\n')
end   = data.find(b'flash.ocx=631\r\n') + len(b'flash.ocx=631\r\n')
entries = sorted(
    (int(l.rsplit('=',1)[1]), l.rsplit('=',1)[0])
    for l in data[start:end].decode().split('\r\n') if '=' in l
)
# 631 entries: book.swf, bookContent.swf, config, 207 × large pages, search xmls, thumbs…
```

---

## 5. Step 3 — Walk the blobs (the core carving algorithm)

Blobs sit **back-to-back right after the manifest**. Flash `.swf` files start with:

- `FWS` = uncompressed SWF → next uncompressed,
- `CWS` = **zlib-compressed** SWF → and zlib streams are **self-terminating**!

So a compressed SWF's exact length can be found with `zlib.decompressobj()` (check `unused_data`).
)(__A SWF header contains its own declared length — but that is length after decompression;
the stored size is smaller, so we measure the zlib stream instead__):

```python
pos = after_manifest
while pos < len(data):
    if data[pos:pos+3] in (b'FWS', b'CWS'):
        ver  = data[pos+3]                              # must be 1..32
        decl = struct.unpack('<I', data[pos+4:pos+8])[0]
        try:
            d    = zlib.decompressobj()
            body = d.decompress(data[pos+8:])           # decompresses the whole stream
            ok   = (len(body) == decl - 8)              # sanity check
            d2   = zlib.decompressobj()
            d2.decompress(data[pos+8:])
            stored = (len(data)-pos-8) - len(d2.unused_data) + 8  # true stored size!
        except Exception:
            ok = False
        if ok:
            save_blob(pos, stored)                      # → record/emit and advance
            pos += stored
            continue
    pos += 1                                            # not a blob → slide forward
```

Result here: **212 compressed SWFs** recovered byte-perfect.

> **Bug worth remembering:** my first pass extracted **0 files** because I compared
> `data[pos:pos+4] == b'CWS'` — that includes the version byte. Signature = first **3** bytes.

---

## 6. Step 4 — What's inside a page? (structure analysis)

Wi th a minimal SWF tag parser (tag table on the SWF spec) each recovered page showed:

```text
PlaceObject2 ×26, DefineShape3 ×32, DefineText2 ×20, CSMTextSettings ×74,
DefineEditText ×75, JPEG-type tags ×…
```

Translation: **each page = real text + vector shapes + some embedded JPEGs** —
NOT an image of the page. That's why the PDF came out crisp and searchable.

A big asset SWF (`bookContent.swf`, 1.7 MB) holds 227 bitmaps (cover art, UI graphics). Ignore it.

---

## 7. Step 5 — The hard part: fixing the page ORDER

This is the step that ate most of the night. The trap:

- Blobs are **stored in the manifest's order = LEXICOGRAPHIC**:
  `1, 10, 100, 101, …, 11, 110, …, 2, 20, 200, …`
- But the flipbook page N (as the reader displays it) is file `large/N.swf` in **numeric** order.

So blob #3 in file order = page 1, blob #114 = page 2, blob #133 = page 3… (!!)

### How I verified it (this is the learning part)

The reader itself included a gift: `bookText.xml` — a full-text search index:

```xml
<Search><pages Count="207">
 <page Index="1"><![CDATA[Natural Language Processing …]]></page>
 <page Index="207"><![CDATA[DEPENDENCY PARSING-EXAMPLE … 209]]></page>
</pages></Search>
```

Three jiu-jitsu moves chained together:

1. **Footer numbers.** Each page has a printed footer. Statistically
   `printed = Index + 2` for 133/207 pages — a strong candidate rule.
2. **Exact-phrase matching.** Extract the text inside each page SWF with ffdec
   (`-format text:plain -export text`), take distinctive phrases, search which
   `Index` contains them — unique sentence = definitive page.
3. **Model test.** Hypothesis `blob_j = lexpos(page) + 2` (`lexpos` = position of the
   number when you sort 1..207 **as strings**). Predicted pages matched perfectly:
   P=1→cover ✓, 2→"Mansoura University…" ✓, 3→"OUTLINE" ✓, 4→definition ✓, 5→"TAXONOMY 5" ✓,
   207→"DEPENDENCY PARSING… 209" ✓. Re-validated by word-overlap on all 207 (26 "misses"
   were just word-order scrambles of the same text).

```python
lexseq = sorted(range(1, 208), key=str)          # 1, 10, 100, …, 2, 20, …
j_for_page = lexseq.index(P) + 3                 # +3: blob1=book, blob2=content, 1-based
```

> **Lessons:**
> - A book's own search index = ground truth for ordering. Always look for one.
> - Don't trust file numbers as page numbers.
> - Cross-validated rules beat clever one-off heuristics.

---

## 8. Step 6 — Convert: export frames → PDF

All 207 SWFs were single-frame (verified by counting `ShowFrame` tags).

```bash
# one invocation for the whole folder → one PDF per page
java -Xmx3g -Djava.awt.headless=true -jar ffdec.jar \
     -format frame:pdf -select 1 -export frame PDFOUT PAGES
# 44 seconds for 207 pages ✔
```

Quality checks that justified "no quality loss":

```bash
pdftotext PDFOUT/p003.swf/frames.pdf -      # → title text appears  = selectable text ✔
pdffonts PDFOUT/p073.swf/frames.pdf         # → CID TrueType, emb=yes, uni=yes ✔
pdftoppm -png -r 60 ...                     # → visual check vs the reader's look ✔
```

---

## 9. Step 7 — Merge in true page order + bookmarks

```python
from pypdf import PdfWriter, PdfReader
import json

writer = PdfWriter()
titles = json.load(open('meta/xml_pages.json'))  # from the search index

for P in range(1, 208):
    r = PdfReader(f'PDFOUT/p{P:03d}.swf/frames.pdf')
    for pg in r.pages:
        writer.add_page(pg)
    writer.add_outline_item(f"Page {P} — {first_words(titles[P])}",
                            writer.get_num_pages() - 1)

writer.add_metadata({'/Title': 'Natural Language Processing — 4th Grade CS …',
                     '/Author': 'dr. Zeinab Awad'})
writer.write('Downloads/Natural Language Processing.pdf')
```

Final: **207 pages, 12.5 MB, searchable, bookmarked, fonts embedded.**

---

## 10. Cheat-sheet / generalizing to other books

| You see… | Meaning | Do this |
|---|---|---|
| `7z` lists it | self-extracting archive | just extract; find PDFs/text inside |
| strings → "Flash Player", `*.swf` | Flash flipbook | this exact method |
| strings → `bookText.xml`, `<Search>` | searchable pages + ground truth | use for order verification |
| strings → `EBooksWriter`, "ACTIV E-Book" | compiled ebook reader | extract resources; or print-to-PDF from its own reader |
| single giant blob, no sigs | whole-fs compressed | hunt for `78 9c` zlib markers / binwalk |
| pages are pure text SWFs | great | ffdec `frame:pdf` = vector, searchable |
| pages are image SWFs | still fine | ffdec PNG at high DPI → img2pdf |

**General recipe:** identify → find appended/crafted container → manifest → carve
→ find ground truth (search index/config) → fix ordering → render → merge → verify.

---

## 11. Quick verification memory-joggers

- Overlay start = after last PE **section**, not after the PE header.
- SWF signature is **3 bytes** (`FWS`/`CWS`/`ZWS`), 4th byte is version (1–32).
- For CWS, real stored size = length of the zlib stream (use `decompressobj.unused_data`),
  NOT the declared header length.
- `strings | more` first, parser second.
- A page's *printed footer* rarely equals its *Index* (here: footer = Index + 2, mostly).

## 12. If you want to rebuild this exact conversion

Everything lives in: `/tmp/opencode/bookwork/`

```text
book.exe (copy of original), M/ (212 numbered blobs), PAGES/ (ordered p001..p207.swf),
PDFOUT/ (per-page PDFs), meta/ (offsets & xml index), final PDF → Downloads
```
