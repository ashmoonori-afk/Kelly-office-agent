# Third-party source and security boundary

Birkin Lite vendors a small subset of Birkin under `src/kelly/_vendor/office/`.
Upstream repository: https://github.com/ashmoonori-afk/birkin . Inspected checkout HEAD:
`16fe5dad52920837366ceff6e440c41e871031ce`. The byte hashes below identify the actual copied local sources,
including any local state; HEAD alone is not a claim that all files exactly matched that commit.
Original license: MIT, Copyright (c) 2026 ashmoonori. The complete license is preserved at
`src/kelly/_vendor/office/LICENSE` and included in the installed package.

## Retained code and changes

- `windows_native`, `path_identity`, `secure_file_open`: native identities and no-reparse opens.
- `package_scan`, `package_zip`, `package_xml`, `package_relationships`, `package_types`, `limits`,
  `safe_xml`, `artifact_identity`: ZIP/header/CRC/XML/OOXML identity validation.
- `errors`, `artifact_serialization`: closed error/serialization dependency closure.
- `adapters/pdf_inventory`, `adapters/pdf_types`: bounded inert PDF object inspection.
- `_winjob`: existing stdlib Windows Job Object structures and lifecycle.
- `office._dangerous_cell`: the small NFKC/leading-control-space check adapted from
  `birkin/office/csv_safety.py::_dangerous_trigger` under the same MIT license; the return value is
  changed from a trigger character to a Boolean. Source SHA-256:
  `60cdba5ee44d613a5893ae7dfaefea536759f1a85b34974319a6728838fdee7c`.

Absolute `birkin.office.safe_xml` imports were changed to relative imports. Python 3.12's standard
`typing.override` replaces `typing_extensions.override`. Targeted lint suppression preserves vendored
source formatting/type aliases. No runtime module imports the Birkin package.

The `_winjob.WindowsJob.create(memory_bytes)` copy adds committed-memory and active-process limits;
`peak_memory_bytes()` reports OS accounting. `pdf_inventory` adds an unconditional `/XFA` refusal
inventory entry for V1 text-only PDF support. No signature/XFA/script is evaluated.

Lite wrappers reject reported active content/external relationships, macro/embedded-content type
hints, Word external/DDE field instructions (including instructions split over multiple runs), encrypted
or signed PDFs, and unsupported formats. These stricter decisions live in `parser_worker.py`.
The wrapper passes the blueprint limits explicitly: 25 MiB input, 5,000 ZIP entries, 200 MiB expanded,
100:1 compression ratio, XML depth 128, 30-second wall time, 512 MiB committed-memory ceiling.
The maximum XML part/total bytes is 200 MiB; the old 10 MiB part limit would reject ordinary large
worksheets. Node, attribute and text limits from the source remain in force.

## Windows process and file proof

The launcher is created suspended, assigned to a Job, checked for Job membership, resumed, and only
then receives the request. ActiveProcessLimit=2 accounts for Windows' venv launcher plus its Python
interpreter. The entire job shares the 512 MiB limit and is killed on close/timeout. If assignment or
limits cannot be installed, the operation fails closed without parsing. Worker environment uses a
small OS-variable allowlist; credentials and user HOME/PYTHONPATH are absent. Stdout is drained with a
1 MiB cap; parser failures return only safe error codes. This is resource containment, not an OS
AppContainer/network sandbox. V1 parsers execute no macros or external Office applications.

`safe_io` keeps each ancestor directory and source file open against replacement. Source content
hash verification and no-replace rename use the same held Windows file handle. Native filename
buffers reserve the UTF-16 terminator. Directory creation extends the guarded chain one component
at a time. File publication on other operating systems currently fails closed.

Regression evidence is executable in `tests/test_parser_security.py` and `tests/test_safe_io.py`:
real DOCX/XLSX/PDF, UTF-8 text/CSV, hostile ZIP/XML, split DDE fields, PDF XFA, real commit-memory
refusal, timeout and output cap, failed assignment before worker execution, native junctions,
file/directory replacement denial, and no-replace publication using long Korean/emoji names.
Windows symlink creation requires host privileges; its test reports an explicit skip if absent.

A streaming 100,000-row XLSX fixture is generated and preflighted by
`test_streamed_100000_row_package_stays_within_parser_limits`. Its receipt records elapsed milliseconds
and peak committed bytes from the actual Job Object. This proves parser-resource behavior, not model
quality, live Office rendering, or external-provider success.

