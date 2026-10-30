param(
    [Parameter(Mandatory = $true)]
    [string]$Path
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Runtime.WindowsRuntime

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq "AsTask" -and
    $_.IsGenericMethod -and
    $_.GetParameters().Count -eq 1
} | Select-Object -First 1)

function Await-Result {
    param(
        [Parameter(Mandatory = $true)]$Operation,
        [Parameter(Mandatory = $true)][Type]$ResultType
    )
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $task = $asTask.Invoke($null, @($Operation))
    try {
        $task.Wait(-1) | Out-Null
    }
    catch {
        throw $task.Exception.GetBaseException()
    }
    return $task.Result
}

$bytes = [System.IO.File]::ReadAllBytes($Path)
$stream = New-Object "Windows.Storage.Streams.InMemoryRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime"
$writer = New-Object "Windows.Storage.Streams.DataWriter, Windows.Storage.Streams, ContentType = WindowsRuntime" -ArgumentList $stream
$writer.WriteBytes($bytes)
Await-Result ($writer.StoreAsync()) ([UInt32]) | Out-Null
$writer.DetachStream() | Out-Null
$stream.Seek(0)
$decoder = Await-Result (
    [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]::CreateAsync($stream)
) ([Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime])
$bitmap = Await-Result (
    $decoder.GetSoftwareBitmapAsync()
) ([Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime])

$language = New-Object "Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime" "zh-Hans"
$engine = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]::TryCreateFromLanguage($language)
if ($null -eq $engine) {
    throw "Windows OCR language zh-Hans is unavailable"
}
$result = Await-Result (
    $engine.RecognizeAsync($bitmap)
) ([Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType = WindowsRuntime])
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$result.Text
