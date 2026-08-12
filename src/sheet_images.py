import hashlib
import io
import json
import posixpath
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath

import gspread
from requests import RequestException

IMAGE_DIR = Path('assets/catalog')
MANIFEST_PATH = IMAGE_DIR / 'manifest.json'
URL_PREFIX = '/catalog/images'

_REL_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
_PACKAGE_REL_NS = 'http://schemas.openxmlformats.org/package/2006/relationships'
_SHEET_NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
_DRAWING_NS = 'http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing'
_DRAWING_MAIN_NS = 'http://schemas.openxmlformats.org/drawingml/2006/main'


def load_manifest() -> dict[str, dict[int, str]]:
    try:
        raw = json.loads(MANIFEST_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

    return {
        str(title): {int(row): str(url) for row, url in rows.items()}
        for title, rows in raw.items()
        if isinstance(rows, dict)
    }


def save_manifest(manifest: dict[str, dict[int, str]]) -> None:
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    temporary_path = MANIFEST_PATH.with_suffix('.tmp')
    temporary_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    temporary_path.replace(MANIFEST_PATH)


def cache_spreadsheet_images(
    client: gspread.Client,
    spreadsheet_id: str,
    worksheet_titles: list[str],
) -> dict[str, dict[int, str]]:
    try:
        exported = embedded_images(export_xlsx(client, spreadsheet_id))
        manifest = _cache_images(exported, worksheet_titles)
    except (RequestException, zipfile.BadZipFile):
        cached = load_manifest()
        if _manifest_files_exist(cached, worksheet_titles):
            return cached
        raise

    save_manifest(manifest)
    return manifest


def _cache_images(
    exported: dict[str, dict[tuple[int, int], bytes]],
    worksheet_titles: list[str],
) -> dict[str, dict[int, str]]:
    manifest: dict[str, dict[int, str]] = {}
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)

    for title in worksheet_titles:
        images = exported.get(title) or exported.get(_xlsx_title(title), {})
        row_urls = manifest.setdefault(title, {})
        for (row, column), image_data in images.items():
            if column != 1:
                continue

            digest = hashlib.sha256(image_data).hexdigest()[:24]
            filename = f'{digest}{image_suffix(image_data)}'
            image_path = IMAGE_DIR / filename
            if not image_path.is_file() or image_path.stat().st_size != len(image_data):
                image_path.write_bytes(image_data)

            row_urls[row] = f'{URL_PREFIX}/{filename}'

    return manifest


def _xlsx_title(title: str) -> str:
    return re.sub(r'[:\\/?*\[\]]', '', title)[:31]


def _manifest_files_exist(
    manifest: dict[str, dict[int, str]],
    worksheet_titles: list[str],
) -> bool:
    if not manifest or any(title not in manifest for title in worksheet_titles):
        return False

    return all(
        (IMAGE_DIR / url.rsplit('/', 1)[-1]).is_file()
        for title in worksheet_titles
        for url in manifest[title].values()
    )


def export_xlsx(client: gspread.Client, spreadsheet_id: str) -> bytes:
    last_error: RequestException | None = None
    for attempt in range(3):
        try:
            return _export_xlsx(client, spreadsheet_id)
        except RequestException as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2 ** attempt)

    assert last_error is not None
    raise last_error


def _export_xlsx(client: gspread.Client, spreadsheet_id: str) -> bytes:
    response = client.http_client.session.get(
        f'https://www.googleapis.com/drive/v3/files/{spreadsheet_id}/export',
        params={
            'mimeType': (
                'application/vnd.openxmlformats-officedocument.'
                'spreadsheetml.sheet'
            ),
        },
        timeout=(20, 240),
    )

    if response.status_code == 403 and 'exportSizeLimitExceeded' in response.text:
        response = client.http_client.session.get(
            f'https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export',
            params={'format': 'xlsx'},
            timeout=(20, 240),
        )

    response.raise_for_status()
    return response.content


def image_suffix(data: bytes) -> str:
    signatures = (
        (b'\xff\xd8\xff', '.jpg'),
        (b'\x89PNG\r\n\x1a\n', '.png'),
        (b'GIF87a', '.gif'),
        (b'GIF89a', '.gif'),
    )

    for signature, suffix in signatures:
        if data.startswith(signature):
            return suffix

    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return '.webp'

    return '.img'


def embedded_images(workbook: bytes) -> dict[str, dict[tuple[int, int], bytes]]:
    result_images = {}
    with zipfile.ZipFile(io.BytesIO(workbook)) as archive:
        for title, worksheet_path in _workbook_sheets(archive).items():
            worksheet_rels = _relationships(archive, worksheet_path)
            worksheet = ET.fromstring(archive.read(worksheet_path))
            drawing = worksheet.find(f'{{{_SHEET_NS}}}drawing')
            if drawing is None:
                continue

            drawing_id = drawing.attrib.get(f'{{{_REL_NS}}}id', '')
            drawing_path = worksheet_rels.get(drawing_id)
            if not drawing_path:
                continue

            sheet_images = result_images.setdefault(title, {})
            for row, column, media_path in _drawing_images(archive, drawing_path):
                sheet_images[(row, column)] = archive.read(media_path)

    return result_images


def _relationships(archive: zipfile.ZipFile, source: str) -> dict[str, str]:
    pure_path = PurePosixPath(source)
    relationships_path = str(pure_path.parent / '_rels' / f'{pure_path.name}.rels')

    try:
        root = ET.fromstring(archive.read(relationships_path))
    except KeyError:
        return {}

    return {
        relation.attrib['Id']: posixpath.normpath(
            posixpath.join(posixpath.dirname(source), relation.attrib['Target']),
        )
        for relation in root.findall(f'{{{_PACKAGE_REL_NS}}}Relationship')
        if relation.attrib.get('TargetMode') != 'External'
    }


def _workbook_sheets(archive: zipfile.ZipFile) -> dict[str, str]:
    workbook_path = 'xl/workbook.xml'
    relationships = _relationships(archive, workbook_path)
    workbook = ET.fromstring(archive.read(workbook_path))
    return {
        sheet.attrib['name']: relationships[relationship_id]
        for sheet in workbook.findall(f'.//{{{_SHEET_NS}}}sheet')
        if (relationship_id := sheet.attrib.get(f'{{{_REL_NS}}}id', '')) in relationships
    }


def _drawing_images(archive: zipfile.ZipFile, drawing_path: str) -> list[tuple[int, int, str]]:
    relationships = _relationships(archive, drawing_path)
    drawing = ET.fromstring(archive.read(drawing_path))

    result = []
    for anchor_name in ('oneCellAnchor', 'twoCellAnchor'):
        for anchor in drawing.findall(f'{{{_DRAWING_NS}}}{anchor_name}'):
            origin = anchor.find(f'{{{_DRAWING_NS}}}from')
            blip = anchor.find(f'.//{{{_DRAWING_MAIN_NS}}}blip')
            if origin is None or blip is None:
                continue

            row_data = origin.find(f'{{{_DRAWING_NS}}}row')
            column_data = origin.find(f'{{{_DRAWING_NS}}}col')
            media_path = relationships.get(blip.attrib.get(f'{{{_REL_NS}}}embed', ''))

            if row_data is not None and column_data is not None and media_path:
                result.append((
                    int(row_data.text or '0') + 1,
                    int(column_data.text or '0') + 1,
                    media_path,
                ))

    return result
