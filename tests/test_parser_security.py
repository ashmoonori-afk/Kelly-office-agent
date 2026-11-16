import hashlib
import os
import struct
import sys
import time
import zipfile
from pathlib import Path

import pytest
from docx import Document
from openpyxl import Workbook
from pypdf import PdfWriter

from kelly.contracts import AppError
from kelly.sandbox import MAX_RESULT_BYTES, _run_worker, _worker_environment, parse_file


def docx_file(path):
    doc = Document()
    doc.add_paragraph("회의록: 담당자 미정")
    doc.save(path)
    return path


def change_package(path, extra=None, replace=None):
    with zipfile.ZipFile(path) as source:
        entries = [(item, source.read(item)) for item in source.infolist()]
    with zipfile.ZipFile(path, "w") as target:
        for item, data in entries:
            target.writestr(item, (replace or {}).get(item.filename, data))
        for name, data in extra or []:
            target.writestr(name, data)


def test_preflight_real_formats_private_temp_name_and_no_mutation(tmp_path):
    docx = docx_file(tmp_path / "source.tmp")
    before = hashlib.sha256(docx.read_bytes()).hexdigest()
    result = parse_file(docx, "회의록.docx")
    assert result["format"] == "docx"
    assert result["sha256"] == before == hashlib.sha256(docx.read_bytes()).hexdigest()
    assert result["visual_verified"] is False
    assert result["render_status"] == "RENDER_UNAVAILABLE"
    assert 0 < result["sandbox"]["peak_committed_bytes"] < 512 * 1024 * 1024
    book = Workbook()
    book.active.append(["항목", "금액"])
    book.active.append(["서울", 123])
    xlsx = tmp_path / "sheet.tmp"
    book.save(xlsx)
    assert parse_file(xlsx, "자료.xlsx")["format"] == "xlsx"
    pdf = tmp_path / "pdf.tmp"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(pdf)
    assert parse_file(pdf, "자료.pdf")["pages"] == 1


@pytest.mark.parametrize(
    "filename,data",
    [
        ("메모.txt", "한국어".encode()),
        ("메모.md", b"# note"),
        ("table.csv", b"name,total\nA,2\n"),
    ],
)
def test_utf8_formats(tmp_path, filename, data):
    path = tmp_path / "input.tmp"
    path.write_bytes(data)
    assert parse_file(path, filename)["format"] == Path(filename).suffix[1:]


@pytest.mark.parametrize("suffix", ["pptx", "hwpx", "hwp", "docm", "xlsm"])
def test_unsupported_formats_do_not_fake_success(tmp_path, suffix):
    path = tmp_path / "input.tmp"
    path.write_bytes(b"example")
    with pytest.raises(AppError) as error:
        parse_file(path, "source." + suffix)
    assert error.value.code == "UNSUPPORTED_CAPABILITY"


@pytest.mark.parametrize(
    "kind",
    ["traversal", "duplicate", "entity", "utf16_entity", "external", "active", "depth", "special"],
)
def test_malicious_packages_are_rejected_before_office_parser(tmp_path, kind):
    path = docx_file(tmp_path / "unsafe.tmp")
    if kind == "traversal":
        change_package(path, extra=[("../outside.txt", b"no")])
    elif kind == "duplicate":
        with pytest.warns(UserWarning, match="Duplicate"):
            change_package(path, extra=[("word/document.xml", b"<x/>")])
    elif kind in {"entity", "utf16_entity"}:
        xml = '<!DOCTYPE x [<!ENTITY y "unsafe">]><x>&y;</x>'
        data = xml.encode("utf-16" if kind == "utf16_entity" else "utf-8")
        change_package(path, replace={"word/document.xml": data})
    elif kind == "external":
        xml = (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="evil" Type="http://example.org/link" '
            'Target="https://example.invalid/" TargetMode="External"/></Relationships>'
        )
        change_package(path, replace={"word/_rels/document.xml.rels": xml.encode()})
    elif kind == "active":
        change_package(path, extra=[("word/vbaProject.bin", b"macro")])
    elif kind == "depth":
        change_package(path, extra=[("deep.xml", b"<x>" * 129 + b"</x>" * 129)])
    else:
        item = zipfile.ZipInfo("symbolic")
        item.create_system = 3
        item.external_attr = 0o120777 << 16
        change_package(path, extra=[(item, b"outside")])
    original = path.read_bytes()
    with pytest.raises(AppError) as error:
        parse_file(path, "unsafe.docx")
    assert error.value.code in {"PACKAGE_INVALID", "LIMIT_EXCEEDED", "POLICY_DENIED"}
    assert path.read_bytes() == original


def test_compression_bomb_and_identity_disguise(tmp_path):
    path = tmp_path / "bomb.tmp"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("bomb.xml", b"x" * 1024 * 1024)
    with pytest.raises(AppError) as bomb:
        parse_file(path, "bomb.docx")
    assert bomb.value.code == "LIMIT_EXCEEDED"
    path.write_bytes(b"not a document")
    with pytest.raises(AppError):
        parse_file(path, "wrong.xlsx")


@pytest.mark.parametrize("encrypted", [False, True])
def test_zip_local_central_flags_and_encryption_are_rejected(tmp_path, encrypted):
    path = docx_file(tmp_path / "flags.tmp")
    data = bytearray(path.read_bytes())
    local = data.index(b"PK\x03\x04")
    central = data.index(b"PK\x01\x02")
    flags = struct.unpack_from("<H", data, local + 6)[0] | 1
    struct.pack_into("<H", data, local + 6, flags)
    if encrypted:
        struct.pack_into("<H", data, central + 8, flags)
    path.write_bytes(data)
    with pytest.raises(AppError) as error:
        parse_file(path, "flags.docx")
    assert error.value.code == "PACKAGE_INVALID"


