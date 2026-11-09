#Requires -Version 7.2
<# Attach-only Windows Terminal launcher. Never starts the controller or services.
Size semantics: https://learn.microsoft.com/en-us/windows/terminal/command-line-arguments
The terminal menu provides reconnect/results; users never need a shell command. #>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ReadyFile,
    [Parameter(Mandatory)][string]$WindowsTerminal,
    [ValidateSet('80x24','120x36','180x55')][string]$Size='120x36',
    [string]$Distribution='Ubuntu',
    [switch]$Execute
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$readyPath=[IO.Path]::GetFullPath($ReadyFile)
$cursor=$readyPath
while($cursor) {
    if($cursor -match '(?i)(^|[\\/])OneDrive([\\/]|$)') { throw 'Use the reviewed bundle outside OneDrive.' }
    if((Test-Path -LiteralPath $cursor) -and ((Get-Item -LiteralPath $cursor).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Reparse path refused.' }
    $cursor=[IO.Path]::GetDirectoryName($cursor)
}
$stream=[IO.FileStream]::new($readyPath,[IO.FileMode]::Open,[IO.FileAccess]::Read,([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
try {
    if($stream.Length -gt 65536){ throw 'Ready file too large.' }
    $reader=[IO.StreamReader]::new($stream,[Text.UTF8Encoding]::new($false,$true),$true,4096,$true)
    try { $ready=$reader.ReadToEnd() | ConvertFrom-Json -AsHashtable } finally { $reader.Dispose() }
} finally { $stream.Dispose() }
if($ready['expires_unix_time'] -le [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()) { throw 'Interactive run expired.' }
foreach($key in @('controller','python_launcher','manifest')) {
    if($ready[$key] -isnot [string] -or -not $ready[$key].StartsWith('/') -or $ready[$key] -match '[;\r\n\x00]') { throw 'Invalid literal Linux path.' }
}
if([IO.Path]::GetFileName($ready['controller']) -cnotin @('run-interactive-ux080.py','run-baseline-ux072.py') -or $Size -cnotin $ready['sizes']) { throw 'Wrong interactive controller or geometry.' }
if(-not [IO.Path]::IsPathFullyQualified($WindowsTerminal) -or -not (Test-Path -LiteralPath $WindowsTerminal -PathType Leaf) -or
    [IO.Path]::GetFileName($WindowsTerminal) -ine 'wt.exe' -or $Distribution -notmatch '^[A-Za-z0-9_.-]+$') { throw 'Use the installed Windows Terminal executable and explicit WSL distribution.' }
$argv=@('-w','new','--size',$Size.Replace('x',','),'new-tab','--title',('DevBrain UX '+$Size),
    (Join-Path $env:WINDIR 'System32\wsl.exe'),'--distribution',$Distribution,'--exec',$ready['python_launcher'],'-I','-B',$ready['controller'],
    '--attach','--manifest',$ready['manifest'],'--size',$Size)
if(-not $Execute) {
    @{execute=$false; argv=$argv; service_start=$false; automatic_visual_pass=$false} | ConvertTo-Json -Depth 4
    return
}
$info=[Diagnostics.ProcessStartInfo]::new()
$info.FileName=$WindowsTerminal
$info.UseShellExecute=$false
$info.WindowStyle=[Diagnostics.ProcessWindowStyle]::Normal # explicitly requested interactive terminal
foreach($argument in $argv){$info.ArgumentList.Add($argument)}
$process=[Diagnostics.Process]::Start($info)
try {
    # wt may hand the visible window to its existing application process. Its
    # launcher exit is NOT a claim of Linux viewer exit or completed human tasks.
    @{requested=$true; launch_pid=$process.Id; size=$Size; automatic_visual_pass=$false} | ConvertTo-Json
} finally { $process.Dispose() }