Official API references:
- https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
- https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_extended_limit_information
- https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-setfileinformationbyhandle
- https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_rename_info

## Source hash ledger

| Vendored path | Original source SHA-256 | Vendored SHA-256 |
|---|---|---|
| `_winjob.py` | `1866ccc191c09d1b7a4be9276d36e444ed50b5b6deadb0c6d1050f4e02b93e13` | `1efd66a756cbb594aa0230ee2cae638982984c8438788c67105a94c27309a5e8` |
| `adapters/pdf_inventory.py` | `d82b84267ce1fb9f6550ddc94d56f72a2cfa2c8137b59c06cbaa7f2f1df9d311` | `96e3fe2be7b7088aee7baa2bce95d47a116454cfab08ad0c8b5b148acc0bb6a9` |
| `adapters/pdf_types.py` | `cf818fc8a90382dffe2bb66fd020a75aa1430c45cd1970f043319e0fd817c4af` | `cf818fc8a90382dffe2bb66fd020a75aa1430c45cd1970f043319e0fd817c4af` |
| `artifact_identity.py` | `1b6c768635b10c4d1777824b2dfc6dc64dae9bd64c8eb0c17b16770e8a8a0408` | `9aaff41bd07d17704b6eaaa71838ebaa1e529c3f7eeb5196f82ce910294e94d3` |
| `artifact_serialization.py` | `e0b2277a34063dbc20d5955329a0fbca1674035bcf719b3fbf9247d74423b1b2` | `e6a0234fec37d7ed214111b9ae313151e92f45d4875f3abc22b43c9aedb4655b` |
| `errors.py` | `833386042633f027ffc658da0080416626cf97dfd55bcc96cade74d5cfd1d2e4` | `8b561b4e6fa646d91752e97c9e45515fb22841bf744eeeaa44586e4975f1b915` |
| `limits.py` | `ae625bf0e1da18deb0c2a296804deb0aea779a2795a9b19a5b9bea124f65d67b` | `ba65f10d76423018c1ebb7a7de3a83a47aded1237ff5b8fc13e32802c9bc786b` |
| `package_relationships.py` | `8739063a35b76189dfef5568914107f5121d3102271ec719e6e0d39f61bec222` | `2200e6fb5e73be6de142a91035e440862c86135c5793e32681ec1e5588b57f36` |
| `package_scan.py` | `b60d5e3c11c3147ac6b7cca79d895cb8f06b46a13fdb2246d3b570062cec8751` | `b60d5e3c11c3147ac6b7cca79d895cb8f06b46a13fdb2246d3b570062cec8751` |
| `package_types.py` | `30ca9191a0425e732d1f9956740a10da119312e749fed2edf5786c3a150afa50` | `30ca9191a0425e732d1f9956740a10da119312e749fed2edf5786c3a150afa50` |
| `package_xml.py` | `b2485762cf70263bff6d3411edaa3ae1b060fcf7f465b6edb3fecba73da26cd8` | `9338a11a2015d70eaac20b6b956504d2c3b2f9bda566fab4b10911a31d60ec61` |
| `package_zip.py` | `71f9671be78151291886db97224e0b79f9ba310679c3aaaaf9c0cd6c26b18ce8` | `71f9671be78151291886db97224e0b79f9ba310679c3aaaaf9c0cd6c26b18ce8` |
| `path_identity.py` | `51f24c42f0d01c2359b3b9a3a1bec7fe4d40cdfcf619f91c046442cfce457931` | `51f24c42f0d01c2359b3b9a3a1bec7fe4d40cdfcf619f91c046442cfce457931` |
| `safe_xml.py` | `d8e81b4986058318b62f6d831f5baee2c7d5d5fe97b5a8698221f662fcc63727` | `78bce0379feaa401a737f5a6f7f1a4d4365b013285789cc84afb3928d8edc06f` |
| `secure_file_open.py` | `a22123d9a35d900cc7e3b0e3a336b1c441e014627adfdc9befdf1d4c3e74b093` | `a22123d9a35d900cc7e3b0e3a336b1c441e014627adfdc9befdf1d4c3e74b093` |
| `windows_native.py` | `f9fa46e5e278503a60178eddea091522d227ade71255830af496c4e3ad09257a` | `f9fa46e5e278503a60178eddea091522d227ade71255830af496c4e3ad09257a` |
