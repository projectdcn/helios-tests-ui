"""Parsers for SONiC CLI tables, sonic-db-cli output, rccl-tests and TransferBench."""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

_SEP_RE = re.compile(r"^\s*-+(\s+-+)+\s*$")
_RULE_RE = re.compile(r"^\s*-{3,}\s*$")


def parse_table(text: str) -> list[dict[str, str]]:
    """Parse a tabulate-style table (header line, dashed separator, rows).

    Column boundaries come from the dashed separator, so cells containing
    spaces ("0.00 B/s", "PSU 1") and blank cells parse correctly. Preamble
    text before the header and trailers ("----", "Total entries ...") are ignored.
    """
    lines = text.splitlines()
    sep_idx = next((i for i, ln in enumerate(lines) if i > 0 and _SEP_RE.match(ln)), None)
    if sep_idx is None:
        return []
    starts = [m.start() for m in re.finditer(r"-+", lines[sep_idx])]
    bounds = list(zip(starts, starts[1:] + [None]))

    def cells(line: str) -> list[str]:
        return [line[s:e].strip() if e is not None else line[s:].strip() for s, e in bounds]

    headers = cells(lines[sep_idx - 1])
    rows = []
    for line in lines[sep_idx + 1:]:
        if not line.strip() or _RULE_RE.match(line) or line.lstrip().startswith("Total entries"):
            break
        rows.append(dict(zip(headers, cells(line))))
    return rows


def parse_kv(text: str, sep: str = ":") -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if sep in line:
            k, v = line.split(sep, 1)
            out[k.strip()] = v.strip()
    return out


def parse_db_hash(text: str) -> dict[str, str]:
    """sonic-db-cli HGETALL prints a Python dict repr; redis-cli prints alternating lines."""
    s = text.strip()
    if not s:
        return {}
    if s.startswith("{"):
        val = ast.literal_eval(s)
        return {str(k): str(v) for k, v in val.items()}
    lines = s.splitlines()
    return {lines[i]: lines[i + 1] for i in range(0, len(lines) - 1, 2)}


def to_float(value) -> float | None:
    if value is None:
        return None
    s = str(value).strip().replace(",", "").rstrip("%").strip()
    if not s or s.upper() in ("N/A", "NA", "NONE", "-"):
        return None
    m = re.match(r"^-?\d+(\.\d+)?", s)
    return float(m.group(0)) if m else None


def parse_speed_gbps(value: str) -> float | None:
    """'400G' -> 400; '400000' (Mb/s, CONFIG_DB style) -> 400; 'N/A' -> None."""
    s = str(value).strip().upper()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([GM]?)", s)
    if not m or not m.group(1):
        return None
    n = float(m.group(1))
    if m.group(2) == "G":
        return n
    if m.group(2) == "M" or n >= 1000:
        return n / 1000
    return n


_SIZE_UNITS = {"": 1, "B": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}


def parse_size(value: str | int) -> int:
    """'1K' -> 1024, '1G' -> 1073741824 (rccl-tests / TransferBench conventions)."""
    if isinstance(value, int):
        return value
    m = re.fullmatch(r"\s*(\d+)\s*([KMGT]?)B?\s*", str(value).upper())
    if not m:
        raise ValueError(f"bad size {value!r}")
    return int(m.group(1)) * _SIZE_UNITS[m.group(2)]


# ---------------------------------------------------------------- rccl-tests

@dataclass
class RcclRow:
    size: int
    count: int
    dtype: str
    redop: str
    root: int
    oop_time_us: float | None
    oop_algbw: float | None
    oop_busbw: float | None
    oop_wrong: int | None
    ip_time_us: float | None
    ip_algbw: float | None
    ip_busbw: float | None
    ip_wrong: int | None


@dataclass
class RcclResult:
    rows: list[RcclRow] = field(default_factory=list)
    avg_busbw: float | None = None
    out_of_bounds: int | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def max_size(self) -> int:
        return max((r.size for r in self.rows), default=0)

    @property
    def wrong_rows(self) -> list[RcclRow]:
        return [r for r in self.rows if (r.oop_wrong or 0) > 0 or (r.ip_wrong or 0) > 0]


_RCCL_ERR_RE = re.compile(r"(?i)(test failure|nccl failure|rccl failure|hip failure|segmentation fault|\bNCCL WARN\b)")


