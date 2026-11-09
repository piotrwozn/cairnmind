"""GDB-only restricted metadata reader for this controlled fixture's CPython core.

No locals, globals, argument values or environment are read. Offsets are from the
verified interpreter's installed CPython 3.13 headers (classic GIL, x86_64).
"""
import gdb
import hashlib
import json
from pathlib import Path

inferior = gdb.selected_inferior()


def uint(address, size=8):
    return int.from_bytes(bytes(inferior.read_memory(address, size)), "little")


def ascii_metadata(address):
    length = uint(address + 16)
    flags = uint(address + 32, 4)
    if not (0 < length < 2048 and flags & 0x60 == 0x60 and (flags >> 2) & 7 == 1):
        raise ValueError("Not bounded compact ASCII metadata")
    return bytes(inferior.read_memory(address + 40, length)).decode("ascii")


gdb.execute("frame 6", to_string=True)
frame = int(gdb.selected_frame().read_register("r14"))
code_type = int(gdb.parse_and_eval("(long)&PyCode_Type"))
rows = []
for index in range(40):
    try:
        code = uint(frame)
        if uint(code + 8) != code_type:
            raise ValueError("Frame does not point to PyCode_Type")
        filename = ascii_metadata(uint(code + 112))
        name = ascii_metadata(uint(code + 120))
        qualname = ascii_metadata(uint(code + 128))
        if not filename.startswith(("/home/piotr/.cache/devbrain-runtime-080/", "<frozen ")):
            raise ValueError("Code filename outside controlled runtime/fixture")
        units = uint(code + 16)
        if not (0 < units < 65536):
            raise ValueError("Code size out of bounds")
        bytecode = bytes(inferior.read_memory(code + 200, units * 2))
        offset = uint(frame + 56) - (code + 200)
        rows.append({"frame_index": index, "filename": filename, "name": name,
                     "qualname": qualname, "firstlineno": uint(code + 68, 4),
                     "code_units": units, "instruction_byte_offset": offset,
                     "adaptive_bytecode_sha256": hashlib.sha256(bytecode).hexdigest(),
                     "instruction_window": list(bytecode[max(0, offset - 8):offset + 10])})
        if index == 0:
            rows[-1]["adaptive_bytecode"] = list(bytecode)
        frame = uint(frame + 8)
        if not frame:
            break
    except (ValueError, gdb.MemoryError) as exc:
        rows.append({"frame_index": index, "error": str(exc)})
        break
output = Path("/home/piotr/.cache/devbrain-runtime-080/evidence/python313-core-code-metadata.json")
output.write_text(json.dumps(rows, indent=2) + "\n")
print(json.dumps(rows))
