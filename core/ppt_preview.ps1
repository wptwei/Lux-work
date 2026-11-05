param([string]$InputFile, [string]$OutputFolder)
$ErrorActionPreference = "Stop"
$ppt = $null
$deck = $null
try {
    $ppt = New-Object -ComObject PowerPoint.Application
    $deck = $ppt.Presentations.Open($InputFile, $true, $false, $false)
    for ($i = 1; $i -le $deck.Slides.Count; $i++) {
        $dest = Join-Path $OutputFolder ("slide-{0:D3}.png" -f $i)
        $deck.Slides.Item($i).Export($dest, "PNG", 1600, 900)
    }
} finally {
    if ($null -ne $deck) { $deck.Close() }
    if ($null -ne $ppt) { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($ppt) }
}
