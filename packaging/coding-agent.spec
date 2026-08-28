# PyInstaller onedir build for the optional native WebView launcher.
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

project_root = Path(SPECPATH).parent.parent
static_files = collect_data_files("coding_agent", includes=["web/static/*"])

a = Analysis(
    [str(project_root / "src" / "coding_agent" / "desktop.py")],
    pathex=[str(project_root / "src")],
    datas=static_files,
    hiddenimports=["webview"],
    excludes=[],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="code-helper", console=True)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, name="code-helper")
