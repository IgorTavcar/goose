#!/usr/bin/env python3
"""Validate this static wiki and its recorded source snapshot (standard library only)."""

import argparse
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit


class Document(HTMLParser):
    VOID = set('area base br col embed hr img input link meta param source track wbr'.split())

    def __init__(self, path):
        super().__init__(convert_charrefs=True)
        self.path = path
        self.stack = []
        self.ids = set()
        self.links = []
        self.excerpts = []
        self.active_excerpt = None
        self.counts = {}
        self.errors = []

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        self.counts[tag] = self.counts.get(tag, 0) + 1
        if 'id' in attrs:
            if attrs['id'] in self.ids:
                self.errors.append(f'duplicate id: {attrs["id"]}')
            self.ids.add(attrs['id'])
        for key in ('href', 'src'):
            if key in attrs:
                self.links.append(attrs[key])
        if tag == 'code' and 'data-source' in attrs:
            self.active_excerpt = {'path': attrs['data-source'], 'start': int(attrs['data-start']),
                                   'end': int(attrs['data-end']), 'text': ''}
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag == 'code' and self.active_excerpt is not None:
            self.excerpts.append(self.active_excerpt)
            self.active_excerpt = None
        if tag in self.VOID:
            self.errors.append(f'unexpected void closing tag: {tag}')
        elif not self.stack or self.stack[-1] != tag:
            self.errors.append(f'unbalanced closing tag: {tag}; stack={self.stack[-3:]}')
        else:
            self.stack.pop()

    def handle_data(self, text):
        if self.active_excerpt is not None:
            self.active_excerpt['text'] += text


def validate():
    directory = Path(__file__).resolve().parent
    root = directory.parents[1]
    manifest = json.loads((directory / 'manifest.json').read_text())
    revision = manifest['source_revision']
    cache = {}

    def historical_source(path):
        if path not in cache:
            cache[path] = subprocess.check_output(['git', 'show', f'{revision}:{path}'], cwd=root)
        return cache[path]

    errors = []
    documents = {}
    for file in sorted(directory.glob('*.html')):
        document = Document(file)
        content = file.read_text()
        document.feed(content)
        document.close()
        if document.stack:
            document.errors.append(f'unclosed tags: {document.stack}')
        if not content.lower().startswith('<!doctype html>'):
            document.errors.append('missing HTML5 doctype')
        for tag in ('html', 'head', 'body', 'main', 'h1', 'title'):
            if document.counts.get(tag) != 1:
                document.errors.append(f'expected exactly one {tag}')
        errors.extend(f'{file.name}: {message}' for message in document.errors)
        documents[file.resolve()] = document

    expected_pages = {entry['slug'] + '.html' for entry in manifest['pages']}
    if expected_pages != {p.name for p in documents}:
        errors.append('page set differs from manifest')

    local_links = 0
    source_links = 0
    for file, document in documents.items():
        for link in document.links:
            parts = urlsplit(link)
            if parts.scheme:
                prefix = f'https://github.com/IgorTavcar/goose/blob/{revision}/'
                if link.startswith(prefix):
                    path = unquote(parts.path.split(f'/blob/{revision}/', 1)[1])
                    lines = historical_source(path).decode().splitlines()
                    if path not in {x['path'] for x in manifest['sources']}:
                        errors.append(f'unlisted source: {path}')
                    if parts.fragment and not re.fullmatch(r'L[1-9][0-9]*', parts.fragment):
                        errors.append(f'invalid source fragment: {link}')
                    elif parts.fragment and int(parts.fragment[1:]) > len(lines):
                        errors.append(f'source line out of range: {link}')
                    source_links += 1
                continue
            if parts.netloc:
                continue
            target = (file.parent / unquote(parts.path)).resolve() if parts.path else file
            if not target.exists():
                errors.append(f'{file.name}: missing local target {link}')
            elif parts.fragment and target in documents and unquote(parts.fragment) not in documents[target].ids:
                errors.append(f'{file.name}: missing fragment {link}')
            local_links += 1

    for entry in manifest['sources']:
        digest = hashlib.sha256(historical_source(entry['path'])).hexdigest()
        if digest != entry['sha256']:
            errors.append(f'source hash mismatch: {entry["path"]}')

    actual_excerpts = []
    for document in documents.values():
        for excerpt in document.excerpts:
            lines = historical_source(excerpt['path']).decode().splitlines()
            expected = '\n'.join(lines[excerpt['start']-1:excerpt['end']])
            if excerpt['text'] != expected:
                errors.append(f'excerpt differs from source: {excerpt["path"]}:{excerpt["start"]}')
            actual_excerpts.append({key: value for key, value in excerpt.items() if key != 'text'} |
                                   {'sha256': hashlib.sha256(excerpt['text'].encode()).hexdigest()})
    key = lambda entry: (entry['path'], entry['start'], entry['end'], entry['sha256'])
    if sorted(actual_excerpts, key=key) != sorted(manifest['excerpts'], key=key):
        errors.append('excerpt set differs from manifest')

    declared_methods = []
    for entry in manifest['sources']:
        if entry['path'].startswith('crates/goose-sdk-types/src/'):
            text = historical_source(entry['path']).decode()
            for attribute in re.findall(r'#\[request\((.*?)\)\]', text, re.S):
                declared_methods.append(re.search(r'method\s*=\s*"([^"]+)"', attribute).group(1))
    if len(declared_methods) != manifest['custom_request_count'] or len(set(declared_methods)) != len(declared_methods):
        errors.append('custom request inventory count or uniqueness mismatch')
    api_text = (directory / 'apis.html').read_text()
    for method in declared_methods:
        if '<td>' + method + '</td>' not in api_text:
            errors.append(f'method missing from API table: {method}')

    result = {'status': 'passed' if not errors else 'failed', 'pages': len(documents),
              'local_links': local_links, 'source_links': source_links,
              'source_files': len(manifest['sources']), 'exact_excerpts': len(actual_excerpts),
              'declared_client_to_agent_methods': len(declared_methods), 'errors': errors}
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, help='Write structural results as JSON to this path.')
    arguments = parser.parse_args()
    result = validate()
    if arguments.report:
        arguments.report.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result['status'] == 'passed' else 1)
