# PyInstaller onedir build for the optional native WebView launcher.
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

project_root = Path(SPECPATH).parent
static_files = collect_data_files("coding_agent", includes=["web/static/*"])
tiktoken_extensions = collect_submodules("tiktoken_ext")
skill_files = [
    (str(path), str(path.parent.relative_to(project_root)))
    for path in (project_root / "skills").glob("*/SKILL.md")
]

a = Analysis(
    [str(project_root / "packaging" / "desktop_entry.py")],
    pathex=[str(project_root / "src")],
    datas=[*static_files, *skill_files],
    hiddenimports=["webview", *tiktoken_extensions],
    excludes=[],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="code-helper", console=False)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, name="code-helper")