def test_split_word_dde_instruction_is_refused_between_other_fields(tmp_path):
    path = docx_file(tmp_path / "field.tmp")
    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body><w:p><w:fldSimple w:instr="PAGE"/></w:p><w:p>'
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        "<w:r><w:instrText>DD</w:instrText></w:r>"
        "<w:r><w:instrText>EAUTO fixture fixture</w:instrText></w:r>"
        '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p></w:body></w:document>'
    )
    change_package(path, replace={"word/document.xml": xml.encode()})
    with pytest.raises(AppError) as error:
        parse_file(path, "field.docx")
    assert error.value.code == "POLICY_DENIED"


def test_streamed_100000_row_package_stays_within_parser_limits(tmp_path):
    path = tmp_path / "large.tmp"
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("매출")
    sheet.append(["id", "amount", "region"])
    for index in range(100_000):
        sheet.append([index, (index * 7919) % 100_003, "지역" + str(index % 100)])
    workbook.save(path)
    result = parse_file(path, "매출.xlsx")
    assert result["format"] == "xlsx"
    assert result["expanded_bytes"] > 10 * 1024 * 1024
    assert result["sandbox"]["elapsed_ms"] < 30_000
    assert result["sandbox"]["peak_committed_bytes"] < 512 * 1024 * 1024


def test_pdf_actions_and_passwords_are_inert_and_refused(tmp_path):
    for protected in (False, True):
        path = tmp_path / f"pdf-{protected}.tmp"
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        if protected:
            writer.encrypt("fixture password")
        else:
            writer.add_js("app.alert('must never execute')")
        writer.write(path)
        with pytest.raises(AppError) as error:
            parse_file(path, "source.pdf")
        assert error.value.code == "POLICY_DENIED"


def test_pdf_xfa_is_refused_without_interpreting_xml(tmp_path):
    from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    xfa = DecodedStreamObject()
    xfa.set_data(
        b'<xdp:xdp xmlns:xdp="http://ns.adobe.com/xdp/"><script>fixture</script></xdp:xdp>'
    )
    writer.root_object[NameObject("/AcroForm")] = DictionaryObject(
        {
            NameObject("/Fields"): ArrayObject(),
            NameObject("/XFA"): writer._add_object(xfa),
        }
    )
    path = tmp_path / "xfa.tmp"
    writer.write(path)
    with pytest.raises(AppError) as error:
        parse_file(path, "xfa.pdf")
    assert error.value.code == "POLICY_DENIED"


def test_worker_environment_drops_provider_secrets(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-fixture-secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-fixture-secret")
    assert not {"OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN", "PYTHONPATH"} & _worker_environment().keys()
    code = "import sys,json,os;sys.stdin.read();print(json.dumps({'names':list(os.environ)}))"
    result = _run_worker([sys.executable, "-I", "-c", code], {}, timeout_s=5, memory_mb=128)
    assert "OPENAI_API_KEY" not in result["names"]
    assert "TELEGRAM_BOT_TOKEN" not in result["names"]


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object containment")
def test_actual_windows_memory_limit_refuses_commit():
    code = (
        "import sys,json;sys.stdin.read();\ntry:\n b=bytearray(256*1024*1024)\n"
        "except MemoryError:\n print(json.dumps({'limited':True}))\n"
        "else:\n print(json.dumps({'limited':False}))"
    )
    result = _run_worker([sys.executable, "-I", "-c", code], {}, timeout_s=5, memory_mb=64)
    assert result == {"limited": True}


@pytest.mark.skipif(os.name != "nt", reason="Windows suspended launch")
def test_job_assignment_failure_never_runs_worker_code(tmp_path, monkeypatch):
    from kelly._vendor.office._winjob import WindowsJob

    def fail_assign(self, pid):
        raise OSError("synthetic job failure")

    monkeypatch.setattr(WindowsJob, "assign", fail_assign)
    marker = tmp_path / "uncontained.txt"
    code = "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('escaped')"
    with pytest.raises(AppError) as error:
        _run_worker([sys.executable, "-I", "-c", code, str(marker)], {}, timeout_s=5, memory_mb=128)
    assert error.value.code == "UNSUPPORTED_CAPABILITY"
    assert not marker.exists()


def test_actual_worker_timeout_reaps_process(tmp_path):
    marker = tmp_path / "late.txt"
    code = (
        "import sys,time,pathlib;sys.stdin.read();time.sleep(2);"
        "pathlib.Path(sys.argv[1]).write_text('escaped')"
    )
    started = time.monotonic()
    with pytest.raises(AppError) as error:
        _run_worker(
            [sys.executable, "-I", "-c", code, str(marker)], {}, timeout_s=0.15, memory_mb=128
        )
    assert error.value.code == "PARSER_TIMEOUT"
    assert time.monotonic() - started < 2
    assert not marker.exists()


def test_output_cap_is_enforced_while_draining():
    code = f"import sys;sys.stdin.read();sys.stdout.write('x'*{MAX_RESULT_BYTES + 1})"
    with pytest.raises(AppError) as error:
        _run_worker([sys.executable, "-I", "-c", code], {}, timeout_s=5, memory_mb=128)
    assert error.value.code == "LIMIT_EXCEEDED"


def test_vendor_has_no_runtime_import_dependency():
    import ast

    import kelly._vendor.office as vendor

    for path in Path(vendor.__file__).parent.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                assert all(not alias.name.startswith("birkin.") for alias in node.names)
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("birkin.")
