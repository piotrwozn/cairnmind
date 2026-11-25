<#
.SYNOPSIS
Install this checkout of Cairnmind as an isolated uv tool on native Windows.

.DESCRIPTION
Installs Cairnmind as a uv tool together with ArcadeDB 26.11.1 (about 86 MB,
archive verified with SHA-256), starts the engine and initialises local memory.
Requires Java 21+ (or Docker Desktop, which then runs the engine).
Does not change Codex or Claude Code configuration.
Does not download models or the Copilot runtime by default.

With CAIRNMIND_ARCADE_CONNECT_URL set, the installation only connects to a
database that runs elsewhere (docs/DEPLOYMENT.md): no ArcadeDB is downloaded,
no engine is started and Java is not needed.

.PARAMETER PullModels
Download both local memory models into Ollama after installation (several GB; Ollama must be running).

.PARAMETER Help
Show this help message.
#>
[CmdletBinding()]
param(
    [switch]$PullModels,
    [switch]$Help
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($Help) {
    Write-Output @"
Usage: pwsh -File scripts/install-windows.ps1 [-PullModels]
   or: powershell -ExecutionPolicy Bypass -File scripts/install-windows.ps1 [-PullModels]

Installs Cairnmind as a uv tool together with ArcadeDB 26.11.1 (about 86 MB,
archive verified with SHA-256), starts the engine and initialises local memory.
Requires Java 21+ (or Docker Desktop, which then runs the engine).
Does not change Codex or Claude Code configuration.

With CAIRNMIND_ARCADE_CONNECT_URL set, the installation only connects to a database
that runs elsewhere (docs/DEPLOYMENT.md): no ArcadeDB is downloaded, no engine is
started and Java is not needed. The address is saved in config.json of the data
directory, so later commands need no variable to stay connect-only.
Does not download models or the Copilot runtime by default; prepare Copilot in the terminal UI.

  -PullModels  Download both models after installation (several GB; Ollama must be running).
  -Help        Show this help.
"@
    exit 0
}

# SDK runtime is prepared explicitly from the UI, never as an installation side effect.
$env:COPILOT_SKIP_CLI_DOWNLOAD = "1"

function Write-Err {
    param([string]$Message)
    [Console]::Error.WriteLine($Message)
}

function Get-UpgradeMarkPath {
    $localAppData = $env:LOCALAPPDATA
    if (-not $localAppData) {
        $localAppData = Join-Path $HOME "AppData\Local"
    }
    return Join-Path (Join-Path (Join-Path $localAppData "cairnmind") "state") "upgrade-in-progress"
}

function Begin-Upgrade {
    $mark = Get-UpgradeMarkPath
    $dir = Split-Path -Parent $mark
    try {
        if (-not (Test-Path -LiteralPath $dir)) {
            New-Item -ItemType Directory -Path $dir -Force | Out-Null
        }
        $tmp = "$mark.$PID"
        [System.IO.File]::WriteAllText($tmp, "$PID`n", [System.Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $tmp -Destination $mark -Force
    } catch {
        Write-Err "Cannot write $mark, which tells running Cairnmind processes that an upgrade is in progress."
        exit 1
    }
}

function End-Upgrade {
    $mark = Get-UpgradeMarkPath
    $tmp = "$mark.$PID"
    if (Test-Path -LiteralPath $tmp) {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $mark) {
        try {
            $owner = ([System.IO.File]::ReadAllText($mark)).Trim()
            if ($owner -eq "$PID") {
                Remove-Item -LiteralPath $mark -Force -ErrorAction SilentlyContinue
            }
        } catch {
            # Leave unreadable or foreign mark untouched.
        }
    }
}

function Find-Java21 {
    $javaBin = $null
    $javaSource = ""
    $javaHome = $env:JAVA_HOME
    if ($javaHome) {
        $candidate = Join-Path (Join-Path $javaHome "bin") "java.exe"
        if ([System.IO.Path]::IsPathRooted($javaHome) -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            $javaBin = $candidate
            $javaSource = " (JAVA_HOME=$javaHome)"
        } else {
            Write-Err "JAVA_HOME is set to $javaHome, which has no bin\java.exe; it is ignored."
        }
    }
    if (-not $javaBin) {
        $cmd = Get-Command java -ErrorAction SilentlyContinue
        if ($cmd) {
            $javaBin = $cmd.Source
        }
    }
    if (-not $javaBin) {
        return @{ Ok = $false; Message = "Java not found. Install Java 21+: winget install EclipseAdoptium.Temurin.21.JDK" }
    }
    try {
        $psi = [System.Diagnostics.ProcessStartInfo]::new()
        $psi.FileName = $javaBin
        $psi.Arguments = "-version"
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $proc = [System.Diagnostics.Process]::Start($psi)
        if (-not $proc.WaitForExit(10000)) {
            try { $proc.Kill() } catch {}
            return @{ Ok = $false; Message = "$javaBin -version did not answer within 10 seconds." }
        }
        $output = $proc.StandardError.ReadToEnd() + "`n" + $proc.StandardOutput.ReadToEnd()
        if ($proc.ExitCode -ne 0) {
            return @{ Ok = $false; Message = "$javaBin -version did not run. Java 21+ is required: winget install EclipseAdoptium.Temurin.21.JDK" }
        }
    } catch {
        return @{ Ok = $false; Message = "$javaBin -version could not be run. Java 21+ is required: winget install EclipseAdoptium.Temurin.21.JDK" }
    }
    if ($output -match 'version "(\d+)') {
        $major = [int]$Matches[1]
        if ($major -lt 21) {
            return @{ Ok = $false; Message = "Found Java $major$javaSource. Java 21+ is required: winget install EclipseAdoptium.Temurin.21.JDK" }
        }
        return @{ Ok = $true; Path = $javaBin; Major = $major }
    }
    return @{ Ok = $false; Message = "Cannot detect the Java version. Check java -version; Java 21+ is required." }
}

function Test-DockerAnswers {
    $dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
    if (-not $dockerCmd) {
        return @{ Installed = $false; Ok = $false; Detail = "" }
    }
    try {
        $psi = [System.Diagnostics.ProcessStartInfo]::new()
        $psi.FileName = $dockerCmd.Source
        $psi.Arguments = "version --format {{.Server.Version}}"
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $proc = [System.Diagnostics.Process]::Start($psi)
        if (-not $proc.WaitForExit(5000)) {
            try { $proc.Kill() } catch {}
            return @{ Installed = $true; Ok = $false; Detail = "no answer within 5 seconds" }
        }
        $stdout = ($proc.StandardOutput.ReadToEnd()).Trim()
        $stderr = ($proc.StandardError.ReadToEnd()).Trim()
        if ($proc.ExitCode -eq 0 -and $stdout) {
            return @{ Installed = $true; Ok = $true; Detail = $stdout }
        }
        $first = ($stderr -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -First 1)
        if (-not $first) { $first = "exit status $($proc.ExitCode)" }
        $first = ($first -replace '[^\x20-\x7e]', '')
        if ($first.Length -gt 200) { $first = $first.Substring(0, 200) }
        return @{ Installed = $true; Ok = $false; Detail = $first }
    } catch {
        return @{ Installed = $true; Ok = $false; Detail = $_.Exception.Message }
    }
}

function Get-LiveToolProcesses {
    param(
        [string]$PythonExe,
        [string]$Kind
    )
    $pids = @()
    if (-not $PythonExe) { return $pids }
    $needle = $PythonExe.ToLowerInvariant()
    try {
        $procs = Get-CimInstance Win32_Process -ErrorAction Stop
    } catch {
        return $null
    }
    foreach ($p in $procs) {
        $cmd = $p.CommandLine
        if (-not $cmd) { continue }
        $low = $cmd.ToLowerInvariant()
        if (-not $low.Contains($needle)) { continue }
        if ($Kind -eq "bridges") {
            if ($low -match '\bmcp\b.*(--client\s+external|--client=external)') {
                $pids += [int]$p.ProcessId
            }
        } elseif ($Kind -eq "runs") {
            if ($low -match '\bcairnmind(\.exe)?\s+run\b') {
                $pids += [int]$p.ProcessId
            }
        }
    }
    return $pids
}

# --- Pre-install checks ---
Write-Output "Detected Windows."
if ($PullModels) {
    Write-Output "Local GPU acceleration on Windows uses your installed Windows GPU driver with Ollama."
}

$connectUrl = $env:CAIRNMIND_ARCADE_CONNECT_URL
if (-not $connectUrl) { $connectUrl = $env:DEVBRAIN_ARCADE_CONNECT_URL }
$engineInDocker = $false

if (-not $connectUrl) {
    $javaCheck = Find-Java21
    if (-not $javaCheck.Ok) {
        $dockerCheck = Test-DockerAnswers
        if (-not $dockerCheck.Installed) {
            Write-Err $javaCheck.Message
            Write-Err "Or install Docker Desktop and start it; the engine then runs in a container. Then run this installer again."
            Write-Err "Nothing has been installed yet. Java is the simpler choice."
            exit 1
        }
        if ($dockerCheck.Ok) {
            Write-Output "Docker answers, so cairnmind init can run the engine in Docker (Java 21 or newer was not found); it says which mode it chose."
            $engineInDocker = $true
        } else {
            Write-Err $javaCheck.Message
            Write-Err "Docker is on PATH but did not answer (it said: $($dockerCheck.Detail)). Either start Docker Desktop and run the installer again, or install Java 21+: winget install EclipseAdoptium.Temurin.21.JDK"
            Write-Err "Nothing has been installed yet. Java is the simpler choice."
            exit 1
        }
    }
}

$uvCmd = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uvCmd) {
    Write-Err "uv not found on PATH. Install it, open a new terminal and run this installer again:"
    Write-Err "  winget install astral-sh.uv"
    Write-Err '  or: powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"'
    exit 1
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$packageDir = Split-Path -Parent $scriptDir
if (-not (Test-Path -LiteralPath (Join-Path $packageDir "pyproject.toml")) -or
    -not (Test-Path -LiteralPath (Join-Path $packageDir "uv.lock"))) {
    Write-Err "pyproject.toml or uv.lock is missing next to scripts\. Copy the whole project."
    exit 1
}

$constraintsFile = [System.IO.Path]::GetTempFileName()
$installLog = [System.IO.Path]::GetTempFileName()

try {
    Write-Output "Preparing dependencies from uv.lock..."
    & uv export --directory $packageDir --locked --no-dev --no-emit-project `
        --format requirements-txt --output-file $constraintsFile | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Err "uv export failed (exit code $LASTEXITCODE)."
        exit $LASTEXITCODE
    }

    Begin-Upgrade

    $toolDir = (& uv tool dir).Trim()
    $toolBinDir = (& uv tool dir --bin).Trim()
    $toolList = (& uv tool list 2>$null) -join "`n"

    if ($toolList -match '(?m)^cairnmind ') {
        $existingPython = Join-Path (Join-Path (Join-Path $toolDir "cairnmind") "Scripts") "python.exe"
        $existingBin = Join-Path $toolBinDir "cairnmind.exe"
        $bridges = Get-LiveToolProcesses -PythonExe $existingPython -Kind "bridges"
        if ($null -eq $bridges) {
            Write-Err "Note: process inspection failed, so open MCP servers of the installed version could not be ruled out."
        } elseif ($bridges.Count -gt 0) {
            $joined = $bridges -join ", "
            Write-Err "Editors have MCP servers of the installed version open (cairnmind mcp --client external), processes: $joined"
            Write-Err "Their next tool call would start the knowledge service again, from the version being replaced."
            Write-Err "Close them in the editor or stop them with Stop-Process -Id $joined, then run this installer again."
            exit 1
        }
        $runs = Get-LiveToolProcesses -PythonExe $existingPython -Kind "runs"
        if ($runs -and $runs.Count -gt 0) {
            $joined = $runs -join ", "
            Write-Err "Coding sessions started with cairnmind run of the installed version are open, processes: $joined"
            Write-Err "Finish there and exit the coding client, then run this installer again."
            exit 1
        }
        if (Test-Path -LiteralPath $existingBin) {
            & $existingBin cockpit stop 2>$null | Out-Null
            & $existingBin stop 2>$null | Out-Null
        }
    }

    $installPython = $env:CAIRNMIND_INSTALL_PYTHON
    if (-not $installPython) { $installPython = $env:DEVBRAIN_INSTALL_PYTHON }
    if (-not $installPython) { $installPython = "3.12" }

    Write-Output "Installing Cairnmind from $packageDir..."
    & uv tool install --python $installPython --reinstall --constraints $constraintsFile $packageDir *> $installLog
    if ($LASTEXITCODE -ne 0) {
        $code = $LASTEXITCODE
        Get-Content -LiteralPath $installLog -ErrorAction SilentlyContinue | ForEach-Object { Write-Err $_ }
        exit $code
    }
    Write-Output "Installed Cairnmind and its dependencies."

    $cairnmindBin = Join-Path $toolBinDir "cairnmind.exe"
    $cairnmindPython = Join-Path (Join-Path (Join-Path $toolDir "cairnmind") "Scripts") "python.exe"
    if (-not (Test-Path -LiteralPath $cairnmindBin)) {
        Write-Err "Installation did not provide the executable: $cairnmindBin"
        exit 1
    }

    End-Upgrade

    function Show-InitMarks {
        param([string]$InitJson)
        if (-not $InitJson) { return }
        $pyCode = @"
import json, sys
from cairnmind.first_start import INIT_MARKED
marks = json.load(sys.stdin).get("confirmations_before_rule")
if marks:
    print(INIT_MARKED.format(marked=marks["marked_as_a_persons"], left=marks["still_before_the_rule"]))
"@
        $InitJson | & $cairnmindPython -I -c $pyCode
        if ($LASTEXITCODE -ne 0) {
            Write-Err "The result of init could not be read; it said:"
            Write-Err $InitJson
        }
    }

    $storedMode = (& $cairnmindPython -I -c "from cairnmind.config import Settings; from cairnmind.database_mode import engine_mode; print(engine_mode(Settings.load()))" 2>$null)
    if ($storedMode) { $storedMode = $storedMode.Trim() }

    if ($connectUrl) {
        $shownUrl = $connectUrl -replace '^([a-zA-Z]+://)[^/@]*@', '$1' -replace '[?#].*$', ''
        Write-Output ""
        Write-Output "This installation only connects to the database at $shownUrl; no engine is installed."
        & $cairnmindBin engine status *> $null
        if ($LASTEXITCODE -eq 0) {
            $initSaid = (& $cairnmindBin init --json) -join "`n"
            if ($LASTEXITCODE -eq 0) {
                Show-InitMarks $initSaid
            } else {
                Write-Err ""
                Write-Err "The database is not ready for this installation yet. Follow docs/DEPLOYMENT.md,"
                Write-Err '"Connect-only installations", then run: cairnmind init'
            }
        } else {
            Write-Err ""
            Write-Err "The database is not ready for this installation yet. Follow docs/DEPLOYMENT.md,"
            Write-Err '"Connect-only installations", then run: cairnmind init'
        }
    } elseif ($engineInDocker -or $storedMode -eq "docker") {
        Write-Output ""
        Write-Output "The engine runs in Docker: no ArcadeDB is downloaded here; cairnmind init downloads its image."
        $initSaid = (& $cairnmindBin init --json) -join "`n"
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        Show-InitMarks $initSaid
        & $cairnmindBin engine status *> $null
    } else {
        $present = (& $cairnmindPython -I -c "from cairnmind.config import Settings; from cairnmind.engine import runtime_dir; print('yes' if (runtime_dir(Settings.load()) / 'lib').is_dir() else 'no')" 2>$null)
        if ($present -and $present.Trim() -eq "yes") {
            Write-Output ""
            Write-Output "The pinned ArcadeDB release is already installed here; nothing is downloaded."
        } else {
            Write-Output ""
            Write-Output "Installing the pinned ArcadeDB release (about 86 MB, verified with SHA-256)."
        }
        $arcadeDist = $env:CAIRNMIND_ARCADE_DIST
        if (-not $arcadeDist) { $arcadeDist = $env:DEVBRAIN_ARCADE_DIST }
        if ($arcadeDist) {
            & $cairnmindBin engine install --distribution $arcadeDist | Out-Null
        } else {
            & $cairnmindBin engine install | Out-Null
        }
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        $initSaid = (& $cairnmindBin init --json) -join "`n"
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        Show-InitMarks $initSaid
        & $cairnmindBin engine status *> $null
    }

    if ($PullModels) {
        & $cairnmindBin models list *> $null
        if ($LASTEXITCODE -ne 0) {
            Write-Err "Cannot reach Ollama. Start Ollama, then run cairnmind models pull."
            exit 1
        }
        & $cairnmindBin models pull
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }

    Write-Output ""
    Write-Output "Cairnmind installed: $cairnmindBin"
    $pathEntries = ($env:PATH -split ";") | ForEach-Object { $_.TrimEnd("\").ToLowerInvariant() }
    if ($pathEntries -notcontains $toolBinDir.TrimEnd("\").ToLowerInvariant()) {
        Write-Output "Add this directory to PATH: $toolBinDir (or run: uv tool update-shell), then open a new terminal."
    }
    Write-Output "Open the terminal workspace: cairnmind"
    Write-Output "Projects, memory, conflicts, plans, impact, graph and settings are available in the terminal UI."

    Write-Output ""
    Write-Output "Checking the installation..."
    $doctorJson = (& $cairnmindBin doctor) -join "`n"
    $doctorExit = $LASTEXITCODE
    $doctorJson | & $cairnmindPython -I -m cairnmind.doctor_attention
    exit $doctorExit
} finally {
    End-Upgrade
    Remove-Item -LiteralPath $constraintsFile -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $installLog -Force -ErrorAction SilentlyContinue
}
