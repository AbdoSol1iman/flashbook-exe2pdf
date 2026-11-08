#!/usr/bin/env python3
"""
flashbook-exe2pdf : convert "Flash flipbook" style .exe ebooks to real PDFs.

Many old "eBook .exe" files (Flip PDF Professional and similar) are a Windows
launcher with the whole book appended after the PE sections, as:

    [PE image][manifest: name=index lines][blobs: swf/xml/jpg...]

The page files are compressed Flash SWFs (real vector text + images), stored in
LEXICOGRAPHIC order (1, 10, 100, ...) while the reader shows them in NUMERIC
order.  This tool:

  1. locates the appended overlay (PE section table parsing)
  2. reads the manifest and carves out every embedded compressed SWF
  3. finds the reader's own full-text search index (bookText.xml) as ground truth
  4. rebuilds the true page order using the lexicographic-rank model,
     verified against that index (with a best-effort fallback)
  5. renders every page with JPEXS ffdec  (frame -> PDF, vector, fonts embedded)
  6. merges everything into one searchable, bookmarked PDF

Requirements : Python 3.7+ and Java (JRE).  `pip install pypdf`.
                 ffdec.jar is downloaded automatically on first run.

Typical use  :
    python3 flashbook_exe2pdf.py "My Book .exe"
    python3 flashbook_exe2pdf.py "Book.exe" -o Book.pdf --extract-only
"""

import argparse
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
import zlib

try:
    from pypdf import PdfReader, PdfWriter
except ImportError:
    sys.exit("pypdf is required:  pip install pypdf")

SIGS = (b"FWS", b"CWS")
FFDEC_URL = ("https://github.com/jindrapetrik/jpexs-decompiler/releases/"
             "latest/download/ffdec.zip")


def info(msg):    print(f"  [info] {msg}")
def warn(msg):    print(f"  [WARN] {msg}")
def step(n, msg): print(f"\n[{n}/6] {msg}")


# --------------------------------------------------------------------------- #
# 1. PE overlay
# --------------------------------------------------------------------------- #
def pe_overlay_start(data: bytes) -> int:
    """Offset of the first byte after all PE sections (start of the overlay)."""
    if data[:2] != b"MZ":
        raise ValueError("not a Windows PE executable (no MZ header)")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\x00\x00":
        raise ValueError("bad PE signature")
    nsec = struct.unpack_from("<H", data, pe + 6)[0]
    optsz = struct.unpack_from("<H", data, pe + 20)[0]
    s = pe + 24 + optsz
    end = 0
    for i in range(nsec):
        r = s + i * 40
        rsz, ro = struct.unpack_from("<II", data, r + 16)
        end = max(end, ro + rsz)
    return end


# --------------------------------------------------------------------------- #
# 2. manifest + blob carving
# --------------------------------------------------------------------------- #
def parse_manifest(data: bytes, overlay: int):
    """
    Flip-style overlay starts with 'name=1\\r\\nname2=2\\r\\n...' (lexicographic).
    Returns (ordered_file_names, blob_area_start_offset); ([], None) if absent.
    """
    # manifest may sit exactly at the overlay start or after some padding
    m = (re.match(rb"([^\s=\r\n]+)=1\r\n", data[overlay:])
         or re.search(rb"\r\n([^\s=\r\n]+)=1\r\n", data[overlay:]))
    if not m:
        return [], None
    start = overlay + m.start(1)
    entries, i, pos = [], 1, start
    while True:
        lm = re.match(rb"([^\s=\r\n]+)=(\d+)\r\n", data[pos:])
        if not lm or int(lm.group(2)) != i:
            break
        entries.append(lm.group(1).decode())
        pos += lm.end()
        i += 1
    return entries, pos


def carve_swf(data: bytes, pos: int):
    """
    Try to read one SWF at `pos`.
    Returns (content_as_FWS_bytes, stored_size) or (None, 0).
    NOTE: signature is 3 bytes; the 4th byte is the version.
    """
    sig = data[pos:pos + 3]
    if sig not in SIGS:
        return None, 0
    ver = data[pos + 3]
    if not 1 <= ver <= 32:
        return None, 0
    decl = struct.unpack_from("<I", data, pos + 4)[0]
    if decl > len(data) - pos:
        return None, 0
    try:
        d = zlib.decompressobj()
        body = d.decompress(data[pos + 8:])
    except zlib.error:
        return None, 0
    if sig == b"CWS":
        if len(body) != decl - 8:
            return None, 0
        stored = (len(data) - pos - 8) - len(d.unused_data) + 8
        return b"FWS" + data[pos + 3:pos + 8] + body, stored
    # FWS: exact appended length needs a full tag walk; rare here
    return data[pos:pos + decl], decl


