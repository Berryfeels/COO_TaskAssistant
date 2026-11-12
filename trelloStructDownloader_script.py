#!/usr/bin/env python3
"""
Downloads Trello's OpenAPI spec and produces a compact index of its endpoints,
grouped by tag (Cards, Lists, Boards, ...).

Usage:
    python trello_index.py                  # downloads the spec and creates trello_index.txt
    python trello_index.py spec.json        # uses a local, already downloaded file
    python trello_index.py --tag Cards      # prints only one tag (handy for pasting into a prompt)

Output: trello_index.txt, one line per endpoint:
    POST /cards  (idList*, name, desc, due, idMembers, ...)
where * = required parameter.
"""
import json
import sys
import urllib.request
from collections import defaultdict

SPEC_URL = "https://dac-static.atlassian.com/cloud/trello/swagger.v3.json"
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def load_spec(path=None):
    if path:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    req = urllib.request.Request(SPEC_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def resolve(spec, param):
    """Risolve eventuali $ref a #/components/parameters/..."""
    ref = param.get("$ref")
    if not ref:
        return param
    node = spec
    for part in ref.lstrip("#/").split("/"):
        node = node.get(part, {})
    return node


def build_index(spec):
    by_tag = defaultdict(list)
    total = 0
    for path, item in spec.get("paths", {}).items():
        shared = item.get("parameters", [])  # parametri a livello di path
        for method, op in item.items():
            if method.lower() not in HTTP_METHODS:
                continue
            total += 1
            params = []
            seen = set()
            for p in shared + op.get("parameters", []):
                p = resolve(spec, p)
                name = p.get("name")
                if not name or name in seen:
                    continue
                if name in ("key", "token"):  # auth, non interessa all'LLM
                    continue
                seen.add(name)
                params.append(name + ("*" if p.get("required") else ""))
            # i parametri nel path ({id}) sono gia' nel path: li tolgo dall'elenco
            params = [p for p in params if "{" + p.rstrip("*") + "}" not in path]
            line = f"{method.upper()} {path}  ({', '.join(params)})"
            summary = op.get("summary", "").strip()
            if summary:
                line += f"  # {summary}"
            for tag in op.get("tags") or ["Untagged"]:
                by_tag[tag].append(line)
    return by_tag, total


def render(by_tag, only_tag=None):
    out = []
    for tag in sorted(by_tag):
        if only_tag and tag.lower() != only_tag.lower():
            continue
        out.append(f"[{tag}]")
        out.extend(sorted(by_tag[tag]))
        out.append("")
    return "\n".join(out)


def main():
    args = sys.argv[1:]
    only_tag = None
    if "--tag" in args:
        i = args.index("--tag")
        only_tag = args[i + 1]
        del args[i:i + 2]
    spec = load_spec(args[0] if args else None)

    by_tag, total = build_index(spec)
    text = render(by_tag, only_tag)

    if only_tag:
        print(text)
        return

    with open("trello_index.txt", "w", encoding="utf-8") as f:
        f.write(text)

    # Verifica di completezza
    listed = sum(len(v) for v in by_tag.values())
    print(f"Endpoint nella spec: {total}")
    print(f"Righe nell'indice (un endpoint con piu' tag compare piu' volte): {listed}")
    print("Tag trovati:", ", ".join(sorted(by_tag)))
    print("Scritto: trello_index.txt")


if __name__ == "__main__":
    main()