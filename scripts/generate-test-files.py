#!/usr/bin/env python3
"""Generate realistic test files for stress testing the V1FS malware scanner.

Produces files with valid format headers and structures that exercise
all scanner analysis paths, unlike random binary data which is trivially
dismissed. File sizes vary to match real-world distributions.

Usage:
    python3 generate-test-files.py <output_dir> <count> [--eicar <count>]
"""

import io
import os
import random
import string
import struct
import sys
import time
import zipfile


def random_str(length=20):
    return "".join(random.choices(string.ascii_letters + string.digits, k=length))


def random_text(min_words=50, max_words=500):
    words = [
        "the", "be", "to", "of", "and", "a", "in", "that", "have", "it",
        "for", "not", "on", "with", "he", "as", "you", "do", "at", "this",
        "but", "his", "by", "from", "they", "we", "say", "her", "she", "or",
        "an", "will", "my", "one", "all", "would", "there", "their", "what",
        "so", "up", "out", "if", "about", "who", "get", "which", "go", "me",
        "when", "make", "can", "like", "time", "no", "just", "him", "know",
        "take", "people", "into", "year", "your", "good", "some", "could",
        "them", "see", "other", "than", "then", "now", "look", "only", "come",
        "its", "over", "think", "also", "back", "after", "use", "two", "how",
        "our", "work", "first", "well", "way", "even", "new", "want", "because",
        "any", "these", "give", "day", "most", "us", "report", "analysis",
        "system", "data", "file", "process", "security", "network", "server",
        "client", "application", "service", "function", "module", "class",
        "method", "variable", "parameter", "return", "value", "error", "status",
    ]
    count = random.randint(min_words, max_words)
    sentences = []
    i = 0
    while i < count:
        slen = random.randint(5, 15)
        sentence = " ".join(random.choices(words, k=slen))
        sentence = sentence[0].upper() + sentence[1:] + "."
        sentences.append(sentence)
        i += slen
    return " ".join(sentences)


# --- File size distributions (bytes) ---

SIZES = {
    "tiny": (100, 2_000),           # scripts, configs
    "small": (2_000, 20_000),       # small scripts, text files
    "medium": (20_000, 100_000),    # typical documents, small executables
    "large": (100_000, 500_000),    # larger documents, images
    "xlarge": (500_000, 2_000_000), # big executables, archives
}

# Weighted toward small/medium to match real-world distribution
# Average file ~50-80 KB, total ~2-4 GB for 50K files
SIZE_WEIGHTS = {
    "tiny": 15,
    "small": 30,
    "medium": 35,
    "large": 15,
    "xlarge": 5,
}


def pick_size():
    category = random.choices(
        list(SIZE_WEIGHTS.keys()),
        weights=list(SIZE_WEIGHTS.values()),
        k=1,
    )[0]
    return random.randint(*SIZES[category])


# --- Valid PE executable ---

def generate_pe(target_size):
    """Generate a minimal valid PE file."""
    # DOS header
    dos_header = bytearray(64)
    dos_header[0:2] = b"MZ"  # e_magic
    struct.pack_into("<I", dos_header, 60, 64)  # e_lfanew -> PE header at offset 64

    # PE signature
    pe_sig = b"PE\x00\x00"

    # COFF header (20 bytes)
    coff = bytearray(20)
    struct.pack_into("<H", coff, 0, 0x14C)   # Machine: i386
    struct.pack_into("<H", coff, 2, 1)        # NumberOfSections
    struct.pack_into("<I", coff, 4, int(time.time()))  # TimeDateStamp
    struct.pack_into("<H", coff, 16, 224)     # SizeOfOptionalHeader
    struct.pack_into("<H", coff, 18, 0x0102)  # Characteristics: EXECUTABLE_IMAGE

    # Optional header (PE32, 224 bytes)
    opt = bytearray(224)
    struct.pack_into("<H", opt, 0, 0x10B)    # Magic: PE32
    opt[2] = 14                               # MajorLinkerVersion
    struct.pack_into("<I", opt, 16, 0x1000)  # AddressOfEntryPoint
    struct.pack_into("<I", opt, 28, 0x400000) # ImageBase
    struct.pack_into("<I", opt, 32, 0x1000)  # SectionAlignment
    struct.pack_into("<I", opt, 36, 0x200)   # FileAlignment
    struct.pack_into("<H", opt, 40, 6)       # MajorOSVersion
    struct.pack_into("<H", opt, 44, 6)       # MajorSubsystemVersion
    struct.pack_into("<I", opt, 56, 0x10000) # SizeOfImage
    struct.pack_into("<I", opt, 60, 0x200)   # SizeOfHeaders
    struct.pack_into("<H", opt, 68, 3)       # Subsystem: CONSOLE
    struct.pack_into("<I", opt, 76, 0x100000) # SizeOfStackReserve
    struct.pack_into("<I", opt, 80, 0x1000)  # SizeOfStackCommit
    struct.pack_into("<I", opt, 84, 0x100000) # SizeOfHeapReserve
    struct.pack_into("<I", opt, 88, 0x1000)  # SizeOfHeapCommit
    struct.pack_into("<I", opt, 92, 16)      # NumberOfRvaAndSizes

    # Section header (.text, 40 bytes)
    section = bytearray(40)
    section[0:6] = b".text\x00"
    data_size = max(target_size - 512, 512)
    struct.pack_into("<I", section, 8, data_size)   # VirtualSize
    struct.pack_into("<I", section, 12, 0x1000)     # VirtualAddress
    struct.pack_into("<I", section, 16, data_size)  # SizeOfRawData
    struct.pack_into("<I", section, 20, 0x200)      # PointerToRawData
    struct.pack_into("<I", section, 36, 0x60000020) # Characteristics: CODE|EXECUTE|READ

    header = dos_header + pe_sig + bytes(coff) + bytes(opt) + bytes(section)
    padding = b"\x00" * (0x200 - len(header))
    section_data = os.urandom(data_size)

    return bytes(header) + padding + section_data


