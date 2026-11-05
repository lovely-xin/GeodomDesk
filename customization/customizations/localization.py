"""Append feature keys without changing existing translator-owned entries."""
import re


def complete_catalog(source, baseline, files):
    name = "src/lang/cn.rs"
    keys = re.compile(r'\("(?:\\.|[^"\\])*",')
    original = set(keys.findall(baseline[name].decode("utf-8")))
    additions = list(dict.fromkeys(
        key for key in keys.findall(files[name].decode("utf-8")) if key not in original
    ))
    for path in sorted((source / "src/lang").glob("*.rs")):
        if path.name in {"en.rs", "mod.rs"}:
            continue
        name = path.relative_to(source).as_posix()
        text = files.get(name, path.read_bytes()).decode("utf-8").replace("\r\n", "\n")
        present = set(keys.findall(text))
        missing = [key for key in additions if key not in present]
        if missing:
            end = text.rindex("    ]")
            text = text[:end] + "".join('        ' + key + ' ""),\n' for key in missing) + text[end:]
            files[name] = text.encode("utf-8")