def _int_or_none(tok: str) -> int | None:
    return int(tok) if re.fullmatch(r"-?\d+", tok) else None


def parse_rccl(text: str) -> RcclResult:
    """Parse rccl-tests / nccl-tests stdout (``*_perf`` binaries)."""
    res = RcclResult()
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if _RCCL_ERR_RE.search(s):
            res.errors.append(s)
        if s.startswith("#"):
            m = re.search(r"Avg bus bandwidth\s*:\s*([\d.]+)", s)
            if m:
                res.avg_busbw = float(m.group(1))
            m = re.search(r"Out of bounds values\s*:\s*(\d+)", s)
            if m:
                res.out_of_bounds = int(m.group(1))
            continue
        toks = s.split()
        if len(toks) < 13 or not toks[0].isdigit():
            continue
        tail = toks[-8:]
        root = _int_or_none(toks[4])
        res.rows.append(RcclRow(
            size=int(toks[0]), count=int(toks[1]), dtype=toks[2], redop=toks[3],
            root=-1 if root is None else root,
            oop_time_us=to_float(tail[0]), oop_algbw=to_float(tail[1]),
            oop_busbw=to_float(tail[2]), oop_wrong=_int_or_none(tail[3]),
            ip_time_us=to_float(tail[4]), ip_algbw=to_float(tail[5]),
            ip_busbw=to_float(tail[6]), ip_wrong=_int_or_none(tail[7]),
        ))
    return res


# ------------------------------------------------------------- TransferBench

DEFAULT_BW_REGEXES = [
    r"(?i)aggregate[^\n]*?([\d.]+)\s*GB/s",
    r"(?i)average[^\n]*?bandwidth[^\n]*?([\d.]+)\s*GB/s",
]


def parse_bandwidth(text: str, regexes: list[str] | None = None) -> float | None:
    """Return the *last* bandwidth figure matched by the first regex that matches.

    TransferBench's summary format differs across releases, so the regexes are
    inventory-configurable (``commands.transferbench.bw_regex``)."""
    for rx in regexes or DEFAULT_BW_REGEXES:
        hits = re.findall(rx, text)
        if hits:
            last = hits[-1]
            return float(last[0] if isinstance(last, tuple) else last)
    return None


# -------------------------------------------------------------- perftest

@dataclass
class PerftestRow:
    size: int
    iterations: int
    bw_peak: float | None
    bw_avg: float | None
    msg_rate: float | None


@dataclass
class PerftestResult:
    rows: list[PerftestRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def bw_avg(self) -> float | None:
        """Average BW of the last (largest) message size row, in the units perftest printed."""
        return self.rows[-1].bw_avg if self.rows else None


_PERF_ERR_RE = re.compile(r"(?i)(completion with error|cqe error|failed status|couldn't|failed to|unable to|error:|timeout)")


def parse_perftest(text: str) -> PerftestResult:
    """Parse ib_write_bw / ib_send_bw / ib_read_bw output (``#bytes #iterations BW peak BW average MsgRate``)."""
    res = PerftestResult()
    in_table = False
    for line in text.splitlines():
        s = line.strip()
        if _PERF_ERR_RE.search(s):
            res.errors.append(s)
        if s.startswith("#bytes"):
            in_table = True
            continue
        if not in_table:
            continue
        toks = s.split()
        if len(toks) >= 4 and toks[0].isdigit():
            res.rows.append(PerftestRow(int(toks[0]), int(toks[1]), to_float(toks[2]), to_float(toks[3]),
                                        to_float(toks[4]) if len(toks) > 4 else None))
        elif s.startswith("-"):
            in_table = False
    return res


# ------------------------------------------------------------ Prometheus

@dataclass
class Sample:
    name: str
    labels: dict[str, str]
    value: float


_PROM_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)(?:\s+\d+)?$")
_LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


def parse_prometheus(text: str) -> list[Sample]:
    """Prometheus text exposition format -> samples (comments/HELP/TYPE skipped, NaN kept as nan)."""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = _PROM_RE.match(s)
        if not m:
            continue
        try:
            value = float(m.group(3))
        except ValueError:
            continue
        labels = {k: v.replace('\\"', '"').replace("\\\\", "\\") for k, v in _LABEL_RE.findall(m.group(2) or "")}
        out.append(Sample(m.group(1), labels, value))
    return out


