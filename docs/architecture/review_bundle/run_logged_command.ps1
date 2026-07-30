param(
    [Parameter(Mandatory = $true)]
    [string]$Executable,

    [Parameter(Mandatory = $true)]
    [string]$Arguments,

    [Parameter(Mandatory = $true)]
    [string]$WorkingDirectory,

    [Parameter(Mandatory = $true)]
    [string]$LogBase,

    [Parameter(Mandatory = $true)]
    [string]$PythonExecutable
)

$ErrorActionPreference = "Stop"
$resolvedWorkingDirectory = (Resolve-Path -LiteralPath $WorkingDirectory).Path
$logDirectory = Split-Path -Parent $LogBase
if ($logDirectory) {
    [System.IO.Directory]::CreateDirectory($logDirectory) | Out-Null
}

$pythonVersion = (& $PythonExecutable --version 2>&1 | Out-String).Trim()
$started = [DateTimeOffset]::Now
$stopwatch = [System.Diagnostics.Stopwatch]::StartNew()

$startInfo = [System.Diagnostics.ProcessStartInfo]::new()
$startInfo.FileName = $Executable
$startInfo.Arguments = $Arguments
$startInfo.WorkingDirectory = $resolvedWorkingDirectory
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
$startInfo.RedirectStandardOutput = $true
$startInfo.RedirectStandardError = $true
$startInfo.StandardOutputEncoding = [System.Text.Encoding]::UTF8
$startInfo.StandardErrorEncoding = [System.Text.Encoding]::UTF8

$process = [System.Diagnostics.Process]::new()
$process.StartInfo = $startInfo
if (-not $process.Start()) {
    throw "Failed to start: $Executable $Arguments"
}
$stdoutTask = $process.StandardOutput.ReadToEndAsync()
$stderrTask = $process.StandardError.ReadToEndAsync()
$process.WaitForExit()
$stdout = $stdoutTask.GetAwaiter().GetResult()
$stderr = $stderrTask.GetAwaiter().GetResult()
$exitCode = $process.ExitCode
$stopwatch.Stop()
$finished = [DateTimeOffset]::Now
$process.Dispose()

$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[System.IO.File]::WriteAllText("$LogBase.stdout.txt", $stdout, $utf8NoBom)
[System.IO.File]::WriteAllText("$LogBase.stderr.txt", $stderr, $utf8NoBom)

$metadata = @(
    "command=$Executable $Arguments"
    "directory=$resolvedWorkingDirectory"
    "python=$pythonVersion"
    "started=$($started.ToString('o'))"
    "finished=$($finished.ToString('o'))"
    "duration_seconds=$([Math]::Round($stopwatch.Elapsed.TotalSeconds, 3))"
    "exit_code=$exitCode"
)
[System.IO.File]::WriteAllLines("$LogBase.meta.txt", $metadata, $utf8NoBom)

if ($stdout) {
    [Console]::Out.Write($stdout)
}
if ($stderr) {
    [Console]::Error.Write($stderr)
}
exit $exitCode