# --- Valid PDF ---

def generate_pdf(target_size):
    """Generate a valid PDF document."""
    text = random_text(100, 2000)
    # Pad to reach target size
    while len(text) < target_size - 500:
        text += "\n" + random_text(50, 200)
    text = text[:max(target_size - 500, 100)]

    lines = text.split(". ")
    text_lines = "\n".join(f"({line.strip()}.) Tj T*" for line in lines if line.strip())

    content = f"""1 0 0 1 50 750 Tm
/F1 12 Tf
{text_lines}"""

    stream = content.encode()
    pdf = f"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj

2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj

3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]
   /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>
endobj

4 0 obj
<< /Length {len(stream)} >>
stream
{content}
endstream
endobj

5 0 obj
<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>
endobj

xref
0 6
0000000000 65535 f
0000000009 00000 n
0000000058 00000 n
0000000115 00000 n
0000000266 00000 n
trailer
<< /Size 6 /Root 1 0 R >>
startxref
0
%%EOF"""
    data = pdf.encode()
    if len(data) < target_size:
        # Pad with a PDF comment block
        data += b"\n%" + os.urandom(target_size - len(data) - 2)
    return data[:target_size]


# --- Valid Office document (DOCX) ---

def generate_docx(target_size):
    """Generate a valid DOCX file (ZIP with XML content)."""
    text = random_text(50, 1000)
    while len(text) < target_size // 2:
        text += "\n" + random_text(50, 300)

    paragraphs = text.split(". ")
    body_xml = "\n".join(
        f'<w:p><w:r><w:t>{p.strip()}.</w:t></w:r></w:p>'
        for p in paragraphs if p.strip()
    )

    document_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:body>
{body_xml}
</w:body>
</w:document>"""

    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""

    rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", document_xml)
        # Add padding file to reach target size
        pad_size = max(target_size - buf.tell() - 200, 0)
        if pad_size > 0:
            zf.writestr("word/media/image1.bin", os.urandom(pad_size))
    return buf.getvalue()


# --- Valid XLSX ---

