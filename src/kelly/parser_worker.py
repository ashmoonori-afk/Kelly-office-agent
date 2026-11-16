"""Private entrypoint. No Office parser or input is opened before the stdin gate."""

import hashlib
import json
import os
import sys
from pathlib import Path

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_RESULT_BYTES = 1024 * 1024
SUPPORTED = {"txt", "md", "csv", "docx", "xlsx", "pdf"}
PREFLIGHT_VERSION = "birkin-lite-preflight-1"


def _word_field_instructions(xml):
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    fields = []
    orphan_parts = []
    for item in xml.iter():
        if item.tag == namespace + "fldSimple":
            yield item.attrib.get(namespace + "instr", "")
        elif item.tag == namespace + "fldChar":
            kind = item.attrib.get(namespace + "fldCharType")
            if kind == "begin":
                fields.append([])
            elif kind == "end" and fields:
                yield "".join(fields.pop())
        elif item.tag == namespace + "instrText":
            (fields[-1] if fields else orphan_parts).append(item.text or "")
    for parts in fields:
        yield "".join(parts)
    yield "".join(orphan_parts)


def preflight(path, filename, args):
    from kelly._vendor.office.artifact_identity import verify_descriptor_identity
    from kelly._vendor.office.package_scan import preflight_package
    from kelly._vendor.office.package_types import PackageLimits
    from kelly._vendor.office.safe_xml import ElementTree
    from kelly.contracts import AppError
    from kelly.safe_io import open_regular

    fmt = Path(filename).suffix.lower().lstrip(".")
    if fmt not in SUPPORTED:
        raise AppError("UNSUPPORTED_CAPABILITY")
    with open_regular(path, path.parent) as source:
        size = os.fstat(source.fileno()).st_size
        if size > MAX_FILE_BYTES:
            raise AppError("LIMIT_EXCEEDED")
        before = hashlib.file_digest(source, "sha256").hexdigest()
        source.seek(0)
        result = {
            "format": fmt,
            "byte_size": size,
            "sha256": before,
            "visual_verified": False,
            "render_status": "RENDER_UNAVAILABLE",
            "parser_version": PREFLIGHT_VERSION,
        }
        if fmt in {"txt", "md", "csv"}:
            data = source.read(MAX_FILE_BYTES + 1)
            try:
                text = data.decode("utf-8-sig")
            except UnicodeError:
                raise AppError("INVALID_INPUT") from None
            if "\x00" in text:
                raise AppError("INVALID_INPUT")
            result.update(
                encoding="utf-8-sig" if data.startswith(b"\xef\xbb\xbf") else "utf-8",
                text_characters=len(text),
            )
            if fmt == "csv":
                import csv
                import io

                delimiter = args.get("delimiter", ",")
                if delimiter not in {",", "\t", ";", "|"}:
                    raise AppError("INVALID_INPUT")
                csv.field_size_limit(1024 * 1024)
                rows = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
                try:
                    count = sum(1 for _ in rows)
                except csv.Error:
                    raise AppError("INVALID_INPUT") from None
                result.update(delimiter=delimiter, rows=count)
        elif fmt in {"docx", "xlsx"}:
            limits = PackageLimits(
                max_entries=5000,
                max_uncompressed_bytes=200 * 1024 * 1024,
                max_entry_ratio=100,
                max_xml_depth=128,
                # A streamed 100k-row worksheet can exceed 10 MiB without being complex.
                max_xml_bytes=200 * 1024 * 1024,
                max_total_xml_bytes=200 * 1024 * 1024,
            )
            manifest = preflight_package(path, limits)
            if manifest["active_content"] or manifest["external_relationships"]:
                raise AppError("POLICY_DENIED")
            for name, part in manifest["parts"].items():
                if name == "[Content_Types].xml" or name.endswith(".rels"):
                    xml = ElementTree.fromstring(part["bytes"])
                    for item in xml.iter():
                        values = " ".join(item.attrib.values()).lower()
                        if any(
                            word in values
                            for word in (
                                "vbaproject",
                                "macroenabled",
                                "activex",
                                "oleobject",
                                "attachedtemplate",
                            )
                        ):
                            raise AppError("POLICY_DENIED")
                if fmt == "docx" and name.startswith("word/") and name.endswith(".xml"):
                    xml = ElementTree.fromstring(part["bytes"])
                    if any(
                        instruction.upper().split()[:1]
                        and instruction.upper().split()[0]
                        in {"DDE", "DDEAUTO", "INCLUDETEXT", "INCLUDEPICTURE", "LINK"}
                        for instruction in _word_field_instructions(xml)
                    ):
                        raise AppError("POLICY_DENIED")
            verify_descriptor_identity(source.fileno(), Path(filename))
            result.update(
                package_entries=len(manifest["parts"]),
                expanded_bytes=sum(len(part["bytes"]) for part in manifest["parts"].values()),
                active_content=False,
                external_relationships=False,
            )
        else:
            from pypdf import PdfReader

            from kelly._vendor.office.adapters.pdf_inventory import inventory

            verify_descriptor_identity(source.fileno(), Path(filename))
            source.seek(0)
            reader = PdfReader(source, strict=True)
            if reader.is_encrypted:
                raise AppError("POLICY_DENIED")
            signatures, active = inventory(reader, size)
            if signatures or active:
                raise AppError("POLICY_DENIED")
            if len(reader.pages) > 200:
                raise AppError("LIMIT_EXCEEDED")
            result.update(
                pages=len(reader.pages),
                active_content=False,
                signed=False,
                encrypted=False,
                ocr_supported=False,
            )
        source.seek(0)
        if hashlib.file_digest(source, "sha256").hexdigest() != before:
            raise AppError("SOURCE_CHANGED")
        return result


def main():
    # Source checkout and installed wheel both use this file's trusted package root.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        if os.name == "posix":
            import resource

            memory = int(sys.argv[1]) * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
            resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
        # Parent assigns the Windows Job before writing anything to this pipe.
        raw = sys.stdin.buffer.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            raise ValueError("request too large")
        request = json.loads(raw)
        path = Path(request["path"])
        operation = request["operation"]
        args = request["args"]
        from kelly.safe_io import open_regular

        # Keep the exact source protected between preflight and the later Office operation.
        with open_regular(path, path.parent):
            metadata = preflight(path, request["filename"], args)
            if operation == "preflight":
                result = metadata
            else:
                try:
                    from kelly.office import worker_operation
                except ImportError:
                    from kelly.contracts import AppError

                    raise AppError("UNSUPPORTED_CAPABILITY") from None
                result = worker_operation(operation, path, {**args, "format": metadata["format"]})
        response = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(response) > MAX_RESULT_BYTES:
            response = b'{"error":"LIMIT_EXCEEDED"}'
    except MemoryError:
        response = b'{"error":"LIMIT_EXCEEDED"}'
    except Exception as exc:  # noqa: BLE001 - parser process error boundary, no raw exceptions leave it
        from kelly._vendor.office.errors import DocumentError
        from kelly.contracts import AppError

        if isinstance(exc, AppError):
            code = exc.code
        elif isinstance(exc, DocumentError):
            code = exc.code.value
        else:
            code = "PARSER_FAILED"
        response = json.dumps({"error": code}).encode("ascii")
    sys.stdout.buffer.write(response)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
