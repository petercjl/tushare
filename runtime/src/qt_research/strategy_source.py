"""Identify one strategy artifact while excluding its provider credential value."""
import ast
import hashlib
from pathlib import Path


def build_id(text):
    tree=ast.parse(text)
    for node in tree.body:
        if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id in ('TUSHARE_TOKEN','STRATEGY_BUILD_ID') for t in node.targets):
            node.value=ast.Constant('<private-credential-or-build-placeholder>')
    return hashlib.sha256(ast.dump(tree,include_attributes=False).encode()).hexdigest()


def verify(text):
    tree=ast.parse(text)
    declared=None
    for node in tree.body:
        if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='STRATEGY_BUILD_ID' for t in node.targets):
            declared=ast.literal_eval(node.value)
    actual=build_id(text)
    if declared!=actual:raise ValueError('Single-source strategy build ID differs; rebuild before running')
    return actual


def seal(args):
    source,out=Path(args.file),Path(args.out)
    if out.exists():raise FileExistsError('Sealed strategy exists; choose a new path')
    text=source.read_text();tree=ast.parse(text);value=None
    for node in tree.body:
        if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='STRATEGY_BUILD_ID' for t in node.targets):value=node.value
    if value is None:raise ValueError('Strategy lacks the single-source build ID declaration')
    lines=text.encode().splitlines(keepends=True);offset=[0]
    for line in lines:offset.append(offset[-1]+len(line))
    a=offset[value.lineno-1]+value.col_offset;b=offset[value.end_lineno-1]+value.end_col_offset
    raw=text.encode();updated=(raw[:a]+repr(build_id(text)).encode()+raw[b:]).decode()
    identity=verify(updated);out.parent.mkdir(parents=True,exist_ok=True)
    with out.open('x') as f:f.write(updated)
    return dict(out=str(out.resolve()),strategy_build_id=identity,source_sha256=hashlib.sha256(out.read_bytes()).hexdigest())
