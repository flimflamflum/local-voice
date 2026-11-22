# One-time setup: Python venv + dependencies + Kokoro voice model (~115 MB).
# Usage:  powershell -ExecutionPolicy Bypass -File setup.ps1 [-Shortcut]
param([switch]$Shortcut)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path .venv)) {
    Write-Host "Creating virtual environment..."
    python -m venv .venv
}
Write-Host "Installing dependencies..."
.\.venv\Scripts\python.exe -m pip install -q --upgrade pip
.\.venv\Scripts\python.exe -m pip install -q -r requirements.txt

New-Item -ItemType Directory -Force models | Out-Null
$base = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
foreach ($f in "kokoro-v1.0.int8.onnx", "voices-v1.0.bin") {
    if (-not (Test-Path "models\$f")) {
        Write-Host "Downloading $f..."
        curl.exe -sSL -o "models\$f" "$base/$f"
    }
}

if (-not (Test-Path ui\jarvis.ico)) { .\.venv\Scripts\python.exe tools\make_icon.py }

if ($Shortcut) {
    $root = (Get-Location).Path
    $desktop = [Environment]::GetFolderPath('Desktop')
    Remove-Item (Join-Path $desktop 'Jarvis.lnk') -ErrorAction SilentlyContinue  # the app's old name
    $lnkPath = Join-Path $desktop 'Vesper.lnk'
    $lnk = (New-Object -ComObject WScript.Shell).CreateShortcut($lnkPath)
    $lnk.TargetPath = Join-Path $root '.venv\Scripts\pythonw.exe'
    $lnk.Arguments = '"' + (Join-Path $root 'app.py') + '"'
    $lnk.WorkingDirectory = $root
    $lnk.IconLocation = (Join-Path $root 'ui\jarvis.ico') + ',0'
    $lnk.Description = 'Voice chat with your local Ollama models'
    $lnk.Save()
    # same taskbar identity as the running app, so pinning works nicely
    .\.venv\Scripts\python.exe -c "import sys,pythoncom; from win32com.propsys import propsys,pscon; from win32com.shell import shellcon; s=propsys.SHGetPropertyStoreFromParsingName(sys.argv[1],None,shellcon.GPS_READWRITE,propsys.IID_IPropertyStore); s.SetValue(pscon.PKEY_AppUserModel_ID,propsys.PROPVARIANTType('Jarvis.VoiceAssistant',pythoncom.VT_LPWSTR)); s.Commit()" $lnkPath
    Write-Host "Desktop shortcut created."
}

Write-Host "`nDone. Make sure Ollama is running, then launch Vesper.bat"