def generate_xlsx(target_size):
    """Generate a valid XLSX file (ZIP with XML content)."""
    rows = []
    num_rows = max(target_size // 100, 10)
    for i in range(1, min(num_rows, 5000) + 1):
        vals = [str(random.randint(1, 99999)) for _ in range(5)]
        cells = "".join(
            f'<c r="{chr(65+j)}{i}" t="n"><v>{v}</v></c>'
            for j, v in enumerate(vals)
        )
        rows.append(f"<row r=\"{i}\">{cells}</row>")

    sheet_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<sheetData>
{"".join(rows)}
</sheetData>
</worksheet>"""

    workbook_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""

    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>"""

    rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

    wb_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        pad_size = max(target_size - buf.tell() - 200, 0)
        if pad_size > 0:
            zf.writestr("xl/media/chart1.bin", os.urandom(pad_size))
    return buf.getvalue()


# --- Valid PNG image ---

def generate_png(target_size):
    """Generate a valid PNG image with random pixel data."""
    import zlib

    # Keep dimensions small for speed, pad with ancillary chunks
    width = min(int((target_size // 4) ** 0.5), 256)
    height = max(width, 1)

    sig = b"\x89PNG\r\n\x1a\n"

    def chunk(ctype, data):
        c = ctype + data
        crc = struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
        return struct.pack(">I", len(data)) + c + crc

    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    ihdr = chunk(b"IHDR", ihdr_data)

    raw = b"\x00" * (width * 3 + 1) * height  # all-zero pixels (fast, compresses well)
    compressed = zlib.compress(raw, 1)
    idat = chunk(b"IDAT", compressed)

    # Pad to target size with tEXt chunks (valid PNG ancillary chunks)
    padding = b""
    current = len(sig) + len(ihdr) + len(idat) + 12  # 12 for IEND
    while current + len(padding) < target_size:
        text_data = b"Comment\x00" + os.urandom(min(target_size - current - len(padding) - 12, 8192))
        padding += chunk(b"tEXt", text_data)

    iend = chunk(b"IEND", b"")
    return sig + ihdr + idat + padding + iend


# --- Valid JPEG image ---

def generate_jpeg(target_size):
    """Generate a valid JPEG file (minimal JFIF with random data)."""
    # JPEG SOI + JFIF APP0 header
    header = bytes([
        0xFF, 0xD8,                     # SOI
        0xFF, 0xE0,                     # APP0
        0x00, 0x10,                     # Length
        0x4A, 0x46, 0x49, 0x46, 0x00,   # "JFIF\0"
        0x01, 0x01,                     # Version 1.1
        0x00,                           # Aspect ratio units
        0x00, 0x01, 0x00, 0x01,         # 1x1 aspect ratio
        0x00, 0x00,                     # No thumbnail
    ])
    # Add a comment marker with random data to reach target size
    comment_data = os.urandom(max(target_size - len(header) - 4, 10))
    # Split into chunks of max 65533 bytes (JPEG segment limit)
    chunks = []
    for i in range(0, len(comment_data), 65533):
        seg = comment_data[i:i+65533]
        seg_len = len(seg) + 2
        chunks.append(bytes([0xFF, 0xFE]) + struct.pack(">H", seg_len) + seg)
    trailer = bytes([0xFF, 0xD9])  # EOI
    return header + b"".join(chunks) + trailer


# --- Script files ---

def generate_powershell(target_size):
    """Generate a realistic PowerShell script."""
    functions = [
        'function Get-{name} {{\n    param([string]$Path)\n    $items = Get-ChildItem -Path $Path -Recurse\n    foreach ($item in $items) {{\n        Write-Host "Processing: $($item.FullName)"\n        $hash = Get-FileHash -Path $item.FullName -Algorithm SHA256\n        [PSCustomObject]@{{\n            Name = $item.Name\n            Size = $item.Length\n            Hash = $hash.Hash\n        }}\n    }}\n}}',
        'function Set-{name} {{\n    param(\n        [Parameter(Mandatory=$true)]\n        [string]$ComputerName,\n        [int]$Port = 443\n    )\n    try {{\n        $connection = Test-NetConnection -ComputerName $ComputerName -Port $Port\n        if ($connection.TcpTestSucceeded) {{\n            Write-Host "Connection to $ComputerName`:$Port succeeded"\n        }} else {{\n            Write-Warning "Connection to $ComputerName`:$Port failed"\n        }}\n    }} catch {{\n        Write-Error "Error: $_"\n    }}\n}}',
        'function Invoke-{name} {{\n    $results = @()\n    $services = Get-Service | Where-Object {{ $_.Status -eq "Running" }}\n    foreach ($svc in $services) {{\n        $results += [PSCustomObject]@{{\n            ServiceName = $svc.ServiceName\n            DisplayName = $svc.DisplayName\n            Status = $svc.Status\n        }}\n    }}\n    $results | Export-Csv -Path "$env:TEMP\\services.csv" -NoTypeInformation\n    return $results\n}}',
    ]
    script = "# Auto-generated PowerShell script\n# Generated: {}\n\n".format(time.strftime("%Y-%m-%d"))
    while len(script) < target_size:
        func = random.choice(functions).format(name=random_str(8))
        script += func + "\n\n"
    return script[:target_size].encode()


def generate_batch(target_size):
    """Generate a realistic Windows batch file."""
    commands = [
        "@echo off",
        "setlocal enabledelayedexpansion",
        "set LOGFILE=%TEMP%\\process_{name}.log",
        "echo [%date% %time%] Starting process >> %LOGFILE%",
        "for /f \"tokens=*\" %%a in ('dir /b /s \"%ProgramFiles%\\*.dll\"') do (",
        "    echo Processing: %%a >> %LOGFILE%",
        ")",
        "if exist \"%SystemRoot%\\System32\\config\" (",
        "    echo System config directory found >> %LOGFILE%",
        ") else (",
        "    echo WARNING: System config not found >> %LOGFILE%",
        ")",
        "net user > %TEMP%\\users_{name}.txt 2>&1",
        "ipconfig /all > %TEMP%\\network_{name}.txt 2>&1",
        "tasklist /v > %TEMP%\\tasks_{name}.txt 2>&1",
        "echo [%date% %time%] Process complete >> %LOGFILE%",
        "endlocal",
    ]
    script = ""
    while len(script) < target_size:
        for cmd in commands:
            script += cmd.format(name=random_str(6)) + "\r\n"
        script += "\r\nREM --- Section {} ---\r\n".format(random_str(4))
    return script[:target_size].encode()


def generate_vbs(target_size):
    """Generate a realistic VBScript file."""
    blocks = [
        "Dim fso, ws, shell\nSet fso = CreateObject(\"Scripting.FileSystemObject\")\nSet ws = CreateObject(\"WScript.Shell\")\n",
        "Function GetSystemInfo_{name}()\n    Dim info\n    info = ws.ExpandEnvironmentStrings(\"%COMPUTERNAME%\")\n    GetSystemInfo_{name} = info\nEnd Function\n",
        "Sub ProcessFiles_{name}(folderPath)\n    Dim folder, file\n    Set folder = fso.GetFolder(folderPath)\n    For Each file In folder.Files\n        WScript.Echo \"Found: \" & file.Name & \" (\" & file.Size & \" bytes)\"\n    Next\nEnd Sub\n",
        "Sub WriteLog_{name}(message)\n    Dim logFile\n    Set logFile = fso.OpenTextFile(\"C:\\temp\\log_{name}.txt\", 8, True)\n    logFile.WriteLine Now & \" - \" & message\n    logFile.Close\nEnd Sub\n",
    ]
    script = "' VBScript - Generated {}\nOption Explicit\n\n".format(time.strftime("%Y-%m-%d"))
    while len(script) < target_size:
        block = random.choice(blocks).format(name=random_str(6))
        script += block + "\n"
    return script[:target_size].encode()


def generate_javascript(target_size):
    """Generate a realistic JavaScript file."""
    blocks = [
        "const {name} = async (url, options = {{}}) => {{\n    const response = await fetch(url, {{\n        method: options.method || 'GET',\n        headers: {{ 'Content-Type': 'application/json', ...options.headers }},\n        body: options.body ? JSON.stringify(options.body) : undefined\n    }});\n    if (!response.ok) throw new Error(`HTTP ${{response.status}}`);\n    return response.json();\n}};\n",
        "class {name} {{\n    constructor(config) {{\n        this.config = config;\n        this.cache = new Map();\n        this.initialized = false;\n    }}\n    async initialize() {{\n        if (this.initialized) return;\n        console.log('Initializing {name}...');\n        this.initialized = true;\n    }}\n    process(data) {{\n        return data.map(item => ({{\n            ...item,\n            processed: true,\n            timestamp: Date.now()\n        }}));\n    }}\n}}\n",
        "function validate{name}(input) {{\n    const errors = [];\n    if (!input || typeof input !== 'object') errors.push('Invalid input');\n    if (!input.id || typeof input.id !== 'string') errors.push('Missing id');\n    if (!input.data || !Array.isArray(input.data)) errors.push('Missing data array');\n    if (errors.length > 0) throw new Error(errors.join('; '));\n    return true;\n}}\n",
    ]
    script = "// Generated JavaScript module\n'use strict';\n\n"
    while len(script) < target_size:
        block = random.choice(blocks).format(name=random_str(8))
        script += block + "\n"
    return script[:target_size].encode()


def generate_html(target_size):
    """Generate a valid HTML document with inline JavaScript."""
    text = random_text(200, 2000)
    paragraphs = "\n".join(f"<p>{s.strip()}.</p>" for s in text.split(". ") if s.strip())
    js = """
    <script>
    document.addEventListener('DOMContentLoaded', function() {
        const elements = document.querySelectorAll('p');
        elements.forEach((el, i) => {
            el.dataset.index = i;
            el.addEventListener('click', function() {
                console.log('Clicked paragraph ' + this.dataset.index);
            });
        });
        fetch('/api/status').then(r => r.json()).then(data => {
            console.log('Status:', data);
        }).catch(e => console.error(e));
    });
    </script>"""
    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>{random_str(10)}</title></head>
<body>
<h1>{random_str(15)}</h1>
{paragraphs}
{js}
</body>
</html>"""
    while len(html) < target_size:
        html += f"\n<!-- {random_text(50, 200)} -->"
    return html[:target_size].encode()


def generate_zip_archive(target_size):
    """Generate a valid ZIP archive containing multiple files."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        num_files = random.randint(3, 15)
        size_per = max(target_size // num_files, 100)
        for i in range(num_files):
            ext = random.choice(["txt", "dat", "log", "csv", "xml"])
            content = random_text(20, 500).encode()
            if len(content) < size_per:
                content += os.urandom(size_per - len(content))
            zf.writestr(f"{random_str(10)}.{ext}", content)
    return buf.getvalue()


def generate_xml(target_size):
    """Generate a valid XML document."""
    records = []
    while len("\n".join(records)) < target_size - 200:
        records.append(
            f'  <record id="{random_str(8)}" timestamp="{time.strftime("%Y-%m-%dT%H:%M:%S")}">'
            f"\n    <name>{random_str(12)}</name>"
            f"\n    <value>{random.randint(1, 99999)}</value>"
            f"\n    <status>{random.choice(['active', 'pending', 'completed', 'error'])}</status>"
            f"\n    <description>{random_text(5, 20)}</description>"
            f"\n  </record>"
        )
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<data>\n' + "\n".join(records) + "\n</data>"
    return xml[:target_size].encode()


# --- Generator dispatch ---

GENERATORS = {
    "exe": (generate_pe, 15),
    "dll": (generate_pe, 5),
    "pdf": (generate_pdf, 15),
    "docx": (generate_docx, 10),
    "xlsx": (generate_xlsx, 5),
    "png": (generate_png, 5),
    "jpg": (generate_jpeg, 5),
    "ps1": (generate_powershell, 8),
    "bat": (generate_batch, 4),
    "vbs": (generate_vbs, 4),
    "js": (generate_javascript, 8),
    "html": (generate_html, 6),
    "zip": (generate_zip_archive, 5),
    "xml": (generate_xml, 5),
}

EICAR = bytes.fromhex(
    "58354f2150254041505b345c505a58353428505e293743432937"
    "7d2445494341522d5354414e444152442d414e544956495255"
    "532d544553542d46494c452124482b482a"
)


def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <output_dir> <count> [--eicar <count>]")
        sys.exit(1)

    outdir = sys.argv[1]
    total = int(sys.argv[2])
    eicar_count = 0
    if "--eicar" in sys.argv:
        eicar_count = int(sys.argv[sys.argv.index("--eicar") + 1])

    clean_count = total - eicar_count
    os.makedirs(outdir, exist_ok=True)

    extensions = list(GENERATORS.keys())
    weights = [GENERATORS[e][1] for e in extensions]

    start = time.time()
    total_bytes = 0

    # Generate clean files with valid formats
    for i in range(1, clean_count + 1):
        ext = random.choices(extensions, weights=weights, k=1)[0]
        generator_fn = GENERATORS[ext][0]
        target_size = pick_size()
        try:
            data = generator_fn(target_size)
        except Exception:
            data = os.urandom(target_size)  # fallback
        suffix = random_str(6)
        path = os.path.join(outdir, f"test-{i:05d}-{suffix}.{ext}")
        with open(path, "wb") as f:
            f.write(data)
        total_bytes += len(data)
        if i % 5000 == 0:
            elapsed = time.time() - start
            print(f"  {i}/{clean_count} files ({total_bytes/1048576:.0f} MB, {elapsed:.0f}s)")

    # Generate EICAR files
    for i in range(1, eicar_count + 1):
        suffix = random_str(6)
        path = os.path.join(outdir, f"eicar-{i:05d}-{suffix}.exe")
        with open(path, "wb") as f:
            f.write(EICAR)
        total_bytes += len(EICAR)

    elapsed = time.time() - start
    print(f"Generated {clean_count + eicar_count} files ({total_bytes/1048576:.1f} MB) in {elapsed:.0f}s")
    print(f"  Clean: {clean_count} (valid format files)")
    print(f"  EICAR: {eicar_count}")


if __name__ == "__main__":
    main()
