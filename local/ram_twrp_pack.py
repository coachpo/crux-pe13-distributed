#!/usr/bin/env python3
"""FIT and newc CPIO helpers for the RAM-only install TWRP (used by build-b-twrp.py).

fit_components() reads a U-Boot FIT's images; cpio_entries()/cpio_archive()
round-trip a newc ramdisk byte for byte; write_fit() packs kernel, DT and a new
ramdisk with the same FIT metadata through U-Boot mkimage.
"""
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import struct
import subprocess

PROFILE = "crux-ram-twrp-profile"


def require(value, message):
    if not value:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def aligned4(value):
    return (value + 3) & ~3


def fdt_nodes(data):
    """Read a complete inline FDT (the FIT is itself an FDT)."""
    magic, total, pos, string_offset, _, _, _, _, string_size, struct_size = struct.unpack_from(
        ">10I", data
    )
    require(magic == 0xD00DFEED and total == len(data), "invalid inline FDT header")
    end = pos + struct_size
    require(end <= total and string_offset + string_size <= total, "invalid FDT ranges")
    strings = data[string_offset:string_offset + string_size]
    nodes, stack = {}, []
    while pos < end:
        tag, = struct.unpack_from(">I", data, pos)
        pos += 4
        if tag == 1:
            zero = data.index(0, pos, end)
            stack.append(data[pos:zero].decode("ascii"))
            path = "/".join(stack)
            require(path not in nodes, f"duplicate FDT node: {path}")
            nodes[path] = {}
            pos = aligned4(zero + 1)
        elif tag == 2:
            stack.pop()
        elif tag == 3:
            size, offset = struct.unpack_from(">II", data, pos)
            pos += 8
            require(pos + size <= end, "invalid FDT property range")
            name = strings[offset:strings.index(0, offset)].decode("ascii")
            props = nodes["/".join(stack)]
            require(name not in props, f"duplicate FDT property: {name}")
            props[name] = data[pos:pos + size]
            pos = aligned4(pos + size)
        elif tag == 4:
            continue
        elif tag == 9:
            require(not stack, "unclosed FDT node")
            return nodes
        else:
            raise ValueError(f"invalid FDT tag: {tag}")
    raise ValueError("missing FDT end tag")


def fit_components(data):
    tree = fdt_nodes(data)
    conf = tree["/configurations"]["default"].rstrip(b"\0").decode("ascii")
    result = {}
    for name in ("kernel", "fdt", "ramdisk"):
        require(tree[f"/configurations/{conf}"][name] == name.encode() + b"\0", "unexpected FIT configuration")
        path = f"/images/{name}"
        component = tree[path]
        verify = tree[path + "/hash-1"]
        require(verify["algo"] == b"sha256\0" and verify["value"].hex() == sha(component["data"]),
                f"invalid {name} component hash")
        result[name] = component
    return result


@dataclass(frozen=True)
class Entry:
    name: str
    fields: tuple
    data: bytes

    def with_data(self, data, mode=None):
        values = list(self.fields)
        values[6] = len(data)
        if mode is not None:
            values[1] = mode
        return replace(self, fields=tuple(values), data=data)

    def emit(self):
        name = self.name.encode() + b"\0"
        header = b"070701" + b"".join(f"{value:08x}".encode() for value in self.fields)
        require(len(name) == self.fields[11] and len(self.data) == self.fields[6], "invalid CPIO metadata")
        return header + name + b"\0" * (-(len(header) + len(name)) % 4) + self.data + b"\0" * (-len(self.data) % 4)


def cpio_entries(archive):
    pos, entries = 0, {}
    while pos + 110 <= len(archive):
        header = archive[pos:pos + 110]
        require(header[:6] == b"070701", "expected newc CPIO")
        fields = tuple(int(header[i:i + 8], 16) for i in range(6, 110, 8))
        size, name_size = fields[6], fields[11]
        name = archive[pos + 110:pos + 110 + name_size]
        require(name.endswith(b"\0") and name.count(b"\0") == 1, "invalid CPIO path")
        name = name[:-1].decode()
        start = aligned4(pos + 110 + name_size)
        pos = aligned4(start + size)
        require(pos <= len(archive) and name not in entries, "invalid or duplicate CPIO entry")
        entries[name] = Entry(name, fields, archive[start:start + size])
        if name == "TRAILER!!!":
            require(not any(archive[pos:]), "unexpected data after the retained newc archive")
            return entries
    raise ValueError("missing newc archive trailer")


def cpio_archive(entries):
    data = b"".join(entry.emit() for entry in entries.values())
    return data + b"\0" * (-len(data) % 512)


def write_fit(work, components, ramdisk, mkimage):
    payload = {"kernel": components["kernel"]["data"], "fdt": components["fdt"]["data"], "ramdisk": ramdisk}
    image_nodes = []
    for role, data in payload.items():
        (work / role).write_bytes(data)
        props = components[role]
        settings = []
        for key in ("type", "arch", "os", "compression", "load", "entry"):
            if key not in props:
                continue
            if key in {"load", "entry"}:
                require(len(props[key]) == 4, "unexpected FIT address width")
                value = f"<0x{int.from_bytes(props[key], 'big'):08x}>"
            else:
                value = json.dumps(props[key].rstrip(b"\0").decode("ascii"))
            settings.append(f"{key} = {value};")
        image_nodes.append(f"""{role} {{
            description = "Crux RAM-only TWRP {role}";
            data = /incbin/("{role}");
            {' '.join(settings)}
            hash-1 {{ algo = "sha256"; }};
        }};""")
    its = work / "twrp.its"
    its.write_text("""/dts-v1/;
/ {
    description = "Crux RAM-only installer TWRP (never flash)";
    #address-cells = <1>;
    images {
""" + "\n".join(image_nodes) + """
    };
    configurations {
        default = "conf";
        conf { kernel = "kernel"; fdt = "fdt"; ramdisk = "ramdisk"; };
    };
};
""")
    fit = work / "twrp-ram-install-only.itb"
    subprocess.run([str(mkimage), "-f", its.name, fit.name], cwd=work, check=True)
    return fit
