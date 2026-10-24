"""Read SCADA CSV exports. Excel is not required."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from scada_uploader.mapping import MappedRow, map_row, normalize_header
from scada_uploader.schema import SCADA_COLUMNS


@dataclass
class FileRead:
    path: Path
    rows: list[MappedRow] = field(default_factory=list)
    error: str | None = None
    skipped_unstable: bool = False


def snapshot(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def is_stable_pair(first: tuple[int, int], second: tuple[int, int]) -> bool:
    return first == second and first[0] > 0


def wait_until_stable(path: Path, seconds: float, sleep) -> bool:
    if seconds < 0:
        raise ValueError("file_stable_seconds must be >= 0")
    try:
        first = snapshot(path)
    except OSError:
        return False
    if first[0] <= 0:
        return False
    if seconds == 0:
        return True
    sleep(seconds)
    try:
        second = snapshot(path)
    except OSError:
        return False
    return is_stable_pair(first, second)


def read_bytes_retry(path: Path, retries: int, delay: float, sleep) -> bytes:
    attempts = max(1, retries)
    last: Exception | None = None
    for index in range(attempts):
        try:
            return path.read_bytes()
        except PermissionError as exc:
            last = exc
            if index + 1 < attempts:
                sleep(delay)
    assert last is not None
    raise last


def decode_export(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252")


def read_export_text(
    text: str,
    *,
    source_name: str,
    tzinfo,
    date_formats,
    time_formats,
) -> FileRead:
    """Parse CSV text. `source_name` is stored only for error text here."""
    content: list[tuple[int, str]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        if line.lstrip().startswith("#"):
            continue
        content.append((line_number, line))
    result = FileRead(path=Path(source_name))
    if not content:
        result.error = f"{source_name}: no header row"
        return result

    table = list(csv.reader(line for _, line in content))
    header_cells = [cell.strip() for cell in table[0]]
    header_norm = {normalize_header(cell): cell for cell in header_cells if cell}
    missing = [scada for scada, _field in SCADA_COLUMNS if normalize_header(scada) not in header_norm]
    if "date" not in header_norm:
        missing.insert(0, "Date")
    if missing:
        result.error = f"{source_name}: missing columns: " + ", ".join(missing)
        return result

    for (line_number, _line), cells in zip(content[1:], table[1:]):
        if not any(cell.strip() for cell in cells):
            continue
        row: dict[str, str] = {}
        for index, key in enumerate(header_cells):
            if not key or key in row:
                continue
            row[key] = cells[index].strip() if index < len(cells) else ""
        result.rows.append(
            map_row(
                row,
                line_number=line_number,
                source_name=source_name,
                tzinfo=tzinfo,
                date_formats=date_formats,
                time_formats=time_formats,
                original_headers=header_norm,
            )
        )
    return result


def list_csv_files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        raise FileNotFoundError(f"watch folder does not exist: {folder}")
    found = [
        path
        for path in folder.iterdir()
        if path.is_file()
        and not path.is_symlink()
        and not path.name.startswith(".")
        and path.suffix.lower() == ".csv"
    ]
    return sorted(found, key=lambda path: path.name.lower())


def read_csv_file(path: Path, *, tzinfo, date_formats, time_formats, retries: int, retry_delay: float, sleep) -> FileRead:
    try:
        before = snapshot(path)
    except OSError as exc:
        result = FileRead(path=path)
        result.error = f"{path.name}: could not stat file ({exc.__class__.__name__})"
        return result
    try:
        data = read_bytes_retry(path, retries, retry_delay, sleep)
    except PermissionError as exc:
        result = FileRead(path=path)
        result.error = f"{path.name}: file is locked ({exc.__class__.__name__})"
        return result
    try:
        after = snapshot(path)
    except OSError as exc:
        result = FileRead(path=path)
        result.error = f"{path.name}: could not stat after read ({exc.__class__.__name__})"
        return result
    if not is_stable_pair(before, after) or len(data) != after[0]:
        result = FileRead(path=path, skipped_unstable=True)
        result.error = f"{path.name}: changed while reading; skipped"
        return result
    parsed = read_export_text(
        decode_export(data),
        source_name=path.name,
        tzinfo=tzinfo,
        date_formats=date_formats,
        time_formats=time_formats,
    )
    parsed.path = path
    return parsed
