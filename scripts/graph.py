#!/usr/bin/env python3
"""Stream, index, and slice an existing Ninja graph without running its actions.

The SQLite index refers to byte ranges in the original graph files. A slice
copies selected actions and their lexical variable/rule scopes verbatim. Paths
are never rewritten, so workers must recreate the recorded source/output roots.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import heapq
import json
import os
from pathlib import Path
import posixpath
import re
import shlex
import sqlite3
import sys
import tempfile


def soong_writefile_literal(command, output):
    """Recover the literal written by Soong's unsharded writeFile rule.

    defs.go escapes existing backslashes before replacing newlines. Decode
    that outer layer once; protobuf string escaping is a separate layer.
    """
    args=shlex.split(command)
    if len(args) != 4 or posixpath.basename(args[0]) != 'bash' or args[1] != '-c':
        raise ValueError('unsupported Soong writeFile command shape')
    if shlex.split(args[2]) != ['echo','-e','-n','$0','>',output]:
        raise ValueError('unsupported Soong writeFile literal writer')
    literal=args[3];decoded=[];index=0
    while index < len(literal):
        char=literal[index];index+=1
        if char == '\\':
            if index == len(literal) or literal[index] not in ('\\','n'):
                raise ValueError('unsupported Soong writeFile outer escape')
            char='\n' if literal[index] == 'n' else '\\';index+=1
        decoded.append(char)
    return literal,''.join(decoded)


def sbox_textproto(text):
    """Read the generated string/message subset used by Sbox manifests.

    This is an explicit capture format boundary, not a general protobuf
    decoder. Invalid or unsupported generated syntax fails before any OUT
    payload is opened. Escaped non-UTF8 proto2 strings are unsupported here.
    """
    index=0
    def whitespace():
        nonlocal index
        while index < len(text):
            if text[index].isspace():index+=1
            elif text[index] == '#':
                end=text.find('\n',index);index=len(text) if end < 0 else end+1
            else:break
    def string():
        nonlocal index
        quote=text[index];index+=1;data=bytearray()
        escapes={'a':7,'b':8,'f':12,'n':10,'r':13,'t':9,'v':11,
                 '\\':92,'"':34,"'":39,'?':63}
        while index < len(text):
            char=text[index];index+=1
            if char == quote:
                try:return data.decode('utf-8')
                except UnicodeDecodeError as error:
                    raise ValueError('unsupported non-UTF8 Sbox string') from error
            if char in ('\n','\x00'):
                raise ValueError('invalid raw newline or NUL in Sbox string')
            if char != '\\':data.extend(char.encode('utf-8'));continue
            if index == len(text):break
            char=text[index];index+=1
            if char in escapes:data.append(escapes[char]);continue
            if char in '01234567':
                value=char
                while len(value) < 3 and index < len(text) and text[index] in '01234567':
                    value+=text[index];index+=1
                if int(value,8) > 255:raise ValueError('invalid Sbox octal escape')
                data.append(int(value,8));continue
            if char in ('x','u','U'):
                maximum=2 if char == 'x' else (4 if char == 'u' else 8)
                value=''
                while len(value) < maximum and index < len(text) and text[index] in '0123456789abcdefABCDEF':
                    value+=text[index];index+=1
                if not value or (char != 'x' and len(value) != maximum):
                    raise ValueError('invalid Sbox hexadecimal escape')
                code=int(value,16)
                if char == 'x':data.append(code);continue
                if 0xD800 <= code <= 0xDBFF:
                    following=text[index:index+6]
                    if not re.fullmatch(r'\\u[0-9a-fA-F]{4}',following):
                        raise ValueError('invalid Sbox surrogate pair')
                    low=int(following[2:],16)
                    if not 0xDC00 <= low <= 0xDFFF:raise ValueError('invalid Sbox surrogate pair')
                    index+=6;code=0x10000+((code-0xD800)<<10)+(low-0xDC00)
                try:data.extend(chr(code).encode('utf-8'))
                except (ValueError,UnicodeEncodeError) as error:
                    raise ValueError('invalid Sbox Unicode escape') from error
                continue
            raise ValueError('unsupported Sbox string escape')
        raise ValueError('unterminated Sbox string')
    def message(end=None):
        nonlocal index
        result=collections.defaultdict(list)
        while True:
            whitespace()
            if index == len(text):
                if end:raise ValueError('unterminated Sbox message')
                return dict(result)
            if end and text[index] == end:
                index+=1;return dict(result)
            match=re.match(r'[A-Za-z_][A-Za-z0-9_]*',text[index:])
            if not match:raise ValueError('unsupported Sbox field syntax')
            name=match.group();index+=len(name);whitespace()
            colon=index < len(text) and text[index] == ':'
            if colon:index+=1;whitespace()
            if index == len(text):raise ValueError('missing Sbox field value')
            if text[index] not in ('{','<') and not colon:
                raise ValueError('missing colon before Sbox scalar value')
            if text[index] in ('{','<'):
                closing='}' if text[index] == '{' else '>';index+=1
                value=message(closing)
            elif text[index] in ('"',"'"):
                value=string();whitespace()
                while index < len(text) and text[index] in ('"',"'"):
                    value+=string();whitespace()
            else:
                match=re.match(r'(?:true|false)(?![A-Za-z0-9_])',text[index:])
                if not match:raise ValueError('unsupported Sbox scalar value')
                value=match.group() == 'true';index+=len(match.group())
            result[name].append(value);whitespace()
            if index < len(text) and text[index] in (',',';'):index+=1
    return message()


def native_generated_include_roots(edge, source_root, out_root):
    source_root, out_root = map(lambda p: posixpath.normpath(str(p)), (source_root, out_root))
    search_flags = ('-isystem', '-iquote', '-idirafter', '-I')
    file_flags = ('-include', '-imacros')
    compiler = re.compile(r'^(?:[\w.-]+-)?(?:clang(?:\+\+)?|gcc|g\+\+|cc|c\+\+)(?:-\d+(?:\.\d+)*)?$')
    include_roots, forced_files = set(), set()

    def absolute(path, cwd):
        return posixpath.normpath(path if path.startswith('/') else posixpath.join(cwd, path))

    def contexts(command, cwd):
        lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|()<>')
        lexer.whitespace_split, lexer.commenters = True, ''
        segments, segment = [], []
        for token in lexer:
            if token in ('&&', '||', ';', '|', '&', '(', ')'):
                if segment: segments.append(segment); segment = []
            else: segment.append(token)
        if segment: segments.append(segment)
        for args in segments:
            i, local_cwd = 0, cwd
            while i < len(args):
                token, name = args[i], posixpath.basename(args[i])
                if re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', token) or name in ('exec', 'command', 'do', 'then', 'else', 'time'):
                    i += 1
                elif name == 'env':
                    i += 1
                    while i < len(args) and args[i].startswith('-'):
                        option = args[i]; i += 1
                        if option in ('-u', '--unset'): i += 1
                        elif option in ('-C', '--chdir') and i < len(args):
                            local_cwd = absolute(args[i], local_cwd); i += 1
                elif name in ('ccache', 'sccache', 'distcc'):
                    i += 1
                    while i < len(args) and args[i].startswith('-'): i += 1
                else: break
            if i == len(args): continue
            name, argv = posixpath.basename(args[i]), args[i + 1:]
            if name == 'cd' and argv:
                cwd = absolute(argv[0], cwd)
            elif name in ('bash', 'sh', 'dash'):
                for j, option in enumerate(argv):
                    if option.startswith('-') and not option.startswith('--') and 'c' in option[1:]:
                        if j + 1 < len(argv): yield from contexts(argv[j + 1], local_cwd)
                        break
            elif name != 'llvm-rs-cc' and (compiler.fullmatch(name) or name in ('clang-tidy', 'header-abi-dumper')):
                yield name, argv, local_cwd

    def expand_responses(argv, cwd, active=()):
        result, unknown = [], []
        declared = edge.get('rspfile')
        declared = absolute(declared, source_root) if declared else None
        for token in argv:
            if not token.startswith('@'):
                result.append(token); continue
            path = absolute(token[1:], cwd)
            if path != declared or edge.get('rspfile_content') is None:
                unknown.append(token); result.append(token); continue
            if path in active: raise ValueError('recursive compiler response file: ' + path)
            expanded, nested = expand_responses(shlex.split(edge['rspfile_content']), cwd, active + (path,))
            result.extend(expanded); unknown.extend(nested)
        return result, unknown

    for name, argv, cwd in contexts(edge.get('command', ''), source_root):
        argv, unknown = expand_responses(argv, cwd)
        if name in ('clang-tidy', 'header-abi-dumper'):
            if unknown: raise ValueError('unknown native frontend response file: ' + unknown[0])
            if '--' not in argv: continue
            argv = argv[argv.index('--') + 1:]
        elif not any(mode in argv for mode in ('-c', '-E', '-S')):
            continue
        if unknown: raise ValueError('unknown native compiler response file: ' + unknown[0])
        i = 0
        while i < len(argv):
            token = argv[i]
            for flag in search_flags + file_flags:
                value = None
                if token == flag:
                    if i + 1 == len(argv): raise ValueError('missing compiler operand for ' + flag)
                    i += 1; value = argv[i]
                elif token.startswith(flag) and not (flag == '-include' and token.startswith('-include-pch')):
                    value = token[len(flag):].lstrip('=')
                else: continue
                if value and not value.startswith('-') and '$' not in value:
                    path = absolute(value, cwd)
                    if path == out_root or path.startswith(out_root + '/'):
                        (include_roots if flag in search_flags else forced_files).add(path)
                break
            i += 1
    return {'include_roots': sorted(include_roots), 'forced_files': sorted(forced_files)}


def expand(value, variables):
    """Expand a Ninja EvalString, including escaped separators and continuations."""
    if '$' not in value:
        return value
    result = []
    i = 0
    while i < len(value):
        if value[i] != '$':
            end=value.find('$',i)
            if end < 0:
                result.append(value[i:]); break
            result.append(value[i:end]); i=end; continue
        i += 1
        if i == len(value):
            raise ValueError('unterminated Ninja escape')
        ch = value[i]
        if ch in '$ :':
            result.append(ch); i += 1
        elif ch in '\r\n':
            if ch == '\r' and value[i:i + 2] == '\r\n':
                i += 1
            i += 1
            while i < len(value) and value[i] == ' ':
                i += 1
        elif ch == '{':
            end = value.find('}', i)
            if end < 0:
                raise ValueError('unterminated Ninja variable')
            result.append(variables.get(value[i + 1:end], '')); i = end + 1
        else:
            match = re.match(r'[A-Za-z0-9_]+', value[i:])
            if not match:
                raise ValueError('invalid Ninja escape: $' + ch)
            name = match.group(0)
            result.append(variables.get(name, '')); i += len(name)
    return ''.join(result)


def logical(raw):
    return re.sub(r'\$\r?\n *', '', raw)


def path_tokens(value, variables):
    """Tokenize before expansion: an escaped space remains inside one pathname."""
    if '$' not in value:
        return re.findall(r'\|\||\|@|[:|]|[^\s:|]+',value)
    result = []
    token = []
    i = 0
    def flush():
        if token:
            path = expand(''.join(token), variables)
            if path:
                result.append(path)
            token.clear()
    while i < len(value):
        ch = value[i]
        if ch == '$':
            if i + 1 >= len(value):
                raise ValueError('unterminated Ninja path escape')
            if value[i + 1] == '{':
                end = value.find('}', i + 2)
                if end < 0:
                    raise ValueError('unterminated Ninja variable')
                token.append(value[i:end + 1]); i = end + 1
            elif value[i + 1] in '$ :':
                token.append(value[i:i + 2]); i += 2
            else:
                match = re.match(r'\$[A-Za-z0-9_]+', value[i:])
                if not match:
                    raise ValueError('invalid Ninja path escape')
                token.append(match.group(0)); i += len(match.group(0))
        elif ch.isspace():
            flush(); i += 1
        elif ch in ':|':
            flush()
            if value[i:i + 2] in ('||', '|@'):
                result.append(value[i:i + 2]); i += 2
            else:
                result.append(ch); i += 1
        else:
            end = re.search(r'[\s:$|]',value[i:])
            length = end.start() if end else len(value) - i
            token.append(value[i:i + length]); i += length
    flush()
    return result


def parse_build(line, variables):
    tokens = path_tokens(line[6:], variables)
    split = tokens.index(':')
    outputs = [x for x in tokens[:split] if x != '|']
    if len(tokens) < split + 2:
        raise ValueError('missing Ninja rule')
    rule = tokens[split + 1]
    deps = []
    kind = 'explicit'
    for token in tokens[split + 2:]:
        if token in ('|', '||', '|@'):
            kind = {'|': 'implicit', '||': 'order', '|@': 'validation'}[token]
        else:
            deps.append((token, kind))
    return rule, outputs, deps


def bindings(raw):
    values = {}
    for line in logical(raw).splitlines()[1:]:
        if line[:1].isspace() and '=' in line and not line.lstrip().startswith('#'):
            key,value = line.lstrip().split('=',1)
            values[key.strip()] = value.lstrip(' ')
    return values


def edge_environment(raw, parent):
    # Build path EvalStrings are evaluated after the action's bindings are
    # parsed. Soong uses this for tool dependencies such as `| ${cmd}`.
    environment=collections.ChainMap({},parent)
    for name,value in bindings(raw).items():
        environment[name]=expand(value,environment)
    return environment


def statements(path):
    """Yield (byte offset, byte length, raw text) in bounded memory."""
    with open(path, 'rb') as stream:
        start = 0
        block = bytearray()
        continued = False
        while True:
            position = stream.tell()
            line = stream.readline()
            if not line:
                if block:
                    yield start, len(block), block.decode('utf-8')
                return
            # Bindings following a rule/build belong to that statement. Blank
            # lines and comments are retained with the preceding declaration.
            new = bool(line.strip()) and not line.startswith((b' ', b'\t', b'#'))
            if block and new and not continued:
                yield start, len(block), block.decode('utf-8')
                block = bytearray(); start = position
            if not block:
                start = position
            block.extend(line)
            tail = line.rstrip(b'\r\n')
            dollars = len(tail) - len(tail.rstrip(b'$'))
            continued = bool(dollars % 2)


SCHEMA = '''
CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
CREATE TABLE files(id INTEGER PRIMARY KEY,path TEXT,scope INTEGER,parent INTEGER);
CREATE TABLE statements(id INTEGER PRIMARY KEY,file INTEGER,ordinal INTEGER,kind TEXT,
 name TEXT,offset INTEGER,length INTEGER,ref INTEGER);
CREATE TABLE edges(id INTEGER PRIMARY KEY,rule TEXT,outputs TEXT,deps TEXT);
CREATE TABLE outputs(path TEXT PRIMARY KEY,edge INTEGER);
CREATE INDEX statements_file ON statements(file,ordinal);
'''


class Indexer:
    def __init__(self, database, source_root):
        self.db = sqlite3.connect(database)
        self.db.executescript(SCHEMA)
        self.db.execute('PRAGMA journal_mode=OFF')
        self.db.execute('PRAGMA synchronous=OFF')
        self.source_root = str(Path(source_root).resolve())
        self.files = 0; self.stmts = 0; self.scopes = 0; self.edges = 0
        self.active = set()
        self.duplicates = 0
        self.file_stats = {}

    def parse(self, path, parent=None, env=None, rules=None, scope=None):
        path = str(Path(path).resolve())
        if path in self.active:
            raise ValueError('recursive Ninja include: ' + path)
        self.active.add(path)
        stat = os.stat(path)
        identity = {'size':stat.st_size,'mtime_ns':stat.st_mtime_ns}
        if path in self.file_stats and self.file_stats[path] != identity:
            raise ValueError('Ninja input changed during indexing: '+path)
        self.file_stats[path] = identity
        self.files += 1; file_id = self.files
        if scope is None:
            self.scopes += 1; scope = self.scopes
        env = {} if env is None else env
        rules = {} if rules is None else rules
        self.db.execute('INSERT INTO files VALUES (?,?,?,?)', (file_id,path,scope,parent))
        for ordinal, (offset, length, raw) in enumerate(statements(path)):
            self.stmts += 1; stmt_id = self.stmts
            first = logical(raw).splitlines()[0].lstrip()
            kind = 'other'; name = None; ref = None
            if first.startswith('build '):
                kind = 'build'
                path_env=edge_environment(raw,env) if '$' in first else env
                rule, outputs, deps = parse_build(first,path_env)
                if rule != 'phony' and rule not in rules:
                    raise ValueError(f'{path}:{offset}: unknown rule {rule}')
                ref = rules.get(rule)
                self.db.execute('INSERT INTO edges VALUES (?,?,?,?)',
                    (stmt_id,rule,json.dumps(outputs),json.dumps(deps)))
                for output in outputs:
                    # Ninja with dupbuild=warn keeps the first generating edge.
                    cur = self.db.execute('INSERT OR IGNORE INTO outputs VALUES (?,?)', (output,stmt_id))
                    self.duplicates += cur.rowcount == 0
                self.edges += 1
            elif first.startswith('rule '):
                kind = 'rule'; name = first[5:].strip(); rules[name] = stmt_id
            elif first.startswith('pool '):
                kind = 'pool'; name = first[5:].strip()
            elif first.startswith(('include ', 'subninja ')):
                kind, argument = first.split(None, 1)
                nested = expand(argument, env)
                if not os.path.isabs(nested):
                    nested = os.path.join(self.source_root, nested)
                ref = self.parse(nested, file_id,
                    env.copy() if kind == 'subninja' else env,
                    rules.copy() if kind == 'subninja' else rules,
                    None if kind == 'subninja' else scope)
            elif first.startswith('default '):
                kind = 'default'
            elif '=' in first and not first.startswith('#'):
                kind = 'variable'
                name, value = first.split('=', 1)
                name = name.strip(); env[name] = expand(value.lstrip(' '), env)
            self.db.execute('INSERT INTO statements VALUES (?,?,?,?,?,?,?,?)',
                (stmt_id,file_id,ordinal,kind,name,offset,length,ref))
            if self.stmts % 10000 == 0:
                self.db.commit()
                print(f'indexed {self.edges:,} actions', file=sys.stderr, flush=True)
        self.active.remove(path)
        stat = os.stat(path)
        if identity != {'size':stat.st_size,'mtime_ns':stat.st_mtime_ns}:
            raise ValueError('Ninja input changed during indexing: '+path)
        self.db.commit()
        return file_id


def index_graph(ninja, source_root, database):
    database = Path(database)
    database.parent.mkdir(parents=True, exist_ok=True)
    if database.exists():
        raise ValueError('index already exists: ' + str(database))
    index = Indexer(database, source_root)
    try:
        entry = index.parse(ninja)
        values = {'schema_version': 1, 'source_root': index.source_root,
                  'ninja': str(Path(ninja).resolve()), 'entry_file': entry,
                  'edge_count': index.edges, 'duplicates': index.duplicates,
                  'file_stats':index.file_stats}
        index.db.executemany('INSERT INTO meta VALUES (?,?)',
                            [(k,json.dumps(v)) for k,v in values.items()])
        index.db.commit()
        return values
    finally:
        index.db.close()


def chunks(values, size=400):
    values = list(values)
    for start in range(0,len(values),size):
        yield values[start:start + size]


def augment_graph(database, profile, replace_profile=False):
    """Add reviewed undeclared output trees and atomic action groups to an index."""
    db = sqlite3.connect(database)
    try:
        meta = {key:json.loads(value) for key,value in db.execute('SELECT * FROM meta')}
        if meta.get('ownership_profile'):
            if meta['ownership_profile'] != profile:
                if not replace_profile:
                    raise ValueError('index already has a different ownership profile')
                for directory,edge in db.execute('SELECT path,edge FROM side_output_dirs').fetchall():
                    outputs=json.loads(db.execute('SELECT outputs FROM edges WHERE id=?',(edge,)).fetchone()[0])
                    outputs.remove(directory)
                    db.execute('UPDATE edges SET outputs=? WHERE id=?',(json.dumps(outputs),edge))
                    db.execute('DELETE FROM outputs WHERE path=? AND edge=?',(directory,edge))
                db.execute('DELETE FROM side_output_dirs')
                db.execute("DELETE FROM meta WHERE key IN ('ownership_profile','atomic_groups')")
            else:
                return profile
        else:
            db.execute('CREATE TABLE side_output_dirs(path TEXT PRIMARY KEY,edge INTEGER)')
        for declaration in profile.get('side_output_dirs',[]):
            if not declaration.get('rationale') or not declaration.get('evidence'):
                raise ValueError('side output ownership needs rationale and source evidence')
            row = db.execute('SELECT edge FROM outputs WHERE path=?',(declaration['producer'],)).fetchone()
            if row is None:
                raise ValueError('unknown side output owner: '+declaration['producer'])
            edge = row[0]
            outputs = json.loads(db.execute('SELECT outputs FROM edges WHERE id=?',(edge,)).fetchone()[0])
            for directory in declaration['dirs']:
                existing = db.execute('SELECT edge FROM outputs WHERE path=?',(directory,)).fetchone()
                if existing:
                    raise ValueError('side output is already declared: '+directory)
                db.execute('INSERT INTO side_output_dirs VALUES (?,?)',(directory,edge))
                db.execute('INSERT OR IGNORE INTO outputs VALUES (?,?)',(directory,edge))
                if directory not in outputs:
                    outputs.append(directory)
            db.execute('UPDATE edges SET outputs=? WHERE id=?',(json.dumps(outputs),edge))
        groups=[]
        for group in profile.get('groups',[]):
            members=[]
            for output in group:
                row=db.execute('SELECT edge FROM outputs WHERE path=?',(output,)).fetchone()
                if row is None:
                    raise ValueError('unknown atomic group output: '+output)
                members.append(row[0])
            groups.append(sorted(set(members)))
        db.executemany('INSERT INTO meta VALUES (?,?)',[
            ('ownership_profile',json.dumps(profile)),('atomic_groups',json.dumps(groups))])
        db.commit()
        return profile
    finally:
        db.close()


def append_implicit_outputs(raw, extra):
    if not extra:
        return raw
    text=logical(raw)
    first,separator,rest=text.partition('\n')
    # Ownership profiles contain ordinary absolute output paths. Find the rule
    # separator while skipping escaped colons in pre-existing Ninja paths.
    i=6
    while i < len(first):
        if first[i]=='$':
            if i+1 < len(first) and first[i+1]=='{':
                end=first.find('}',i+2)
                i=end+1
            else:
                i+=2
        elif first[i]==':':
            break
        else:
            i+=1
    if i==len(first):
        raise ValueError('build declaration has no rule separator')
    prefix=first[:i].rstrip()
    implicit=' ' if '|' in prefix else ' | '
    first=prefix+implicit+' '.join(ninja_escape(path) for path in extra)+first[i:]
    return first+separator+rest


def append_order_only_inputs(raw, extra):
    if not extra:
        return raw
    first,separator,rest=logical(raw).partition('\n')
    before,validation_separator,validation=first.partition(' |@ ')
    before += (' ' if ' || ' in before else ' || ')+' '.join(ninja_escape(path) for path in extra)
    return before+validation_separator+validation+separator+rest


def adapt_graph(database, profile, replace_profile=False):
    graph=Graph(database)
    for adaptation in profile.get('command_adaptations',[]):
        if not adaptation.get('rationale') or not adaptation.get('evidence'):
            raise ValueError('runtime adaptation needs rationale and source evidence')
        producer=adaptation['producer'];owner=graph.producers([producer]).get(producer)
        if owner is None:
            raise ValueError('unknown adaptation producer: '+producer)
        rule=graph.db.execute('SELECT * FROM statements WHERE id=?',(owner,)).fetchone() if adaptation.get('scope') == 'edge' else graph.db.execute('SELECT * FROM statements WHERE id=(SELECT ref FROM statements WHERE id=?)',(owner,)).fetchone()
        handles={}
        try:
            count=graph.raw(rule,handles).count(adaptation['replace'])
        finally:
            for handle in handles.values():handle.close()
        if count != adaptation['expected_matches']:
            raise ValueError(f'adaptation matches {count}, expected {adaptation["expected_matches"]}: {producer}')
    graph.db.close()
    db=sqlite3.connect(database)
    try:
        existing=db.execute('SELECT value FROM meta WHERE key=?',('runtime_adaptations',)).fetchone()
        if existing and json.loads(existing[0]) != profile and not replace_profile:
            raise ValueError('index already has a different runtime adaptation profile')
        db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('runtime_adaptations',json.dumps(profile)))
        db.commit()
    finally:
        db.close()
    return profile


class Graph:
    def __init__(self, database):
        self.db = sqlite3.connect(f'file:{Path(database).resolve()}?mode=ro', uri=True)
        self.db.row_factory = sqlite3.Row
        self.meta = {r['key']:json.loads(r['value']) for r in self.db.execute('SELECT * FROM meta')}
        self.common_rows = None
        self.side_output_dirs = {}
        self.group_export_dirs = {}
        self.rule_adaptations = collections.defaultdict(list)
        self.edge_adaptations = collections.defaultdict(list)
        if self.meta.get('ownership_profile'):
            self.side_output_dirs = {r['path']:r['edge'] for r in self.db.execute('SELECT * FROM side_output_dirs')}
            for declaration in self.meta['ownership_profile'].get('group_export_dirs',[]):
                owner=self.producers([declaration['producer']])[declaration['producer']]
                for directory in declaration['dirs']:
                    self.group_export_dirs[directory]=owner
        for adaptation in self.meta.get('runtime_adaptations',{}).get('command_adaptations',[]):
            owner=self.producers([adaptation['producer']])[adaptation['producer']]
            if adaptation.get('scope') == 'edge':
                self.edge_adaptations[owner].append(adaptation)
            else:
                rule=self.db.execute('SELECT ref FROM statements WHERE id=?',(owner,)).fetchone()['ref']
                self.rule_adaptations[rule].append(adaptation)

    def producers(self, paths, directory_fallback=True):
        found = {}
        for batch in chunks(paths):
            marks = ','.join('?' for _ in batch)
            found.update((r['path'],r['edge']) for r in self.db.execute(
                f'SELECT path,edge FROM outputs WHERE path IN ({marks})',batch))
        if directory_fallback:
            for path in set(paths)-set(found):
                for directory,edge in self.side_output_dirs.items():
                    if path.startswith(directory.rstrip('/')+'/'):
                        found[path]=edge
                        break
        return found

    def closure(self, targets, external=(), include_validations=True):
        external = set(external)
        initial = self.producers(targets)
        missing = set(targets) - set(initial)
        if missing:
            raise ValueError('unknown targets: ' + ', '.join(sorted(missing)))
        pending = set(initial.values()); selected = set(); leaves = set(); cuts = set()
        while pending:
            frontier = pending - selected
            if not frontier:
                break
            for group in self.meta.get('atomic_groups',[]):
                if frontier & set(group):
                    frontier.update(set(group)-selected)
            selected.update(frontier)
            dependencies = set()
            for batch in chunks(frontier):
                marks = ','.join('?' for _ in batch)
                for row in self.db.execute(f'SELECT deps FROM edges WHERE id IN ({marks})',batch):
                    dependencies.update(path for path,kind in json.loads(row['deps'])
                                        if include_validations or kind != 'validation')
            cut_paths = dependencies & external
            exact_producers=self.producers(dependencies,directory_fallback=False)
            for directory in external & set(self.side_output_dirs):
                owner=self.side_output_dirs[directory]
                cut_paths.update(path for path in dependencies
                    if path.startswith(directory.rstrip('/')+'/')
                    and (path not in exact_producers or exact_producers[path] == owner))
            cuts.update(cut_paths)
            dependencies -= cut_paths
            producers = self.producers(dependencies)
            leaves.update(dependencies - set(producers))
            pending = set(producers.values()) - selected
        outputs = []; phony = []
        for batch in chunks(selected):
            marks = ','.join('?' for _ in batch)
            for row in self.db.execute(f'SELECT rule,outputs FROM edges WHERE id IN ({marks})',batch):
                (phony if row['rule'] == 'phony' else outputs).extend(json.loads(row['outputs']))
        return {'edge_ids':sorted(selected), 'edge_count':len(selected),
                'outputs':sorted(set(outputs)), 'phony_outputs':sorted(set(phony)),
                'leaf_inputs':sorted(leaves), 'external_inputs':sorted(cuts)}

    def raw(self, row, handles):
        file_id = row['file']
        if file_id not in handles:
            path = self.db.execute('SELECT path FROM files WHERE id=?',(file_id,)).fetchone()['path']
            recorded = self.meta.get('file_stats',{}).get(path)
            if recorded:
                stat = os.stat(path)
                if recorded != {'size':stat.st_size,'mtime_ns':stat.st_mtime_ns}:
                    raise ValueError('indexed Ninja input changed: '+path)
            handles[file_id] = open(path,'rb')
        handle = handles[file_id]; handle.seek(row['offset'])
        return handle.read(row['length']).decode('utf-8')

    def slice(self, targets, destination, external=(), runtime_dir='.crux-task/graph', out_root=None):
        closure = self.closure(targets,external)
        original=self.closure(targets)
        records,producers=self.edge_records(original['edge_ids'])
        metadata=self.native_metadata(records)
        rust=self.rust_runtime_contracts(records,producers,metadata)
        includes=self.generated_include_contracts(records,producers,out_root,metadata)
        embedded=self.embedded_tool_contracts(records,producers,metadata)
        contracts={edge:sorted(set(rust.get(edge,[])) |
                    set(includes.get(edge,{}).get('required_headers',[])) |
                    set(includes.get(edge,{}).get('owned_dirs',[])))
                   for edge in set(rust) | set(includes)}
        selected=set(closure['edge_ids']); own_outputs=set(closure['outputs'])
        closure['external_inputs']=sorted(set(closure['external_inputs']) | {
            path for edge in selected for path in contracts.get(edge,[]) if path not in own_outputs})
        orders=self.local_runtime_order_inputs(closure,contracts,records,producers)
        manifest=self.export_edges(closure,targets,destination,runtime_dir,runtime_order_inputs=orders)
        manifest['generated_include_contract']=self.generated_include_manifest_contract(
            manifest['edges'],includes,producers)
        manifest['embedded_tool_contexts']=self.embedded_tool_manifest_contexts(
            manifest['edges'],embedded,producers)
        (Path(destination)/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
        return manifest

    def edge_records(self, edge_ids):
        records={};producers={}
        for batch in chunks(edge_ids):
            marks=','.join('?' for _ in batch)
            for row in self.db.execute(f'SELECT * FROM edges WHERE id IN ({marks})',batch):
                record={'rule':row['rule'],'outputs':json.loads(row['outputs']),
                        'deps':json.loads(row['deps'])}
                records[row['id']]=record
                for path in record['outputs']:
                    producers[path]=row['id']
        for record in records.values():
            for path,_ in record['deps']:
                if path not in producers:
                    for directory,owner in self.side_output_dirs.items():
                        if path.startswith(directory.rstrip('/')+'/'):
                            producers[path]=owner
                            break
        return records,producers

    def native_metadata(self, records):
        """Evaluate retained native actions in their original lexical scopes."""
        actions={edge:record for edge,record in records.items() if record['rule'] != 'phony'}
        if not actions:
            return {}
        closure={'edge_ids':sorted(actions),'edge_count':len(actions),
                 'outputs':sorted({path for record in actions.values() for path in record['outputs']}),
                 'phony_outputs':[],'leaf_inputs':[],'external_inputs':[]}
        by_output={path:edge for edge,record in actions.items() for path in record['outputs']}
        with tempfile.TemporaryDirectory(prefix='ninja-native-metadata-') as temporary:
            metadata=self.export_edges(closure,[],temporary)
        return {by_output[record['outputs'][0]]:record for record in metadata['edges']}

    def generated_include_contracts(self, records, producers, out_root=None, metadata=None):
        """Bind native include searches to selected ancestor header producers.

        Include flags also survive on synthetic stubs that deliberately omit
        generated-header dependencies. Directory contents cannot establish a
        read or add a producer; only the original selected prerequisite graph
        supplies header readiness and reviewed generated-tree ownership.
        """
        source_root=self.meta['source_root']
        out_root=canonical_path(str(out_root or Path(self.meta['ninja']).parent))
        absolute=lambda path:canonical_path(path if path.startswith('/') else
                                            posixpath.join(source_root,path))
        suffixes=('.h','.hh','.hpp','.hxx','.inc','.inl','.def')
        catalog={absolute(path):(path,edge) for edge,record in records.items()
                 if record['rule'] != 'phony' for path in record['outputs']
                 if path.endswith(suffixes) and absolute(path).startswith(out_root.rstrip('/')+'/')}
        trees={directory:owner for directory,owner in self.side_output_dirs.items()
               if owner in records}
        providers=sorted({owner for _,owner in catalog.values()} | set(trees.values()))
        bits={owner:1 << index for index,owner in enumerate(providers)}
        parents={edge:{producers[path] for path,kind in record['deps']
                       if kind != 'validation' and path in producers and producers[path] != edge}
                 for edge,record in records.items()}
        successors=collections.defaultdict(set)
        remaining={edge:len(before) for edge,before in parents.items()}
        for edge,before in parents.items():
            for owner in before:
                successors[owner].add(edge)
        ready=collections.deque(edge for edge,count in remaining.items() if not count)
        ancestors={}
        while ready:
            edge=ready.popleft();mask=0
            for owner in parents[edge]:
                mask |= ancestors[owner] | bits.get(owner,0)
            ancestors[edge]=mask
            for child in successors[edge]:
                remaining[child]-=1
                if not remaining[child]:ready.append(child)
        if len(ancestors) != len(records):
            raise ValueError('generated include contract requires an acyclic original prerequisite graph')
        metadata=self.native_metadata(records) if metadata is None else metadata
        directories={};contracts={}
        for edge,record in metadata.items():
            parsed=native_generated_include_roots(record,source_root,out_root)
            roots,forced=parsed['include_roots'],parsed['forced_files']
            if not roots and not forced:
                continue
            required=set();owned=[]
            for root in roots:
                if root not in directories:
                    prefix=root.rstrip('/')+'/'
                    directories[root]=[(path,owner) for absolute_path,(path,owner) in catalog.items()
                                       if absolute_path.startswith(prefix)]
                required.update(path for path,owner in directories[root]
                                if ancestors[edge] & bits[owner])
            for path in forced:
                if path in catalog:
                    native,owner=catalog[path]
                    if ancestors[edge] & bits[owner]:required.add(native)
            for directory,owner in trees.items():
                tree=absolute(directory)
                overlap=any(root == tree or root.startswith(tree+'/') or tree.startswith(root+'/')
                            for root in roots) or any(path.startswith(tree+'/') for path in forced)
                if overlap and ancestors[edge] & bits[owner]:owned.append(directory)
            contracts[edge]={'search_roots':roots,'forced_files':forced,
                             'required_headers':sorted(required),'owned_dirs':sorted(owned)}
        return contracts

    def generated_include_manifest_contract(self, metadata, contracts, producers):
        contexts=[];roots=set();headers=set();trees=set()
        for index,record in enumerate(metadata):
            edge=producers[record['outputs'][0]]
            contract=contracts.get(edge)
            if contract is None:
                continue
            roots.update(contract['search_roots']);headers.update(contract['required_headers'])
            trees.update(contract['owned_dirs'])
            contexts.append({'edge_index':index,'primary_output':record['outputs'][0],
                             'search_roots':contract['search_roots'],'forced_files':contract['forced_files'],
                             'reader':'c_cpp_frontend'})
        return {'schema_version':1,'search_roots':sorted(roots),'required_headers':sorted(headers),
                'owned_dirs':sorted(trees),'compiler_contexts':contexts,
                'provenance':{'native_flags_unchanged':True,
                              'selected_original_ancestor_declared_headers_only':True,
                              'zero_provider_roots_preserved':True,
                              'original_ninja':self.meta['ninja'],
                              'old_depfiles_planning_evidence_only':True}}

    def embedded_tool_contracts(self, records, producers, metadata=None):
        """Bind Flatc source reads to the original Sbox consumer action.

        A textproto can cross a task boundary while its native tool command
        disappears from the consumer's Ninja command. Recover it only from
        the original literal writeFile producer. Version 1 supports source
        cwd, a single command block and declared tool/library copies; other
        sandbox mappings need an explicit contract before capture.
        """
        metadata=self.native_metadata(records) if metadata is None else metadata
        contracts={}
        def singular(message,name,default=None,required=False):
            values=message.get(name,[])
            if len(values) > 1 or (required and len(values) != 1):
                raise ValueError('invalid Sbox singular field: '+name)
            return values[0] if values else default
        def validate(message,allowed):
            if not isinstance(message,dict) or set(message)-set(allowed):
                raise ValueError('unsupported Sbox message fields')
        def copy_record(message):
            validate(message,('from','to','executable'))
            source=singular(message,'from',required=True)
            target=singular(message,'to',required=True)
            executable=singular(message,'executable',False)
            if not isinstance(source,str) or not isinstance(target,str) or not isinstance(executable,bool):
                raise ValueError('invalid Sbox copy fields')
            return {'from':source,'to':target,'executable':executable}
        def flatc(command):
            lexer=shlex.shlex(command,posix=True,punctuation_chars=';&|()')
            lexer.whitespace_split=True;lexer.commenters='';first=True
            for token in lexer:
                if token in ('&&','||',';','|','&','(',')'):
                    first=True
                elif first:
                    if posixpath.basename(token) == 'flatc':return True
                    first=False
            return False
        for edge,record in metadata.items():
            command=record.get('command','')
            if 'sbox' not in command or '--manifest' not in command:
                continue
            args=shlex.split(command)
            if '--manifest' not in args:
                continue
            position=args.index('--manifest')
            if position+1 == len(args):raise ValueError('missing Sbox manifest argument')
            manifest=canonical_path(args[position+1]);owner=producers.get(manifest)
            writer=metadata.get(owner)
            # Other Sbox families have their own source readers and manifest
            # writers (e.g. Metalava). This contract only captures a literal
            # writeFile that names Flatc; never open an unrecognized payload.
            if writer is None or writer['rule'] != 'g.android.writeFile' or not re.search(
                    r'(?<![A-Za-z0-9_])flatc(?![A-Za-z0-9_])',writer.get('command','')):
                continue
            if writer['outputs'] != [manifest] or records[owner]['deps']:
                raise ValueError('unsupported Soong writeFile producer contract')
            literal,payload=soong_writefile_literal(writer.get('command',''),manifest)
            parsed=sbox_textproto(payload)
            validate(parsed,('commands','output_depfile'))
            singular(parsed,'output_depfile')
            blocks=parsed.get('commands',[])
            for block in blocks:
                validate(block,('command','chdir','copy_before','copy_after','input_hash','rsp_files'))
                singular(block,'chdir',False);singular(block,'input_hash')
                native=singular(block,'command',required=True)
                if not isinstance(native,str) or not native or '\x00' in native:
                    raise ValueError('unsupported Sbox command string')
            relevant=[block for block in blocks if flatc(singular(block,'command'))]
            if not relevant:
                continue
            if len(blocks) != 1 or len(relevant) != 1:
                raise ValueError('unsupported multi-command Flatc sandbox')
            if posixpath.basename(args[0]) != 'sbox' or any(token in args for token in ('&&','||',';','|')):
                raise ValueError('unsupported Sbox invocation cwd wrapper')
            if (manifest,'validation') in records[edge]['deps'] or not any(
                    path == manifest and kind != 'validation' for path,kind in records[edge]['deps']):
                raise ValueError('Sbox manifest is not an original prerequisite')
            block=relevant[0]
            chdir=singular(block,'chdir',False)
            if chdir is not False:
                raise ValueError('unsupported Flatc sandbox chdir')
            if block.get('rsp_files'):
                raise ValueError('unsupported Flatc sandbox input response mappings')
            copies=[copy_record(item) for item in block.get('copy_before',[])]
            for item in block.get('copy_after',[]):copy_record(item)
            dependencies={path for path,kind in records[edge]['deps'] if kind != 'validation'}
            for item in copies:
                if not item['to'].startswith('tools/') or '..' in item['to'].split('/'):
                    raise ValueError('unsupported Flatc sandbox source input alias')
                if item['from'] not in dependencies:
                    raise ValueError('Sbox tool copy has no original declared prerequisite')
            statement=self.db.execute('SELECT * FROM statements WHERE id=?',(owner,)).fetchone()
            graph_file=self.db.execute('SELECT path FROM files WHERE id=?',(statement['file'],)).fetchone()['path']
            contracts[edge]=[{'schema_version':1,'generated_manifest':manifest,
                'producer_original_edge':owner,'producer_primary_output':manifest,
                'producer_rule':writer['rule'],'command':singular(block,'command'),
                'cwd':self.meta['source_root'],'chdir':False,'copy_before':copies,
                'provenance':{'payload_origin':'original_writeFile_literal',
                    'producer_rule':writer['rule'],
                    'original_ninja':self.meta['ninja'],'producer_graph_file':graph_file,
                    'producer_statement_offset':statement['offset'],
                    'producer_statement_length':statement['length'],
                    'producer_command_sha256':hashlib.sha256(writer['command'].encode()).hexdigest(),
                    'literal_payload_sha256':hashlib.sha256(literal.encode()).hexdigest(),
                    'native_command_flags_scopes_dependencies_unchanged':True}}]
        return contracts

    def embedded_tool_manifest_contexts(self, metadata, contracts, producers):
        contexts=[]
        for index,record in enumerate(metadata):
            owner=producers[record['outputs'][0]]
            for context in contracts.get(owner,[]):
                contexts.append({'edge_index':index,'primary_output':record['outputs'][0],**context})
        return contexts

    def local_runtime_order_inputs(self, closure, contracts, records, producers):
        """Preserve runtime readiness when an external cut removes an ancestor.

        A retained std archive can be a valid input before a locally rebuilt
        core archive exists. The original std-to-core edge is absent after the
        cut, so consumers need that same local ordering stated explicitly.
        """
        selected=set(closure['edge_ids']); external=set(closure['external_inputs'])
        ancestry={}
        def ready_before(owner,consumer):
            key=(owner,consumer)
            if key not in ancestry:
                pending=[consumer];visited=set();found=False
                while pending and not found:
                    edge=pending.pop()
                    if edge in visited:
                        continue
                    visited.add(edge)
                    for path,kind in records[edge]['deps']:
                        if kind=='validation' or path in external:
                            continue
                        before=producers.get(path)
                        if before==owner:
                            found=True;break
                        if before in selected and before not in visited:
                            pending.append(before)
                ancestry[key]=found
            return ancestry[key]
        orders={}
        for edge,required in contracts.items():
            if edge not in selected:
                continue
            missing=[path for path in required if producers[path] in selected
                     and not ready_before(producers[path],edge)]
            if missing:
                orders[edge]=sorted(missing)
        return orders

    def export_edges(self, closure, targets, destination, runtime_dir='.crux-task/graph', defer_validations=False,
                     runtime_order_inputs=None):
        destination = Path(destination); destination.mkdir(parents=True,exist_ok=True)
        runtime_order_inputs=runtime_order_inputs or {}
        selected = set(closure['edge_ids'])
        rules = set()
        for batch in chunks(selected):
            marks = ','.join('?' for _ in batch)
            rules.update(r['ref'] for r in self.db.execute(
                f'SELECT ref FROM statements WHERE id IN ({marks}) AND ref IS NOT NULL',batch))
        relevant_files = set()
        for batch in chunks(selected | rules):
            marks = ','.join('?' for _ in batch)
            relevant_files.update(r['file'] for r in self.db.execute(
                f'SELECT DISTINCT file FROM statements WHERE id IN ({marks})',batch))
        files = {r['id']:dict(r) for r in self.db.execute('SELECT * FROM files')}
        for file_id in list(relevant_files):
            parent = files[file_id]['parent']
            while parent:
                relevant_files.add(parent); parent = files[parent]['parent']
        # Includes may define inherited variables even if no selected action is
        # in that file. Keep every parsed file and only trim rule/build records.
        relevant_files.update(files)
        if self.common_rows is None:
            self.common_rows = list(self.db.execute(
                "SELECT * FROM statements WHERE kind NOT IN ('build','rule','default')"))
        retained = collections.defaultdict(list)
        for row in self.common_rows:
            retained[row['file']].append(row)
        for batch in chunks(selected | rules):
            marks = ','.join('?' for _ in batch)
            for row in self.db.execute(f'SELECT * FROM statements WHERE id IN ({marks})',batch):
                retained[row['file']].append(row)
        for rows in retained.values():
            rows.sort(key=lambda row:row['ordinal'])
        handles = {}; commands = []; depfiles = []; rspfiles = []; edge_records = []; deferred=[]
        synthetic_aliases={}
        for batch in chunks(selected):
            marks=','.join('?' for _ in batch)
            for edge in self.db.execute(f'SELECT deps FROM edges WHERE id IN ({marks})',batch):
                for path,_ in json.loads(edge['deps']):
                    for directory in self.side_output_dirs:
                        if path.startswith(directory.rstrip('/')+'/'):
                            exact=self.db.execute('SELECT edge FROM outputs WHERE path=?',(path,)).fetchone()
                            if exact is None and path not in closure['external_inputs']:
                                synthetic_aliases[path]=directory
        def visit(file_id, env, active_rules):
            name = 'build.ninja' if file_id == self.meta['entry_file'] else f'file-{file_id}.ninja'
            with open(destination/name,'w') as output:
                for row in retained[file_id]:
                    kind = row['kind']
                    if kind == 'build' and row['id'] not in selected:
                        continue
                    if kind == 'rule' and row['id'] not in rules:
                        continue
                    if kind == 'default':
                        continue
                    if kind in ('include','subninja'):
                        child = 'build.ninja' if row['ref'] == self.meta['entry_file'] else f'file-{row["ref"]}.ninja'
                        output.write(f'{kind} {ninja_escape(runtime_dir)}/{child}\n')
                        visit(row['ref'],env.copy() if kind == 'subninja' else env,
                              active_rules.copy() if kind == 'subninja' else active_rules)
                        continue
                    raw = self.raw(row,handles)
                    if kind == 'build':
                        for adaptation in self.edge_adaptations.get(row['id'],[]):
                            count=raw.count(adaptation['replace'])
                            if count != adaptation['expected_matches']:
                                raise ValueError('runtime action changed after adaptation review')
                            raw=raw.replace(adaptation['replace'],adaptation['with'])
                    if kind == 'rule':
                        for adaptation in self.rule_adaptations.get(row['id'],[]):
                            count=raw.count(adaptation['replace'])
                            if count != adaptation['expected_matches']:
                                raise ValueError('runtime action changed after adaptation review')
                            raw=raw.replace(adaptation['replace'],adaptation['with'])
                    if kind == 'build':
                        extra=[directory for directory,owner in self.side_output_dirs.items() if owner == row['id']]
                        raw=append_implicit_outputs(raw,extra)
                        raw=append_order_only_inputs(raw,runtime_order_inputs.get(row['id'],[]))
                        if defer_validations:
                            text=logical(raw); declaration,newline,rest=text.partition('\n')
                            if '|@' in declaration:
                                _,outs,deps=parse_build(declaration,edge_environment(raw,env))
                                deferred.extend({'outputs':outs,'validation':path}
                                                for path,relation in deps if relation == 'validation')
                                raw=declaration.partition('|@')[0].rstrip()+newline+rest
                    first = logical(raw).splitlines()[0].lstrip()
                    if kind == 'variable':
                        key,value = first.split('=',1)
                        env[key.strip()] = expand(value.lstrip(' '),env)
                    elif kind == 'rule':
                        active_rules[row['name']] = bindings(raw)
                    elif kind == 'build':
                        edge_env=edge_environment(raw,env)
                        rule,outs,deps = parse_build(first,edge_env)
                        tokens = path_tokens(first[6:],edge_env)
                        output_tokens = tokens[:tokens.index(':')]
                        explicit_outs = output_tokens[:output_tokens.index('|')] if '|' in output_tokens else output_tokens
                        values = collections.ChainMap({},env)
                        # Ninja's command $in excludes order-only and validation
                        # dependencies, and shell-escapes spaces in command paths.
                        explicit = [canonical_path(path) for path,kind in deps if kind == 'explicit']
                        explicit_outs=[canonical_path(path) for path in explicit_outs]
                        values.update({'in':' '.join(shell_escape(p) for p in explicit),
                            'in_newline':'\n'.join(shell_escape(p) for p in explicit),
                            'out':' '.join(shell_escape(p) for p in explicit_outs)})
                        for key,value in bindings(raw).items():
                            values[key] = expand(value,values)
                        rule_values = active_rules.get(rule,{})
                        resolving = set()
                        class RuleEnv(dict):
                            def get(self, key, default=''):
                                if key in values:
                                    return values[key]
                                if key in resolving:
                                    raise ValueError('cyclic Ninja rule binding: '+key)
                                if key in rule_values:
                                    resolving.add(key)
                                    value = expand(rule_values[key],self)
                                    resolving.remove(key)
                                    return value
                                return default
                        lookup = RuleEnv()
                        record = {'outputs':outs,'rule':rule}
                        for key in ('command','depfile','rspfile','rspfile_content'):
                            if key in ('depfile','rspfile'):
                                saved={name:values[name] for name in ('in','in_newline','out')}
                                values.update({'in':' '.join(explicit),'in_newline':'\n'.join(explicit),
                                               'out':' '.join(explicit_outs)})
                                value=lookup.get(key)
                                values.update(saved)
                            else:
                                value = lookup.get(key)
                            if value:
                                record[key] = value
                        if 'command' in record and rule != 'phony':
                            commands.append(record['command'])
                        if 'depfile' in record:
                            depfiles.append(record['depfile'])
                        if 'rspfile' in record:
                            rspfiles.append(record['rspfile'])
                        if rule != 'phony':
                            edge_records.append(record)
                    output.write(raw); output.write('\n')
        try:
            visit(self.meta['entry_file'],{}, {})
            if synthetic_aliases:
                with open(destination/'build.ninja','a') as output:
                    output.write('\n# Generated tree members follow their reviewed directory producer.\n')
                    for path,directory in sorted(synthetic_aliases.items()):
                        output.write(f'build {ninja_escape(path)}: phony {ninja_escape(directory)}\n')
            external_phonies=sorted(set(closure['external_inputs'])-set(closure['outputs']))
            if external_phonies:
                with open(destination/'build.ninja','a') as output:
                    output.write('\n# Imported inputs are verified by producer receipts before Ninja starts.\n')
                    for path in external_phonies:
                        output.write(f'build {ninja_escape(path)}: phony\n')
            external_owners=set(self.producers(closure['external_inputs']).values())
            all_dirs={**self.side_output_dirs,**self.group_export_dirs}
            manifest = {'schema_version':1,'source_root':self.meta['source_root'],
                'ninja':'build.ninja','runtime_dir':runtime_dir,'targets':list(targets),
                'graph_inputs':[r['path'] for r in files.values()],
                'commands':commands,'depfiles':sorted(set(depfiles)),
                'rspfiles':sorted(set(rspfiles)),'edges':edge_records,
                'declared_side_output_dirs':[directory for directory,owner in self.side_output_dirs.items() if owner in selected],
                'output_dirs':[directory for directory,owner in all_dirs.items() if owner in selected],
                'external_input_dirs':[directory for directory,owner in all_dirs.items() if owner in external_owners],
                'ownership_profile':self.meta.get('ownership_profile'),
                'runtime_adaptations':self.meta.get('runtime_adaptations'),
                'deferred_validations':deferred,
                'synthetic_aliases':synthetic_aliases,
                'external_phony_inputs':external_phonies,
                **{k:v for k,v in closure.items() if k != 'edge_ids'}}
            if runtime_order_inputs:
                manifest['runtime_order_inputs']={str(edge):paths for edge,paths in sorted(runtime_order_inputs.items())}
            (destination/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
            return manifest
        finally:
            for handle in handles.values():
                handle.close()

    def rust_runtime_contracts(self, records, producers, metadata=None):
        """Resolve Rust crate searches from commands and declared producers.

        Soong exports transitive crate directories through ``-L`` while Ninja
        names only direct crates. Cutting those direct edges must retain the
        exact transitive crates; inspecting an existing OUT directory would
        mistake old build products for source inputs.
        """
        known_rules = {'g.rust.rustc','g.rust.clippy','g.rust.rustdoc'}
        rust_edges = {edge for edge,record in records.items() if record['rule'] in known_rules}
        if not rust_edges:
            return {}
        metadata=self.native_metadata({edge:records[edge] for edge in rust_edges}) if metadata is None else metadata
        commands={edge:record['command'] for edge,record in metadata.items()
                  if edge in rust_edges and record.get('command')}
        tokens = {edge:shlex.split(command) for edge,command in commands.items()}
        crate_types = {edge:{kind for token in arguments if token.startswith('--crate-type=')
                              for kind in token.partition('=')[2].split(',')}
                       for edge,arguments in tokens.items()}
        directories = {}
        def libraries(directory):
            if directory not in directories:
                prefix = directory.rstrip('/')+'/'
                found = {}
                for row in self.db.execute(
                        'SELECT o.path,o.edge,e.rule FROM outputs o JOIN edges e ON e.id=o.edge '
                        'WHERE o.path>=? AND o.path<?',(prefix,prefix+'\uffff')):
                    path,owner = row['path'],row['edge']
                    if posixpath.dirname(path) != directory or row['rule'] not in known_rules:
                        continue
                    eligible = path.endswith(('.rlib','.rmeta')) or (
                        path.endswith('.so') and crate_types.get(owner,set()) & {'dylib','proc-macro'})
                    if eligible:
                        if owner not in records:
                            raise ValueError('Rust runtime producer is outside the requested closure: '+path)
                        found[path] = owner
                directories[directory] = found
            return directories[directory]
        ancestry = {}
        def is_ancestor(owner, consumer):
            key = (owner,consumer)
            if key not in ancestry:
                pending = [consumer]; visited = set(); found = False
                while pending and not found:
                    edge = pending.pop()
                    if edge in visited:
                        continue
                    visited.add(edge)
                    for path,kind in records[edge]['deps']:
                        if kind == 'validation':
                            continue
                        before = producers.get(path)
                        if before == owner:
                            found = True; break
                        if before is not None and before not in visited:
                            pending.append(before)
                ancestry[key] = found
            return ancestry[key]
        contracts = {}
        for edge,arguments in tokens.items():
            required = {}
            for index,argument in enumerate(arguments):
                operand = arguments[index+1] if argument == '-L' and index+1 < len(arguments) else (
                    argument[2:] if argument.startswith('-L') and len(argument) > 2 else None)
                if not operand:
                    continue
                if '=' in operand:
                    kind,operand = operand.split('=',1)
                    if kind not in ('all','crate','dependency'):
                        continue
                directory = canonical_path(operand.rstrip('/'))
                required.update(libraries(directory))
            for path,owner in required.items():
                if not is_ancestor(owner,edge):
                    raise ValueError('Rust runtime input lacks original producer ordering: '+path)
            if required:
                contracts[edge] = sorted(required)
        return contracts

    def shard(self, targets, destination, max_actions=4000, max_parallel=20, export=True, out_root=None):
        """Pack DAG frontiers into waves; dependencies inside a job stay local.

        Phony aliases are expanded when scheduling and preserved inside each
        emitted Ninja slice. Only real producer files cross job boundaries.
        """
        if max_actions < 1 or max_parallel < 1:
            raise ValueError('shard capacities must be positive')
        complete = self.closure(targets)
        records,producers = self.edge_records(complete['edge_ids'])
        metadata=self.native_metadata(records)
        rust_contracts = self.rust_runtime_contracts(records,producers,metadata)
        include_contracts=self.generated_include_contracts(records,producers,out_root,metadata)
        embedded_contracts=self.embedded_tool_contracts(records,producers,metadata)
        runtime_contracts={edge:sorted(set(rust_contracts.get(edge,[])) |
                            set(include_contracts.get(edge,{}).get('required_headers',[])) |
                            set(include_contracts.get(edge,{}).get('owned_dirs',[])))
                           for edge in set(rust_contracts) | set(include_contracts)}
        actions = {edge for edge,record in records.items() if record['rule'] != 'phony'}
        aliases = {}
        visiting = set()
        def dependencies(edge):
            if edge in actions:
                return {edge}
            if edge in aliases:
                return aliases[edge]
            if edge in visiting:
                raise ValueError('cycle through phony aliases')
            visiting.add(edge)
            result = set()
            for path,relation in records[edge]['deps']:
                if relation == 'validation':
                    continue
                if path in producers:
                    result.update(dependencies(producers[path]))
            visiting.remove(edge); aliases[edge] = result
            return result
        members={edge:{edge} for edge in actions}
        unit_for={edge:edge for edge in actions}
        for group in self.meta.get('atomic_groups',[]):
            included=set(group)&actions
            if included and included != set(group):
                raise ValueError('partial atomic action group in closure')
            if included:
                prior={unit_for[edge] for edge in included}
                combined=set().union(*(members.pop(unit) for unit in prior))
                identity=min(combined); members[identity]=combined
                for edge in combined:
                    unit_for[edge]=identity
        prerequisites = collections.defaultdict(set); successors = collections.defaultdict(set)
        for edge in sorted(actions):
            before = set()
            for path,relation in records[edge]['deps']:
                if relation == 'validation':
                    continue
                producer = producers.get(path)
                if producer is not None:
                    before.update(dependencies(producer))
            unit=unit_for[edge]
            before_units={unit_for[dependency] for dependency in before}-{unit}
            prerequisites[unit].update(before_units)
        for unit in members:
            for dependency in prerequisites[unit]:
                successors[dependency].add(unit)
        remaining = {edge:len(before) for edge,before in prerequisites.items()}
        ready = [edge for edge,count in remaining.items() if count == 0]
        heapq.heapify(ready)
        done = set(); scheduled = set(); waves = []; assignments = {}
        required_exports = collections.defaultdict(set)
        while len(done) < len(members):
            if not ready:
                unfinished=set(members)-done
                visited=set(); cycle=[]
                for start in sorted(unfinished):
                    if start in visited:
                        continue
                    trail=[start]; positions={start:0}
                    stack=[iter(sorted(prerequisites[start]&unfinished))]
                    visited.add(start)
                    while stack and not cycle:
                        child=next(stack[-1],None)
                        if child is None:
                            stack.pop(); positions.pop(trail.pop()); continue
                        if child in positions:
                            cycle=trail[positions[child]:]+[child]; break
                        if child not in visited:
                            visited.add(child); positions[child]=len(trail); trail.append(child)
                            stack.append(iter(sorted(prerequisites[child]&unfinished)))
                    if cycle:
                        break
                paths=[records[min(members[unit])]['outputs'][0] for unit in cycle]
                raise ValueError('Ninja action graph has a cycle: '+' -> '.join(paths))
            jobs = []
            for _ in range(max_parallel):
                if not ready:
                    break
                chosen = set(); local_ready = []; chosen_count=0
                while chosen_count < max_actions and (local_ready or ready):
                    edge = heapq.heappop(local_ready if local_ready else ready)
                    if edge in scheduled:
                        continue
                    chosen.add(edge); scheduled.add(edge)
                    chosen_count+=len(members[edge])
                    for child in successors[edge]:
                        if child not in scheduled and all(
                                dep in done or dep in chosen for dep in prerequisites[child]):
                            heapq.heappush(local_ready,child)
                if chosen:
                    jobs.append(chosen)
            wave_number = len(waves)
            wave_jobs = []
            for job_number,chosen_units in enumerate(jobs):
                chosen=set().union(*(members[unit] for unit in chosen_units))
                identity = f'w{wave_number:03d}-s{job_number:03d}'
                for edge in chosen:
                    assignments[edge] = identity
                job_targets = [records[edge]['outputs'][0] for edge in sorted(chosen)]
                own_outputs = {path for edge in chosen for path in records[edge]['outputs']}
                cut = set(complete['outputs']) - own_outputs
                selected = self.closure(job_targets,cut,include_validations=False)
                if set(selected['edge_ids']) & actions != chosen:
                    raise ValueError('shard closure differs from assigned actions')
                # Readiness may name only a stamp while an include directory
                # consumes the producer's other declared outputs. Import the
                # same complete contract that the predecessor exports.
                imported=set(selected['external_inputs'])
                runtime_inputs=sorted({path for edge in chosen for path in rust_contracts.get(edge,[])
                                       if path not in own_outputs})
                imported.update(runtime_inputs)
                imported.update(path for edge in chosen for path in runtime_contracts.get(edge,[])
                                if path not in own_outputs)
                for path in list(imported):
                    imported.update(records[producers[path]]['outputs'])
                selected['external_inputs']=sorted(imported)
                upstream = sorted({assignments[producers[path]] for path in selected['external_inputs']})
                for path in selected['external_inputs']:
                    # A timestamp can be the only readiness input while the
                    # action's generated headers are consumed through -I.
                    # Transport the complete declared output contract together.
                    owner=producers[path]
                    required_exports[assignments[owner]].update(records[owner]['outputs'])
                    for directory,owner in {**self.side_output_dirs,**self.group_export_dirs}.items():
                        if owner == producers[path]:
                            required_exports[assignments[owner]].add(directory)
                job = {'id':identity,'wave':wave_number,'action_count':len(chosen),
                       'targets':job_targets,'depends_on':upstream,
                       'external_inputs':selected['external_inputs']}
                if runtime_inputs:
                    job['rust_runtime_inputs'] = runtime_inputs
                planned_metadata=[record for edge,record in metadata.items() if edge in chosen]
                include_contract=self.generated_include_manifest_contract(planned_metadata,include_contracts,producers)
                job['generated_include_contract']=include_contract
                embedded_contexts=self.embedded_tool_manifest_contexts(planned_metadata,embedded_contracts,producers)
                job['embedded_tool_contexts']=embedded_contexts
                if export:
                    directory = Path(destination)/identity
                    orders=self.local_runtime_order_inputs(selected,runtime_contracts,records,producers)
                    manifest = self.export_edges(selected,job_targets,directory,defer_validations=True,
                                                 runtime_order_inputs=orders)
                    actual_contract=self.generated_include_manifest_contract(manifest['edges'],include_contracts,producers)
                    if actual_contract != include_contract:
                        raise ValueError('generated include metadata differs from its planned native actions')
                    manifest['generated_include_contract']=actual_contract
                    actual_embedded=self.embedded_tool_manifest_contexts(manifest['edges'],embedded_contracts,producers)
                    if actual_embedded != embedded_contexts:
                        raise ValueError('embedded tool metadata differs from its original native actions')
                    manifest['embedded_tool_contexts']=actual_embedded
                    if runtime_inputs:
                        manifest['rust_runtime_inputs'] = runtime_inputs
                    (directory/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
                    job['manifest'] = f'{identity}/manifest.json'
                wave_jobs.append(job)
            waves.append(wave_jobs)
            for chosen_units in jobs:
                for edge in chosen_units:
                    done.add(edge)
                    for child in successors[edge]:
                        remaining[child] -= 1
                        if remaining[child] == 0 and child not in scheduled:
                            heapq.heappush(ready,child)
        def terminal_files(path):
            edge = producers.get(path)
            if edge is None:
                return set()
            if edge in actions:
                return {path}
            result = set()
            for dependency,_ in records[edge]['deps']:
                result.update(terminal_files(dependency))
            return result
        terminals = set()
        for target in targets:
            terminals.update(terminal_files(target))
        validation_outputs={path for record in records.values()
                            for path,relation in record['deps'] if relation == 'validation'}
        for path in validation_outputs:
            terminals.update(terminal_files(path))
        for path in terminals:
            required_exports[assignments[producers[path]]].add(path)
        for wave in waves:
            for job in wave:
                job['export_outputs'] = sorted(required_exports[job['id']])
                if export:
                    manifest_path = Path(destination)/job['manifest']
                    manifest = json.loads(manifest_path.read_text())
                    manifest['export_outputs'] = job['export_outputs']
                    manifest_path.write_text(json.dumps(manifest,indent=2)+'\n')
        plan = {'schema_version':1,'source_root':self.meta['source_root'],
                'targets':list(targets),'action_count':len(actions),
                'edge_count':complete['edge_count'],'max_actions':max_actions,
                'max_parallel':max_parallel,'wave_count':len(waves),
                'job_count':sum(len(wave) for wave in waves),'terminal_outputs':sorted(terminals),
                'validation_outputs':sorted(validation_outputs),
                'export_producers':{path:assignments[producers[path]]
                    for paths in required_exports.values() for path in sorted(paths)},
                'waves':waves}
        Path(destination).mkdir(parents=True,exist_ok=True)
        (Path(destination)/'plan.json').write_text(json.dumps(plan,indent=2)+'\n')
        return plan


def ninja_escape(value):
    return value.replace('$','$$').replace(' ','$ ').replace(':','$:')


def shell_escape(value):
    # Ninja deliberately quotes a stricter character set than Python shlex.
    # Match POSIX GetShellEscapedString in Ninja's util.cc.
    if not re.search(r'[^A-Za-z0-9_+./-]',value):
        return value
    return "'"+value.replace("'","'\\''")+"'"


def canonical_path(value):
    value=posixpath.normpath(value)
    return '/'+value.lstrip('/') if value.startswith('//') else value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command',required=True)
    idx = sub.add_parser('index')
    idx.add_argument('--ninja',required=True); idx.add_argument('--source-root',required=True)
    idx.add_argument('--database',required=True)
    aug=sub.add_parser('augment')
    aug.add_argument('--database',required=True)
    aug.add_argument('--ownership',required=True)
    aug.add_argument('--replace-profile',action='store_true')
    ada=sub.add_parser('adapt')
    ada.add_argument('--database',required=True)
    ada.add_argument('--profile',required=True)
    ada.add_argument('--replace-profile',action='store_true')
    for command in ('closure','slice'):
        cmd = sub.add_parser(command); cmd.add_argument('--database',required=True)
        cmd.add_argument('--target',action='append',required=True)
        cmd.add_argument('--external',help='JSON array or newline-separated paths from upstream shards')
        cmd.add_argument('--runtime-dir',default='.crux-task/graph')
        cmd.add_argument('--output' if command == 'closure' else '--output-dir',required=True)
    shard = sub.add_parser('shard')
    shard.add_argument('--database',required=True)
    shard.add_argument('--target',action='append',required=True)
    shard.add_argument('--output-dir',required=True)
    shard.add_argument('--max-actions',type=int,default=4000)
    shard.add_argument('--max-parallel',type=int,default=20)
    shard.add_argument('--plan-only',action='store_true')
    args = parser.parse_args()
    if args.command == 'index':
        result = index_graph(args.ninja,args.source_root,args.database)
        print(json.dumps(result,indent=2)); return
    if args.command == 'augment':
        result=augment_graph(args.database,json.loads(Path(args.ownership).read_text()),args.replace_profile)
        print(json.dumps(result,indent=2)); return
    if args.command == 'adapt':
        result=adapt_graph(args.database,json.loads(Path(args.profile).read_text()),args.replace_profile)
        print(json.dumps(result,indent=2)); return
    if args.command == 'shard':
        result = Graph(args.database).shard(args.target,args.output_dir,
                    args.max_actions,args.max_parallel,not args.plan_only)
        print(json.dumps({k:v for k,v in result.items() if isinstance(v,(str,int,float,bool))},indent=2)); return
    external = []
    if args.external:
        raw = Path(args.external).read_text()
        external = json.loads(raw) if raw.lstrip().startswith('[') else raw.splitlines()
    graph = Graph(args.database)
    if args.command == 'closure':
        result = graph.closure(args.target,external)
        Path(args.output).write_text(json.dumps(result,indent=2)+'\n')
    else:
        result = graph.slice(args.target,args.output_dir,external,args.runtime_dir)
    print(json.dumps({k:v for k,v in result.items() if not isinstance(v,list)},indent=2))


if __name__ == '__main__':
    main()
