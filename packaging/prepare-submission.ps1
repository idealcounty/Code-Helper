[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[^\\/:*?"<>|]+$')]
    [string]$Name,
    [Parameter(Mandatory = $true)]
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })]
    [string]$VideoPath,
    [string]$OutputDirectory = (Join-Path (Get-Location) 'dist-submission')
)

$video = (Resolve-Path -LiteralPath $VideoPath).Path
if ([IO.Path]::GetExtension($video).ToLowerInvariant() -ne '.mp4') {
    throw 'The demonstration video must be an MP4 file.'
}
$size = (Get-Item -LiteralPath $video).Length
if ($size -gt 200MB) {
    throw "The video exceeds the 200 MB submission limit ($size bytes)."
}
try {
    $durationText = & ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 -- $video 2>$null
    $duration = [double]::Parse(($durationText | Select-Object -First 1), [Globalization.CultureInfo]::InvariantCulture)
} catch {
    throw 'ffprobe is required to validate the video duration (install FFmpeg and retry).'
}
if ($duration -gt 120) {
    throw "The video exceeds the 120-second submission limit ($([math]::Round($duration, 1)) seconds)."
}
$readme = Join-Path (Get-Location) 'README.txt'
if (-not (Test-Path -LiteralPath $readme -PathType Leaf)) {
    throw 'README.txt is missing from the project root.'
}

$root = (Resolve-Path -LiteralPath $OutputDirectory -ErrorAction SilentlyContinue)
if ($root) {
    throw "Output directory already exists: $($root.Path). Choose a new -OutputDirectory."
}
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$staging = Join-Path $OutputDirectory $Name
New-Item -ItemType Directory -Path $staging -Force | Out-Null
Copy-Item -LiteralPath $readme -Destination (Join-Path $staging 'README.txt')
Copy-Item -LiteralPath $video -Destination (Join-Path $staging ([IO.Path]::GetFileName($video)))
$archive = Join-Path $OutputDirectory "$Name.zip"
Compress-Archive -LiteralPath $staging -DestinationPath $archive -Force
Write-Output "Created $archive with README.txt and the MP4 video only ($([math]::Round($duration, 1)) seconds)."
