"""Exact edits against the reviewed upstream baseline."""

def replace_once(text, before, after):
    if text.count(before) != 1:
        raise ValueError(f"Upstream changed; review required: {before[:85]!r}")
    return text.replace(before, after, 1)


class SourceEdits(dict):
    def replace(self, name, before, after):
        self[name] = replace_once(self[name], before, after)