def split_http_response(text: str) -> tuple[int | None, dict[str, str], str]:
    """``curl -s -D - URL`` output -> (status, lower-cased headers, body)."""
    text = text.replace("\r\n", "\n")
    head, sep, body = text.partition("\n\n")
    if not sep or not head.startswith("HTTP/"):
        return None, {}, text
    lines = head.splitlines()
    m = re.match(r"HTTP/\S+\s+(\d{3})", lines[0])
    headers = {}
    for ln in lines[1:]:
        k, _, v = ln.partition(":")
        headers[k.strip().lower()] = v.strip()
    return (int(m.group(1)) if m else None), headers, body


# ------------------------------------------------------------ RcclSmokeTest

@dataclass
class SmokeResult:
    rows: dict[str, list[str]] = field(default_factory=dict)  # collective -> cells (P/F/S/-) per size
    sizes: list[str] = field(default_factory=list)
    passed: bool | None = None                                 # from the RESULT line

    def cells(self, code: str) -> list[str]:
        return [f"{coll} @ {self.sizes[i] if i < len(self.sizes) else i}"
                for coll, cells in self.rows.items() for i, c in enumerate(cells) if c == code]


def parse_smoketest(text: str) -> SmokeResult:
    """RcclSmokeTest pass/fail table: '  AllReduce (float32) |  P  |  P  |  F  |' + 'RESULT: ...'."""
    res = SmokeResult()
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("Collective") and "|" in s:
            res.sizes = [c.strip() for c in s.split("|")[1:] if c.strip()]
            continue
        m = re.match(r"^RESULT:\s*(.*)$", s)
        if m:
            res.passed = "ALL TESTS PASSED" in m.group(1).upper()
            continue
        if "|" in s and not s.startswith("-"):
            parts = [p.strip() for p in s.split("|")]
            cells = [p for p in parts[1:] if p]
            if parts[0] and cells and all(c in ("P", "F", "S", "-") for c in cells):
                res.rows[parts[0]] = cells
    return res


# ------------------------------------------------------- TransferBench presets

@dataclass
class TbSmokeResult:
    """TransferBench 'smoketest' preset: '| Copy (H2D) | ALL |  01  | *** |  09  | **. | .** |' (*=pass .=skip F=fail)."""
    rows: dict[str, list[str]] = field(default_factory=dict)  # test name -> result cells
    gpus: int | None = None                                    # 'Running tests on N GPUs total across R rank(s)'
    ranks: int | None = None
    passed: bool = False                                       # 'All tests passed'
    errors: list[str] = field(default_factory=list)            # 'ERROR: ...' lines (e.g. config mismatch across ranks)

    def failed(self) -> list[str]:
        return [name for name, cells in self.rows.items() if any("F" in c for c in cells)]

    def ran(self) -> int:
        """Number of individual checks that ran ('*' or 'F')."""
        return sum(c.count("*") + c.count("F") for cells in self.rows.values() for c in cells)


def parse_tb_smoketest(text: str) -> TbSmokeResult:
    res = TbSmokeResult()
    for line in text.splitlines():
        s = line.strip()
        m = re.search(r"Running tests on (\d+) GPUs total across (\d+) rank", s)
        if m:
            res.gpus, res.ranks = int(m.group(1)), int(m.group(2))
        elif s == "All tests passed":
            res.passed = True
        elif re.match(r"^ERROR\b", s):
            res.errors.append(s)
        elif s.startswith("|") and not s.startswith("|-"):
            parts = [p.strip() for p in s.strip("|").split("|")]
            cells = [p for p in parts[1:] if re.fullmatch(r"[*.F]+", p)]
            if parts[0] and parts[0] != "Name" and cells:
                res.rows[parts[0]] = cells
    return res


@dataclass
class TbPodA2A:
    """TransferBench 'poda2a' preset: a source-GPU x destination-GPU matrix (GB/s) for every GPU of the pod, an
    'RTotal' row (per destination GPU receive totals) and the 'STotal' / 'Actual' aggregate in its last two cells."""
    ranks: int | None = None                                   # '[#Ranks:N]'
    src: list[tuple[int, int, float]] = field(default_factory=list)   # (rank, gpu, send total GB/s)
    rx: list[float] = field(default_factory=list)              # receive total per destination GPU
    stotal: float | None = None
    actual: float | None = None
    errors: list[str] = field(default_factory=list)