def walk_blobs(data: bytes, start: int):
    """Yield (offset, content) for every embedded SWF, in file order."""
    pos = start
    while pos < len(data) - 8:
        content, stored = carve_swf(data, pos)
        if content is not None:
            yield pos, content
            pos += stored
        else:
            pos += 1


# --------------------------------------------------------------------------- #
# 3. ground truth: search index + title
# --------------------------------------------------------------------------- #
def find_search_index(data: bytes) -> dict:
    """Extract {page Index -> page text} from bookText.xml if present."""
    m = re.search(rb"<Search[^>]*>.*</Search>", data, re.S)
    if not m:
        return {}
    txt = m.group(0).decode("utf-8", "replace")
    return {int(i): t for i, t in
            re.findall(r'<page Index="(\d+)"><!\[CDATA\[(.*?)\]\]></page>',
                       txt, re.S)}


def find_exe_title(data: bytes) -> str:
    m = re.search(rb"<ExeCaption><!\[CDATA\[(.*?)\]\]>", data, re.S)
    return m.group(1).decode("utf-8", "replace") if m else "Converted eBook"


# --------------------------------------------------------------------------- #
# ordering helpers
# --------------------------------------------------------------------------- #
_NORM = re.compile(r"[^a-z0-9 ]+")


def norm_tokens(text: str):
    text = text.lower().replace("--- recordseparator ---", " ")
    return set(_NORM.sub(" ", text).split())


def lex_order(npages: int):
    """pages 1..N sorted as strings: 1, 10, 100, ..., 11, 110, ..., 2, ..."""
    return sorted(range(1, npages + 1), key=str)


def order_by_lex_model(blobs, pages: dict):
    """
    Model of this packer family:
        blob#1 = book.swf (reader shell)   blob#2 = bookContent.swf (assets)
        1-based blob #(lexpos1(P)+2) = page P  (lexpos1 = rank of P in lex list)
    """
    lex = lex_order(len(pages))
    needed = max(lex.index(P) for P in pages) + 3   # 1-based blob count required
    if len(blobs) < needed:
        warn(f"lex model needs {needed} blobs, found {len(blobs)} - "
             "falling back to text similarity")
        return order_by_similarity(blobs, pages)

    checked = score = 0
    for P, text in pages.items():
        want = norm_tokens(text)
        if len(want) < 5:
            continue
        blob_text = norm_tokens(blobs[lex.index(P) + 2].text or "")
        if not blob_text:
            continue
        checked += 1
        if want & blob_text:
            score += 1
    if checked and score / checked >= 0.85:
        info(f"lexicographic model verified on {score}/{checked} pages "
             f"({100.0 * score / checked:.0f}%)")
        return [blobs[lex.index(P) + 2].content for P in sorted(pages)]
    if checked:
        warn(f"lex model weak ({score}/{checked}) - using text similarity")
    return order_by_similarity(blobs, pages)


def order_by_similarity(blobs, pages: dict):
    """Best-effort: greedy greedy assignment of blobs to pages by word overlap."""
    ppt = {P: norm_tokens(t) for P, t in sorted(pages.items())}
    bpt = {i: norm_tokens(b.text or "") for i, b in enumerate(blobs)}
    pairs = []
    for i, bt in bpt.items():
        if not bt:
            continue
        for P, xt in ppt.items():
            k = len(bt & xt)
            if k:
                pairs.append((k, i, P))
    pairs.sort(reverse=True)
    used_b, used_p, match = set(), set(), {}
    for k, i, P in pairs:
        if i in used_b or P in used_p:
            continue
        used_b.add(i); used_p.add(P); match[P] = i
    missing = [P for P in sorted(pages) if P not in match]
    if missing:
        warn(f"{len(missing)} pages unmatched by text; appended by index order")
    out = [blobs[match[P]].content for P in sorted(match)]
    for P in missing:
        guess = int(P) - 1
        if guess < len(blobs) and guess not in used_b:
            out.append(blobs[guess].content)
    return out or [b.content for b in blobs[:len(pages)]]


