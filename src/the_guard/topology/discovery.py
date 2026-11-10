"""Deterministic module inventory; discovery reports facts, never invents ownership."""
import ast
import os
from pathlib import Path
import re
from ..config import load_config

IGNORED = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', '.build', 'dist', 'build', '.worktrees', '.guard'}
NON_MODULE_TOP_DIRS = {'docs', 'build_docs', 'scripts', 'tests', 'test', 'bin', 'skills'}
LANGUAGES = {'.py':'Python', '.swift':'Swift', '.ts':'TypeScript', '.tsx':'TypeScript', '.js':'JavaScript', '.go':'Go', '.rs':'Rust', '.java':'Java', '.kt':'Kotlin'}


def source_files(root):
    root = Path(root).resolve()
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in IGNORED and not (Path(directory)/d).is_symlink())
        for name in sorted(files):
            path = Path(directory)/name
            if not path.is_symlink():
                yield path


def discover_topology(root):
    root = Path(root).resolve()
    config = load_config(root)
    files = list(source_files(root))
    rooted_candidates = set()
    fallback_candidates = set()
    for path in files:
        rel = path.relative_to(root)
        parts = rel.parts
        if len(parts) >= 3 and parts[0] in config.module_doc_roots and not parts[1].startswith('.') and parts[1] not in {'tests', 'test'}:
            rooted_candidates.add('/'.join(parts[:2]))
        elif len(parts) >= 2 and not parts[0].startswith('.') and parts[0] not in NON_MODULE_TOP_DIRS and (path.suffix in LANGUAGES or path.name in ('AGENTS.md','CLAUDE.md','README.md','pyproject.toml','package.json')):
            fallback_candidates.add(parts[0])
    candidates = rooted_candidates or fallback_candidates
    modules = []
    module_imports = {}
    for directory in sorted(candidates):
        owned = [p for p in files if p.relative_to(root).as_posix().startswith(directory+'/')]
        imports = set()
        for path in owned:
            if path.suffix == '.py':
                try:
                    tree = ast.parse(path.read_text(errors='replace'))
                except (SyntaxError, UnicodeError):
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        imports.update(a.name for a in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imports.add(node.module)
            elif path.suffix in {'.ts','.tsx','.js','.jsx'}:
                imports.update(re.findall(r'''(?:from\s+|import\s+|require\(\s*)["']([^"']+)["']''', path.read_text(errors='replace')))
        module_imports[directory] = imports
        modules.append({'path':directory, 'languages':sorted({LANGUAGES[p.suffix] for p in owned if p.suffix in LANGUAGES}), 'boundary_docs':[f'{directory}/{name}' for name in ('AGENTS.md','CLAUDE.md','README.md') if (root/directory/name).is_file()]})
    for module in modules:
        imports = module_imports[module['path']]
        module['dependencies'] = sorted({other['path'] for other in modules if other != module and any(name == Path(other['path']).name or name.startswith(Path(other['path']).name+'.') or name.startswith('@'+Path(other['path']).name+'/') for name in imports)})
    return {'modules':modules}