def parse_tb_poda2a(text: str) -> TbPodA2A:
    res, rank = TbPodA2A(), None
    for line in text.splitlines():
        s = line.strip()
        m = re.search(r"#Ranks:(\d+)", s)
        if m:
            res.ranks = int(m.group(1))
        if re.match(r"^ERROR\b", s):
            res.errors.append(s)
            continue
        if "RTotal" in s:
            nums = [float(x) for x in re.findall(r"\d+\.\d+", s)]
            if len(nums) >= 3:
                res.rx, res.stotal, res.actual = nums[:-2], nums[-2], nums[-1]
            continue
        m = re.match(r"^│\s*(?:Rank (\d+))?\s*│\s*GPU (\d+)\s*│(.*)$", s)
        if m:
            if m.group(1) is not None:
                rank = int(m.group(1))
            nums = [float(x) for x in re.findall(r"\d+\.\d+", m.group(3))]
            if len(nums) >= 2 and rank is not None:
                res.src.append((rank, int(m.group(2)), nums[-2]))
    return res


_P2P_ROW = re.compile(r"Rank (\d+)\s*[│|]?\s*GPU (\d+)\s*[│|]\s*Rank (\d+)\s*[│|]?\s*GPU (\d+)\s*[│|]\s*([\d.]+|N/A)")


def parse_tb_podp2p(text: str) -> dict[tuple[int, int, int, int], float | None]:
    """TransferBench 'podp2p' with OUTPUT_FORMAT=0 (one row per link, unidirectional):
    '│  Rank 00    GPU 00 │  Rank 01    GPU 02 │    132.96 │' (v1.70.02: no divider between rank and GPU)
    -> {(src_rank, src_gpu, dst_rank, dst_gpu): GB/s or None (N/A)}. Same-tray links and GPU->itself are listed too.
    With P2P_MODE=0 the unidirectional table comes first; only the first value per link is kept."""
    out: dict[tuple[int, int, int, int], float | None] = {}
    for m in _P2P_ROW.finditer(text):
        k = tuple(int(x) for x in m.groups()[:4])
        if k not in out:
            out[k] = None if m.group(5) == "N/A" else float(m.group(5))
    return out


_XFER_ROW = re.compile(r"Transfer\s+(\d+)\s*[│|]\s*([\d.]+) GB/s\s*[│|].*?[│|]\s*R(\d+)G(\d+)\s*->\s*R\d+G\d+(?::\d+)?\s*->\s*R(\d+)G(\d+)")
_AGG_CPU = re.compile(r"Aggregate \(CPU\)\s*[│|]\s*([\d.]+) GB/s")


@dataclass
class TbTransfers:
    """TransferBench per-Transfer results (cmdline / config runs): 'Transfer 0 │ 45.052 GB/s │ 23.833 ms │ ... │
    R0G0 -> R0G0:8 -> R9G0' and the 'Aggregate (CPU) │ 4458.837 GB/s' summary (last test of the output wins)."""
    transfers: dict[int, tuple[int, int, int, int, float]] = field(default_factory=dict)  # n -> (sr, sg, dr, dg, GB/s)
    aggregate_cpu: float | None = None


def parse_tb_transfers(text: str) -> TbTransfers:
    res = TbTransfers()
    for m in _XFER_ROW.finditer(text):
        res.transfers[int(m.group(1))] = (int(m.group(3)), int(m.group(4)), int(m.group(5)), int(m.group(6)),
                                          float(m.group(2)))
    hits = _AGG_CPU.findall(text)
    if hits:
        res.aggregate_cpu = float(hits[-1])
    return res


def parse_tb_wallclock(text: str) -> float | None:
    """TransferBench 'wallclock' preset: largest timestamp difference between the XCCs of a GPU, in microseconds
    ('Maximum Delta detected: 686845.80 cycles (6868.46 usec)'); None when the line is missing."""
    m = re.search(r"Maximum Delta detected:\s*[\d.]+\s*cycles\s*\(([\d.]+)\s*usec\)", text)
    return float(m.group(1)) if m else None