def order_without_index(blobs, n_pages_hint=0):
    """No search index: assume blobs after the 2 shell SWFs are pages 1..N."""
    warn("no search index embedded - assuming storage order == page order")
    out = blobs[3:] if len(blobs) > 3 else list(blobs)
    if n_pages_hint:
        out = out[:n_pages_hint]
    return [b.content for b in out]


# --------------------------------------------------------------------------- #
# ffdec
# --------------------------------------------------------------------------- #
def locate_ffdec(explicit: str, workdir: str) -> str:
    if explicit and os.path.isfile(explicit):
        return explicit
    cache = os.path.join(os.path.expanduser("~"), ".cache", "flashbook-exe2pdf")
    os.makedirs(cache, exist_ok=True)
    jar = os.path.join(cache, "ffdec.jar")
    if os.path.isfile(jar):
        return jar
    if shutil.which("java") is None:
        sys.exit("Java not found - install a JRE (e.g. apt install default-jre)")
    info("downloading JPEXS ffdec (once, ~22 MB)...")
    tmp = jar + ".zip"
    urllib.request.urlretrieve(FFDEC_URL, tmp)
    subprocess.run(["unzip", "-o", tmp, "-d", cache], check=True,
                   stdout=subprocess.DEVNULL)
    os.remove(tmp)
    if not os.path.isfile(jar):
        sys.exit("ffdec.jar not found after download")
    info(f"saved to {jar}")
    return jar


def ffdec_run(cmd, timeout=900):
    r = subprocess.run(["java", "-Xmx3g", "-Djava.awt.headless=true",
                        "-jar"] + cmd,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       timeout=timeout)
    if r.returncode != 0:
        warn("ffdec exit %s: %s" % (r.returncode, r.stdout[-400:].decode(
            "utf-8", "replace")))
    return r


def ffdec_texts(jar, blobdir: str, outdir: str) -> dict:
    """Dump plain text of every blob (directory input -> per-file subdirs)."""
    ffdec_run([jar, "-format", "text:plain", "-export", "text", outdir,
               blobdir], timeout=1800)
    texts = {}
    for root, _, fs in os.walk(outdir):
        base = os.path.basename(root)
        key = base[:-4] if base.lower().endswith(".swf") else base
        for f in fs:
            if f.endswith(".txt"):
                try:
                    texts[key] = texts.get(key, "") + open(
                        os.path.join(root, f), encoding="utf-8",
                        errors="replace").read()
                except OSError:
                    pass
    return texts


def ffdec_render(jar, src_swf_dir: str, outdir: str) -> list:
    """Render every (single-frame) SWF in src_swf_dir to one PDF per page.
       Directory input makes ffdec write out/srcName.swf/frames.pdf, so the
       mapping back to page number stays trivial."""
    shutil.rmtree(outdir, ignore_errors=True)
    os.makedirs(outdir, exist_ok=True)
    samples = sorted(f for f in os.listdir(src_swf_dir) if f.endswith(".swf"))
    ffdec_run([jar, "-format", "frame:pdf", "-select", "1",
               "-export", "frame", outdir, src_swf_dir], timeout=3600)
    pdfs = []
    for name in samples:
        key, _ = os.path.splitext(name)
        p = os.path.join(outdir, name, "frames.pdf")
        if not os.path.isfile(p):
            sys.exit(f"rendering failed for {key}")
        pdfs.append(p)
    print(f"  rendered {len(pdfs)} pages")
    return pdfs


# --------------------------------------------------------------------------- #
# merge
# --------------------------------------------------------------------------- #
def first_words(t, n=8, maxlen=80):
    w = re.sub(r"\s+", " ", t).strip().split()
    if w and w[-1].isdigit():
        w = w[:-1]
    return " ".join(w[:n])[:maxlen]


def merge(pdf_files, page_texts, title, out_path):
    writer = PdfWriter()
    for p in pdf_files:
        for pg in PdfReader(p).pages:
            writer.add_page(pg)
    for k, t in enumerate(page_texts or []):
        try:
            writer.add_outline_item(f"Page {k + 1} - {first_words(t)}",
                                    writer.get_num_pages() - 1)
        except Exception:
            pass
    writer.add_metadata({"/Title": title, "/Creator": "flashbook-exe2pdf"})
    with open(out_path, "wb") as f:
        writer.write(f)


# --------------------------------------------------------------------------- #
class Blob:
    def __init__(self, offset, content, text=None):
        self.offset, self.content, self.text = offset, content, text


# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Convert a Flash-style eBook .exe into a real PDF.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("exe", help="the .exe book file")
    ap.add_argument("-o", "--out", help="output PDF path (default: same name "
                    "as the exe, with .pdf)")
    ap.add_argument("--workdir", help="scratch dir (default: fresh temp dir)")
    ap.add_argument("--extract-only", action="store_true",
                    help="carve blobs + dump index, then stop (no rendering)")
    ap.add_argument("--ffdec", help="path to ffdec.jar (else auto-download)")
    args = ap.parse_args(argv)

    exe = os.path.abspath(args.exe)
    if not os.path.isfile(exe):
        sys.exit(f"no such file: {exe}")
    out = args.out or os.path.splitext(exe)[0] + ".pdf"
    workdir = args.workdir or tempfile.mkdtemp(prefix="flashbook_")

    print(f"== flashbook-exe2pdf")
    print(f"   input:   {exe}")
    print(f"   output:  {out}")
    if not args.workdir:
        print(f"   scratch: {workdir}")

    data = open(exe, "rb").read()

    step(1, "locating the appended overlay")
    try:
        overlay = pe_overlay_start(data)
    except ValueError as e:
        sys.exit(str(e))
    print(f"  PE image ends at {overlay:#x}; appended data: "
          f"{len(data) - overlay:,} bytes")

    step(2, "parsing manifest and carving embedded SWFs")
    manifest, blob_start = parse_manifest(data, overlay)
    if not blob_start:
        sys.exit("no Flip-style manifest found - this tool targets Flash "
                 "flipbook containers (Flip PDF Professional et al.)")
    print(f"  manifest entries: {len(manifest)}")
    blobs = [Blob(off, content) for off, content in walk_blobs(data, blob_start)]
    print(f"  embedded compressed SWFs: {len(blobs)}")
    if not blobs:
        sys.exit("no Flash blobs found after the manifest")
    blobdir = os.path.join(workdir, "blobs")
    os.makedirs(blobdir, exist_ok=True)
    for i, b in enumerate(blobs, 1):
        open(os.path.join(blobdir, f"blob{i:03d}.swf"), "wb").write(b.content)
    if args.extract_only:
        json.dump({str(i): b.offset for i, b in enumerate(blobs, 1)},
                  open(os.path.join(workdir, "offsets.json"), "w"))
        print(f"\n[done] {len(blobs)} blobs saved in {blobdir} "
              f"(file order = storage order)")
        return

    step(3, "searching for the reader's text index (ground truth)")
    pages = find_search_index(data)
    title = find_exe_title(data).strip() or "Converted eBook"
    if pages:
        print(f"  found: {len(pages)} pages of text, title: {title!r}")
    else:
        info("no <Search> index embedded")

    step(4, "rebuilding the true page order")
    if pages:
        jar = locate_ffdec(args.ffdec, workdir)
        info("extracting blob text with ffdec (for verification)...")
        texts = ffdec_texts(jar, blobdir, os.path.join(workdir, "texts"))
        for i, b in enumerate(blobs, 1):
            b.text = texts.get(f"blob{i:03d}", "")
        ordered = order_by_lex_model(blobs, pages)
        page_texts = [pages[P] for P in sorted(pages)]
    else:
        ordered = order_without_index(blobs)
        page_texts = [""] * len(ordered)

    step(5, "rendering pages to PDF (JPEXS ffdec)")
    pagetmp = os.path.join(workdir, "pagetmp")
    os.makedirs(pagetmp, exist_ok=True)
    swfs = []
    for k, content in enumerate(ordered, 1):
        p = os.path.join(pagetmp, f"pg{k:03d}.swf")
        open(p, "wb").write(content)
        swfs.append(p)
    pdf_files = ffdec_render(locate_ffdec(args.ffdec, workdir), pagetmp,
                             os.path.join(workdir, "parts"))

    step(6, "merging into the final PDF")
    merge(pdf_files, page_texts, title, out)
    n = len(PdfReader(out).pages)
    print(f"\n[OK] {out}  -  {n} pages, {os.path.getsize(out) // 1024} KB")


if __name__ == "__main__":
    main()
